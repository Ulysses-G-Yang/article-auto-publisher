"""微博账号会话适配器。

登录态与身份验证链路（真实扫码 → 跳转回首页 → 身份捕获/DOM 确认），
内容投递能力尚未接入：所有投递方法显式拒绝。

真实登录页（https://passport.weibo.com/sso/signin?entry=miniblog...）：
- 「扫描二维码登录」为默认 Tab，二维码为约 140x140 的 img（v2.qr.weibo.cn）。
- 登录成功信号：扫码确认后页面从 passport 跳转回 weibo.com。
- 身份接口带签名/cookie 约束，裸 fetch 不可靠；采用「捕获页面自身响应」
  模式 + 首页 DOM 兜底，与小黑盒/小红书一致。
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

LOGIN_URL = (
    "https://passport.weibo.com/sso/signin?entry=miniblog"
    "&source=miniblog&disp=popup&url=https%3A%2F%2Fweibo.com%2F"
)
HOME_URL = "https://weibo.com/"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name", "nick"}
IDENTITY_UID_KEYS = {"uid", "user_id"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class WeiboPlatform(BasePlatform):
    """微博账号会话适配器；内容投递能力保持关闭。"""

    platform_name = "weibo"
    # 游客也有 SUB/SUBP/WBPSESS；SCF/ALF/SSOLoginState 仅登录后存在。
    SESSION_COOKIE_NAMES = frozenset({"SCF", "ALF", "SSOLoginState"})
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
        """登录会话 cookie（SCF/ALF/SSOLoginState）作为登录成功信号。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies([
                "https://weibo.com/",
                "https://www.weibo.com/",
                "https://weibo.cn/",
            ])
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def _on_home(self) -> bool:
        """是否已离开 passport 并处于微博首页（登录成功信号）。"""

        try:
            url = self.page.url or ""
            return "passport.weibo.com" not in url and "weibo.com" in url
        except Exception:
            return False

    async def check_login(self) -> bool:
        """只读验证现有 Profile；首页身份确认成功才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("微博登录态检测")
            await self.page.goto(
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(4)
            if await self._has_session_cookie_signal():
                identity = await self.fetch_identity_payload()
                if identity.get("ok"):
                    return True
                self.last_login_error = (
                    "WEIBO_IDENTITY_MISSING: 微博会话存在但身份未确认"
                )
                return False
            self.last_login_error = "LOGIN_REQUIRED: 微博账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "WEIBO_LOGIN_CHECK_ERROR: 微博登录态验证失败"
            return False

    async def login(self):
        """打开微博扫码登录页，等待用户在隔离 Profile 中扫码登录。"""

        self._require_page_alive("微博打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        try:
            await self.page.wait_for_function(
                """() => {
                    const imgs = Array.from(
                        document.querySelectorAll('img')
                    );
                    return imgs.some((i) => {
                        const src = i.src || '';
                        return src.includes('qr.weibo.cn')
                            && i.naturalWidth > 100 && i.naturalWidth < 260;
                    });
                }""",
                timeout=20000,
            )
        except Exception:
            pass
        await self._show_scan_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("微博等待登录")
            if await self._on_home() or await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: 微博登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_scan_hint(self):
        """页面顶部显示扫码提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'wb-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#ff8200;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请用微博 App 扫码登录'
                        + '<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """返回微博首页同源确认的最小平台身份（捕获 + DOM 兜底）。"""

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        response.request.resource_type in ("xhr", "fetch")
                        and "weibo.com" in response.url
                        and any(
                            key in response.url.lower()
                            for key in ("user", "logininfo", "profile", "account")
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
                    HOME_URL,
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
                    "BROWSER_CONTEXT_CLOSED: 微博身份捕获时页面已关闭"
                ) from exc
            logger.warning("微博身份捕获失败: {}", exc)

        # DOM 兜底：头像链接 /u/<uid> + 顶部用户区昵称（登录后首页）
        if not (
            isinstance(self._identity_payload, dict) and self._identity_payload.get("ok")
        ):
            try:
                dom = await self.page.evaluate(
                    """() => {
                        const uidLink = document.querySelector('a[href*="/u/"]');
                        const uidMatch = uidLink
                            ? (uidLink.getAttribute('href') || '').match(/\\/u\\/(\\d+)/)
                            : null;
                        const nickEl = document.querySelector('[class*="_nick_"]');
                        let nickname = nickEl
                            ? (nickEl.innerText || '').trim()
                            : '';
                        if (nickname.startsWith('@')) {
                            nickname = '';
                        }
                        return {
                            user_id: uidMatch ? uidMatch[1] : '',
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

    # ==================== 草稿投递链路 ====================

    @staticmethod
    def _not_implemented(operation: str):
        raise PlatformNotImplementedError(
            f"PLATFORM_NOT_IMPLEMENTED: 微博{operation}能力尚未接入"
        )

    async def navigate_to_editor(self):
        """打开微博头条文章编辑器：草稿箱视图 → 写文章。

        真实结构（2026-08 探测）：标题 ``textarea``「请输入标题」（0/32），
        正文 ``div.tiptap.ProseMirror``（TipTap），保存草稿按钮常驻，
        侧栏「草稿箱 (N/30)」计数可供验证。
        """
        self._require_page_alive("微博打开编辑器")
        try:
            await self.page.goto(
                "https://card.weibo.com/article/v5/editor#/draft",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            self._draft_box_count_before = await self._draft_box_count()
            await self._click_sidebar_text("写文章")
            await self.page.wait_for_selector(
                "textarea[placeholder='请输入标题']",
                state="visible",
                timeout=20000,
            )
            # 编辑器加载中的 spinner 遮罩会拦截点击，等它消失
            try:
                await self.page.wait_for_selector(
                    ".wb-editor-spin, .n-spin-body",
                    state="detached",
                    timeout=20000,
                )
            except Exception:
                pass
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博打开编辑器时页面已关闭"
                ) from exc
            raise SelectorError("微博头条文章编辑器未找到标题输入框") from exc

    async def _click_sidebar_text(self, text: str) -> None:
        """点击侧栏中文本精确匹配的元素（写文章），等待渲染后点击。"""

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
            raise SelectorError(f"微博侧栏未找到「{text}」入口")
        await self.simulator.random_delay(1, 2)

    async def _draft_box_count(self) -> int | None:
        """读取头条文章编辑器侧栏「草稿箱 (N/30)」计数。

        页面可能先渲染占位值，要求两次连续读数一致才算稳定。
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
                        const m = (el.innerText || '').match(/草稿箱\\s*\\(\\s*(\\d+)\\s*\\//);
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

    async def fill_title(self, title: str):
        """填写微博头条文章标题（textarea，placeholder「请输入标题」，0/32）。"""

        self._require_page_alive("微博填写标题")
        title_field = self.page.locator("textarea[placeholder='请输入标题']").first
        try:
            if await title_field.count() == 0 or not await title_field.is_visible():
                raise RuntimeError("标题输入框不可见")
            # 直接 fill（不依赖 click，避免加载遮罩拦截命中）
            await title_field.fill(str(title or "").strip())
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博填写标题时页面已关闭"
                ) from exc
            logger.error("微博标题填写失败: {}", exc)
            raise SelectorError("微博标题输入框未找到或填写失败") from exc

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文：键盘逐段写入 TipTap 编辑器，回读并有序校验。"""

        self._require_page_alive("微博填写正文")
        editor = self.page.locator("div.tiptap.ProseMirror").first
        try:
            if await editor.count() == 0 or not await editor.is_visible():
                raise RuntimeError("正文编辑器不可见")
            # 等待可能的加载遮罩消失后，用 evaluate 聚焦（不依赖点击命中测试）
            try:
                await self.page.wait_for_selector(
                    ".wb-editor-spin, .n-spin-body",
                    state="detached",
                    timeout=10000,
                )
            except Exception:
                pass
            await editor.evaluate("(el) => el.focus()")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("微博正文编辑器未找到") from exc

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
                lines = text.splitlines() or [text]
                for i, line in enumerate(lines):
                    if line.strip():
                        await self.page.keyboard.insert_text(line.strip())
                    if i < len(lines) - 1:
                        await self.page.keyboard.press("Enter")
                first_text = False

        actual_text = await editor.inner_text()
        expected_count = ensure_valid_content(
            content_blocks,
            actual_text,
            platform="微博",
            phase="输入后",
        )
        logger.info("微博正文文字输入并验证成功: {} 个文本段落", expected_count)

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
                    await self.simulator.random_delay(0.2, 0.5)
                else:
                    failed_images.append(
                        {"filename": "", "error": "文章图片块没有对应本地文件"}
                    )

        actual_text = await editor.inner_text()
        ensure_valid_content(
            content_blocks,
            actual_text,
            platform="微博",
            phase="图片处理后",
        )
        logger.info("微博正文输入并最终验证成功: {} 个文本段落", expected_count)

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
                "微博图片处理结果: expected={}, uploaded={}, failed={}",
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
        """通过头条文章编辑器的文件控件上传图片；以编辑器内图片数量增加为判据。"""

        self._require_page_alive("微博上传图片")
        try:
            file_inputs = self.page.locator("input[type=file]")
            if await file_inputs.count() == 0:
                return {"success": False, "error": "微博图片上传控件未找到"}
            before = await self.page.evaluate(
                """() => document.querySelectorAll(
                    '.tiptap img, .ProseMirror img'
                ).length"""
            )
            await file_inputs.first.set_input_files(str(image_path), timeout=15000)
            after = before
            for _ in range(10):
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
                    "BROWSER_CONTEXT_CLOSED: 微博上传图片时页面已关闭"
                ) from exc
            return {"success": False, "error": str(exc)}

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """微博保存草稿不需要话题；公开话题选择尚未接入，如实报告。"""

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
            "error": "微博话题选择尚未接入（保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """点击「保存草稿」，以「草稿箱计数 +1」验证。

        头条文章草稿箱为侧栏「草稿箱 (N/30)」计数（Web 端草稿卡片在
        草稿箱视图中，保存后回到草稿箱视图可核对计数与标题关键字）。
        """
        self._require_page_alive("微博保存草稿")
        before = getattr(self, "_draft_box_count_before", None)
        if not isinstance(before, int):
            before = await self._draft_box_count()
        captured: dict = {}

        async def _on_response(response) -> None:
            try:
                if response.request.method in ("POST", "PUT", "PATCH"):
                    captured["status"] = response.status
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
                        (el.innerText || '').replace(/\\s+/g, '').includes('保存草稿'));
                    if (target) target.click();
                }"""
            )
            await self.simulator.random_delay(2, 4)
            for _ in range(10):
                if captured.get("status"):
                    break
                await asyncio.sleep(1)
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if not captured.get("status"):
            logger.error("微博保存草稿未产生任何保存请求")
            return ""

        # 回到草稿箱视图验证计数 +1 与标题关键字
        try:
            await self.page.goto(
                "https://card.weibo.com/article/v5/editor#/draft",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            after = None
            for _ in range(3):
                await self.simulator.random_delay(3, 5)
                after = await self._draft_box_count()
                if after is not None and before is not None and after == before + 1:
                    break
            if after is None or before is None:
                logger.error("微博草稿箱计数读取失败: before={}, after={}", before, after)
                return ""
            if after != before + 1:
                logger.error("微博草稿箱计数未增加: before={}, after={}", before, after)
                return ""
            keyword = str(title or "").strip()[:12]
            if keyword:
                found = bool(
                    await self.page.evaluate(
                        "(kw) => (document.body.innerText || '').includes(kw)",
                        keyword,
                    )
                )
                if not found:
                    logger.warning("微博草稿箱页面未显示标题关键字（计数已验证）: {}", keyword)
            logger.info(
                "微博草稿验证成功: 草稿箱计数 {} -> {}，保存接口 {}",
                before,
                after,
                captured.get("status"),
            )
            return "https://card.weibo.com/article/v5/editor#/draft"
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博验证草稿时页面已关闭"
                ) from exc
            logger.error("微博草稿验证失败: {}", exc)
            return ""

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
