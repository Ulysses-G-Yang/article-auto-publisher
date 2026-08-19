"""小黑盒（Xiaoheihe）平台自动化"""
import asyncio
import json
from pathlib import Path

from loguru import logger

from platforms.base import BasePlatform, BrowserLifecycleError, SelectorError
from platforms.content_validation import (
    ensure_valid_content,
    safe_media_error,
)


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
    DRAFT_BOX_BTN = "button.editor-publish__btn.sub-btn.margin-left"  # 草稿箱
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

    def _raise_if_page_closed(self, stage: str):
        self._require_page_alive(stage)

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
        """返回 restore_login 捕获/重放的最小平台身份；缺失时如实返回未确认。"""

        payload = getattr(self, "_restore_profile", None)
        if isinstance(payload, dict) and payload.get("ok"):
            return dict(payload)
        url = getattr(self, "_restore_url", None)
        if not url:
            return {"ok": False, "user_id": "", "display_name": ""}
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
            # 多等一会儿，确保 SPA 完成 hydration、cookie 已注入页面上下文
            await asyncio.sleep(4)
            return await self._is_logged_in_dom()
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
        expected = " ".join((title or "").split())[:30]
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

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文（真实字段：.article__edit-content--inner 内的 contenteditable ProseMirror）"""
        self._raise_if_page_closed("小黑盒填写正文")
        editor = await self._current_body_editor()
        await editor.fill("")
        await self._place_body_caret_at_end()
        await self.simulator.random_delay(0.3, 0.8)

        expected_images = sum(
            1 for block in content_blocks if block.get("type") == "image"
        )
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
                if content_started:
                    await self._place_body_caret_at_end()
                    await self.page.keyboard.press("Enter")
                    await self.page.keyboard.press("Enter")
                await self._place_body_caret_at_end()
                lines = text.splitlines() or [text]
                for i, line in enumerate(lines):
                    if line.strip():
                        await self.page.keyboard.insert_text(line.strip())
                    if i < len(lines) - 1:
                        await self.page.keyboard.press("Enter")
                content_started = True
                continue

            if btype != "image":
                continue

            if content_started:
                await self._place_body_caret_at_end()
                await self.page.keyboard.press("Enter")
                await self.page.keyboard.press("Enter")
            await self._place_body_caret_at_end()

            img_path = block.get("local_path")
            if not img_path and images:
                # 按 position 匹配，否则取第一张
                for img in images:
                    if img.get("position_index") == block.get("position"):
                        img_path = img.get("local_path")
                        break
                if not img_path:
                    img_path = images[0].get("local_path")
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

            # 图片操作可能重建正文编辑器；立即从新节点读取截至当前块的
            # 所有文字，首个缺失/乱序必须硬失败，后续图片不得继续。
            await self._validate_written_text(
                content_blocks[: block_index + 1],
                phase=f"图片处理后第{block_index + 1}块",
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
        expected_count = ensure_valid_content(
            content_blocks,
            actual_text,
            platform="小黑盒",
            phase="图片处理后",
        )
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
        return ensure_valid_content(
            text_blocks,
            actual_text,
            platform="小黑盒",
            phase=phase,
        )

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
        """保存草稿并返回草稿箱 URL；保存失败返回空串（绝不以当前页 URL 冒充成功）

        真实保存按钮：button.editor-publish__save-draft
        保存后去「草稿箱」(button.editor-publish__btn.sub-btn.margin-left) 验证标题是否出现，
        以草稿箱真实存在该草稿作为成功判据（直接回应「账号/草稿箱里都找不到」的投诉）。
        """
        self._raise_if_page_closed("小黑盒保存草稿")
        # 若在编辑器外（被话题选择等带偏），先回到编辑器
        if "creator/editor" not in (self.page.url or ""):
            await self._back_to_editor()

        if "creator/editor" not in (self.page.url or ""):
            logger.error("小黑盒保存草稿失败：不在编辑器页面，url={}", self.page.url)
            return ""

        # 点击真实保存草稿按钮
        try:
            # close leftover modal mask if any (e.g. community/topic picker left open)
            await self._dismiss_overlays()
            await self.page.click(self.SAVE_DRAFT_BTN, timeout=8000)
        except Exception as e:
            if self._exception_means_browser_closed(e):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 小黑盒点击保存草稿时页面已关闭") from e
            logger.error("小黑盒点击保存草稿按钮失败: {}", e)
            return ""

        await self.simulator.random_delay(2, 4)

        # 验证：进入草稿箱，确认标题存在
        return await self._verify_draft_in_drafts(title)

    async def _verify_draft_in_drafts(self, title: str) -> str:
        """前往草稿箱，确认刚写的草稿存在，返回草稿箱 URL；不存在返回空串"""
        try:
            self._raise_if_page_closed("小黑盒验证草稿箱")
            draft_box = self.page.locator(self.DRAFT_BOX_BTN, has_text="草稿箱").first
            if await draft_box.count() > 0:
                await draft_box.click(timeout=5000)
                await self.simulator.random_delay(3, 5)
            else:
                # 直接导航草稿箱
                await self.page.goto("https://www.xiaoheihe.cn/creator/draft", wait_until="domcontentloaded")
                await self.simulator.random_delay(3, 5)
        except Exception as e:
            if self._exception_means_browser_closed(e):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: 打开小黑盒草稿箱时页面已关闭") from e
            logger.error("小黑盒打开草稿箱失败: {}", e)
            return ""

        drafts_url = self.page.url
        if "/creator/draft" not in (drafts_url or ""):
            logger.error("小黑盒草稿箱路由验证失败: {}", drafts_url)
            return ""

        if title:
            keyword = title[:12]
            try:
                found = await self.page.evaluate("(kw) => (document.body.innerText || '').includes(kw)", keyword)
                if found:
                    return drafts_url
                # 再等一会，草稿可能异步出现
                await self.simulator.random_delay(3, 5)
                found = await self.page.evaluate("(kw) => (document.body.innerText || '').includes(kw)", keyword)
                if found:
                    return drafts_url
            except Exception:
                pass
            logger.error("小黑盒草稿箱未找到标题包含「{}」的草稿", keyword)
            return ""

        # 无标题可校验，乐观返回草稿箱 URL
        return drafts_url

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
