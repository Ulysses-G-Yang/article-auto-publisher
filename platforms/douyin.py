"""抖音创作者中心账号会话与草稿投递适配器。

登录态与身份验证链路已接入（真实扫码 → 会话 cookie → 身份捕获），
草稿投递链路（2026-08 真实编辑器结构探测后实现）：
    navigate_to_editor → fill_title → fill_content → save_draft

真实发布页（https://creator.douyin.com/creator-micro/content/publish?type=article）：
- 标题：``input.semi-input``，placeholder「填写作品标题，为作品获得更多流量」。
- 正文：``div.zone-container.editor-kit-container``（contenteditable，作品描述 0/1000）。

2026-08 真实验收结论：抖音创作者中心 Web 端**没有草稿机制**——无草稿箱、
管理页无草稿 API、「暂存离开」点击后不产生任何保存请求且内容直接丢失。
因此抖音**不满足「真实草稿验收」前置条件，投递保持关闭**；save_draft
如实失败（返回空串），绝不以假成功放行。将来若需投递抖音，只能走公开
发布验收（需另行决策并显式开启公开发布开关）。

登录页（https://creator.douyin.com/）：
- 「扫码登录」为默认 Tab，二维码为约 180x180 的 base64 PNG。
- 登录成功信号：.douyin.com 出现 sessionid / sessionid_ss / sid_tt cookie。
- 创作者首页身份接口带 a_bogus/msToken 签名，裸 fetch 会被拒；
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

CREATOR_HOME = "https://creator.douyin.com/"
CREATOR_MICRO_HOME = "https://creator.douyin.com/creator-micro/home"
PUBLISH_URL = "https://creator.douyin.com/creator-micro/content/publish?type=article"
TITLE_SELECTOR = "input.semi-input[placeholder^='填写作品标题']"
BODY_SELECTOR = "div.zone-container.editor-kit-container[contenteditable='true']"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name"}
IDENTITY_UID_KEYS = {"sec_uid", "uid", "user_id"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class DouyinPlatform(BasePlatform):
    """抖音创作者中心账号会话适配器；内容投递能力保持关闭。"""

    platform_name = "douyin"
    SESSION_COOKIE_NAMES = frozenset({"sessionid", "sessionid_ss", "sid_tt"})
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
                "https://creator.douyin.com/",
                "https://www.douyin.com/",
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
            self._require_page_alive("抖音登录态检测")
            await self.actions.perform(
                self.page.goto,
                CREATOR_HOME,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            # 等待 SPA 完成 hydration、cookie 注入页面上下文
            await asyncio.sleep(4)
            if await self._has_session_cookie_signal():
                return True
            self.last_login_error = await self.login_obstacle_code()
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 抖音登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "DOUYIN_LOGIN_CHECK_ERROR: 抖音登录态验证失败"
            return False

    async def login(self):
        """打开抖音创作者中心登录页，等待用户在隔离 Profile 中扫码登录。"""

        self._require_page_alive("抖音打开登录页")
        await self.actions.perform(
            self.page.goto,
            CREATOR_HOME,
            wait_until="domcontentloaded",
            timeout=15000,
        )
        # 确保「扫码登录」Tab 激活（默认即扫码登录，点击幂等）
        try:
            await self.actions.perform(self.page.evaluate, """() => {
                    const nodes = Array.from(document.querySelectorAll('span, div'));
                    const tab = nodes.find(
                        (el) => (el.innerText || '').trim() === '扫码登录'
                    );
                    if (tab) tab.click();
                }""")
        except Exception:
            pass
        # 等待二维码出现：约 180x180 的 base64 PNG
        try:
            await self.page.wait_for_function(
                """() => {
                    const imgs = Array.from(
                        document.querySelectorAll('img[src^="data:image/png;base64"]')
                    );
                    return imgs.some(
                        (i) => i.naturalWidth > 140 && i.naturalWidth < 260
                    );
                }""",
                timeout=15000,
            )
        except Exception:
            pass
        await self._show_scan_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("抖音等待登录")
            if await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: 抖音登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_scan_hint(self):
        """页面顶部显示扫码提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'dy-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#fe2c55;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请用抖音 App 扫码登录'
                        + '<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """捕获创作者首页自身的身份响应，返回最小平台身份。

        缺失时如实返回未确认，绝不伪造。
        """

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        "creator.douyin.com" in response.url
                        and response.request.resource_type == "xhr"
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
                await self.actions.perform(
                    self.page.goto,
                    CREATOR_MICRO_HOME,
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
                    "BROWSER_CONTEXT_CLOSED: 抖音身份捕获时页面已关闭"
                ) from exc
            logger.warning("抖音身份捕获失败: {}", exc)

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
            f"PLATFORM_NOT_IMPLEMENTED: 抖音{operation}能力尚未接入"
        )

    async def navigate_to_editor(self):
        """打开抖音文章发布页（type=article），等待标题输入框出现。"""

        self._require_page_alive("抖音打开编辑器")
        try:
            await self.actions.perform(
                self.page.goto,
                PUBLISH_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.page.wait_for_selector(
                TITLE_SELECTOR,
                state="visible",
                timeout=20000,
            )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 抖音打开编辑器时页面已关闭"
                ) from exc
            raise SelectorError("抖音编辑器未找到标题输入框") from exc

    async def fill_title(self, title: str):
        """填写抖音作品标题（semi-input，placeholder 以「填写作品标题」开头）。"""

        self._require_page_alive("抖音填写标题")
        title_field = self.page.locator(TITLE_SELECTOR).first
        try:
            if await title_field.count() == 0 or not await title_field.is_visible():
                raise RuntimeError("标题输入框不可见")
            await self.actions.perform(title_field.click)
            await self.actions.fill(title_field, str(title or "").strip())
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 抖音填写标题时页面已关闭"
                ) from exc
            logger.error("抖音标题填写失败: {}", exc)
            raise SelectorError("抖音标题输入框未找到或填写失败") from exc

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文：键盘逐段写入作品描述编辑器，回读并有序校验。

        标题/描述计数器（0/30、0/1000）由页面自行管理；正文完整性以
        content_validation 有序段落校验为准，绝不伪造。
        """
        self._require_page_alive("抖音填写正文")
        editor = self.page.locator(BODY_SELECTOR).first
        try:
            if await editor.count() == 0 or not await editor.is_visible():
                raise RuntimeError("正文编辑器不可见")
            await self.actions.perform(editor.click)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 抖音定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("抖音正文编辑器未找到") from exc

        try:
            await self.actions.perform(self.page.keyboard.press, "Control+A")
            await self.actions.perform(self.page.keyboard.press, "Backspace")
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
                    await self.actions.perform(self.page.keyboard.press, "Enter")
                lines = text.splitlines() or [text]
                for i, line in enumerate(lines):
                    if line.strip():
                        await self.actions.insert_text(self.page.keyboard, line.strip())
                    if i < len(lines) - 1:
                        await self.actions.perform(self.page.keyboard.press, "Enter")
                first_text = False

        actual_text = await editor.inner_text()
        expected_count = ensure_valid_content(
            content_blocks,
            actual_text,
            platform="抖音",
            phase="输入后",
        )
        logger.info("抖音正文文字输入并验证成功: {} 个文本段落", expected_count)

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
            platform="抖音",
            phase="图片处理后",
        )
        logger.info("抖音正文输入并最终验证成功: {} 个文本段落", expected_count)

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
                "抖音图片处理结果: expected={}, uploaded={}, failed={}",
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
        """通过作品编辑器的文件控件上传图片；以编辑器内图片数量增加为成功判据。"""

        self._require_page_alive("抖音上传图片")
        try:
            file_inputs = self.page.locator(
                "input[type=file]:not([accept*='video'])"
            )
            if await file_inputs.count() == 0:
                return {"success": False, "error": "抖音图片上传控件未找到"}
            before = await self.page.evaluate(
                """() => document.querySelectorAll(
                    '.zone-container img, .editor-kit-container img'
                ).length"""
            )
            await self.actions.perform(
                file_inputs.first.set_input_files,
                str(image_path),
                timeout=15000,
            )
            after = before
            for _ in range(10):
                await asyncio.sleep(1)
                after = await self.page.evaluate(
                    """() => document.querySelectorAll(
                        '.zone-container img, .editor-kit-container img'
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
                    "BROWSER_CONTEXT_CLOSED: 抖音上传图片时页面已关闭"
                ) from exc
            return {"success": False, "error": str(exc)}

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """抖音保存草稿不需要话题；公开话题选择尚未接入，如实报告。"""

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
            "error": "抖音话题选择尚未接入（保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """点击「暂存离开」保存草稿，捕获草稿 API 响应，并在内容管理页按标题验证。

        返回内容管理页 URL；验证失败返回空串（绝不以当前页 URL 冒充成功）。
        """
        self._require_page_alive("抖音保存草稿")
        captured: dict = {}

        async def _on_response(response) -> None:
            try:
                if "draft" in response.url and response.request.method in {
                    "POST",
                    "PUT",
                }:
                    captured["status"] = response.status
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            captured["ok"] = (
                                body.get("status_code") in (0, None)
                                or body.get("status") in (0, "ok", "success")
                            )
                    except Exception:
                        captured["ok"] = response.status < 300
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", _on_response)
            await self.actions.perform(self.page.evaluate, """() => {
                    const nodes = Array.from(document.querySelectorAll('button, [role=button]'));
                    const target = nodes.find((el) =>
                        (el.innerText || '').replace(/\\s+/g, '').includes('暂存离开'));
                    if (target) target.click();
                }""")
            await self.simulator.random_delay(2, 4)
            # 可能的确认弹窗
            try:
                confirm = self.page.locator(
                    "button:has-text('确定'), button:has-text('暂存')"
                ).first
                if await confirm.count() > 0:
                    await self.actions.perform(confirm.click, timeout=3000)
                    await self.simulator.random_delay(2, 4)
            except Exception:
                pass
            # 等待草稿 API 响应
            for _ in range(10):
                if captured.get("status"):
                    break
                await asyncio.sleep(1)
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if not captured.get("ok"):
            logger.error("抖音草稿 API 未确认成功: {}", captured)
            return ""
        # 去内容管理页验证标题
        try:
            await self.actions.perform(
                self.page.goto,
                "https://creator.douyin.com/creator-micro/content/manage",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.simulator.random_delay(3, 5)
            keyword = str(title or "").strip()[:12]
            found = False
            if keyword:
                found = bool(
                    await self.page.evaluate(
                        "(kw) => (document.body.innerText || '').includes(kw)",
                        keyword,
                    )
                )
                if not found:
                    await self.simulator.random_delay(3, 5)
                    found = bool(
                        await self.page.evaluate(
                            "(kw) => (document.body.innerText || '').includes(kw)",
                            keyword,
                        )
                    )
            if keyword and not found:
                logger.error("抖音内容管理页未找到标题包含「{}」的草稿", keyword)
                return ""
            return "https://creator.douyin.com/creator-micro/content/manage"
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 抖音验证草稿时页面已关闭"
                ) from exc
            logger.error("抖音草稿验证失败: {}", exc)
            return ""

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
