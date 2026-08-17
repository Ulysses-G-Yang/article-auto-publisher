"""小红书创作服务平台账号会话适配器。

⚠️ 风控警告：小红书风控严格，所有涉及小红的任务必须先读
``docs/XIAOHONGSHU_RISK_CONTROL.md``，并遵守其中的硬性纪律。

登录态与身份验证链路（真实扫码 → 会话 cookie → 身份捕获），
内容投递能力尚未接入：所有投递方法显式拒绝，避免把未经真实编辑器
验证的逻辑伪装成可用能力。

真实登录页（https://creator.xiaohongshu.com/login）：
- 「APP扫一扫登录」为默认 Tab，二维码为约 160x160 的 base64 PNG。
- 登录成功信号：.xiaohongshu.com 出现 web_session cookie。
- 平台接口带重型反爬（as.xiaohongshu.com 签名/验证码），裸 fetch 会被拒；
  因此身份提取采用「捕获页面自身响应」模式，与小黑盒 restore_login 一致。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    LoginRequiredError,
    PlatformAutomationError,
    SelectorError,
)
from platforms.content_validation import ensure_valid_content, safe_media_error

CREATOR_HOME = "https://creator.xiaohongshu.com/"
CREATOR_MAIN = "https://creator.xiaohongshu.com/new/home"
LOGIN_URL = "https://creator.xiaohongshu.com/login"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name"}
IDENTITY_UID_KEYS = {"user_id", "sec_uid", "uid"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class XiaohongshuPlatform(BasePlatform):
    """小红书创作服务平台账号会话适配器；内容投递能力保持关闭。"""

    platform_name = "xiaohongshu"
    # 2026-08 真实验证：小红书已不再下发 web_session，现行会话 cookie 为
    # customer-sso-sid / customerClientId / access-token-creator.* /
    # x-user-id-creator.* / galaxy_creator_session_id；web_session 保留兼容。
    SESSION_COOKIE_NAMES = frozenset({
        "web_session",
        "customer-sso-sid",
        "customerClientId",
        "access-token-creator.xiaohongshu.com",
        "x-user-id-creator.xiaohongshu.com",
        "galaxy_creator_session_id",
        "galaxy.creator.beaker.session.id",
    })
    LOGIN_POLL_ATTEMPTS = 40
    LOGIN_POLL_INTERVAL_SECONDS = 3

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None

    # ==================== 登录态与身份 ====================

    async def _has_session_cookie_signal(self) -> bool:
        """会话 cookie 作为登录成功信号；不返回、不记录 cookie 名和值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies([
                "https://creator.xiaohongshu.com/",
                "https://www.xiaohongshu.com/",
            ])
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def check_login(self) -> bool:
        """只读验证现有 Profile；会话 cookie 出现才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("小红书登录态检测")
            await self.page.goto(
                CREATOR_HOME,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(4)
            if await self._has_session_cookie_signal():
                return True
            self.last_login_error = "LOGIN_REQUIRED: 小红书账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "XHS_LOGIN_CHECK_ERROR: 小红书登录态验证失败"
            return False

    async def login(self):
        """打开小红书创作服务登录页，等待用户在隔离 Profile 中扫码登录。"""

        self._require_page_alive("小红书打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=15000,
        )
        # 等待二维码出现：约 160x160 的 base64 PNG
        try:
            await self.page.wait_for_function(
                """() => {
                    const imgs = Array.from(
                        document.querySelectorAll('img[src^="data:image/png;base64"]')
                    );
                    return imgs.some(
                        (i) => i.naturalWidth > 130 && i.naturalWidth < 220
                    );
                }""",
                timeout=15000,
            )
        except Exception:
            pass
        await self._show_scan_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("小红书等待登录")
            if await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: 小红书登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_scan_hint(self):
        """页面顶部显示扫码提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'xhs-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#ff2442;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请用小红书 App 扫码登录'
                        + '<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """返回创作者首页同源确认的最小平台身份。

        策略：优先捕获页面自身的用户接口响应（resource_type 同时覆盖
        xhr 与 fetch）；捕获失败时回退到首页 DOM 提取（侧栏 ``user-info``
        昵称 + 「小红书账号:」正则）。缺失时如实返回未确认，绝不伪造。
        """

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        response.request.resource_type in ("xhr", "fetch")
                        and "xiaohongshu.com" in response.url
                        and any(
                            key in response.url.lower()
                            for key in ("user", "profile", "account", "selfinfo")
                        )
                    ):
                        payload = await response.json()
                        found = self._extract_identity_from_json(payload)
                        if found and self._identity_payload is None:
                            self._identity_payload = {
                                "ok": True,
                                "user_id": found[0],
                                "display_name": found[1],
                            }
                except Exception:  # noqa: BLE001
                    pass

            self.page.on("response", _on_response)
            try:
                await self.page.goto(
                    CREATOR_MAIN,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                for _ in range(8):
                    if self._identity_payload is not None:
                        break
                    await asyncio.sleep(1)
            finally:
                try:
                    self.page.remove_listener("response", _on_response)
                except Exception:  # noqa: BLE001
                    pass
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书身份捕获时页面已关闭"
                ) from exc
            logger.warning("小红书身份捕获失败: {}", exc)

        # DOM 兜底：侧栏 user-info 昵称 + 「小红书账号:」行
        if not (
            isinstance(self._identity_payload, dict) and self._identity_payload.get("ok")
        ):
            try:
                dom = await self.page.evaluate(
                    """() => {
                        const body = document.body.innerText || '';
                        const idMatch = body.match(/小红书账号[:：]\\s*(\\d{5,})/);
                        const userInfo = document.querySelector('[class*="user-info"]');
                        const nickname = userInfo
                            ? (userInfo.innerText || '').trim()
                            : '';
                        return {
                            user_id: idMatch ? idMatch[1] : '',
                            display_name: nickname,
                        };
                    }"""
                )
                if (
                    isinstance(dom, dict)
                    and dom.get("user_id")
                    and dom.get("display_name")
                ):
                    self._identity_payload = {
                        "ok": True,
                        "user_id": str(dom["user_id"]),
                        "display_name": str(dom["display_name"]),
                    }
            except Exception:  # noqa: BLE001
                pass

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        return {"ok": False, "user_id": "", "display_name": ""}

    @staticmethod
    def _extract_identity_from_json(payload) -> tuple[str, str] | None:
        """递归扫描响应 JSON，寻找同时含昵称与 ID 的最小身份对象。"""

        if not isinstance(payload, dict):
            return None
        found: list[tuple[str, str]] = []

        def walk(node) -> None:
            if isinstance(node, dict):
                name = next(
                    (str(node[key]) for key in IDENTITY_NAME_KEYS if node.get(key)),
                    "",
                )
                uid = next(
                    (str(node[key]) for key in IDENTITY_UID_KEYS if node.get(key)),
                    "",
                )
                if name and uid:
                    found.append((uid, name))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(payload)
        return found[0] if found else None

    # ==================== 投递链路（尚未接入） ====================

    @staticmethod
    def _not_implemented(operation: str):
        raise PlatformNotImplementedError(
            f"PLATFORM_NOT_IMPLEMENTED: 小红书{operation}能力尚未接入"
        )

    async def navigate_to_editor(self):
        """打开小红书长文编辑器：发布页 → 写长文 → 新的创作。

        真实结构（2026-08 探测）：标题 ``textarea.d-text``（0/64），
        正文 ``div.tiptap.ProseMirror``（TipTap contenteditable）。
        进入编辑器前先记录发布页侧栏的草稿箱计数，供 save_draft 验证。
        """
        self._require_page_alive("小红书打开编辑器")
        try:
            await self.page.goto(
                "https://creator.xiaohongshu.com/publish/publish",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            self._draft_box_count_before = await self._draft_box_count()
            await self._click_sidebar_text("写长文")
            await self._click_sidebar_text("新的创作")
            await self.page.wait_for_selector(
                "textarea.d-text",
                state="visible",
                timeout=20000,
            )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书打开编辑器时页面已关闭"
                ) from exc
            raise SelectorError("小红书长文编辑器未找到标题输入框") from exc

    async def _draft_box_count(self) -> int | None:
        """读取发布页侧栏「草稿箱(N)」计数。

        页面先渲染占位「草稿箱(0)」，真实计数稍后到达；因此要求两次
        连续读数一致才算稳定，避免把占位 0 当基准。读不到返回 None。
        """

        last: int | None = None
        for _ in range(6):
            try:
                value = await self.page.evaluate(
                    """() => {
                        const nodes = Array.from(document.querySelectorAll('*'));
                        const el = nodes.find((n) => {
                            const t = (n.innerText || '').trim();
                            return t.startsWith('草稿箱') && t.length < 20;
                        });
                        if (!el) return null;
                        const m = (el.innerText || '').match(/草稿箱\\s*\\((\\d+)\\)/);
                        return m ? parseInt(m[1], 10) : null;
                    }"""
                )
                if isinstance(value, int):
                    if last is not None and value == last:
                        return value
                    last = value
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(3)
        return last

    async def _click_sidebar_text(self, text: str) -> None:
        """点击侧栏中文本精确匹配的元素（写长文/新的创作）。

        先等待目标文本渲染完成（SPA 侧栏异步加载），再点击。
        """

        try:
            await self.page.wait_for_function(
                """(t) => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    return nodes.some((el) => {
                        const txt = (el.innerText || '').trim();
                        return txt === t && el.children.length <= 3;
                    });
                }""",
                arg=text,
                timeout=15000,
            )
        except Exception:
            pass
        clicked = await self.page.evaluate(
            """(t) => {
                const nodes = Array.from(document.querySelectorAll('*'));
                const target = nodes.find((el) => {
                    const txt = (el.innerText || '').trim();
                    return txt === t && el.children.length <= 3;
                });
                if (target) { target.click(); return true; }
                return false;
            }""",
            text,
        )
        if not clicked:
            raise SelectorError(f"小红书侧栏未找到「{text}」入口")
        await self.simulator.random_delay(1, 2)

    async def fill_title(self, title: str):
        """填写小红书长文标题（textarea.d-text，placeholder「输入标题」）。"""

        self._require_page_alive("小红书填写标题")
        title_field = self.page.locator("textarea.d-text").first
        try:
            if await title_field.count() == 0 or not await title_field.is_visible():
                raise RuntimeError("标题输入框不可见")
            await title_field.click()
            await title_field.fill(str(title or "").strip())
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书填写标题时页面已关闭"
                ) from exc
            logger.error("小红书标题填写失败: {}", exc)
            raise SelectorError("小红书标题输入框未找到或填写失败") from exc

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文：键盘逐段写入 TipTap 编辑器，回读并有序校验。"""

        self._require_page_alive("小红书填写正文")
        editor = self.page.locator("div.tiptap.ProseMirror").first
        try:
            if await editor.count() == 0 or not await editor.is_visible():
                raise RuntimeError("正文编辑器不可见")
            await editor.click()
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("小红书正文编辑器未找到") from exc

        try:
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
        except Exception:
            pass
        await self.simulator.random_delay(0.3, 0.8)

        first_text = True
        for block in content_blocks:
            btype = block.get("type")
            if btype in ("text", "heading") and block.get("text"):
                text = str(block["text"]).strip()
                if not text:
                    continue
                if not first_text:
                    await self.page.keyboard.press("Enter")
                    # 分段间留足节奏，降低风控敏感度
                    await self.simulator.random_delay(0.8, 1.5)
                lines = text.splitlines() or [text]
                for i, line in enumerate(lines):
                    if line.strip():
                        await self.page.keyboard.insert_text(line.strip())
                        await self.simulator.random_delay(0.1, 0.3)
                    if i < len(lines) - 1:
                        await self.page.keyboard.press("Enter")
                        await self.simulator.random_delay(0.8, 1.5)
                first_text = False

        actual_text = await editor.inner_text()
        expected_count = ensure_valid_content(
            content_blocks,
            actual_text,
            platform="小红书",
            phase="输入后",
        )
        logger.info("小红书正文文字输入并验证成功: {} 个文本段落", expected_count)

        expected_images = sum(
            1 for block in content_blocks if block.get("type") == "image"
        )
        uploaded_images = 0
        failed_images = []
        for block in content_blocks:
            if block.get("type") == "image":
                img_path = block.get("local_path")
                if not img_path and images:
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
                        failed_images.append(
                            {
                                "filename": Path(str(img_path)).name,
                                "error": safe_media_error(
                                    upload_result.get("error"),
                                    fallback="图片上传失败",
                                ),
                            }
                        )
                    # 每张图片之间放慢节奏，避免触发平台风控
                    await self.simulator.random_delay(2.5, 4.5)
                else:
                    failed_images.append(
                        {"filename": "", "error": "文章图片块没有对应本地文件"}
                    )

        actual_text = await editor.inner_text()
        ensure_valid_content(
            content_blocks,
            actual_text,
            platform="小红书",
            phase="图片处理后",
        )
        logger.info("小红书正文输入并最终验证成功: {} 个文本段落", expected_count)

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
                "小红书图片处理结果: expected={}, uploaded={}, failed={}",
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

    async def _upload_image(self, image_path: str) -> dict:
        """通过长文编辑器的文件控件上传图片；以编辑器内图片数量增加为成功判据。

        2026-08-17 用户反馈：小红书图片上传应可用，失败疑似自动化节奏
        过快触发风控。优先选择 accept 含 image 的控件（避免封面/其他
        上传控件），上传后等待放宽到 15 秒轮询，节奏放缓。
        """

        self._require_page_alive("小红书上传图片")
        try:
            file_inputs = self.page.locator("input[type=file]")
            count = await file_inputs.count()
            if count == 0:
                return {"success": False, "error": "小红书图片上传控件未找到"}
            target_input = None
            for i in range(count):
                accept = (await file_inputs.nth(i).get_attribute("accept")) or ""
                if "image" in accept.lower():
                    target_input = file_inputs.nth(i)
                    break
            if target_input is None:
                target_input = file_inputs.first
            before = await self.page.evaluate(
                """() => document.querySelectorAll(
                    '.tiptap img, .ProseMirror img'
                ).length"""
            )
            await target_input.set_input_files(str(image_path), timeout=20000)
            after = before
            for _ in range(15):
                await asyncio.sleep(1)
                after = await self.page.evaluate(
                    """() => document.querySelectorAll(
                        '.tiptap img, .ProseMirror img'
                    ).length"""
                )
                if after > before:
                    break
            if after <= before:
                return {"success": False, "error": "上传后编辑器图片数量未增加"}
            return {"success": True, "error": ""}
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书上传图片时页面已关闭"
                ) from exc
            return {"success": False, "error": str(exc)}

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """小红书保存草稿不需要话题；公开话题选择尚未接入，如实报告。"""

        if not (topic or community or selection_query):
            return {
                "success": True,
                "selection_status": "not_required",
                "selection": {},
            }
        return {
            "success": False,
            "needs_selection": True,
            "error_code": "TOPIC_SELECTION_NOT_IMPLEMENTED",
            "error": "小红书话题选择尚未接入（保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """点击「暂存离开」保存草稿，以「草稿箱计数 +1 + 保存接口 2xx」验证。

        2026-08 实测：小红书 Web 端草稿的唯一可见入口是发布页侧栏的
        「草稿箱(N)」计数（写长文子视图/笔记管理均无草稿卡片列表），
        因此验证语义 = 保存前计数 N → 点击暂存离开（捕获全部 POST/PUT，
        任一 2xx 视为保存信号）→ 重新加载后计数 N+1。两者都满足才返回
        发布页 URL；否则如实返回空串，绝不以当前页 URL 冒充成功。
        """
        self._require_page_alive("小红书保存草稿")
        before = getattr(self, "_draft_box_count_before", None)
        if not isinstance(before, int):
            # 未在发布页记录基准计数（如直接进入编辑器），读不到则如实失败
            before = await self._draft_box_count()
        captured: dict = {}

        async def _on_response(response) -> None:
            try:
                if response.request.method in ("POST", "PUT", "PATCH"):
                    captured["status"] = response.status
                    if "draft" in response.url.lower():
                        captured["url"] = response.url[:160]
                        try:
                            body = await response.json()
                            if isinstance(body, dict):
                                captured["ok"] = (
                                    body.get("code") in (0, None)
                                    or body.get("success") is True
                                )
                        except Exception:
                            captured["ok"] = response.status < 300
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", _on_response)
            await self.page.evaluate(
                """() => {
                    const nodes = Array.from(
                        document.querySelectorAll('button, [role=button]')
                    );
                    const target = nodes.find((el) =>
                        (el.innerText || '').replace(/\\s+/g, '').includes('暂存离开'));
                    if (target) target.click();
                }"""
            )
            # 风控敏感：点击后放慢节奏，给平台保存请求留足时间
            await self.simulator.random_delay(5, 8)
            try:
                confirm = self.page.locator(
                    "button:has-text('确定'), button:has-text('暂存')"
                ).first
                if await confirm.count() > 0:
                    await confirm.click(timeout=3000)
                    await self.simulator.random_delay(4, 6)
            except Exception:
                pass
            for _ in range(15):
                if captured.get("status"):
                    break
                await asyncio.sleep(1)
            await self.simulator.random_delay(2, 3)
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if not captured.get("status"):
            logger.error("小红书暂存离开未产生任何保存请求")
            return ""
        if not captured.get("ok", captured.get("status", 0) < 300):
            logger.error("小红书草稿 API 未确认成功: {}", captured)
            return ""

        # 重新加载发布页，确认草稿箱计数 +1
        try:
            await self.page.goto(
                "https://creator.xiaohongshu.com/publish/publish",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            after = None
            for _ in range(3):
                await self.simulator.random_delay(5, 8)
                after = await self._draft_box_count()
                if after is not None and before is not None and after == before + 1:
                    break
            if after is None or before is None:
                logger.error("小红书草稿箱计数读取失败: before={}, after={}", before, after)
                return ""
            if after != before + 1:
                logger.error(
                    "小红书草稿箱计数未增加: before={}, after={}", before, after
                )
                return ""
            logger.info(
                "小红书草稿验证成功: 草稿箱计数 {} -> {}，保存接口 {}",
                before,
                after,
                captured.get("status"),
            )
            return "https://creator.xiaohongshu.com/publish/publish"
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书验证草稿时页面已关闭"
                ) from exc
            logger.error("小红书草稿验证失败: {}", exc)
            return ""

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
