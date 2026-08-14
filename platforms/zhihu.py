"""知乎账号会话与草稿投递适配器。

登录态与身份验证已接入（真实扫码 → 同源身份 API 确认 → 身份入库）。

草稿投递链路（2026-08 真实编辑器结构探测后实现）：
    navigate_to_editor → fill_title → fill_content → save_draft

真实编辑器结构（https://zhuanlan.zhihu.com/write，Draft.js，无 iframe）：
- 标题：``textarea.Input``，placeholder「请输入标题（最多 100 个字）」。
- 正文：``div.notranslate.public-DraftEditor-content``（contenteditable）。
- 草稿自动保存（页面右下角「刚刚 · 草稿」提示），无独立保存按钮；
  保存判据 = 草稿箱列表出现对应标题，绝不拿当前页 URL 冒充成功。
- 正文图片：工具栏「图片」按钮 + ``input[type=file]:not(.UploadPicture-input)[accept*='image']``。

公开发布（publish_now）仍明确拒绝：真实发布验收完成并开放开关前，不允许知乎公开投递。
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

TITLE_SELECTOR = "textarea.Input[placeholder^='请输入标题']"
BODY_SELECTOR = "div.notranslate.public-DraftEditor-content"
EDITOR_URL = "https://zhuanlan.zhihu.com/write"
DRAFTS_URL = "https://www.zhihu.com/creator/manage/creation/drafts"
BODY_IMAGE_INPUT = (
    "input[type=file]:not(.UploadPicture-input)[accept*='image']"
)


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class ZhihuPlatform(BasePlatform):
    """知乎账号会话与草稿投递适配器；公开发布能力保持关闭。"""

    platform_name = "zhihu"
    SESSION_COOKIE_NAMES = frozenset({"z_c0", "d_c0", "q_c1"})
    LOGIN_POLL_ATTEMPTS = 60
    LOGIN_POLL_INTERVAL_SECONDS = 2

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """通过同源身份 API 返回最小、脱敏后的账号身份。"""

        self._require_page_alive("知乎身份 API 验证")
        payload = await self.page.evaluate(
            """async (identityPath) => {
                try {
                    const response = await fetch(identityPath, {
                        credentials: 'include',
                        headers: { Accept: 'application/json' },
                    });
                    const body = await response.json().catch(() => ({}));
                    const data = body && body.data ? body.data : body;
                    const id = data && data.id ? String(data.id) : '';
                    const urlToken = data && data.url_token
                        ? String(data.url_token)
                        : '';
                    const displayName = data && data.name
                        ? String(data.name)
                        : '';
                    return {
                        ok: Boolean(response.ok && (id || urlToken) && displayName),
                        status: response.status,
                        user_id: id || urlToken,
                        display_name: displayName,
                    };
                } catch (_) {
                    return {
                        ok: false,
                        status: 0,
                        user_id: '',
                        display_name: '',
                    };
                }
            }""",
            self.platform_cfg.get("identity_api_path", "/api/v4/me"),
        )
        if not isinstance(payload, dict):
            payload = {}
        sanitized: dict[str, str | int | bool] = {
            "ok": bool(payload.get("ok")),
            "status": int(payload.get("status") or 0),
            "user_id": _text(payload.get("user_id")),
            "display_name": _text(payload.get("display_name")),
        }
        sanitized["ok"] = bool(
            sanitized["ok"]
            and sanitized["user_id"]
            and sanitized["display_name"]
        )
        self._identity_payload = sanitized if sanitized["ok"] else None
        return sanitized

    async def _has_session_cookie_signal(self) -> bool:
        """Cookie 只作为弱信号；不返回、不记录 Cookie 名和值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies(["https://www.zhihu.com/"])
        except TypeError:
            cookies = await self.context.cookies()
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def check_login(self) -> bool:
        """只读验证现有 Profile；身份 API 成功才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("知乎登录态检测")
            await self.page.goto(
                self.platform_cfg.get("home_url", "https://www.zhihu.com/"),
                wait_until="domcontentloaded",
                timeout=15000,
            )
            identity = await self.fetch_identity_payload()
            if identity["ok"]:
                return True
            has_weak_signal = await self._has_session_cookie_signal()
            self.last_login_error = (
                "ZHIHU_SESSION_INVALID: 知乎身份 API 未确认当前会话"
                if has_weak_signal
                else "LOGIN_REQUIRED: 知乎账号需要登录"
            )
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "ZHIHU_LOGIN_CHECK_ERROR: 知乎登录态验证失败"
            return False

    async def login(self):
        """打开知乎官方登录页，等待用户在隔离 Profile 中交互登录。"""

        self._require_page_alive("知乎打开登录页")
        await self.page.goto(
            self.platform_cfg.get("login_url", "https://www.zhihu.com/signin"),
            wait_until="domcontentloaded",
            timeout=15000,
        )
        for attempt in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("知乎等待登录")
            identity = await self.fetch_identity_payload()
            if identity["ok"]:
                self.last_login_error = ""
                return
            if attempt + 1 < self.LOGIN_POLL_ATTEMPTS:
                await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: 知乎登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    # ==================== 草稿投递链路 ====================

    async def navigate_to_editor(self):
        """打开知乎写文章编辑器并等待标题输入框出现。

        优先使用配置里的编辑器地址（可能被重定向），超时后回退到
        zhuanlan.zhihu.com/write。
        """
        self._require_page_alive("知乎打开编辑器")
        configured = self.platform_cfg.get("editor_url") or EDITOR_URL
        urls = [configured] if configured == EDITOR_URL else [configured, EDITOR_URL]
        last_error: Exception | None = None
        for url in urls:
            try:
                await self.page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await self.page.wait_for_selector(
                    TITLE_SELECTOR,
                    state="visible",
                    timeout=20000,
                )
                return
            except BrowserLifecycleError:
                raise
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 知乎打开编辑器时页面已关闭"
                    ) from exc
                last_error = exc
        raise SelectorError(
            f"知乎编辑器未找到标题输入框（{last_error}）"
        )

    async def fill_title(self, title: str):
        """填写知乎文章标题（textarea.Input，placeholder 以「请输入标题」开头）。"""

        self._require_page_alive("知乎填写标题")
        title_field = self.page.locator(TITLE_SELECTOR).first
        try:
            if await title_field.count() == 0 or not await title_field.is_visible():
                raise RuntimeError("标题输入框不可见")
            await title_field.click()
            await title_field.fill(str(title or "").strip())
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎填写标题时页面已关闭"
                ) from exc
            logger.error("知乎标题填写失败: {}", exc)
            raise SelectorError("知乎标题输入框未找到或填写失败") from exc

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文：键盘逐段写入 Draft.js 编辑器，回读并有序校验。

        heading 块按普通段落写入（知乎标题样式切换后续细化），正文完整性
        以 content_validation 有序段落校验为准，绝不伪造。
        """
        self._require_page_alive("知乎填写正文")
        editor = self.page.locator(BODY_SELECTOR).first
        try:
            if await editor.count() == 0 or not await editor.is_visible():
                raise RuntimeError("正文编辑器不可见")
            await editor.click()
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("知乎正文编辑器未找到") from exc

        # 清空编辑器：全选 + 退格（新建页通常为空，幂等处理）
        try:
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
        except Exception:
            pass
        await self.simulator.random_delay(0.3, 0.8)

        # 先完整写入文字：block 间按 Enter 分段，块内行按 Enter 换行
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
            platform="知乎",
            phase="输入后",
        )
        logger.info("知乎正文文字输入并验证成功: {} 个文本段落", expected_count)

        expected_images = sum(
            1 for block in content_blocks if block.get("type") == "image"
        )
        uploaded_images = 0
        failed_images = []

        # 文字验证通过后再上传图片；图片失败不抹掉正文，如实返回结构化结果
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

        # 图片操作可能触发编辑器重渲染，最终再校验一次正文
        actual_text = await editor.inner_text()
        ensure_valid_content(
            content_blocks,
            actual_text,
            platform="知乎",
            phase="图片处理后",
        )
        logger.info("知乎正文输入并最终验证成功: {} 个文本段落", expected_count)

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
                "知乎图片处理结果: expected={}, uploaded={}, failed={}",
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
        """通过正文图片文件控件上传单张图片；以编辑器内图片数量增加为成功判据。"""

        self._require_page_alive("知乎上传图片")
        try:
            file_input = self.page.locator(BODY_IMAGE_INPUT).first
            if await file_input.count() == 0:
                return {"success": False, "error": "知乎正文图片上传控件未找到"}
            opened = await self.page.evaluate(
                """() => {
                    const btns = Array.from(
                        document.querySelectorAll('button, [role=button]')
                    );
                    const target = btns.find((b) =>
                        (b.innerText || '')
                            .replace(/[\\u200b\\u200c\\n\\s]/g, '') === '图片'
                    );
                    if (target) { target.click(); return true; }
                    return false;
                }"""
            )
            await self.simulator.random_delay(0.3, 0.8)
            if not opened:
                return {"success": False, "error": "知乎工具栏图片按钮未找到"}

            before = await self.page.evaluate(
                """(sel) => {
                    const root = document.querySelector(sel);
                    return root ? root.querySelectorAll('img').length : 0;
                }""",
                BODY_SELECTOR,
            )
            await file_input.set_input_files(str(image_path), timeout=15000)
            after = before
            for _ in range(10):
                await asyncio.sleep(1)
                after = await self.page.evaluate(
                    """(sel) => {
                        const root = document.querySelector(sel);
                        return root ? root.querySelectorAll('img').length : 0;
                    }""",
                    BODY_SELECTOR,
                )
                if after > before:
                    break
            if after <= before:
                return {
                    "success": False,
                    "error": "上传后编辑器图片数量未增加",
                }
            return {"success": True, "error": ""}
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎上传图片时页面已关闭"
                ) from exc
            return {"success": False, "error": str(exc)}

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """知乎保存草稿不需要话题；公开发布的话题选择尚未接入，如实报告。"""

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
            "error": "知乎话题选择尚未接入（保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """知乎 /write 自动保存草稿；等自动保存稳定后去草稿箱按标题验证。

        返回草稿箱 URL；标题未在草稿箱出现则返回空串（绝不以当前页 URL 冒充成功）。
        """
        self._require_page_alive("知乎保存草稿")
        # 输入停止后知乎通常在数秒内自动保存；等待状态提示消失
        await self.simulator.random_delay(3, 5)
        try:
            await self.page.wait_for_selector(
                ".DraftStatusTip",
                state="detached",
                timeout=12000,
            )
        except Exception:
            pass

        drafts_url = self.platform_cfg.get("drafts_url") or DRAFTS_URL
        try:
            await self.page.goto(
                drafts_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎打开草稿箱时页面已关闭"
                ) from exc
            logger.error("知乎打开草稿箱失败: {}", exc)
            return ""
        await self.simulator.random_delay(3, 5)

        keyword = str(title or "").strip()[:12]
        if not keyword:
            return drafts_url
        try:
            found = await self._draft_list_contains(keyword)
            if not found:
                await self.simulator.random_delay(3, 5)
                found = await self._draft_list_contains(keyword)
            if not found:
                logger.error("知乎草稿箱未找到标题包含「{}」的草稿", keyword)
                return ""
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎验证草稿箱时页面已关闭"
                ) from exc
            logger.error("知乎草稿箱验证失败: {}", exc)
            return ""
        return drafts_url

    async def _draft_list_contains(self, keyword: str) -> bool:
        """草稿箱页面正文是否包含关键字；必要时先点击侧栏「草稿箱」入口。"""

        if "creation/drafts" not in (self.page.url or ""):
            clicked = await self.page.evaluate(
                """() => {
                    const nodes = Array.from(
                        document.querySelectorAll('a, button, [role=button]')
                    );
                    const target = nodes.find((el) =>
                        (el.innerText || '').trim().startsWith('草稿箱')
                    );
                    if (target) { target.click(); return true; }
                    return false;
                }"""
            )
            await self.simulator.random_delay(2, 4)
            if not clicked:
                return False
        found = await self.page.evaluate(
            "(kw) => (document.body.innerText || '').includes(kw)",
            keyword,
        )
        return bool(found)

    @staticmethod
    def _not_implemented(operation: str):
        raise PlatformNotImplementedError(
            f"PLATFORM_NOT_IMPLEMENTED: 知乎{operation}能力尚未接入"
        )

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")


def _text(value: object) -> str:
    return " ".join(str(value or "").split())[:255]
