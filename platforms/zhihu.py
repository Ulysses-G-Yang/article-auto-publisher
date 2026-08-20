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
import copy
import io
import re
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    DraftResultUnknownError,
    LoginRequiredError,
    PlatformAutomationError,
    SelectorError,
)
from platforms.content_validation import (
    ContentValidationError,
    extract_expected_paragraphs,
    normalize_for_comparison,
    safe_media_error,
)

TITLE_SELECTOR = "textarea.Input[placeholder^='请输入标题']"
BODY_SELECTOR = "div.notranslate.public-DraftEditor-content"
EDITOR_URL = "https://zhuanlan.zhihu.com/write"
DRAFTS_URL = "https://www.zhihu.com/creator/manage/creation/drafts"
BODY_IMAGE_INPUT = "input[type=file]:not(.UploadPicture-input)[accept*='image']"


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class ZhihuPlatform(BasePlatform):
    """知乎账号会话与草稿投递适配器；公开发布能力保持关闭。"""

    platform_name = "zhihu"
    SESSION_COOKIE_NAMES = frozenset({"z_c0", "d_c0", "q_c1"})
    LOGIN_POLL_ATTEMPTS = 60
    LOGIN_POLL_INTERVAL_SECONDS = 2
    PERSIST_VERIFY_ATTEMPTS = 15
    PERSIST_VERIFY_INTERVAL_SECONDS = 2

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None
        self._expected_persisted_blocks: list[dict] | None = None
        self._pending_cover_path = ""

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
            sanitized["ok"] and sanitized["user_id"] and sanitized["display_name"]
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
        return any(str(item.get("name") or "") in self.SESSION_COOKIE_NAMES for item in cookies)

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
        raise SelectorError(f"知乎编辑器未找到标题输入框（{last_error}）")

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
        """按冻结 ContentVersion 的图文顺序写入并校验 Draft.js 正文。"""

        self._require_page_alive("知乎填写正文")
        self._expected_persisted_blocks = copy.deepcopy(content_blocks)
        editor = await self._current_body_editor()
        await editor.click()
        await self.page.keyboard.press("Control+A")
        await self.page.keyboard.press("Backspace")
        await self.simulator.random_delay(0.3, 0.8)

        expected_images = sum(1 for block in content_blocks if block.get("type") == "image")
        uploaded_images = 0
        failed_images: list[dict[str, str]] = []
        content_started = False

        for block_index, block in enumerate(content_blocks):
            if not isinstance(block, dict):
                raise ContentValidationError("ZHIHU_CONTENT_CONTRACT_INVALID: 正文块无效")
            block_type = block.get("type")
            if block_type in {"text", "heading"}:
                text = str(block.get("text") or "").strip()
                if not text:
                    continue
                if block_type == "heading":
                    if block.get("level") != 2 or "\n" in text or "\r" in text:
                        raise ContentValidationError(
                            "ZHIHU_HEADING_UNSUPPORTED: 仅支持单行二级标题"
                        )
                if content_started:
                    await self._place_body_caret_at_end()
                    await self.page.keyboard.press("Enter")
                await self._place_body_caret_at_end()
                lines = text.splitlines() or [text]
                for line_index, line in enumerate(lines):
                    if line.strip():
                        await self.page.keyboard.insert_text(line.strip())
                    if line_index < len(lines) - 1:
                        await self.page.keyboard.press("Enter")
                if block_type == "heading":
                    await self._apply_h2_to_current_block()
                content_started = True
                continue

            if block_type != "image":
                raise ContentValidationError("ZHIHU_CONTENT_CONTRACT_INVALID: 未知正文块类型")

            if content_started:
                await self._place_body_caret_at_end()
                await self.page.keyboard.press("Enter")
            await self._place_body_caret_at_end()
            image_path = self._image_path_for_block(block, images)
            if image_path:
                upload_result = await self._upload_image(image_path) or {}
                if upload_result.get("success"):
                    uploaded_images += 1
                else:
                    failed_images.append(
                        {
                            "filename": Path(image_path).name,
                            "error_code": str(
                                upload_result.get("error_code") or "PLATFORM_MEDIA_INCOMPLETE"
                            ),
                            "error": safe_media_error(
                                upload_result.get("error"),
                                fallback="图片上传失败",
                            ),
                        }
                    )
            else:
                failed_images.append(
                    {
                        "filename": "",
                        "error_code": "IMAGE_PATH_UNRESOLVED",
                        "error": "文章图片块没有唯一对应本地文件",
                    }
                )
            await self._dismiss_media_overlay()
            content_started = True
            await self._validate_dom_prefix(
                content_blocks[: block_index + 1],
                phase=f"图片处理后第{block_index + 1}块",
            )
            await self.simulator.random_delay(1.5, 3.0)

        await self._validate_dom_exact(content_blocks, phase="正文最终")
        expected_count = len(extract_expected_paragraphs(content_blocks))
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

    async def _current_body_editor(self):
        self._require_page_alive("知乎定位当前正文编辑器")
        editor = self.page.locator(BODY_SELECTOR).first
        try:
            if await editor.count() == 0 or not await editor.is_visible():
                raise SelectorError("知乎正文编辑器未找到或当前不可见")
            return editor
        except SelectorError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("知乎正文编辑器未找到或当前不可见") from exc

    async def _place_body_caret_at_end(self) -> None:
        editor = await self._current_body_editor()
        try:
            # Draft.js 维护自己的 SelectionState。直接改 DOM Range 看似移动了
            # 光标，但不会可靠同步内部状态；真实键盘 End 事件才会让后续
            # Enter 在文档末尾创建新 block。
            await editor.press("Control+End")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎移动正文光标时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "ZHIHU_CARET_POSITION_FAILED: 正文末尾光标定位失败"
            ) from exc

    async def _apply_h2_to_current_block(self) -> None:
        try:
            heading_menu = self.page.get_by_role("button", name="标题", exact=True)
            await heading_menu.click(timeout=5000)
            h2_option = self.page.get_by_role("button", name="二级标题", exact=True)
            await h2_option.click(timeout=5000)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎设置二级标题时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "ZHIHU_HEADING_APPLY_FAILED: 二级标题样式未能应用"
            ) from exc

    async def _dismiss_media_overlay(self) -> None:
        """图片上传后关闭模态层；未关闭时拒绝继续写后续正文。"""

        try:
            await self.page.keyboard.press("Escape")
            backdrop = self.page.locator(".Modal-backdrop").first
            for _ in range(6):
                if await backdrop.count() == 0 or not await backdrop.is_visible():
                    return
                await asyncio.sleep(0.25)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎关闭图片上传层时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "ZHIHU_MEDIA_OVERLAY_STUCK: 图片上传层状态无法确认"
            ) from exc
        raise ContentValidationError("ZHIHU_MEDIA_OVERLAY_STUCK: 图片上传层未关闭，禁止继续写入")

    @staticmethod
    def _image_path_for_block(block: dict, images: list[dict]) -> str | None:
        direct = block.get("local_path") if isinstance(block, dict) else None
        if direct:
            return str(direct)
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

    @staticmethod
    def _expected_content_tokens(blocks: list[dict]) -> list[dict]:
        tokens: list[dict] = []
        for block in blocks:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type == "image":
                tokens.append({"kind": "image"})
                continue
            for paragraph in extract_expected_paragraphs([block]):
                if block_type == "heading":
                    tokens.append(
                        {
                            "kind": "heading",
                            "level": int(block.get("level") or 0),
                            "text": paragraph.comparison_text,
                        }
                    )
                else:
                    tokens.append({"kind": "text", "text": paragraph.comparison_text})
        return tokens

    async def _read_editor_dom_tokens(self) -> list[dict]:
        editor = await self._current_body_editor()
        try:
            raw = await editor.evaluate(
                """root => {
                    const tokens = [];
                    const visit = (node) => {
                        if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
                        if (node.tagName.toLowerCase() === 'img') {
                            tokens.push({kind: 'image'});
                            return;
                        }
                        if (node.matches('[data-block="true"]')) {
                            const images = node.querySelectorAll('img');
                            if (images.length) {
                                for (const _image of images) {
                                    tokens.push({kind: 'image'});
                                }
                                return;
                            }
                            const text = node.innerText || node.textContent || '';
                            if (!text.trim()) return;
                            const tag = node.tagName.toLowerCase();
                            if (tag === 'h3') {
                                tokens.push({kind: 'heading', level: 2, text});
                            } else if (tag === 'h2') {
                                tokens.push({kind: 'heading', level: 1, text});
                            } else {
                                tokens.push({kind: 'text', text});
                            }
                            return;
                        }
                        for (const child of node.children) visit(child);
                    };
                    for (const child of root.children) visit(child);
                    return tokens;
                }"""
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎读取正文 DOM 时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "ZHIHU_CONTENT_DOM_VERIFY_FAILED: 正文 DOM 回读失败"
            ) from exc
        if not isinstance(raw, list):
            raise ContentValidationError("ZHIHU_CONTENT_DOM_VERIFY_FAILED: 正文 DOM 序列无效")
        normalized: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ContentValidationError("ZHIHU_CONTENT_DOM_VERIFY_FAILED: DOM token 无效")
            kind = item.get("kind")
            if kind == "image":
                normalized.append({"kind": "image"})
                continue
            text = normalize_for_comparison(item.get("text"))
            if not text:
                continue
            if kind == "heading":
                normalized.append(
                    {
                        "kind": "heading",
                        "level": int(item.get("level") or 0),
                        "text": text,
                    }
                )
            elif kind == "text":
                for paragraph in extract_expected_paragraphs([{"type": "text", "text": text}]):
                    normalized.append({"kind": "text", "text": paragraph.comparison_text})
            else:
                raise ContentValidationError("ZHIHU_CONTENT_DOM_VERIFY_FAILED: DOM token 类型无效")
        return normalized

    @staticmethod
    def _tokens_match(expected: list[dict], actual: list[dict]) -> bool:
        return expected == actual

    @staticmethod
    def _token_shape(tokens: list[dict]) -> str:
        shape = []
        for token in tokens[:40]:
            if token.get("kind") == "image":
                shape.append("I")
            elif token.get("kind") == "heading":
                shape.append(f"H{token.get('level')}:{len(token.get('text') or '')}")
            else:
                shape.append(f"T:{len(token.get('text') or '')}")
        return ",".join(shape) or "EMPTY"

    async def _validate_dom_prefix(self, blocks: list[dict], *, phase: str) -> None:
        expected = self._expected_content_tokens(blocks)
        actual = await self._read_editor_dom_tokens()
        if actual[: len(expected)] != expected:
            raise ContentValidationError(
                f"CONTENT_VALIDATION_ERROR: 知乎{phase}图文顺序不完整; "
                f"expected={self._token_shape(expected)}; "
                f"actual={self._token_shape(actual)}"
            )

    async def _validate_dom_exact(self, blocks: list[dict], *, phase: str) -> None:
        expected = self._expected_content_tokens(blocks)
        actual = await self._read_editor_dom_tokens()
        if not self._tokens_match(expected, actual):
            raise ContentValidationError(
                f"CONTENT_VALIDATION_ERROR: 知乎{phase}图文顺序不完整; "
                f"expected={self._token_shape(expected)}; "
                f"actual={self._token_shape(actual)}"
            )

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
            return {
                "success": False,
                "error_code": "IMAGE_UPLOAD_FAILED",
                "error": safe_media_error(exc, fallback="知乎图片上传失败"),
            }

    async def apply_cover(self, cover: dict | None = None) -> dict:
        """使用知乎独立 ``UploadPicture`` 控件上传冻结封面。"""

        strategy = str((cover or {}).get("strategy") or "NONE").upper()
        if strategy == "NONE":
            self._pending_cover_path = ""
            return {"success": True, "cover_status": "not_required"}
        if strategy not in {"FIRST_BODY_IMAGE", "EXPLICIT"}:
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "ZHIHU_COVER_STRATEGY_UNSUPPORTED",
                "error": "知乎不支持该封面策略",
            }
        requested = str((cover or {}).get("local_path") or "")
        try:
            cover_path = Path(requested).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "ZHIHU_COVER_ASSET_UNAVAILABLE",
                "error": "知乎封面素材不可用",
            }
        wrapper = self.page.locator(".UploadPicture-wrapper")
        inputs = wrapper.locator("input[type=file][accept='.jpeg, .jpg, .png']")
        if await wrapper.count() != 1 or await inputs.count() != 1:
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "ZHIHU_COVER_INPUT_AMBIGUOUS",
                "error": "知乎封面上传控件不存在或候选不唯一",
            }
        try:
            await inputs.first.set_input_files(str(cover_path), timeout=15000)
            ready = False
            for _ in range(30):
                await asyncio.sleep(0.5)
                ready = bool(
                    await wrapper.evaluate(
                        """root => Array.from(root.querySelectorAll('img')).some(
                            image => image.complete && image.naturalWidth > 0
                        )"""
                    )
                )
                if ready:
                    break
            if not ready:
                raise ContentValidationError(
                    "ZHIHU_COVER_PREVIEW_NOT_READY: 知乎封面预览未稳定加载"
                )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎设置封面时页面已关闭"
                ) from exc
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": getattr(exc, "error_code", None)
                or "ZHIHU_COVER_UPLOAD_FAILED",
                "error": safe_media_error(exc, fallback="知乎封面上传失败"),
            }
        self._pending_cover_path = str(cover_path)
        return {
            "success": True,
            "cover_status": "pending_verification",
            "cover_mode": "EXPLICIT_UPLOAD",
        }

    @staticmethod
    def _cover_hash(payload: bytes) -> tuple[int, ...] | None:
        try:
            from PIL import Image, ImageOps

            with Image.open(io.BytesIO(payload)) as source:
                image = ImageOps.exif_transpose(source).convert("L").resize((16, 16))
                pixels = list(image.getdata())
        except Exception:
            return None
        average = sum(pixels) / len(pixels)
        return tuple(int(value >= average) for value in pixels)

    async def verify_persisted_cover(
        self,
        *,
        title: str,
        draft_url: str,
        cover: dict | None,
        apply_result: dict,
    ) -> dict:
        del title, draft_url, cover
        wrapper = self.page.locator(".UploadPicture-wrapper")
        if await wrapper.count() != 1:
            matched = False
        else:
            urls = await wrapper.locator("img").evaluate_all(
                "images => images.filter(image => image.complete && image.naturalWidth > 0)"
                ".map(image => image.currentSrc || image.src || '').filter(Boolean)"
            )
            observed = b""
            if urls and self.context is not None:
                try:
                    response = await self.context.request.get(str(urls[0]), timeout=15000)
                    if response.ok:
                        observed = await response.body()
                except Exception:
                    observed = b""
            try:
                expected = Path(self._pending_cover_path).read_bytes()
            except OSError:
                expected = b""
            expected_hash = self._cover_hash(expected)
            observed_hash = self._cover_hash(observed)
            matched = bool(
                expected_hash is not None
                and observed_hash is not None
                and sum(
                    left != right
                    for left, right in zip(
                        expected_hash, observed_hash, strict=True
                    )
                )
                <= 32
            )
        if not matched:
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "ZHIHU_COVER_PERSISTENCE_UNVERIFIED",
                "error": "知乎草稿重开后封面与冻结素材不一致",
            }
        return {
            **apply_result,
            "success": True,
            "cover_status": "completed",
            "cover_mode": "EXPLICIT_UPLOAD",
        }

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
        """等待自动保存，取得唯一草稿 ID，并重开核验完整持久化正文。"""

        self._require_page_alive("知乎保存草稿")
        expected_title = " ".join(str(title or "").split())
        if not expected_title:
            raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 知乎自动保存结果缺少可核验标题")

        # 知乎从标题首次输入起便可能产生自动保存副作用；此处以后任何
        # 不确定状态只能标记 RESULT_UNKNOWN，绝不能返回可自动重试的普通失败。
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
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 知乎自动保存后无法打开草稿箱"
            ) from exc
        await self.simulator.random_delay(3, 5)

        try:
            draft = await self._find_unique_exact_draft(expected_title)
            if draft is None:
                await self.simulator.random_delay(3, 5)
                draft = await self._find_unique_exact_draft(expected_title)
            if draft is None:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 知乎未找到标题精确匹配的唯一草稿"
                )
            edit_url = self._draft_edit_url(draft.get("id"))
            await self._verify_persisted_draft(expected_title, edit_url)
        except Exception as exc:
            if isinstance(exc, DraftResultUnknownError):
                raise
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎验证草稿箱时页面已关闭"
                ) from exc
            raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 知乎持久化草稿核验失败") from exc
        return edit_url

    async def _find_unique_exact_draft(self, expected_title: str) -> dict | None:
        """只接受列表 API 中标题精确且唯一的草稿实体。"""

        try:
            async with self.page.expect_response(
                self._is_drafts_list_response,
                timeout=20000,
            ) as response_info:
                await self.page.reload(
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await self.simulator.random_delay(2, 4)
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
                return None
            response = await response_info.value
            payload = await response.json()
            candidates = [
                {
                    "id": str(item.get("id") or item.get("url_token") or ""),
                    "title": " ".join(str(item.get("title") or "").split()),
                }
                for item in (payload.get("data") or [])
                if isinstance(item, dict)
            ]
            matches = [
                item for item in candidates if item["id"] and item["title"] == expected_title
            ]
            return matches[0] if len(matches) == 1 else None
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎草稿列表 API 验证时页面已关闭"
                ) from exc
            logger.warning(
                "知乎草稿列表 API 验证失败: error_type={}",
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _draft_edit_url(draft_id: object) -> str:
        safe_id = str(draft_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", safe_id):
            raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 知乎草稿实体 ID 无效")
        return f"https://zhuanlan.zhihu.com/p/{safe_id}/edit"

    async def _verify_persisted_draft(
        self,
        expected_title: str,
        edit_url: str,
    ) -> None:
        blocks = self._expected_persisted_blocks
        if blocks is None:
            raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 知乎缺少冻结内容核验快照")
        try:
            await self.page.goto(
                edit_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.page.wait_for_selector(
                TITLE_SELECTOR,
                state="visible",
                timeout=20000,
            )
            expected_tokens = self._expected_content_tokens(blocks)
            actual_tokens: list[dict] = []
            title_matched = False
            for attempt in range(self.PERSIST_VERIFY_ATTEMPTS):
                title_field = self.page.locator(TITLE_SELECTOR).first
                actual_title = " ".join((await title_field.input_value()).split())
                title_matched = actual_title == expected_title
                if title_matched:
                    actual_tokens = await self._read_editor_dom_tokens()
                    if self._tokens_match(expected_tokens, actual_tokens):
                        return
                if attempt + 1 < self.PERSIST_VERIFY_ATTEMPTS:
                    await asyncio.sleep(self.PERSIST_VERIFY_INTERVAL_SECONDS)
            if not title_matched:
                raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 知乎草稿重开后标题不一致")
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 知乎草稿重开后图文结构不完整; "
                f"expected={self._token_shape(expected_tokens)}; "
                f"actual={self._token_shape(actual_tokens)}"
            )
        except DraftResultUnknownError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎重开草稿核验时页面已关闭"
                ) from exc
            raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 知乎草稿重开核验失败") from exc

    @staticmethod
    def _is_drafts_list_response(response) -> bool:
        parts = urlsplit(response.url)
        return parts.path == "/api/v4/articles/my_drafts" and response.request.method == "GET"

    @staticmethod
    def _not_implemented(operation: str):
        raise PlatformNotImplementedError(f"PLATFORM_NOT_IMPLEMENTED: 知乎{operation}能力尚未接入")

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")


def _text(value: object) -> str:
    return " ".join(str(value or "").split())[:255]
