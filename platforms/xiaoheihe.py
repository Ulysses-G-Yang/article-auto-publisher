"""小黑盒（Xiaoheihe）平台自动化"""
import asyncio
import copy
import io
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    DraftResultUnknownError,
    DraftVerificationEvidence,
    SelectorError,
)
from platforms.content_validation import (
    ContentValidationError,
    ensure_valid_content,
    extract_expected_paragraphs,
    normalize_for_comparison,
    safe_media_error,
)
from platforms.media_progress import safe_media_progress


class XiaoheihePlatform(BasePlatform):
    platform_name = "xiaoheihe"

    # 真实选择器（经 DOM 抓取确认，非猜测）：
    #  - 登录入口按钮：右上角头像 `button.nav-user-trigger`
    #  - 二维码容器：`div.qrcode-in-card`
    #  - 头像：`img.nav-user-avatar`
    # 登录态判断依据：未登录时头像是默认游客图（地址含 `icon_83.5`），
    # 登录后变为用户真实头像。这是最可靠的信号，不依赖会变的 class。
    LOGIN_TRIGGER = "button.nav-user-trigger"
    QR_SELECTOR = "div.qrcode-in-card"
    GUEST_AVATAR_MARKER = "icon_83.5"  # 小黑盒默认游客头像地址特征

    # ---- 编辑器真实选择器（2026-07-31 浏览器实时 DOM 抓取确认）----
    PUBLISH_BTN = ".publish-btn"                                  # 首页右上「发布内容」
    TAB_ARTICLE = "div.slide-tab__tab-label"                       # 类型面板：发布图文/发布文章/发布视频
    TITLE_FIELD = ".editor-title__container [contenteditable='true'], .editor-title__container .ProseMirror"
    BODY_FIELD = ".article__edit-content--inner [contenteditable='true'], .article__edit-content--inner .ProseMirror"
    SAVE_DRAFT_BTN = "button.editor-publish__save-draft"           # 保存草稿
    SAVE_DRAFT_CANDIDATES = (
        "button.editor-publish__save-draft,"
        "button.editor-publish__btn:has-text('保存草稿'),"
        "button:has-text('保存草稿')"
    )
    DRAFT_BOX_BTN = "button.editor-publish__btn.sub-btn.margin-left"  # 草稿箱
    DRAFTS_URL = "https://www.xiaoheihe.cn/creator/draft"
    DRAFT_EDIT_PATH_PATTERN = re.compile(
        r"^/creator/editor/edit/article/(?P<draft_id>[0-9]+)/?$"
    )
    DRAFT_VERIFY_DELAYS = (1.0, 2.0, 3.0, 5.0, 8.0)
    DRAFT_CONTENT_POLL_DELAYS = (1.0, 2.0, 3.0)
    DRAFT_ROUTE_VERIFY_ATTEMPTS = 30
    DRAFT_ROUTE_VERIFY_INTERVAL_SECONDS = 0.25
    IDENTITY_CAPTURE_ATTEMPTS = 6
    IDENTITY_CAPTURE_INTERVAL_SECONDS = 0.5
    PUBLISH_NOW_BTN = "button.editor-publish__btn.main-btn"        # 发布
    IMAGE_LOCAL_UPLOAD = (
        ".editor-model__image-model .model-image__local-box "
        ".editor-image-wrapper__box.upload, "
        ".model-image__local-box .editor-image-wrapper__box.upload"
    )
    IMAGE_MODAL_CONFIRM = (
        ".editor-model__image-model "
        ".editor-__model-frame-bottom-btn:has-text('确定')"
    )
    # 真实编辑器输入规则证据（2026-08-19）：`# ` 生成 H2，`## ` 生成 H3。
    # 未经 DOM 回读证明的层级必须保持 fail-closed。
    HEADING_MARKDOWN_PREFIX = {2: "# ", 3: "## "}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 只保存在内存中，用于保存后重新打开平台草稿并核对。不得用比较视图
        # 回写正文或重算 ContentVersion 哈希。
        self._expected_persisted_blocks: list[dict] | None = None
        self._pending_cover_path = ""

    def _raise_if_page_closed(self, stage: str):
        self._require_page_alive(stage)

    @staticmethod
    def _media_status(expected: int, uploaded: int, failed: int) -> str:
        if expected == 0:
            return "not_required"
        if uploaded == expected and failed == 0:
            return "completed"
        if uploaded == 0 and failed == expected:
            return "failed"
        if uploaded + failed == expected:
            return "partial"
        return "in_progress"

    def _media_progress_snapshot(self) -> dict[str, int | str] | None:
        state = getattr(self, "_media_progress_state", None)
        if not isinstance(state, dict):
            return None
        expected = state.get("expected_images")
        uploaded = state.get("uploaded_images")
        failed = state.get("failed_image_count")
        if not all(isinstance(value, int) and not isinstance(value, bool)
                   for value in (expected, uploaded, failed)):
            return None
        return safe_media_progress(
            {
                "expected_images": expected,
                "uploaded_images": uploaded,
                "failed_image_count": failed,
                "media_status": self._media_status(expected, uploaded, failed),
            }
        )

    def _attach_media_progress(self, exc: ContentValidationError) -> None:
        progress = self._media_progress_snapshot()
        if progress is not None:
            exc.media_progress = progress

    def _ensure_valid_content(
        self,
        content_blocks: list,
        actual_text: str | None,
        *,
        phase: str,
    ) -> int:
        try:
            return ensure_valid_content(
                content_blocks,
                actual_text,
                platform="小黑盒",
                phase=phase,
            )
        except ContentValidationError as exc:
            self._attach_media_progress(exc)
            raise

    async def initialize(self):
        """标准初始化 + 监听 restore_login 响应，暂存同源确认的平台身份。"""
        await super().initialize()
        self._restore_profile: dict | None = None
        self._restore_url: str | None = None
        self._install_restore_capture()

    def _install_restore_capture(self):
        """监听页面自身的 ``account/restore_login`` 响应（带平台签名参数）。

        小黑盒首页导航不渲染昵称，昵称只存在于该登录态恢复接口的响应里；
        页面自身发起的请求携带 JS 计算的 hkey/nonce 签名，直接重放 URL 也可用。
        """

        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        "account/restore_login" in response.url
                        and response.request.method == "GET"
                    ):
                        self._restore_url = response.url
                        payload = await response.json()
                        profile = (payload.get("result") or {}).get("profile") or {}
                        if profile.get("nickname") and profile.get("heybox_id"):
                            self._restore_profile = {
                                "ok": True,
                                "user_id": str(profile["heybox_id"]),
                                "display_name": str(profile["nickname"]),
                            }
                except Exception:  # noqa: BLE001
                    pass

            self.page.on("response", _on_response)
        except Exception:  # noqa: BLE001
            pass

    async def fetch_identity_payload(self) -> dict:
        """有界等待 restore_login 身份，避免一次异步/网络抖动产生假失败。"""

        if self.page is None:
            return {"ok": False, "user_id": "", "display_name": ""}
        for attempt in range(self.IDENTITY_CAPTURE_ATTEMPTS):
            payload = getattr(self, "_restore_profile", None)
            if isinstance(payload, dict) and payload.get("ok"):
                return dict(payload)
            url = getattr(self, "_restore_url", None)
            if url:
                try:
                    result = await self.page.evaluate(
                        """async (u) => {
                            try {
                                const response = await fetch(u, { credentials: 'include' });
                                const body = await response.json().catch(() => ({}));
                                const profile = (body && body.result && body.result.profile) || {};
                                return {
                                    ok: Boolean(response.ok && profile.nickname && profile.heybox_id),
                                    user_id: profile.heybox_id ? String(profile.heybox_id) : '',
                                    display_name: profile.nickname ? String(profile.nickname) : '',
                                };
                            } catch (_) {
                                return { ok: false, user_id: '', display_name: '' };
                            }
                        }""",
                        url,
                    )
                    if isinstance(result, dict) and result.get("ok"):
                        return result
                except Exception:  # noqa: BLE001
                    pass
            if attempt + 1 < self.IDENTITY_CAPTURE_ATTEMPTS:
                await asyncio.sleep(self.IDENTITY_CAPTURE_INTERVAL_SECONDS)
        return {"ok": False, "user_id": "", "display_name": ""}

    async def _wait_first_visible(self, selector: str, timeout_ms: int = 5000):
        """等待 SPA 弹窗/编辑器控件完成渲染，再返回第一个可见元素。"""
        self._raise_if_page_closed(f"等待控件: {selector}")
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            candidate = await self._first_visible(selector)
            if candidate is not None:
                return candidate
            await asyncio.sleep(0.2)
        raise TimeoutError(f"等待可见控件超时: {selector}")

    async def _is_logged_in_dom(self) -> bool:
        """读取当前页 DOM + 登录态 cookie 判断登录态（不依赖头像图片，避免误判）

        关键修正：之前用「头像图片地址是否含 icon_83.5 默认游客图」判断登录态，
        但用户登录后若未设自定义头像，小黑盒返回的就是默认图，导致已登录被误判为游客。
        现改为以「登录态 cookie」为权威信号：heybox_id / nickname / pkey 是登录后
        才有且非 httponly 的 cookie，游客态不存在这些。

        2026-08 稳定性验收修正：小黑盒已把登录 cookie 更名为 user_heybox_id /
        user_pkey（旧名 heybox_id / pkey 不再下发），两种命名都纳入登录态信号，
        避免真实已登录账号被误判为 LOGIN_REQUIRED。
        """
        LOGIN_COOKIE_NAMES = {
            "heybox_id",
            "nickname",
            "pkey",
            "user_heybox_id",
            "user_pkey",
        }
        # 1. 权威信号：Playwright context.cookies()，同时覆盖 HttpOnly Cookie。
        try:
            if self.context:
                cookies = await self.context.cookies([
                    "https://www.xiaoheihe.cn/",
                    "https://www.xiaoheihe.cn/creator/editor",
                ])
                cookie_names = {item.get("name", "") for item in cookies}
                if cookie_names.intersection(LOGIN_COOKIE_NAMES):
                    return True
        except Exception as exc:
            logger.debug("小黑盒 context Cookie 检测失败: {}", exc)

        # 2. 兼容旧浏览器上下文：登录态 cookie（可在 JS 中读取，游客态不会有）
        try:
            cookie_signal = await self.page.evaluate("""() => {
                const c = document.cookie || '';
                return c.includes('heybox_id=') || c.includes('nickname=')
                    || c.includes('pkey=') || c.includes('user_heybox_id=')
                    || c.includes('user_pkey=');
            }""")
            if cookie_signal:
                return True
        except Exception:
            pass
        # 3. 兜底：头像为非默认游客图（已换成用户真实头像）→ 已登录
        try:
            avatar = self.page.locator(
                "img.nav-user-avatar, img[class*='avatar'], .nav-user img"
            ).first
            if await avatar.count() > 0:
                src = await avatar.get_attribute("src") or ""
                if self.GUEST_AVATAR_MARKER in src:
                    return False
                if src:
                    return True
        except Exception:
            pass
        # 4. 兜底：存在用户名/昵称元素也算已登录
        try:
            if await self.page.locator(".nav-user-name, .user-name, [class*=nickname]").count() > 0:
                return True
        except Exception:
            pass
        return False

    async def check_login(self) -> bool:
        """检查小黑盒登录状态——以登录态 cookie 为权威信号。

        注意：x_xhh_tokenid 是未登录也存在的埋点 cookie，不能用作登录态判断。
        游客态不会有 heybox_id / nickname / pkey 等登录 cookie。
        """
        try:
            self._raise_if_page_closed("小黑盒登录态检测")
            await self.page.goto("https://www.xiaoheihe.cn/", wait_until="domcontentloaded", timeout=15000)
            # SPA hydration 和登录 Cookie 可在不同时间落地。保持原有 4 秒
            # 总等待预算，但改为多次只读观测，避免某一次 context.cookies()
            # 暂时失败就把已验证账号降级为 LOGIN_REQUIRED。
            await asyncio.sleep(2)
            for attempt in range(3):
                if await self._is_logged_in_dom():
                    return True
                if attempt < 2:
                    await asyncio.sleep(1)
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 小黑盒登录态检测时页面已关闭") from exc
            return False

    async def login(self):
        """打开首页，点击右上角头像弹出二维码，等待扫码登录"""
        # 1. 访问首页
        self._raise_if_page_closed("小黑盒打开登录页")
        await self.page.goto("https://www.xiaoheihe.cn/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # 2. 点击右上角头像，弹出登录二维码面板
        try:
            await self.page.click(self.LOGIN_TRIGGER, timeout=8000)
        except Exception as e:
            if self._exception_means_browser_closed(e):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 小黑盒打开登录入口时页面已关闭") from e
            logger.warning("小黑盒点击用户入口失败，尝试重新打开: {}", e)
            await self.page.goto("https://www.xiaoheihe.cn/", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            try:
                await self.page.click(self.LOGIN_TRIGGER, timeout=8000)
            except Exception as e2:
                if self._exception_means_browser_closed(e2):
                    raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 小黑盒重试登录入口时页面已关闭") from e2
                logger.error("小黑盒仍未找到登录入口: {}", e2)

        await asyncio.sleep(2)

        # 3. 确认二维码已出现
        has_qr = await self.page.locator(self.QR_SELECTOR).count()
        if has_qr == 0:
            logger.warning("小黑盒未检测到二维码，请手动点击右上角头像打开扫码面板")

        # 4. 显示提示
        await self.page.evaluate("""
            () => {
                const div = document.createElement('div');
                div.id = 'wb-login-hint';
                div.style.cssText = 'position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#0d6efd;color:#fff;padding:12px 24px;border-radius:8px;font-size:16px;z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);text-align:center;';
                div.innerHTML = '二维码已显示 — 请用小黑盒 App 扫码<br><small>登录成功后工具自动关闭</small>';
                document.body.appendChild(div);
            }
        """)

        logger.info("小黑盒登录二维码已显示，请用 App 扫描右上角头像弹出的二维码")

        # 5. 等待登录完成：头像从默认游客图变为真实头像是成功信号
        # 30 次 × 3 秒 = 90 秒超时；用户关掉浏览器时立即中止，不空转。
        max_iters = 30
        for _ in range(max_iters):
            self._raise_if_page_closed("小黑盒等待登录")
            if await self._is_logged_in_dom():
                logger.info("小黑盒登录成功")
                await self.page.evaluate("""
                    () => {
                        const hint = document.getElementById('wb-login-hint');
                        if (hint) {
                            hint.style.background = '#198754';
                            hint.innerHTML = '✓ 登录成功！<br><small>3秒后关闭此窗口...</small>';
                        }
                    }
                """)
                await asyncio.sleep(3)
                return
            await asyncio.sleep(3)  # 每 3 秒检查一次，不频繁干扰页面

        raise RuntimeError("LOGIN_REQUIRED: 小黑盒登录超时（90 秒），请重新扫码")

    async def navigate_to_editor(self):
        """导航到小黑盒「文章」编辑器（真实路径：首页 → 发布内容 → 发布文章）

        关键修正：旧代码直接 goto /app/bbs/post，但该地址会 302 跳转到
        /app/bbs/home 首页信息流，编辑器从未真正打开，导致后续 fill/save
        全部在首页上做无用功，save_draft 还把首页/搜索页 URL 当草稿 URL 返回，
        造成「显示成功但草稿箱里找不到」的假成功。
        正确路径：点击右上「发布内容」(.publish-btn) 弹出类型面板，再点
        「发布文章」(div.slide-tab__tab-label)，才进入真正的编辑器，URL 形如
        /creator/editor/draft/article/local_xxx（该 URL 本身即草稿地址）。
        """
        self._editor_url = ""
        self._raise_if_page_closed("小黑盒打开文章编辑器")

        # 先回首页，确保从干净状态进入
        await self.page.goto("https://www.xiaoheihe.cn/", wait_until="domcontentloaded")
        await self.simulator.random_delay(2, 4)
        self._raise_if_page_closed("小黑盒首页发布入口")

        # 点击「发布内容」
        try:
            publish_button = await self._wait_first_visible(self.PUBLISH_BTN, timeout_ms=10000)
            await self._click_with_fallback(publish_button, timeout=10000)
        except Exception as e:
            if self._exception_means_browser_closed(e):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 小黑盒打开发布菜单时页面已关闭") from e
            logger.warning("小黑盒点击发布内容失败，尝试创作中心兜底: {}", e)
            try:
                await self.page.goto("https://www.xiaoheihe.cn/creator", wait_until="domcontentloaded")
                await self.simulator.random_delay(2, 4)
                publish_button = await self._wait_first_visible(self.PUBLISH_BTN, timeout_ms=10000)
                await self._click_with_fallback(publish_button, timeout=10000)
            except Exception as fallback_error:
                if self._exception_means_browser_closed(fallback_error):
                    raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 小黑盒打开创作中心时页面已关闭") from fallback_error
                logger.error("小黑盒创作中心兜底仍失败: {}", fallback_error)
        await self.simulator.random_delay(2, 3)

        # 点击「发布文章」类型标签
        try:
            article_tab = self.page.locator(self.TAB_ARTICLE, has_text="发布文章").first
            if await article_tab.count() == 0 or not await article_tab.is_visible():
                raise TimeoutError("发布文章标签不可见")
            await self._click_with_fallback(article_tab, timeout=8000)
        except Exception as e:
            if self._exception_means_browser_closed(e):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 小黑盒选择文章类型时页面已关闭") from e
            logger.warning("小黑盒点击发布文章失败，回退到发布图文: {}", e)
            try:
                image_tab = self.page.locator(self.TAB_ARTICLE, has_text="发布图文").first
                if await image_tab.count() == 0 or not await image_tab.is_visible():
                    raise TimeoutError("发布图文标签不可见")
                await self._click_with_fallback(image_tab, timeout=8000)
            except Exception as fallback_error:
                if self._exception_means_browser_closed(fallback_error):
                    raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 小黑盒选择文章类型时页面已关闭") from fallback_error
                raise SelectorError(f"小黑盒文章类型入口失败: {fallback_error}") from fallback_error
        await self.simulator.random_delay(3, 5)

        # 等待编辑器标题框出现（真实标题框：.editor-title__container 内的 contenteditable ProseMirror）
        try:
            await self._wait_first_visible(self.TITLE_FIELD, timeout_ms=15000)
        except Exception as e:
            if isinstance(e, BrowserLifecycleError):
                raise
            raise SelectorError(f"小黑盒编辑器标题框未找到: {e}") from e
        try:
            await self._wait_first_visible(self.BODY_FIELD, timeout_ms=15000)
        except Exception as e:
            if isinstance(e, BrowserLifecycleError):
                raise
            raise SelectorError(f"小黑盒编辑器正文框未找到: {e}") from e

        # 记录编辑器 URL（同时是草稿 URL，供 save_draft/publish_now 回退定位）
        self._editor_url = self.page.url
        if "creator/editor" not in (self._editor_url or ""):
            raise SelectorError(f"小黑盒编辑器路由验证失败，当前 URL: {self._editor_url}")

    async def fill_title(self, title: str):
        """填写文章标题（真实字段：.editor-title__container 内的 contenteditable ProseMirror）"""
        self._raise_if_page_closed("小黑盒填写标题")
        expected = self._normalize_platform_title(title)
        locator = self.page.locator(self.TITLE_FIELD).first
        try:
            if await locator.count() == 0 or not await locator.is_visible():
                raise RuntimeError("真实标题编辑器不可见")
            await locator.click()
            # Locator.fill 对 contenteditable 会先清空已有 ProseMirror 节点，
            # 避免 Ctrl+A 在 SPA 编辑器焦点尚未稳定时把文字追加到旧内容。
            await locator.fill(expected)
            await locator.evaluate(
                "el => el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText'}))"
            )
            actual = " ".join((await locator.inner_text()).split())
            if actual != expected:
                raise RuntimeError(f"小黑盒标题验证失败: expected={expected!r}, actual={actual!r}")
            await self.simulator.random_delay(0.5, 1.0)
            logger.info("小黑盒标题输入并验证成功: {}", expected)
            return
        except Exception as e:
            if self._exception_means_browser_closed(e):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 小黑盒填写标题时页面已关闭") from e
            logger.error("小黑盒标题输入失败: {}", e)
            raise SelectorError("小黑盒标题输入框未找到或验证失败") from e

    @staticmethod
    def _normalize_platform_title(title: str | None) -> str:
        """按平台实际输入规则规范化标题，供填写和草稿箱核对共用。"""
        return " ".join(str(title or "").split())[:30]

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文（真实字段：.article__edit-content--inner 内的 contenteditable ProseMirror）"""
        self._raise_if_page_closed("小黑盒填写正文")
        self._expected_persisted_blocks = copy.deepcopy(content_blocks)
        expected_images = sum(
            1 for block in content_blocks if block.get("type") == "image"
        )
        self._media_progress_state = {
            "expected_images": expected_images,
            "uploaded_images": 0,
            "failed_image_count": 0,
        }
        editor = await self._current_body_editor()
        await editor.fill("")
        await self._place_body_caret_at_end()
        await self.simulator.random_delay(0.3, 0.8)

        uploaded_images = 0
        failed_images = []
        content_started = False

        # 必须沿着 ContentVersion 的原始块顺序写入。小黑盒的图片弹窗可能
        # 重建 ProseMirror，先把所有文字写完再批量插图会让插入点落到中段，
        # 进而破坏后续段落；每个块都从当前可见编辑器末尾继续。
        for block_index, block in enumerate(content_blocks):
            btype = block.get("type")
            if btype in ("text", "heading") and block.get("text"):
                text = block["text"].strip()
                if not text:
                    continue
                heading_level = None
                if btype == "heading" and "level" in block:
                    heading_level = self._validated_heading_level(block.get("level"))
                    if "\n" in text or "\r" in text:
                        raise ContentValidationError(
                            "小黑盒标题块包含换行，无法安全映射为单一标题节点"
                        )
                if content_started:
                    await self._place_body_caret_at_end()
                    await self.page.keyboard.press("Enter")
                    await self.page.keyboard.press("Enter")
                await self._place_body_caret_at_end()
                if heading_level is not None:
                    await self.page.keyboard.type(
                        self.HEADING_MARKDOWN_PREFIX[heading_level]
                    )
                lines = text.splitlines() or [text]
                for i, line in enumerate(lines):
                    if line.strip():
                        await self.page.keyboard.insert_text(line.strip())
                    if i < len(lines) - 1:
                        await self.page.keyboard.press("Enter")
                content_started = True
                if heading_level is not None:
                    await self._validate_heading_structure(
                        content_blocks[: block_index + 1],
                        phase=f"标题处理后第{block_index + 1}块",
                    )
                continue

            if btype != "image":
                continue

            if content_started:
                await self._place_body_caret_at_end()
                await self.page.keyboard.press("Enter")
                await self.page.keyboard.press("Enter")
            await self._place_body_caret_at_end()

            img_path = self._image_path_for_block(block, images)
            if img_path:
                upload_result = await self._upload_image(img_path) or {}
                if upload_result.get("success"):
                    uploaded_images += 1
                else:
                    failed_images.append({
                        "filename": Path(str(img_path)).name,
                        "error": safe_media_error(
                            upload_result.get("error"),
                            fallback="图片上传失败",
                        ),
                    })
            else:
                failed_images.append({
                    "filename": "",
                    "error": "文章图片块没有对应本地文件",
                })

            content_started = True
            self._media_progress_state.update(
                uploaded_images=uploaded_images,
                failed_image_count=len(failed_images),
            )

            # 图片操作可能重建正文编辑器；立即从新节点读取截至当前块的
            # 所有文字，首个缺失/乱序必须硬失败，后续图片不得继续。
            await self._validate_written_text(
                content_blocks[: block_index + 1],
                phase=f"图片处理后第{block_index + 1}块",
            )
            await self._validate_heading_structure(
                content_blocks[: block_index + 1],
                phase=f"图片处理后标题第{block_index + 1}块",
            )
            if img_path:
                # 每张图片之间放慢节奏，降低风控敏感度
                await self.simulator.random_delay(2.5, 4.5)

        editor = await self._current_body_editor()
        await editor.evaluate(
            "el => el.dispatchEvent(new InputEvent('input', {bubbles: true, "
            "inputType: 'insertText'}))"
        )

        actual_text = await editor.inner_text()
        expected_count = self._ensure_valid_content(
            content_blocks,
            actual_text,
            phase="图片处理后",
        )
        await self._validate_heading_structure(content_blocks, phase="正文最终")
        logger.info("小黑盒正文输入并最终验证成功: {} 个文本段落", expected_count)

        if expected_images == 0:
            media_status = "not_required"
            media_error = None
        elif uploaded_images == expected_images:
            media_status = "completed"
            media_error = None
        elif uploaded_images == 0:
            media_status = "failed"
            media_error = f"{expected_images} 张图片全部上传失败"
        else:
            media_status = "partial"
            media_error = f"{expected_images - uploaded_images} 张图片上传失败"

        if failed_images:
            logger.warning(
                "小黑盒图片处理结果: expected={}, uploaded={}, failed={}",
                expected_images,
                uploaded_images,
                len(failed_images),
            )
        return {
            "text_ok": True,
            "expected_images": expected_images,
            "uploaded_images": uploaded_images,
            "failed_images": failed_images,
            "media_status": media_status,
            "media_error": media_error,
        }

    @staticmethod
    def _image_path_for_block(block: dict, images: list[dict]) -> str | None:
        """按冻结块位置精确解析图片路径，缺失或歧义时 fail closed。

        旧逻辑在位置匹配失败时回退 ``images[0]``，会把第一张图片重复写到
        其他位置，数量校验仍可能通过。正文图片身份不明确时宁可失败，也不能
        猜测上传。
        """

        direct_path = block.get("local_path") if isinstance(block, dict) else None
        if direct_path:
            return str(direct_path)
        position = block.get("position") if isinstance(block, dict) else None
        matches = [
            image
            for image in images or []
            if isinstance(image, dict) and image.get("position_index") == position
        ]
        if len(matches) != 1:
            return None
        local_path = matches[0].get("local_path")
        return str(local_path) if local_path else None

    async def _read_editor_dom_tokens(self, editor) -> list[dict]:
        """读取正文内文本、标题和图片的有序结构，不读取页面全局内容。"""

        try:
            raw_tokens = await editor.evaluate(
                """
                (root) => {
                    const tokens = [];
                    const blockTags = new Set([
                        'P', 'DIV', 'LI', 'BLOCKQUOTE', 'PRE'
                    ]);
                    const ignoredUiTags = new Set([
                        'BUTTON', 'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'
                    ]);
                    const textOf = (node) => (
                        node.innerText !== undefined
                            ? node.innerText
                            : (node.textContent || '')
                    );
                    const visit = (node) => {
                        if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
                        const tag = node.tagName.toLowerCase();
                        if (tag === 'img') {
                            tokens.push({kind: 'image'});
                            return;
                        }
                        if (ignoredUiTags.has(node.tagName)) return;
                        if (node.getAttribute('contenteditable') === 'false') {
                            for (const image of node.querySelectorAll('img')) visit(image);
                            return;
                        }
                        if (/^h[1-6]$/.test(tag)) {
                            tokens.push({kind: 'heading', tag, text: textOf(node)});
                            return;
                        }
                        const hasImage = Boolean(node.querySelector('img'));
                        const hasBlockChild = Array.from(node.children).some(
                            (child) => blockTags.has(child.tagName)
                                || /^h[1-6]$/i.test(child.tagName)
                        );
                        if (blockTags.has(node.tagName) && !hasImage && !hasBlockChild) {
                            tokens.push({kind: 'text', text: textOf(node)});
                            return;
                        }
                        for (const child of node.childNodes) {
                            if (child.nodeType === Node.TEXT_NODE) {
                                const text = child.textContent || '';
                                if (text.trim()) tokens.push({kind: 'text', text});
                            } else {
                                visit(child);
                            }
                        }
                    };
                    for (const child of root.children) visit(child);
                    return tokens;
                }
                """
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小黑盒读取正文 DOM 时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "XIAOHEIHE_CONTENT_DOM_VERIFY_FAILED: 正文 DOM 回读失败"
            ) from exc

        if not isinstance(raw_tokens, list):
            raise ContentValidationError(
                "XIAOHEIHE_CONTENT_DOM_VERIFY_FAILED: 正文 DOM 序列无效"
            )
        normalized: list[dict] = []
        for raw in raw_tokens:
            if not isinstance(raw, dict):
                raise ContentValidationError(
                    "XIAOHEIHE_CONTENT_DOM_VERIFY_FAILED: DOM token 无效"
                )
            kind = raw.get("kind")
            if kind == "image":
                normalized.append({"kind": "image"})
                continue
            if kind == "heading":
                tag = str(raw.get("tag") or "").lower()
                text = normalize_for_comparison(raw.get("text"))
                if tag not in {"h2", "h3"} or not text:
                    raise ContentValidationError(
                        "XIAOHEIHE_HEADING_DOM_VERIFY_FAILED: 标题 DOM 字段无效"
                    )
                normalized.append({"kind": "heading", "tag": tag, "text": text})
                continue
            if kind == "text":
                paragraphs = extract_expected_paragraphs(
                    [{"type": "text", "text": raw.get("text")}]
                )
                normalized.extend(
                    {"kind": "text", "text": paragraph.comparison_text}
                    for paragraph in paragraphs
                )
                continue
            raise ContentValidationError(
                "XIAOHEIHE_CONTENT_DOM_VERIFY_FAILED: DOM token 类型无效"
            )
        return normalized

    @staticmethod
    def _expected_content_tokens(content_blocks: list[dict]) -> list[dict]:
        expected: list[dict] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                raise ContentValidationError(
                    "XIAOHEIHE_CONTENT_CONTRACT_INVALID: 正文块无效"
                )
            block_type = block.get("type")
            if block_type == "image":
                expected.append({"kind": "image"})
                continue
            for paragraph in extract_expected_paragraphs([block]):
                if block_type == "heading":
                    expected.append(
                        {
                            "kind": "heading",
                            "tag": f"h{block.get('level')}",
                            "text": paragraph.comparison_text,
                        }
                    )
                else:
                    expected.append(
                        {"kind": "text", "text": paragraph.comparison_text}
                    )
        return expected

    @staticmethod
    def _content_token_shape(tokens: list[dict], *, limit: int = 40) -> str:
        """只返回类型与长度，禁止在错误/API 中暴露正文或图片地址。"""

        shape: list[str] = []
        for token in tokens[:limit]:
            kind = token.get("kind")
            if kind == "image":
                shape.append("I")
            elif kind == "heading":
                text = token.get("text")
                shape.append(
                    f"H{token.get('tag', '?')}:{len(text) if isinstance(text, str) else 0}"
                )
            elif kind == "text":
                text = token.get("text")
                shape.append(f"T:{len(text) if isinstance(text, str) else 0}")
            else:
                shape.append("?")
        if len(tokens) > limit:
            shape.append(f"+{len(tokens) - limit}")
        return ",".join(shape) or "EMPTY"

    @staticmethod
    def _content_tokens_match(expected: list[dict], actual: list[dict]) -> bool:
        """严格比较图文位置；平台 CDN 改写图片地址不影响结构判断。"""

        if len(expected) != len(actual):
            return False
        for expected_token, actual_token in zip(expected, actual, strict=True):
            if expected_token.get("kind") != actual_token.get("kind"):
                return False
            if expected_token.get("kind") == "image":
                continue
            if expected_token != actual_token:
                return False
        return True

    async def _current_body_editor(self):
        """每次操作都重新获取可见正文节点，避免使用重渲染前的旧节点。"""

        self._raise_if_page_closed("定位小黑盒当前正文编辑器")
        editor = await self._first_visible(self.BODY_FIELD)
        if editor is None:
            raise SelectorError("小黑盒正文编辑区未找到或当前不可见")
        return editor

    async def _place_body_caret_at_end(self):
        """把插入点放到当前可见正文编辑器末尾，不使用中心 click。"""

        editor = await self._current_body_editor()
        await editor.focus()
        await editor.evaluate(
            """element => {
                element.focus();
                const selection = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(element);
                range.collapse(false);
                selection.removeAllRanges();
                selection.addRange(range);
            }"""
        )
        return editor

    async def _validate_written_text(self, content_blocks, *, phase: str) -> int:
        """从当前编辑器校验已写入的文字块，图片块不参与文字计数。"""

        text_blocks = [
            block
            for block in content_blocks
            if block.get("type") in ("text", "heading")
            and str(block.get("text") or "").strip()
        ]
        if not text_blocks:
            return 0
        editor = await self._current_body_editor()
        actual_text = await editor.inner_text()
        return self._ensure_valid_content(
            text_blocks,
            actual_text,
            phase=phase,
        )

    @staticmethod
    def _validated_heading_level(value) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContentValidationError("小黑盒标题层级无效")
        if value not in XiaoheihePlatform.HEADING_MARKDOWN_PREFIX:
            raise ContentValidationError("小黑盒仅验证了 H2/H3 标题层级")
        return value

    async def _validate_heading_structure(self, content_blocks, *, phase: str) -> None:
        """从当前正文编辑器回读 H2/H3 节点并验证层级与顺序。"""

        expected = []
        for block in content_blocks:
            if block.get("type") != "heading" or "level" not in block:
                continue
            level = self._validated_heading_level(block.get("level"))
            text = normalize_for_comparison(str(block.get("text") or ""))
            if text:
                expected.append({"level": level, "text": text})
        if not expected:
            return

        editor = await self._current_body_editor()
        actual = await editor.evaluate(
            """element => Array.from(
                element.querySelectorAll('h1,h2,h3,h4,h5,h6')
            ).map(node => ({
                tag: node.tagName.toLowerCase(),
                text: node.textContent || '',
            }))"""
        )
        if not isinstance(actual, list):
            raise ContentValidationError(
                f"小黑盒标题层级回读失败，阶段={phase}"
            )
        normalized_actual = [
            (item.get("tag"), normalize_for_comparison(item.get("text")))
            for item in actual
            if isinstance(item, dict)
        ]
        cursor = 0
        for item in expected:
            expected_tag = f"h{item['level']}"
            expected_text = normalize_for_comparison(item["text"])
            found = -1
            for index in range(cursor, len(normalized_actual)):
                if normalized_actual[index] == (expected_tag, expected_text):
                    found = index
                    break
            if found < 0:
                raise ContentValidationError(
                    f"小黑盒标题层级回读失败，阶段={phase}"
                )
            cursor = found + 1

    async def _find_file_input(self, timeout_ms: int = 3000):
        """查找页面或 iframe 中已挂载的文件控件。"""
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            scopes = [self.page]
            try:
                scopes.extend(list(getattr(self.page, "frames", []) or []))
            except Exception:
                pass
            for scope in scopes:
                try:
                    locator = scope.locator("input[type='file']")
                    if await locator.count() > 0:
                        return locator.first
                except Exception:
                    continue
            await asyncio.sleep(0.2)
        return None

    async def _set_file_from_trigger(self, trigger, image_path: str) -> bool:
        """点击本地上传入口并设置文件；一次点击只触发一次。"""
        expect_file_chooser = getattr(self.page, "expect_file_chooser", None)
        if callable(expect_file_chooser):
            try:
                async with expect_file_chooser(timeout=4000) as chooser_info:
                    await self._click_with_fallback(trigger, timeout=5000)
                chooser = await chooser_info.value
                await chooser.set_files(image_path)
                return True
            except Exception as exc:
                logger.debug(
                    "小黑盒原生文件选择器未捕获，检查已挂载文件控件: error_type={}",
                    type(exc).__name__,
                )
        else:
            await self._click_with_fallback(trigger, timeout=5000)

        file_input = await self._find_file_input(timeout_ms=3000)
        if file_input is None:
            return False
        try:
            await file_input.set_input_files(image_path)
            return True
        except Exception as exc:
            logger.debug("小黑盒设置文件控件失败: error_type={}", type(exc).__name__)
            return False

    async def _upload_image(self, image_path: str):
        """通过真实的「本地上传」入口上传单张图片并验证编辑器图片数量。"""
        image_name = Path(str(image_path)).name
        try:
            await self._dismiss_overlays()

            before_count = await self._editor_image_count()
            btn = self.page.locator(
                "button.editor-menu-image__btn, button:has-text('图片'), "
                "[data-action='upload-image'], button[title*='图片']"
            ).first
            if await btn.count() == 0:
                return {"success": False, "error": "未找到图片上传按钮"}
            try:
                if await btn.get_attribute("disabled") is not None or not await btn.is_enabled():
                    return {"success": False, "error": "图片上传按钮当前被禁用，请先激活正文编辑器"}
            except Exception:
                pass

            # 普通 click 前先把按钮滚动到编辑器内部可视区域；如果仍有可见遮罩，
            # 不使用 force=True 绕过它，避免误触平台确认/安全弹窗。
            if await self._has_visible_overlay():
                return {"success": False, "error": "图片按钮仍被页面遮罩拦截"}

            # 当前真实页面先打开「上传图片」弹窗，再点击弹窗内的本地上传入口；
            # 直接等待 input[type=file] 会错过第二层控件。
            await self._click_with_fallback(btn, timeout=5000)
            await self.simulator.random_delay(0.3, 0.8)
            local_upload = await self._first_visible(self.IMAGE_LOCAL_UPLOAD)
            if local_upload is None:
                return {"success": False, "error": "上传图片弹窗未找到本地上传入口"}
            selected = await self._set_file_from_trigger(local_upload, image_path)
            if not selected:
                return {"success": False, "error": "本地上传入口未触发文件选择器"}

            # 文件选择后先落在弹窗预览区，必须点击弹窗确定才会插入正文。
            await self.simulator.random_delay(0.8, 1.5)
            confirm = await self._first_visible(self.IMAGE_MODAL_CONFIRM)
            if confirm is None:
                return {"success": False, "error": "图片预览弹窗未找到确定按钮"}
            await self._click_with_fallback(confirm, timeout=5000)

            await self.simulator.random_delay(1, 2)
            deadline = asyncio.get_running_loop().time() + 15
            after_count = before_count
            while asyncio.get_running_loop().time() < deadline:
                after_count = await self._editor_image_count()
                if after_count > before_count:
                    logger.info("小黑盒图片上传并验证成功: {}", image_name)
                    return {
                        "success": True,
                        "filename": image_name,
                        "before_count": before_count,
                        "after_count": after_count,
                    }
                await asyncio.sleep(0.5)
            return {
                "success": False,
                "error": f"上传后编辑器图片数量未增加: before={before_count}, after={after_count}",
            }
        except Exception as e:
            if self._exception_means_browser_closed(e):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 小黑盒图片上传时页面已关闭") from e
            logger.warning(
                "小黑盒图片上传失败: filename={}, error_type={}",
                image_name,
                type(e).__name__,
            )
            return {
                "success": False,
                "error": safe_media_error(e, fallback="图片上传失败"),
            }

    async def _editor_image_count(self) -> int:
        """读取编辑器正文区域中的图片数量，不把头像/预览图计入。"""
        self._raise_if_page_closed("统计小黑盒编辑器图片")
        selectors = (
            ".article__edit-content--inner img",
            ".article__edit-content--inner [data-type='image']",
        )
        counts = []
        for selector in selectors:
            try:
                counts.append(await self.page.locator(selector).count())
            except Exception:
                counts.append(0)
        return max(counts or [0])

    async def _has_visible_overlay(self) -> bool:
        """只检测仍然可见的遮罩；不通过删除 DOM 绕过平台提示。"""
        selectors = (
            ".hb-cpt__mask-wrapper",
            ".modal-mask",
            "[role='dialog']",
            ".modal",
        )
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
                for index in range(min(count, 10)):
                    if await locator.nth(index).is_visible():
                        return True
            except Exception:
                continue
        return False

    @staticmethod
    def _normalize_query(value: str) -> str:
        """防止关键词 JSON、数组或对象原文进入平台搜索框。"""
        if isinstance(value, (list, tuple)):
            parts = []
            for item in value:
                normalized = XiaoheihePlatform._normalize_query(item)
                if normalized:
                    parts.append(normalized)
            return " ".join(parts).strip()
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, list):
            words = []
            for item in parsed:
                if isinstance(item, dict):
                    word = str(item.get("word") or "").strip()
                else:
                    word = str(item or "").strip()
                if word:
                    words.append(word)
            return " ".join(words[:3])
        if isinstance(parsed, dict) and parsed.get("word"):
            return str(parsed["word"]).strip()
        if raw.startswith(("[", "{")):
            return ""
        return " ".join(raw.replace(",", " ").replace("，", " ").replace("、", " ").split())[:100]

    @classmethod
    def _query_candidates(cls, value, split_terms: bool = False) -> list[str]:
        """生成有限、可验证的实时搜索词，不把关键词 JSON 当候选名称。"""
        values = value if isinstance(value, (list, tuple)) else [value]
        candidates = []
        for item in values:
            normalized = cls._normalize_query(item)
            if not normalized:
                continue
            expanded = normalized.split() if split_terms else [normalized]
            for candidate in expanded:
                candidate = candidate.strip()
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
        return candidates[:4]

    @staticmethod
    def _candidate_is_invalid(name: str, query: str) -> bool:
        """拒绝搜索词回显、JSON/对象文本和空候选，避免误选。"""
        value = " ".join(str(name or "").split()).strip()
        compact_value = "".join(value.split())
        compact_query = "".join(str(query or "").split())
        return (
            not value
            or compact_value == compact_query
            or value.startswith(("[", "{"))
            or "\"word\"" in value
            or value in {"搜索", "搜索结果", "无结果", "暂无结果"}
        )

    async def _extract_candidate_name(self, locator, query: str) -> str:
        """从候选项读取真实显示名称，优先使用名称属性再读取文本行。"""
        for attr in ("data-name", "data-title", "title", "aria-label"):
            try:
                value = await locator.get_attribute(attr)
            except Exception:
                value = None
            if value and not self._candidate_is_invalid(value, query):
                return " ".join(value.split()).strip()

        try:
            raw_text = await locator.inner_text()
        except Exception:
            raw_text = ""
        for line in str(raw_text or "").splitlines():
            value = " ".join(line.split()).strip()
            if not self._candidate_is_invalid(value, query):
                return value
        return ""

    async def _visible_editor_text(self) -> str:
        """读取编辑器可见文本，排除搜索/确认弹窗，避免用搜索框内容误判成功。"""
        self._raise_if_page_closed("验证小黑盒选择结果")
        return await self.page.evaluate("""
            () => {
                const clone = document.body ? document.body.cloneNode(true) : null;
                if (!clone) return '';
                clone.querySelectorAll('.modal, [role="dialog"], [class*="dialog"], .hb-cpt__mask-wrapper')
                    .forEach((el) => el.remove());
                return clone.innerText || '';
            }
        """)

    async def _legacy_select_topic(self, topic: str):
        """选择社区和话题（真实按钮：添加社区 / 添加话题，弹窗选择，不会跳走）

        关键修正：旧代码用 `text=选择社区` 去搜「社区」并点击搜索结果链接，会直接
        跳转到社区/搜索页，把编辑器带偏；save_draft 随后在搜索页取到 URL 当草稿
        URL，造成假成功。新版点击编辑器内的「添加社区/添加话题」弹窗选择，选择后
        页面仍停留在编辑器；若不慎跳走，save_draft 会回退到编辑器 URL 再保存。
        """
        if not topic:
            return

        # 1. 添加社区
        try:
            add_community = self.page.locator("button:has-text('添加社区')").first
            if await add_community.count() > 0:
                await add_community.click(timeout=5000)
                await self.simulator.random_delay(1, 2)
                search = await self.page.wait_for_selector(
                    ".modal input, [class*='dialog'] input, input[placeholder*='社区'], input[placeholder*='搜索']",
                    timeout=5000,
                )
                if search:
                    await search.fill(topic[:10])
                    await self.simulator.random_delay(1, 2)
                    first = await self.page.wait_for_selector(
                        ".modal .community-item:first-child, [class*='dialog'] li:first-child, .search-result-item:first-child",
                        timeout=5000,
                    )
                    if first:
                        await first.click()
                        await self.simulator.random_delay(0.5, 1.0)
                try:
                    await self.page.click("button:has-text('确定'), button:has-text('完成')", timeout=3000)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("小黑盒旧版社区选择失败: {}", e)
        # 关闭可能残留的社区选择弹窗，避免遮罩挡住后续操作
        await self._dismiss_overlays()

        # 2. 添加话题
        try:
            add_topic = self.page.locator("button:has-text('添加话题')").first
            if await add_topic.count() > 0:
                await add_topic.click(timeout=5000)
                await self.simulator.random_delay(1, 2)
                search = await self.page.wait_for_selector(
                    ".modal input, [class*='dialog'] input, input[placeholder*='话题'], input[placeholder*='搜索']",
                    timeout=5000,
                )
                if search:
                    await search.fill(topic[:10])
                    await self.simulator.random_delay(1, 2)
                    first = await self.page.wait_for_selector(
                        ".modal .topic-item:first-child, [class*='dialog'] li:first-child, .search-result-item:first-child",
                        timeout=5000,
                    )
                    if first:
                        await first.click()
                        await self.simulator.random_delay(0.5, 1.0)
                try:
                    await self.page.click("button:has-text('确定'), button:has-text('完成')", timeout=3000)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("小黑盒旧版话题选择失败: {}", e)

        # 关闭可能残留的弹窗遮罩，避免带偏后续保存
        await self._dismiss_overlays()

        # 话题选择是弹窗，不应跳走；若页面偏离编辑器则回退
        if "creator/editor" not in (self.page.url or ""):
            await self._back_to_editor()

    async def _first_visible(self, selector: str):
        """返回选择器命中的第一个可见元素，避免误点页面背景元素。"""
        self._raise_if_page_closed(f"查找控件: {selector}")
        locator = self.page.locator(selector)
        count = await locator.count()
        for index in range(min(count, 30)):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return None

    async def _click_with_fallback(self, locator, timeout: int = 5000):
        """点击编辑器内部滚动容器中的控件，兼容元素可见但坐标不在视口的情况。"""
        self._raise_if_page_closed("点击编辑器控件")
        try:
            await locator.click(timeout=timeout)
            return
        except Exception as first_error:
            try:
                # 小黑盒编辑器有自己的滚动容器。只调用 Playwright 默认的
                # scroll_into_view 可能仍然留下「元素在视口外」，先滚动最近的
                # 可滚动父节点，再让浏览器把控件放到中心位置。
                await locator.evaluate("""
                    el => {
                        let node = el.parentElement;
                        while (node && node !== document.body) {
                            const style = getComputedStyle(node);
                            const scrollable = /(auto|scroll)/.test(style.overflowY)
                                && node.scrollHeight > node.clientHeight;
                            if (scrollable) {
                                node.scrollTop = Math.max(0, el.offsetTop - node.clientHeight / 2);
                                break;
                            }
                            node = node.parentElement;
                        }
                        el.scrollIntoView({block: 'center', inline: 'nearest'});
                    }
                """)
            except Exception:
                pass
            try:
                await locator.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                await locator.click(timeout=2000, force=True)
                return
            except Exception:
                # React/Vue 按钮仍会走正常 click handler；最后用 DOM click，
                # 并保留第一处错误作为日志上下文。
                try:
                    await locator.evaluate(
                        "el => { el.scrollIntoView({block: 'center', inline: 'nearest'}); el.click(); }"
                    )
                    return
                except Exception as final_error:
                    if self._exception_means_browser_closed(final_error):
                        raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 点击编辑器控件时页面已关闭") from final_error
                    raise final_error from first_error

    async def _select_from_editor_dialog(self, kind: str, query, result_selector: str):
        """在有限候选查询中自动选择，所有候选都无效时返回 needs_selection。"""
        queries = self._query_candidates(query)
        if not queries:
            return {
                "success": False,
                "needs_selection": True,
                "error": f"小黑盒{kind}没有可用搜索关键词",
            }
        last_result = None
        for candidate_query in queries:
            last_result = await self._select_from_editor_dialog_once(
                kind, candidate_query, result_selector
            )
            if last_result.get("success"):
                return last_result
        return last_result or {
            "success": False,
            "needs_selection": True,
            "error": f"小黑盒{kind}没有找到有效候选",
        }

    async def _select_from_editor_dialog_once(self, kind: str, query: str, result_selector: str):
        """在编辑器内实时搜索并选择社区/话题，返回实际选择名称。"""
        query = self._normalize_query(query)
        if not query:
            return {
                "success": False,
                "needs_selection": True,
                "error": f"小黑盒{kind}没有可用搜索关键词",
            }

        add_button = await self._first_visible(f"button:has-text('添加{kind}')")
        if add_button is None:
            return {
                "success": False,
                "needs_selection": True,
                "error": f"小黑盒编辑器未找到“添加{kind}”按钮",
            }

        try:
            await self._click_with_fallback(add_button)
            await self.simulator.random_delay(0.8, 1.5)
            search = await self._wait_first_visible(
                ".modal input, [role='dialog'] input, [class*='dialog'] input, "
                f"input[placeholder*='{kind}'], input[placeholder*='搜索']",
                timeout_ms=5000,
            )
            if search is None:
                return {
                    "success": False,
                    "needs_selection": True,
                    "error": f"小黑盒{kind}选择弹窗未找到实时搜索框",
                }
            await search.fill(query[:30])
            await self.simulator.random_delay(1, 2)

            result = await self._wait_first_visible(result_selector, timeout_ms=5000)
            if result is None:
                await self._dismiss_overlays()
                return {
                    "success": False,
                    "needs_selection": True,
                    "error": f"小黑盒实时搜索没有找到{kind}: {query}",
                }

            selected_name = await self._extract_candidate_name(result, query)
            if self._candidate_is_invalid(selected_name, query):
                await self._dismiss_overlays()
                return {
                    "success": False,
                    "needs_selection": True,
                    "error": f"小黑盒{kind}候选名称无效，拒绝把搜索词当作结果: {selected_name or query}",
                }
            await self._click_with_fallback(result)
            await self.simulator.random_delay(0.5, 1.0)

            confirm = await self._first_visible("button:has-text('确定'), button:has-text('完成')")
            if confirm is not None:
                await self._click_with_fallback(confirm, timeout=3000)
                await self.simulator.random_delay(0.5, 1.0)

            await self._dismiss_overlays()

            if "creator/editor" not in (self.page.url or ""):
                await self._back_to_editor()
                return {
                    "success": False,
                    "needs_selection": True,
                    "error": f"小黑盒选择{kind}后离开编辑器",
                }

            editor_text = await self._visible_editor_text()
            if selected_name not in editor_text:
                return {
                    "success": False,
                    "needs_selection": True,
                    "error": f"小黑盒{kind}选择后未发现已选标签: {selected_name}",
                }
            logger.info("小黑盒{}实时搜索选择成功: query={}, selected={}", kind, query, selected_name)
            return {"success": True, "value": selected_name}
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(f"BROWSER_CONTEXT_CLOSED: 小黑盒{kind}选择时页面已关闭") from exc
            await self._dismiss_overlays()
            logger.error("小黑盒{}选择失败: query={}, error={}", kind, query, exc)
            return {
                "success": False,
                "needs_selection": True,
                "error": f"小黑盒{kind}选择失败: {exc}",
            }

    async def select_community(self, community: str = "", selection_query: str = ""):
        """独立选择社区；无手动值时使用文章关键词实时搜索。"""
        query = community or selection_query or ""
        return await self._select_from_editor_dialog(
            "社区",
            query,
            ".modal .community-item, [role='dialog'] .community-item, "
            "[class*='community-item'], .editor-model__topic-list-item, "
            ".search-result-item, [role='dialog'] li",
        )

    async def select_topic(self, topic: str = "", community: str = "",
                           selection_query: str = "", selection_override: dict = None):
        """自动优先选择社区和话题；无结果时进入 needs_selection。"""
        override = selection_override or {}
        explicit_community = override.get("community") or community or ""
        community_query = explicit_community or self._query_candidates(
            selection_query or topic or "", split_terms=True
        )
        explicit_topic = override.get("topic") or topic or ""
        # 话题搜索使用单个关键词，不能把多个关键词拼成一个伪话题名称。
        topic_query = explicit_topic or self._query_candidates(
            selection_query or "", split_terms=True
        )

        community_result = await self.select_community(community_query, selection_query)
        if not community_result.get("success"):
            return community_result
        # 社区选择器和话题选择器使用不同的列表；社区选择完成后先关闭遮罩，
        # 防止下一步误把社区列表第一项当成话题。
        if self.page is not None:
            await self._dismiss_overlays()

        topic_result = await self._select_from_editor_dialog(
            "话题",
            topic_query,
            # 小黑盒社区结果的真实 class 是 editor-model__topic-list-item，
            # 它也会命中宽泛的 [class*='topic-item']。话题必须优先使用
            # hashtag 结果，避免把刚才的社区结果再次记录成话题。
            ".editor-model__hashtag-list-item, .modal .topic-item, "
            "[role='dialog'] .topic-item, [class*='hashtag-item'], "
            ".search-result-item:not(.community-item), [role='dialog'] li",
        )
        if not topic_result.get("success"):
            # 社区已经选中时保留部分结果，人工只需补选话题。
            topic_result["selection"] = {
                "community": community_result.get("value") or "",
                "topic": "",
            }
            topic_result["selection_status"] = "needs_selection"
            return topic_result

        self._selected_values = {
            "community": community_result.get("value") or community_query,
            "topic": topic_result.get("value") or topic_query,
        }
        return {
            "success": True,
            "selection": dict(self._selected_values),
        }

    async def apply_cover(self, cover: dict | None = None) -> dict:
        """登记自动首图封面，保存后再以真实草稿卡片核验。"""

        strategy = str((cover or {}).get("strategy") or "NONE").upper()
        if strategy == "NONE":
            self._pending_cover_path = ""
            return {"success": True, "cover_status": "not_required"}
        if strategy not in {"FIRST_BODY_IMAGE", "EXPLICIT"}:
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "XHH_COVER_STRATEGY_UNSUPPORTED",
                "error": "小黑盒不支持该封面策略",
            }
        requested = str((cover or {}).get("local_path") or "")
        first_image = next(
            (
                str(block.get("local_path"))
                for block in (self._expected_persisted_blocks or [])
                if block.get("type") == "image" and block.get("local_path")
            ),
            "",
        )
        try:
            requested_path = Path(requested).resolve(strict=True)
            first_path = Path(first_image).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "XHH_COVER_ASSET_UNAVAILABLE",
                "error": "小黑盒封面素材不可用",
            }
        if requested_path != first_path:
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "XHH_COVER_MUST_BE_FIRST_BODY_IMAGE",
                "error": "小黑盒草稿封面只能自动使用正文首图",
            }
        self._pending_cover_path = str(requested_path)
        return {
            "success": True,
            "cover_status": "pending_verification",
            "cover_mode": "AUTO_FIRST_BODY_IMAGE",
        }

    @staticmethod
    def _cover_average_hash(payload: bytes) -> tuple[int, ...] | None:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(payload)) as source:
                image = source.convert("L").resize((16, 16))
                pixels = list(image.getdata())
        except Exception:
            return None
        average = sum(pixels) / len(pixels)
        return tuple(int(value >= average) for value in pixels)

    async def _cover_image_bytes(self, url: str) -> bytes:
        if self.context is None or not url:
            return b""
        try:
            response = await self.context.request.get(url, timeout=15000)
            if not response.ok:
                return b""
            return await response.body()
        except Exception:
            return b""

    async def verify_persisted_cover(
        self,
        *,
        title: str,
        draft_url: str,
        cover: dict | None,
        apply_result: dict,
    ) -> dict:
        """证明唯一草稿卡首图与重开正文首图视觉一致。"""

        del draft_url, cover
        expected_title = self._normalize_platform_title(title)
        editor_images = self.page.locator(
            ".article__edit-content--inner img.hb-cpt__image-elem"
        )
        if await editor_images.count() < 1:
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "XHH_COVER_BODY_IMAGE_MISSING",
                "error": "小黑盒重开草稿后正文首图不存在",
            }
        body_url = await editor_images.first.get_attribute("src") or ""
        body_bytes = await self._cover_image_bytes(body_url)
        await self.page.goto(self.DRAFTS_URL, wait_until="domcontentloaded", timeout=30000)
        await self.simulator.random_delay(2, 3)
        card_urls = await self.page.evaluate(
            r"""
            (expectedTitle) => {
                const normalize = (value) => String(value || '')
                    .replace(/\s+/g, ' ').trim().slice(0, 30);
                const cards = Array.from(document.querySelectorAll(
                    '.creator-draft__list article.creator-draft__item'
                )).filter((card) => normalize(
                    card.querySelector('.creator-draft__content')?.textContent
                ) === expectedTitle);
                if (cards.length !== 1) return [];
                return Array.from(cards[0].querySelectorAll('img'))
                    .map((image) => image.currentSrc || image.src || '')
                    .filter(Boolean);
            }
            """,
            expected_title,
        )
        if not isinstance(card_urls, list) or not card_urls:
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "XHH_COVER_DRAFT_CARD_MISSING",
                "error": "小黑盒唯一草稿卡未显示自动首图封面",
            }
        card_bytes = await self._cover_image_bytes(str(card_urls[0]))
        body_hash = self._cover_average_hash(body_bytes)
        card_hash = self._cover_average_hash(card_bytes)
        matched = bool(
            body_hash is not None
            and card_hash is not None
            and sum(
                left != right
                for left, right in zip(body_hash, card_hash, strict=True)
            )
            <= 8
        )
        if not matched:
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "XHH_COVER_VISUAL_MISMATCH",
                "error": "小黑盒草稿卡封面与正文首图不一致",
            }
        return {
            **apply_result,
            "success": True,
            "cover_status": "completed",
            "cover_mode": "AUTO_FIRST_BODY_IMAGE",
        }

    async def _dismiss_overlays(self):
        """关闭可能残留的弹窗遮罩（如社区/话题选择未正常关闭时遗留的 mask），避免遮挡按钮点击。"""
        try:
            self._raise_if_page_closed("关闭小黑盒弹窗遮罩")
            # 一次 Escape 可能只关闭嵌套弹窗，连续两次再检查遮罩状态。
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            await self.page.keyboard.press("Escape")
            await self.simulator.random_delay(0.3, 0.6)

            for selector in (
                "[role='dialog'] button[aria-label*='关闭']",
                ".modal button[aria-label*='关闭']",
                "[role='dialog'] button:has-text('取消'), .modal button:has-text('取消')",
            ):
                close_button = await self._first_visible(selector)
                if close_button is not None:
                    try:
                        await self._click_with_fallback(close_button, timeout=2000)
                    except Exception:
                        pass

            mask = self.page.locator(
                ".hb-cpt__mask-wrapper, .modal-mask, [class*='mask']"
            ).first
            if await mask.count() > 0 and await mask.is_visible():
                logger.warning("小黑盒仍存在可见遮罩，拒绝强制删除 DOM")
            if await self._has_visible_overlay():
                logger.warning("小黑盒弹窗/遮罩关闭后仍可见，后续点击将进行安全校验")
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 关闭弹窗遮罩时页面已关闭") from exc
            pass

    async def _back_to_editor(self):
        """若页面偏离了编辑器（被话题选择等带偏），回到编辑器 URL"""
        if getattr(self, "_editor_url", ""):
            self._raise_if_page_closed("返回小黑盒文章编辑器")
            try:
                await self.page.goto(self._editor_url, wait_until="domcontentloaded")
                await self.simulator.random_delay(2, 4)
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 返回小黑盒编辑器时页面已关闭") from exc
                logger.warning("小黑盒返回编辑器失败: {}", exc)

    async def save_draft(self, title: str = "") -> str:
        """保存草稿并返回稳定编辑 URL；无法证明新草稿存在时返回空串。

        保存成功的唯一业务证据是：保存点击前在同一浏览器 Context 读取到的
        草稿实体快照中不存在的唯一可见卡片，在保存后出现；点击该卡取得数字
        草稿 ID，显式重开相同 ID 后再核对标题和完整图文。列表摘要不当作标题。
        """
        self._raise_if_page_closed("小黑盒保存草稿")
        evidence = DraftVerificationEvidence()
        self._last_draft_evidence = evidence
        expected_title = self._normalize_platform_title(title)
        if not expected_title:
            logger.warning("小黑盒保存草稿拒绝：平台标题为空")
            return ""

        # 若在编辑器外（被话题选择等带偏），先回到编辑器
        if "creator/editor" not in (self.page.url or ""):
            await self._back_to_editor()

        if "creator/editor" not in (self.page.url or ""):
            logger.error("小黑盒保存草稿失败：不在编辑器页面，url={}", self.page.url)
            return ""

        # 必须在保存按钮点击前建立可靠 baseline；明确可见的空草稿箱是
        # 合法基线，只有无法证明页面结构/空态时才 fail closed。
        baseline = await self._collect_draft_baseline()
        if baseline is None:
            logger.warning("小黑盒保存草稿拒绝：保存前无法取得可靠草稿箱基线")
            return ""

        # 点击真实保存草稿按钮。此点之后的任何不确定结果都禁止自动重试，
        # 必须上报 DRAFT_RESULT_UNKNOWN，而不是伪装成“尚未发生副作用”。
        clicked = False
        try:
            # close leftover modal mask if any (e.g. community/topic picker left open)
            await self._dismiss_overlays()
            save_button = await self._resolve_save_draft_button()
            clicked = True
            await save_button.click(timeout=8000)
        except asyncio.CancelledError as exc:
            if clicked:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 小黑盒保存动作已触发，结果未知"
                ) from exc
            raise
        except Exception as e:
            if self._exception_means_browser_closed(e):
                if clicked:
                    raise DraftResultUnknownError(
                        "DRAFT_RESULT_UNKNOWN: 小黑盒保存后页面已关闭"
                    ) from e
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小黑盒点击保存草稿前页面已关闭"
                ) from e
            logger.error("小黑盒点击保存草稿按钮失败: {}", e)
            return ""

        await self.simulator.random_delay(2, 4)

        # 验证：进入草稿箱确认新增实体；若本轮有正文，再打开唯一实体并严格
        # 核对持久化后的文本、标题层级、图片数量和图文顺序。
        try:
            draft_url = await self._verify_draft_in_drafts(expected_title, baseline)
            if not draft_url:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 小黑盒保存后草稿实体无法证明"
                )
            return draft_url
        except DraftResultUnknownError as exc:
            if exc.evidence is None:
                exc.evidence = evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN")
            raise
        except Exception as exc:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒保存后内容无法证明"
            ) from exc

    async def _resolve_save_draft_button(self):
        """只返回唯一、可见、可用的保存草稿按钮。

        小黑盒曾调整按钮 class，单一 CSS 选择器会在正文已经写完后才失败。
        候选仍严格限定为 button，并以真实文字或已验证 class 二次过滤；
        候选不唯一时停止，避免误点旁边的草稿箱或公开发布按钮。
        """

        self._raise_if_page_closed("定位小黑盒保存草稿按钮")
        try:
            controls = self.page.locator(self.SAVE_DRAFT_CANDIDATES)
            visible = []
            for index in range(await controls.count()):
                control = controls.nth(index)
                if not await control.is_visible() or not await control.is_enabled():
                    continue
                text = " ".join((await control.inner_text()).split())
                class_name = str(await control.get_attribute("class") or "")
                if text == "保存草稿" or "editor-publish__save-draft" in class_name:
                    visible.append(control)
            if len(visible) != 1:
                raise SelectorError("小黑盒保存草稿按钮不可用或候选不唯一")
            return visible[0]
        except SelectorError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 定位小黑盒保存草稿按钮时页面已关闭"
                ) from exc
            raise SelectorError("小黑盒保存草稿按钮定位失败") from exc

    @staticmethod
    def _is_drafts_route(url: str | None) -> bool:
        """只接受草稿箱路由，避免被导航/编辑器/登录页的文本误判。"""
        parts = urlsplit(str(url or "").strip())
        return (
            parts.scheme == "https"
            and parts.netloc.lower() == "www.xiaoheihe.cn"
            and parts.path.rstrip("/") == "/creator/draft"
        )

    async def _snapshot_draft_state(
        self,
        page,
    ) -> tuple[list[dict[str, object]], bool]:
        """读取可见的具体草稿卡片，不扫描 document.body 全局文本。

        真实小黑盒卡片的 `.creator-draft__content` 是正文摘要而不是标题。
        因此列表阶段只生成不可逆实体指纹并做保存前后差集；真正标题必须在点击
        卡片、取得数字草稿 ID 并显式重开编辑页后核对。
        """
        self._raise_if_page_closed("读取小黑盒草稿卡片")
        if page is None or not self._is_drafts_route(getattr(page, "url", "")):
            return [], False
        try:
            raw_candidates = await page.evaluate(
                r"""
                () => {
                    const draftLinkSelector = [
                        "a[href*='/creator/editor/draft/']",
                        "a[href*='/creator/editor/edit/article/']",
                    ].join(",");
                    // 小黑盒当前真实草稿箱使用 article.creator-draft__item，
                    // 条目本身没有 href/data-draft-id；必须把它作为受控实体
                    // 读取，不能退回 document.body 的全文匹配。
                    const draftEntitySelector = "article.creator-draft__item";
                    const candidateSelector = [
                        draftLinkSelector,
                        "[data-draft-id]",
                        draftEntitySelector,
                    ].join(",");
                    const rootSelector = [
                        draftEntitySelector,
                        "[data-draft-id]",
                        "[class*='draft-card']",
                        "[class*='draft-item']",
                        "[class*='draft-list-item']",
                        "article",
                        "li",
                    ].join(",");
                    const listRootSelector = [
                        ".creator-draft__list",
                        "[data-draft-list]",
                        "[data-testid='draft-list']",
                        "[role='list'][aria-label='草稿箱']",
                        "ul[aria-label='草稿箱']",
                        ".draft-list",
                        ".draft-list-container",
                        ".drafts-list",
                    ].join(",");
                    const emptyStateSelector = [
                        ".creator-draft__empty",
                        "[data-draft-empty]",
                        ".draft-empty",
                        ".empty-draft",
                    ].join(",");
                    const emptyLabels = new Set([
                        "暂无草稿", "暂无草稿内容", "还没有草稿", "没有草稿",
                    ]);

                    const visible = (element) => {
                        try {
                            if (!element || element.nodeType !== Node.ELEMENT_NODE) return false;
                            if (element.hidden || element.getAttribute("aria-hidden") === "true") {
                                return false;
                            }
                            const style = window.getComputedStyle(element);
                            if (style.display === "none" || style.visibility === "hidden" ||
                                Number(style.opacity) === 0) return false;
                            const rect = element.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                        } catch (_error) {
                            return false;
                        }
                    };

                    const normalize = (value) => String(value || "")
                        .replace(/\s+/g, " ").trim();

                    const fingerprint = (value) => {
                        let hash = 2166136261;
                        for (const character of String(value || "")) {
                            hash ^= character.charCodeAt(0);
                            hash = Math.imul(hash, 16777619);
                        }
                        return (hash >>> 0).toString(16).padStart(8, "0");
                    };

                    const isConcreteDraftHref = (href) => {
                        try {
                            const url = new URL(href, window.location.href);
                            if (url.origin !== window.location.origin) return false;
                            const path = url.pathname;
                            const route = new RegExp(
                                '/creator/editor/(?:draft/[^/]+|edit/article/[0-9]+)/?$'
                            );
                            return route.test(path);
                        } catch (_error) {
                            return false;
                        }
                    };

                    const listRootFor = (element) => {
                        try {
                            return element.closest(listRootSelector);
                        } catch (_error) {
                            return null;
                        }
                    };

                    const entityRootFor = (element) => {
                        try {
                            return element.matches(draftEntitySelector)
                                ? element
                                : element.closest(draftEntitySelector) ||
                                    element.closest(rootSelector) || element;
                        } catch (_error) {
                            return null;
                        }
                    };

                    const byOpaque = new Map();
                    const processedRoots = new Set();
                    const draftCards = Array.from(
                        document.querySelectorAll(draftEntitySelector)
                    );
                    const result = [];
                    for (const element of document.querySelectorAll(candidateSelector)) {
                        if (!visible(element)) continue;
                        const root = entityRootFor(element);
                        if (!root || !visible(root)) continue;
                        if (processedRoots.has(root)) continue;
                        processedRoots.add(root);
                        const listRoot = listRootFor(root) || listRootFor(element);
                        if (!listRoot || !visible(listRoot)) continue;
                        const link = root.matches(draftLinkSelector)
                            ? root
                            : root.querySelector(draftLinkSelector);
                        const href = link ? String(link.getAttribute("href") || "") : "";
                        const nestedIdNode = root.querySelector("[data-draft-id]");
                        const draftId = String(
                            root.getAttribute("data-draft-id") ||
                            (nestedIdNode ? nestedIdNode.getAttribute("data-draft-id") : "") ||
                            element.getAttribute("data-draft-id") || ""
                        ).trim();
                        if (!draftId && !href && !root.matches(draftEntitySelector)) continue;
                        if (href && !isConcreteDraftHref(href)) continue;

                        let opaque = draftId ? `data:${draftId}` : `href:${href}`;
                        if (!draftId && !href) {
                            // 真实 article 条目没有公开 ID；只用该条目自身的
                            // 正文摘要、类型和图片路径生成稳定指纹。编辑时间、按钮文字
                            // 会随页面刷新变化，禁止纳入实体差集。
                            let identity = "";
                            try {
                                const preview = normalize(
                                    root.querySelector(".creator-draft__content")?.textContent
                                );
                                const type = normalize(
                                    root.querySelector(".creator-draft__type")?.textContent
                                );
                                const images = Array.from(
                                    root.querySelectorAll(".creator-draft__image")
                                ).map((image) => {
                                    try {
                                        const url = new URL(
                                            image.getAttribute("src") || "",
                                            window.location.href
                                        );
                                        return `${url.origin}${url.pathname}`;
                                    } catch (_error) {
                                        return "";
                                    }
                                }).filter(Boolean);
                                if (!preview && !type && images.length === 0) continue;
                                identity = JSON.stringify({preview, type, images});
                            } catch (_error) {
                                identity = "";
                            }
                            if (!identity) continue;
                            opaque = `fingerprint:${fingerprint(identity)}`;
                        }
                        const existing = byOpaque.get(opaque);
                        if (existing) {
                            existing.occurrence_count += 1;
                            existing.card_index = -1;
                            continue;
                        }
                        const candidate = {
                            opaque,
                            occurrence_count: 1,
                            card_index: draftCards.indexOf(root),
                        };
                        byOpaque.set(opaque, candidate);
                        result.push(candidate);
                    }
                    const visibleListRoots = Array.from(
                        document.querySelectorAll(listRootSelector)
                    ).filter(visible);
                    const hasVisibleEmptyState = visibleListRoots.some((root) =>
                        Array.from(root.querySelectorAll(emptyStateSelector)).some(
                            (element) => {
                                const text = normalize(element.innerText);
                                return visible(element) && emptyLabels.has(text);
                            }
                        )
                    );
                    return {
                        candidates: result,
                        // 仅列表骨架可见不能证明草稿箱为空；必须已有具体卡片，
                        // 或平台明确渲染可见空态。
                        reliable: result.length > 0 || hasVisibleEmptyState,
                    };
                }
                """
            )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 读取小黑盒草稿卡片时页面已关闭"
                ) from exc
            logger.warning(
                "小黑盒草稿卡片读取失败：error_type={}",
                type(exc).__name__,
            )
            return [], False

        reliable = False
        if isinstance(raw_candidates, dict):
            reliable = raw_candidates.get("reliable") is True
            raw_candidates = raw_candidates.get("candidates")
        elif isinstance(raw_candidates, list):
            # 旧测试桩/兼容调用只返回候选列表；非空列表仍能证明结构，
            # 空列表没有足够证据，避免把任意空页面当成空草稿箱。
            reliable = bool(raw_candidates)
        if not isinstance(raw_candidates, list):
            return [], False
        candidates = []
        seen = set()
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            opaque = item.get("opaque")
            if not isinstance(opaque, str) or not opaque:
                continue
            if opaque in seen:
                continue
            seen.add(opaque)
            occurrence_count = item.get("occurrence_count", 1)
            card_index = item.get("card_index", -1)
            if not isinstance(occurrence_count, int) or occurrence_count < 1:
                continue
            if not isinstance(card_index, int):
                card_index = -1
            candidates.append(
                {
                    "opaque": opaque[:512],
                    "occurrence_count": occurrence_count,
                    "card_index": card_index,
                }
            )
        return candidates, reliable or bool(candidates)

    async def _snapshot_draft_candidates(self, page) -> list[dict[str, object]]:
        """兼容性投影：只返回安全的具体草稿候选，不暴露可靠性细节。"""
        candidates, _reliable = await self._snapshot_draft_state(page)
        return candidates

    async def _collect_draft_baseline(self) -> list[dict[str, object]] | None:
        """在同一 Context 的独立页面读取保存前草稿卡片快照并始终关闭页面。"""
        context = self.context
        new_page = getattr(context, "new_page", None) if context is not None else None
        if not callable(new_page):
            logger.warning("小黑盒保存草稿拒绝：当前 Context 不支持独立草稿箱页面")
            return None
        baseline_page = None
        try:
            baseline_page = await new_page()
            await baseline_page.goto(self.DRAFTS_URL, wait_until="domcontentloaded")
            if not self._is_drafts_route(getattr(baseline_page, "url", "")):
                logger.warning("小黑盒保存草稿拒绝：草稿箱页面路由未确认")
                return None
            await self.simulator.random_delay(1, 2)
            candidates, reliable = await self._snapshot_draft_state(baseline_page)
            return candidates if reliable else None
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 读取保存前草稿箱时页面已关闭"
                ) from exc
            logger.warning(
                "小黑盒保存前草稿箱读取失败：error_type={}",
                type(exc).__name__,
            )
            return None
        finally:
            if baseline_page is not None:
                try:
                    await baseline_page.close()
                except Exception as exc:
                    if self._exception_means_browser_closed(exc):
                        logger.debug("小黑盒保存前草稿箱页面已关闭")
                    else:
                        logger.debug(
                            "小黑盒关闭保存前草稿箱页面失败：error_type={}",
                            type(exc).__name__,
                        )

    @staticmethod
    def _new_draft_candidate(
        baseline: list[dict[str, object]],
        current: list[dict[str, object]],
    ) -> dict[str, object] | None:
        """返回 baseline 之外唯一且无指纹冲突的新草稿实体。"""

        if current is None or not current:
            return None
        baseline_keys = {
            item.get("opaque")
            for item in baseline
            if isinstance(item, dict) and item.get("opaque")
        }
        matches = [
            item for item in current
            if isinstance(item, dict)
            and item.get("opaque") not in baseline_keys
            and item.get("occurrence_count", 1) == 1
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _new_matching_draft_candidate(
        baseline: list[dict[str, object]],
        current: list[dict[str, object]],
        expected_title: str,
    ) -> dict[str, object] | None:
        """旧私有入口兼容；列表摘要不是标题，因此不参与实体差集。"""

        del expected_title
        return XiaoheihePlatform._new_draft_candidate(baseline, current)

    @staticmethod
    def _has_new_matching_draft(
        baseline: list[dict[str, object]],
        current: list[dict[str, object]],
        expected_title: str,
    ) -> bool:
        """兼容性布尔投影：是否存在唯一可绑定的新草稿卡。"""

        return XiaoheihePlatform._new_matching_draft_candidate(
            baseline,
            current,
            expected_title,
        ) is not None

    @classmethod
    def _draft_id_from_editor_url(cls, url: str | None) -> str:
        """从已观察到的小黑盒编辑路由提取稳定数字草稿 ID。"""

        parts = urlsplit(str(url or "").strip())
        if parts.scheme != "https" or parts.netloc.lower() != "www.xiaoheihe.cn":
            return ""
        match = cls.DRAFT_EDIT_PATH_PATTERN.fullmatch(parts.path)
        return match.group("draft_id") if match else ""

    @classmethod
    def _canonical_draft_editor_url(cls, draft_id: str) -> str:
        """由已校验的数字 ID 构造无 query/fragment 的规范编辑 URL。"""

        if not str(draft_id).isdigit():
            return ""
        return (
            "https://www.xiaoheihe.cn/creator/editor/edit/article/"
            f"{draft_id}"
        )

    async def _reopen_bound_draft(self, draft_id: str) -> str:
        """按数字 ID 显式重开草稿，并拒绝任何异域或串稿重定向。"""

        editor_url = self._canonical_draft_editor_url(draft_id)
        if not editor_url:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒草稿 ID 无效"
            )
        try:
            await self.page.goto(
                editor_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 重开小黑盒草稿时页面已关闭"
                ) from exc
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒草稿无法按稳定 ID 重开"
            ) from exc
        if self._draft_id_from_editor_url(self.page.url) != draft_id:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒草稿重开后 ID 不一致"
            )
        return editor_url

    async def _open_unique_new_draft(
        self,
        expected_candidate: dict[str, object],
    ) -> str:
        """点击唯一新增卡片，取得数字 ID 后显式重开规范编辑 URL。"""

        expected_opaque = str(expected_candidate.get("opaque") or "")
        if not expected_opaque:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒新增草稿缺少实体指纹"
            )

        try:
            match_count = await self.page.evaluate(
                r"""
                (expectedOpaque) => {
                    const normalize = (value) => String(value || '')
                        .replace(/\s+/g, ' ').trim();
                    const fingerprint = (value) => {
                        let hash = 2166136261;
                        for (const character of String(value || '')) {
                            hash ^= character.charCodeAt(0);
                            hash = Math.imul(hash, 16777619);
                        }
                        return (hash >>> 0).toString(16).padStart(8, '0');
                    };
                    const visible = (element) => {
                        if (!element || element.hidden ||
                            element.getAttribute('aria-hidden') === 'true') return false;
                        const style = window.getComputedStyle(element);
                        if (style.display === 'none' || style.visibility === 'hidden' ||
                            Number(style.opacity) === 0) return false;
                        const rect = element.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const cardOpaque = (card) => {
                        const draftIdNode = card.querySelector('[data-draft-id]');
                        const draftId = String(
                            card.getAttribute('data-draft-id') ||
                            (draftIdNode ? draftIdNode.getAttribute('data-draft-id') : '') || ''
                        ).trim();
                        if (draftId) return `data:${draftId}`;
                        const link = card.querySelector(
                            "a[href*='/creator/editor/draft/']," +
                            "a[href*='/creator/editor/edit/article/']"
                        );
                        const href = link ? String(link.getAttribute('href') || '') : '';
                        if (href) return `href:${href}`;
                        const type = normalize(
                            card.querySelector('.creator-draft__type')?.textContent
                        );
                        const preview = normalize(
                            card.querySelector('.creator-draft__content')?.textContent
                        );
                        const images = Array.from(
                            card.querySelectorAll('.creator-draft__image')
                        ).map((image) => {
                            try {
                                const url = new URL(
                                    image.getAttribute('src') || '',
                                    window.location.href
                                );
                                return `${url.origin}${url.pathname}`;
                            } catch (_error) {
                                return '';
                            }
                        }).filter(Boolean);
                        const identity = JSON.stringify({preview, type, images});
                        return `fingerprint:${fingerprint(identity)}`;
                    };
                    const matches = Array.from(document.querySelectorAll(
                        '.creator-draft__list article.creator-draft__item'
                    )).filter((card) => {
                        if (!visible(card)) return false;
                        return cardOpaque(card) === expectedOpaque;
                    });
                    if (matches.length === 1) {
                        const clickTarget = matches[0].querySelector(
                            '.creator-draft__item-main'
                        ) || matches[0];
                        clickTarget.click();
                    }
                    return matches.length;
                }
                """,
                expected_opaque,
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小黑盒打开已保存草稿时页面已关闭"
                ) from exc
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒无法定位唯一草稿实体"
            ) from exc
        if match_count != 1:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒新增草稿实体无法唯一定位"
            )

        draft_id = ""
        for _attempt in range(self.DRAFT_ROUTE_VERIFY_ATTEMPTS):
            draft_id = self._draft_id_from_editor_url(self.page.url)
            if draft_id:
                break
            await asyncio.sleep(self.DRAFT_ROUTE_VERIFY_INTERVAL_SECONDS)
        if not draft_id:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒草稿卡未进入带稳定 ID 的编辑页面"
            )

        return await self._reopen_bound_draft(draft_id)

    async def _open_unique_matching_draft(self, expected_title: str) -> str:
        """旧私有入口不再按摘要猜标题；仅允许重开当前已绑定数字 ID。"""

        del expected_title
        draft_id = self._draft_id_from_editor_url(self.page.url)
        if not draft_id:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒当前页面没有已绑定草稿 ID"
            )
        return await self._reopen_bound_draft(draft_id)

    async def _verify_persisted_draft_content(self, expected_title: str) -> None:
        """兼容入口：按标题唯一打开草稿，再核验完整图文结构。"""

        editor_url = await self._open_unique_matching_draft(expected_title)
        await self._verify_open_draft_content(
            expected_title,
            self._draft_id_from_editor_url(editor_url),
        )

    async def _verify_open_draft_content(
        self,
        expected_title: str,
        expected_draft_id: str,
        evidence: DraftVerificationEvidence | None = None,
    ) -> bool:
        """对已经绑定并打开的草稿编辑页核验真正落盘的图文结构。"""

        if not expected_draft_id:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒持久化核验缺少草稿 ID"
            )
        blocks = self._expected_persisted_blocks
        expected_tokens = self._expected_content_tokens(blocks or [])
        actual_tokens: list[dict] = []
        title_matched = False
        for delay in self.DRAFT_CONTENT_POLL_DELAYS:
            await asyncio.sleep(delay)
            try:
                if self._draft_id_from_editor_url(self.page.url) != expected_draft_id:
                    continue
                title_editor = await self._first_visible(self.TITLE_FIELD)
                if title_editor is None:
                    continue
                actual_title = self._normalize_platform_title(
                    await title_editor.inner_text()
                )
                if actual_title != expected_title:
                    continue
                title_matched = True
                if evidence is not None:
                    evidence.mark_reopen(
                        title_match=True,
                        dom_blocks_match=None,
                    )
                if blocks is None:
                    return False
                editor = await self._current_body_editor()
                actual_tokens = await self._read_editor_dom_tokens(editor)
                if self._content_tokens_match(expected_tokens, actual_tokens):
                    if evidence is not None:
                        evidence.mark_reopen(
                            title_match=True,
                            dom_blocks_match=True,
                        )
                    return True
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 小黑盒持久化正文核验时页面已关闭"
                    ) from exc
        if evidence is not None:
            evidence.mark_reopen(
                title_match=title_matched,
                dom_blocks_match=False if title_matched and blocks is not None else None,
            )
        if not title_matched:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小黑盒草稿重开后标题不一致"
            )
        raise DraftResultUnknownError(
            "DRAFT_RESULT_UNKNOWN: 小黑盒草稿重开后图文结构不完整; "
            f"expected={self._content_token_shape(expected_tokens)}; "
            f"actual={self._content_token_shape(actual_tokens)}"
        )

    async def _verify_draft_in_drafts(
        self,
        expected_title: str,
        baseline: list[dict[str, object]],
    ) -> str:
        """保存后轮询具体草稿卡片，确认新实体出现；失败返回空串。"""
        try:
            self._raise_if_page_closed("小黑盒验证草稿箱")
            await self.page.goto(self.DRAFTS_URL, wait_until="domcontentloaded")
            await self.simulator.random_delay(3, 5)
        except BrowserLifecycleError:
            raise
        except Exception as e:
            if self._exception_means_browser_closed(e):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 打开小黑盒草稿箱时页面已关闭"
                ) from e
            logger.error("小黑盒打开草稿箱失败：error_type={}", type(e).__name__)
            return ""

        drafts_url = self.page.url
        if not self._is_drafts_route(drafts_url):
            logger.error("小黑盒草稿箱路由验证失败")
            return ""

        for attempt, delay in enumerate(self.DRAFT_VERIFY_DELAYS):
            if attempt:
                try:
                    # SPA 草稿列表可能保留首次空快照。重新导航只读页面，
                    # 强制取得新的列表响应；不会重复保存，也不会产生副作用。
                    await self.page.goto(
                        self.DRAFTS_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as exc:
                    if self._exception_means_browser_closed(exc):
                        raise BrowserLifecycleError(
                            "BROWSER_CONTEXT_CLOSED: 刷新小黑盒草稿箱时页面已关闭"
                        ) from exc
                    logger.warning(
                        "小黑盒草稿箱第{}次只读刷新失败：error_type={}",
                        attempt + 1,
                        type(exc).__name__,
                    )
            await self.simulator.random_delay(delay, delay + 0.5)
            current, reliable = await self._snapshot_draft_state(self.page)
            candidate = self._new_draft_candidate(baseline, current) if reliable else None
            if candidate is not None:
                evidence = self._last_draft_evidence
                edit_url = await self._open_unique_new_draft(candidate)
                draft_id = self._draft_id_from_editor_url(edit_url)
                evidence.set_draft_url(edit_url)
                try:
                    await self._verify_open_draft_content(
                        expected_title,
                        draft_id,
                        evidence,
                    )
                    evidence.mark_entity_binding(
                        bound=True,
                        source="baseline_new_id",
                        id_match=True,
                    )
                except Exception:
                    title_matched = evidence.reopen_title_match is True
                    evidence.mark_entity_binding(
                        bound=title_matched,
                        source="baseline_new_id",
                        id_match=title_matched,
                    )
                    raise
                evidence.finalize()
                return edit_url
        logger.warning("小黑盒草稿箱未确认保存后新增的匹配草稿卡片")
        return ""

    async def publish_now(self, title: str = "") -> str:
        """点击「发布」按钮真正发布文章，返回发布后的文章 URL；失败返回空串"""
        if "creator/editor" not in (self.page.url or ""):
            await self._back_to_editor()
        if "creator/editor" not in (self.page.url or ""):
            return ""

        try:
            btn = self.page.locator(self.PUBLISH_NOW_BTN, has_text="发布").first
            if await btn.count() == 0:
                btn = self.page.locator("span.editor-publish__button-content", has_text="发布").first
            if await btn.count() == 0:
                return ""
            # close leftover modal mask if any
            await self._dismiss_overlays()
            await btn.click(timeout=8000)
        except Exception as e:
            logger.error("小黑盒点击发布失败: {}", e)
            return ""

        await self.simulator.random_delay(3, 6)

        # 可能弹出确认框
        try:
            confirm = self.page.locator("button:has-text('确定'), button:has-text('确认发布')").first
            if await confirm.count() > 0:
                await confirm.click(timeout=3000)
                await self.simulator.random_delay(3, 6)
        except Exception:
            pass

        post_url = self.page.url
        try:
            success = await self.page.evaluate("""() => {
                const t = document.body.innerText || '';
                return t.includes('发布成功') || t.includes('发布完成') || t.includes('已发布');
            }""")
            if success:
                return post_url
            if "creator/editor" not in post_url:
                return post_url
        except Exception:
            pass
        # 无法确认，返回空（视为仅存草稿）
        return ""
