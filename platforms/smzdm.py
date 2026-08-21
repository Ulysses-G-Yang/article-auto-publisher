"""什么值得买（smzdm）账号会话适配器。

登录方式说明（2026-08 真实页面探测）：
- smzdm Web 登录页（https://zhiyou.smzdm.com/user/login）只有
  手机号/邮箱 + 密码 + 「60 天内免登录」表单，**没有扫码登录**
  （页面上 120x120 二维码是 App 下载码，不是登录码）。
- 因此本适配器采用「人工凭据登录」：打开可见浏览器窗口，由用户在
  窗口内输入账号密码（凭据不进入代码、不存储、不记录），适配器
  轮询会话 cookie 确认登录成功。

登录成功信号：.smzdm.com 出现 sess 会话 cookie（登录后才下发）。
身份提取采用「捕获页面自身响应」模式 + 首页 DOM 兜底。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

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
from platforms.media_progress import safe_media_progress

LOGIN_URL = "https://zhiyou.smzdm.com/user/login"
HOME_URL = "https://zhiyou.smzdm.com/"
USERNAME_SELECTOR = "input#username.form-input"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name", "nick"}
IDENTITY_UID_KEYS = {"smzdm_id", "uid", "user_id", "id"}
TITLE_SELECTOR = "textarea.article-title"
BODY_SELECTOR = "div.ProseMirror"
DRAFTS_URL = "https://post.smzdm.com/tougao/"
BODY_IMAGE_TRIGGER = ".right-menu-bar:has(svg.zicon-picture)"
BODY_IMAGE_INPUT = 'input[type="file"][accept*="image"]'


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class SmzdmPlatform(BasePlatform):
    """什么值得买账号会话与 DRAFT-only 图文投递适配器。"""

    platform_name = "smzdm"
    SESSION_COOKIE_NAMES = frozenset({"sess"})
    LOGIN_POLL_ATTEMPTS = 120
    LOGIN_POLL_INTERVAL_SECONDS = 3
    POST_IMAGE_PARAGRAPH_POLL_ATTEMPTS = 20
    POST_IMAGE_PARAGRAPH_POLL_INTERVAL_SECONDS = 0.25

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None
        self._expected_persisted_blocks: list[dict] | None = None
        self._media_progress_state: dict[str, int] | None = None
        self._pending_cover_path = ""
        self._preflight_title = ""
        self._preflight_draft_ids: frozenset[str] | None = None

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None

    # ==================== 登录态与身份 ====================

    async def _has_session_cookie_signal(self) -> bool:
        """sess 会话 cookie 作为登录成功信号；不返回、不记录 cookie 值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies([
                "https://www.smzdm.com/",
                "https://zhiyou.smzdm.com/",
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
            self._require_page_alive("smzdm 登录态检测")
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
                self.last_login_error = "SMZDM_IDENTITY_MISSING: 会话存在但身份未确认"
                return False
            self.last_login_error = "LOGIN_REQUIRED: smzdm 账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "SMZDM_LOGIN_CHECK_ERROR: smzdm 登录态验证失败"
            return False

    async def login(self):
        """打开 smzdm 登录页，由用户在可见窗口中输入凭据完成登录。"""

        self._require_page_alive("smzdm 打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        try:
            await self.page.wait_for_selector(
                USERNAME_SELECTOR,
                state="visible",
                timeout=20000,
            )
        except Exception:
            pass
        await self._show_login_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("smzdm 等待登录")
            if await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: smzdm 登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_login_hint(self):
        """页面顶部显示登录提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'smzdm-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#fe6d01;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请在下方表单输入手机号/邮箱和密码登录'
                        + '（可勾选 60 天内免登录）<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """返回 smzdm 同源确认的最小平台身份（捕获 + DOM 兜底）。"""

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        response.request.resource_type in ("xhr", "fetch")
                        and "smzdm.com" in response.url
                        and any(
                            key in response.url.lower()
                            for key in ("user", "profile", "account", "info")
                        )
                    ):
                        # smzdm 当前用户接口是 JSONP（callback 包裹），
                        # response.json() 会失败，必须取文本后剥壳解析。
                        text = await response.text()
                        payload = self._parse_jsonp(text)
                        if not isinstance(payload, dict):
                            return
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
                    "BROWSER_CONTEXT_CLOSED: smzdm 身份捕获时页面已关闭"
                ) from exc
            logger.warning("smzdm 身份捕获失败: {}", exc)

        # DOM 兜底：个人中心昵称 + user cookie 中的用户 ID
        if not (
            isinstance(self._identity_payload, dict) and self._identity_payload.get("ok")
        ):
            try:
                nickname = await self.page.evaluate(
                    """() => {
                        const el = document.querySelector('.info-stuff-nickname');
                        return el ? (el.innerText || '').trim() : '';
                    }"""
                )
                user_id = ""
                if self.context is not None:
                    cookies = await self.context.cookies([
                        "https://www.smzdm.com/",
                        "https://zhiyou.smzdm.com/",
                    ])
                    user_cookie = next(
                        (c.get("value", "") for c in cookies if c.get("name") == "user"),
                        "",
                    )
                    m = re.search(r"user%3A(\d+)\|(\d+)", str(user_cookie))
                    if m:
                        user_id = m.group(2) or m.group(1)
                if user_id and nickname:
                    self._identity_payload = {
                        "ok": True,
                        "user_id": user_id,
                        "display_name": nickname,
                    }
            except Exception:  # noqa: BLE001
                pass

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        return {"ok": False, "user_id": "", "display_name": ""}

    @staticmethod
    def _parse_jsonp(text: str) -> dict | None:
        """剥掉 JSONP 的 callback 包裹并解析为 dict；失败返回 None。"""

        match = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", text or "", re.S)
        if not match:
            return None
        try:
            payload = json.loads(match.group(1))
            return payload if isinstance(payload, dict) else None
        except Exception:  # noqa: BLE001
            return None

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
            f"PLATFORM_NOT_IMPLEMENTED: smzdm{operation}能力尚未接入"
        )

    async def navigate_to_editor(self):
        """打开 smzdm 投稿编辑器：投稿页 → 发布新文章。

        真实结构（2026-08 探测）：标题 ``textarea.article-title``
        （0/30），正文 ``div.ProseMirror``（TipTap），「草稿将自动保存」。
        """
        self._require_page_alive("smzdm 打开编辑器")
        try:
            await self.page.goto(
                "https://post.smzdm.com/tougao/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.page.wait_for_function(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    return nodes.some((el) => {
                        const t = (el.innerText || '').trim();
                        return (t === '发布新文章' || t === '写文章')
                            && el.children.length <= 3;
                    });
                }""",
                timeout=20000,
            )
            clicked = await self.page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    const target = nodes.find((el) => {
                        const t = (el.innerText || '').trim();
                        return (t === '发布新文章' || t === '写文章')
                            && el.children.length <= 3;
                    });
                    if (target) { target.click(); return true; }
                    return false;
                }"""
            )
            if not clicked:
                raise RuntimeError("发布新文章入口未找到")
            await self.page.wait_for_selector(
                "textarea.article-title",
                state="visible",
                timeout=20000,
            )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 打开编辑器时页面已关闭"
                ) from exc
            raise SelectorError("smzdm 投稿编辑器未找到标题输入框") from exc

    async def fill_title(self, title: str):
        """填写 smzdm 文章标题（textarea.article-title，0/30）。"""

        self._require_page_alive("smzdm 填写标题")
        title_field = self.page.locator("textarea.article-title").first
        try:
            if await title_field.count() == 0 or not await title_field.is_visible():
                raise RuntimeError("标题输入框不可见")
            await title_field.fill(str(title or "").strip())
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 填写标题时页面已关闭"
                ) from exc
            logger.error("smzdm 标题填写失败: {}", exc)
            raise SelectorError("smzdm 标题输入框未找到或填写失败") from exc

    async def fill_content(self, content_blocks: list, images: list):
        """按冻结 ContentVersion 的原始顺序写入 TipTap 图文并严格回读。"""

        self._require_page_alive("smzdm 填写正文")
        self._expected_persisted_blocks = copy.deepcopy(content_blocks)
        expected_images = sum(1 for block in content_blocks if block.get("type") == "image")
        self._media_progress_state = {
            "expected_images": expected_images,
            "uploaded_images": 0,
            "failed_image_count": 0,
        }
        editor = await self._current_body_editor()
        try:
            await editor.click(timeout=5000)
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
            await self.simulator.random_delay(0.3, 0.8)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 清空正文时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "SMZDM_EDITOR_RESET_FAILED: 正文编辑器无法安全清空"
            ) from exc

        uploaded_images = 0
        failed_images: list[dict[str, str]] = []
        content_started = False
        paragraph_ready_after_image = False
        for block_index, block in enumerate(content_blocks):
            if not isinstance(block, dict):
                raise ContentValidationError("SMZDM_CONTENT_CONTRACT_INVALID: 正文块无效")
            block_type = block.get("type")
            if block_type in {"text", "heading"}:
                text = str(block.get("text") or "").strip()
                if not text:
                    continue
                if block_type == "heading" and (
                    block.get("level") != 2 or "\n" in text or "\r" in text
                ):
                    raise ContentValidationError(
                        "SMZDM_HEADING_UNSUPPORTED: 仅支持单行二级标题"
                    )
                if content_started:
                    await self._place_body_caret_at_end()
                    if paragraph_ready_after_image:
                        paragraph_ready_after_image = False
                    else:
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
                raise ContentValidationError(
                    "SMZDM_CONTENT_CONTRACT_INVALID: 未知正文块类型"
                )
            if content_started:
                await self._place_body_caret_at_end()
                if paragraph_ready_after_image:
                    paragraph_ready_after_image = False
                else:
                    await self.page.keyboard.press("Enter")
            await self._place_body_caret_at_end()
            image_path = self._image_path_for_block(block, images)
            if image_path:
                upload_result = await self._upload_image(image_path) or {}
                if upload_result.get("success"):
                    uploaded_images += 1
                    await self._create_paragraph_after_image()
                    paragraph_ready_after_image = True
                else:
                    failed_images.append(
                        {
                            "filename": Path(image_path).name,
                            "error_code": str(
                                upload_result.get("error_code")
                                or "PLATFORM_MEDIA_INCOMPLETE"
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
            content_started = True
            self._media_progress_state.update(
                uploaded_images=uploaded_images,
                failed_image_count=len(failed_images),
            )
            try:
                await self._validate_dom_prefix(
                    content_blocks[: block_index + 1],
                    phase=f"图片处理后第{block_index + 1}块",
                )
            except ContentValidationError as exc:
                self._attach_media_progress(exc)
                raise
            await self.simulator.random_delay(2.5, 4.5)

        try:
            await self._validate_dom_exact(content_blocks, phase="正文最终")
        except ContentValidationError as exc:
            self._attach_media_progress(exc)
            raise
        expected_count = len(extract_expected_paragraphs(content_blocks))
        logger.info("smzdm 正文输入并最终验证成功: {} 个文本段落", expected_count)

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
                "smzdm 图片处理结果: expected={}, uploaded={}, failed={}",
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
    def _media_status(expected: int, uploaded: int, failed: int) -> str:
        if expected == 0:
            return "not_required"
        if uploaded == expected:
            return "completed"
        if uploaded == 0 and failed:
            return "failed"
        if uploaded + failed == expected:
            return "partial"
        return "in_progress"

    def _attach_media_progress(self, exc: ContentValidationError) -> None:
        state = self._media_progress_state
        if not isinstance(state, dict):
            return
        progress = safe_media_progress(
            {
                **state,
                "media_status": self._media_status(
                    state["expected_images"],
                    state["uploaded_images"],
                    state["failed_image_count"],
                ),
            }
        )
        if progress is not None:
            exc.media_progress = progress

    async def _current_body_editor(self):
        self._require_page_alive("smzdm 定位当前正文编辑器")
        editor = self.page.locator(BODY_SELECTOR).first
        try:
            if await editor.count() == 0 or not await editor.is_visible():
                raise SelectorError("smzdm 正文编辑器未找到或当前不可见")
            return editor
        except SelectorError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("smzdm 正文编辑器未找到或当前不可见") from exc

    async def _place_body_caret_at_end(self) -> None:
        editor = await self._current_body_editor()
        try:
            await editor.press("Control+End")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 移动正文光标时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "SMZDM_CARET_POSITION_FAILED: 正文末尾光标定位失败"
            ) from exc

    async def _apply_h2_to_current_block(self) -> None:
        try:
            menu = self.page.locator("button.list-item:has(svg.zicon-t)").first
            if await menu.count() == 0 or not await menu.is_visible():
                raise RuntimeError("标题菜单不可见")
            await menu.click(timeout=5000)
            option = self.page.locator(".dropdown-listitem").filter(
                has_text=re.compile(r"^二级标题$")
            ).first
            if await option.count() == 0 or not await option.is_visible():
                raise RuntimeError("二级标题选项不可见")
            await option.click(timeout=5000)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 设置二级标题时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "SMZDM_HEADING_APPLY_FAILED: 二级标题样式未能应用"
            ) from exc

    async def _create_paragraph_after_image(self) -> None:
        """通过页面公开的 TipTap Editor API 创建图片后的正文段落。

        图片是 TipTap/ProseMirror 的原子节点。过去依赖方向键和回车猜测
        浏览器光标，连续图片或最后一张图片时会落到错误节点。这里直接从
        编辑器根节点取得 ``editor`` 实例，通过 ``commands.insertContentAt``
        向文档末尾插入 paragraph，并把 selection 放入新段落；拿不到真实
        TipTap API 时 fail closed，绝不回退到键盘碰运气。
        """

        safe_reasons = {
            "editor-api-unavailable",
            "paragraph-node-unavailable",
            "transaction-result-invalid",
            "transaction-evaluation-failed",
            "dom-tail-not-ready",
        }
        last_reason = "dom-tail-not-ready"
        try:
            for attempt in range(self.POST_IMAGE_PARAGRAPH_POLL_ATTEMPTS):
                editor = await self._current_body_editor()
                try:
                    mutation = await editor.evaluate(
                """root => {
                    const domTailIsParagraph = () => {
                        const tail = root.lastElementChild;
                        return !!tail && tail.tagName.toLowerCase() === 'p'
                            && tail.querySelectorAll('img').length === 0;
                    };
                    if (domTailIsParagraph()) {
                        return {ok: true, action: 'existing'};
                    }

                    // 真实页面把 TipTap Editor 实例公开挂在 ProseMirror 根节点
                    // 的 editor 属性上。不要再依赖 pmViewDesc 等私有实现细节。
                    const tiptap = root.editor || null;
                    const commands = tiptap && tiptap.commands;
                    const state = tiptap && (tiptap.state || tiptap.view?.state);
                    if (!tiptap || !commands || !state || !state.doc ||
                        typeof commands.insertContentAt !== 'function' ||
                        typeof commands.focus !== 'function') {
                        return {ok: false, reason: 'editor-api-unavailable'};
                    }
                    const paragraphType = state.schema?.nodes?.paragraph;
                    if (!paragraphType) {
                        return {ok: false, reason: 'paragraph-node-unavailable'};
                    }

                    const modelTail = state.doc.lastChild;
                    let modelTailHasImage = false;
                    if (modelTail && typeof modelTail.descendants === 'function') {
                        modelTail.descendants(node => {
                            if (node.type && node.type.name === 'image') {
                                modelTailHasImage = true;
                                return false;
                            }
                            return !modelTailHasImage;
                        });
                    }
                    // 图片通常是 paragraph 内的 inline atom。仅判断末节点类型
                    // 会把 <p><img></p> 误认为可输入段落，必须确认其中无图片。
                    if (modelTail && modelTail.type === paragraphType
                        && !modelTailHasImage) {
                        commands.focus('end');
                        return {ok: true, action: 'existing-model'};
                    }

                    const insertAt = state.doc.content.size;
                    const inserted = commands.insertContentAt(
                        insertAt,
                        {type: 'paragraph'},
                        {updateSelection: true},
                    );
                    if (inserted === false) {
                        return {ok: false, reason: 'transaction-result-invalid'};
                    }
                    commands.focus('end');
                    return {ok: true, action: 'inserted'};
                }"""
                    )
                except Exception as exc:
                    if self._exception_means_browser_closed(exc):
                        raise BrowserLifecycleError(
                            "BROWSER_CONTEXT_CLOSED: smzdm 图片后创建正文段落时页面已关闭"
                        ) from exc
                    mutation = None
                    last_reason = "transaction-evaluation-failed"
                mutation_ok = (
                    isinstance(mutation, dict) and mutation.get("ok") is True
                )
                if mutation_ok:
                    last_reason = "dom-tail-not-ready"
                elif isinstance(mutation, dict):
                    reason = str(mutation.get("reason") or "")
                    last_reason = reason if reason in safe_reasons else "transaction-result-invalid"
                else:
                    last_reason = "transaction-result-invalid"

                if mutation_ok:
                    # 图片上传组件可能异步替换编辑器根节点。每轮都重新取得
                    # 当前 editor；下一轮会重新执行幂等 transaction。
                    editor = await self._current_body_editor()
                    tail_ready = bool(
                        await editor.evaluate(
                            """root => {
                                const tail = root.lastElementChild;
                                return !!tail && tail.tagName.toLowerCase() === 'p'
                                    && tail.querySelectorAll('img').length === 0;
                            }"""
                        )
                    )
                    if tail_ready:
                        return
                if attempt + 1 < self.POST_IMAGE_PARAGRAPH_POLL_ATTEMPTS:
                    await asyncio.sleep(
                        self.POST_IMAGE_PARAGRAPH_POLL_INTERVAL_SECONDS
                    )
            raise ContentValidationError(
                "SMZDM_POST_IMAGE_PARAGRAPH_FAILED: 图片后无法建立正文插入点; "
                f"reason={last_reason}"
            )
        except ContentValidationError:
            raise
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 图片后创建正文段落时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "SMZDM_POST_IMAGE_PARAGRAPH_FAILED: 图片后无法建立正文插入点; "
                "reason=transaction-evaluation-failed"
            ) from exc

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
                    for (const node of root.children) {
                        const images = node.querySelectorAll('img');
                        if (images.length) {
                            for (const _image of images) tokens.push({kind: 'image'});
                            continue;
                        }
                        const text = node.innerText || node.textContent || '';
                        if (!text.trim()) continue;
                        const tag = node.tagName.toLowerCase();
                        if (tag === 'h3') {
                            tokens.push({kind: 'heading', level: 2, text});
                        } else if (tag === 'h2') {
                            tokens.push({kind: 'heading', level: 1, text});
                        } else {
                            tokens.push({kind: 'text', text});
                        }
                    }
                    return tokens;
                }"""
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 读取正文 DOM 时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "SMZDM_CONTENT_DOM_VERIFY_FAILED: 正文 DOM 回读失败"
            ) from exc
        if not isinstance(raw, list):
            raise ContentValidationError("SMZDM_CONTENT_DOM_VERIFY_FAILED: DOM 序列无效")
        normalized: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ContentValidationError("SMZDM_CONTENT_DOM_VERIFY_FAILED: DOM token 无效")
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
                for paragraph in extract_expected_paragraphs(
                    [{"type": "text", "text": text}]
                ):
                    normalized.append(
                        {"kind": "text", "text": paragraph.comparison_text}
                    )
            else:
                raise ContentValidationError(
                    "SMZDM_CONTENT_DOM_VERIFY_FAILED: DOM token 类型无效"
                )
        return normalized

    @staticmethod
    def _token_shape(tokens: list[dict]) -> str:
        shape: list[str] = []
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
                f"CONTENT_VALIDATION_ERROR: smzdm{phase}图文顺序不完整; "
                f"expected={self._token_shape(expected)}; "
                f"actual={self._token_shape(actual)}"
            )

    async def _validate_dom_exact(self, blocks: list[dict], *, phase: str) -> None:
        expected = self._expected_content_tokens(blocks)
        actual = await self._read_editor_dom_tokens()
        if actual != expected:
            raise ContentValidationError(
                f"CONTENT_VALIDATION_ERROR: smzdm{phase}图文顺序不完整; "
                f"expected={self._token_shape(expected)}; "
                f"actual={self._token_shape(actual)}"
            )

    async def _upload_image(self, image_path: str) -> dict:
        """点击真实正文图片入口后上传一张图，并验证正文图片数量增长。"""

        self._require_page_alive("smzdm 上传图片")
        try:
            trigger = self.page.locator(BODY_IMAGE_TRIGGER).first
            if await trigger.count() == 0 or not await trigger.is_visible():
                return {
                    "success": False,
                    "error_code": "SMZDM_BODY_IMAGE_TRIGGER_NOT_FOUND",
                    "error": "smzdm 正文图片入口未找到，已安全停止",
                }
            before = await self.page.locator(f"{BODY_SELECTOR} img").count()
            await trigger.click(timeout=5000)
            await self.simulator.random_delay(0.3, 0.8)
            file_inputs = self.page.locator(BODY_IMAGE_INPUT)
            image_candidates = []
            for index in range(await file_inputs.count()):
                candidate = file_inputs.nth(index)
                if await candidate.is_visible():
                    image_candidates.append(candidate)
            if not image_candidates:
                return {
                    "success": False,
                    "error_code": "SMZDM_BODY_IMAGE_INPUT_NOT_FOUND",
                    "error": "smzdm 未发现可证明属于正文的图片控件，已安全停止",
                }
            if len(image_candidates) != 1:
                return {
                    "success": False,
                    "error_code": "SMZDM_BODY_IMAGE_INPUT_AMBIGUOUS",
                    "error": "smzdm 正文图片控件候选不唯一，已安全停止",
                }
            target_input = image_candidates[0]
            await target_input.set_input_files(str(image_path), timeout=15000)
            ready_streak = 0
            for _ in range(40):
                await asyncio.sleep(0.25)
                upload_ready = bool(
                    await self.page.evaluate(
                        """() => {
                            const visible = (el) => !!(
                                el.offsetWidth || el.offsetHeight || el.getClientRects().length
                            );
                            const preview = document.querySelector('.pic-box img.thumb-imgs');
                            const progress = Array.from(
                                document.querySelectorAll('.pic-box .progress-bar')
                            ).some(visible);
                            return !!preview && preview.complete
                                && preview.naturalWidth > 0 && !progress;
                        }"""
                    )
                )
                ready_streak = ready_streak + 1 if upload_ready else 0
                if ready_streak >= 2:
                    break
            if ready_streak < 2:
                return {
                    "success": False,
                    "error_code": "SMZDM_BODY_IMAGE_UPLOAD_NOT_READY",
                    "error": "smzdm 图片上传未达到可插入正文状态",
                }
            insert_button = self.page.locator(
                '.btn-item:has-text("插入正文")'
            ).first
            insert_ready = False
            for _ in range(15):
                if await insert_button.count() and await insert_button.is_visible():
                    insert_ready = True
                    break
                await asyncio.sleep(1)
            if not insert_ready:
                return {
                    "success": False,
                    "error_code": "SMZDM_BODY_IMAGE_INSERT_NOT_READY",
                    "error": "smzdm 图片已选择，但插入正文控件未就绪",
                }
            await insert_button.click(timeout=5000)
            after = before
            stable_streak = 0
            for _ in range(30):
                await asyncio.sleep(0.5)
                observed = await self.page.locator(f"{BODY_SELECTOR} img").count()
                if observed <= before:
                    stable_streak = 0
                    continue
                remote_ready = bool(
                    await self.page.evaluate(
                        """({selector, before}) => {
                            const root = document.querySelector(selector);
                            if (!root) return false;
                            const images = Array.from(root.querySelectorAll('img')).slice(before);
                            return images.length > 0 && images.every((image) => {
                                const src = image.getAttribute('src') || '';
                                return image.complete && image.naturalWidth > 0
                                    && (src.startsWith('http://')
                                        || src.startsWith('https://')
                                        || src.startsWith('//'));
                            });
                        }""",
                        {"selector": BODY_SELECTOR, "before": before},
                    )
                )
                if remote_ready:
                    after = observed
                    stable_streak += 1
                else:
                    stable_streak = 0
                if stable_streak >= 3:
                    break
            if after <= before or stable_streak < 3:
                return {
                    "success": False,
                    "error_code": "SMZDM_EDITOR_IMAGE_COUNT_UNCHANGED",
                    "error": "上传后正文编辑器图片数量未稳定增加",
                }
            panel_closed = False
            for _ in range(8):
                visible_inputs = 0
                for index in range(await file_inputs.count()):
                    if await file_inputs.nth(index).is_visible():
                        visible_inputs += 1
                if visible_inputs == 0:
                    panel_closed = True
                    break
                await asyncio.sleep(0.25)
            if not panel_closed:
                return {
                    "success": False,
                    "error_code": "SMZDM_BODY_IMAGE_PANEL_STUCK",
                    "error": "smzdm 图片插入后上传面板未关闭，已安全停止",
                }
            return {"success": True, "error": ""}
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 上传图片时页面已关闭"
                ) from exc
            return {
                "success": False,
                "error_code": "SMZDM_IMAGE_UPLOAD_FAILED",
                "error": safe_media_error(exc, fallback="smzdm 图片上传失败"),
            }

    async def _set_cover_variant(self, label: str, image_path: str) -> None:
        triggers = self.page.get_by_text(label, exact=True)
        visible_triggers = [
            triggers.nth(index)
            for index in range(await triggers.count())
            if await triggers.nth(index).is_visible()
        ]
        if len(visible_triggers) != 1:
            raise ContentValidationError(
                f"SMZDM_COVER_TRIGGER_AMBIGUOUS: {label}入口不存在或候选不唯一"
            )
        await visible_triggers[0].click(timeout=5000)
        await asyncio.sleep(0.5)
        inputs = self.page.locator(
            'input[type=file][accept="image/gif, image/png, image/jpeg"]'
        )
        visible_inputs = [
            inputs.nth(index)
            for index in range(await inputs.count())
            if await inputs.nth(index).is_visible()
        ]
        if len(visible_inputs) != 1:
            raise ContentValidationError(
                "SMZDM_COVER_INPUT_AMBIGUOUS: 封面图片控件不存在或候选不唯一"
            )
        before = await self.page.locator(".pic-box img.thumb-imgs").count()
        await visible_inputs[0].set_input_files(image_path, timeout=15000)
        ready = False
        for _ in range(30):
            await asyncio.sleep(0.5)
            previews = self.page.locator(".pic-box img.thumb-imgs")
            if await previews.count() <= before:
                continue
            ready = bool(
                await previews.last.evaluate(
                    "image => image.complete && image.naturalWidth > 0"
                )
            )
            if ready:
                break
        if not ready:
            raise ContentValidationError(
                "SMZDM_COVER_UPLOAD_NOT_READY: 封面素材上传预览未就绪"
            )
        actions = self.page.get_by_text("设为封面图", exact=True)
        visible_actions = [
            actions.nth(index)
            for index in range(await actions.count())
            if await actions.nth(index).is_visible()
        ]
        if len(visible_actions) != 1:
            raise ContentValidationError(
                "SMZDM_COVER_ACTION_AMBIGUOUS: 设为封面图操作不唯一"
            )
        await visible_actions[0].click(timeout=5000)
        modal_title = f"封面图-{label.removeprefix('添加')}编辑"
        await self.page.get_by_text(modal_title, exact=True).wait_for(
            state="visible", timeout=10000
        )
        confirms = self.page.get_by_text("确认", exact=True)
        visible_confirms = [
            confirms.nth(index)
            for index in range(await confirms.count())
            if await confirms.nth(index).is_visible()
        ]
        if len(visible_confirms) != 1:
            raise ContentValidationError(
                "SMZDM_COVER_CONFIRM_AMBIGUOUS: 封面裁剪确认控件不唯一"
            )
        await visible_confirms[0].click(timeout=5000)
        await self.page.get_by_text(modal_title, exact=True).wait_for(
            state="hidden", timeout=10000
        )
        await self.page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

    async def apply_cover(self, cover: dict | None = None) -> dict:
        """把同一冻结素材分别写入详情长图和列表方图。"""

        strategy = str((cover or {}).get("strategy") or "NONE").upper()
        if strategy == "NONE":
            self._pending_cover_path = ""
            return {"success": True, "cover_status": "not_required"}
        if strategy not in {"FIRST_BODY_IMAGE", "EXPLICIT"}:
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "SMZDM_COVER_STRATEGY_UNSUPPORTED",
                "error": "什么值得买不支持该封面策略",
            }
        requested = str((cover or {}).get("local_path") or "")
        try:
            cover_path = Path(requested).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "SMZDM_COVER_ASSET_UNAVAILABLE",
                "error": "什么值得买封面素材不可用",
            }
        try:
            await self._set_cover_variant("添加长图", str(cover_path))
            await self._set_cover_variant("添加方图", str(cover_path))
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 什么值得买设置封面时页面已关闭"
                ) from exc
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": getattr(exc, "error_code", None)
                or "SMZDM_COVER_UPLOAD_FAILED",
                "error": safe_media_error(exc, fallback="什么值得买封面上传失败"),
            }
        self._pending_cover_path = str(cover_path)
        return {
            "success": True,
            "cover_status": "pending_verification",
            "cover_mode": "EXPLICIT_LONG_AND_SQUARE",
        }

    async def verify_persisted_cover(
        self,
        *,
        title: str,
        draft_url: str,
        cover: dict | None,
        apply_result: dict,
    ) -> dict:
        del title, draft_url, cover
        state = await self.page.evaluate(
            """() => {
                const visible = (element) => {
                    if (!element) return false;
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const label = Array.from(document.querySelectorAll('*')).find(
                    element => visible(element) &&
                        (element.innerText || '').trim() === '封面图'
                );
                let region = label;
                for (let index = 0; index < 6 && region; index += 1) {
                    const text = region.innerText || '';
                    const images = Array.from(region.querySelectorAll('img')).filter(
                        image => visible(image) && image.complete && image.naturalWidth > 0
                    );
                    if (text.includes('添加长图') && text.includes('添加方图')) {
                        return {loaded: images.length};
                    }
                    region = region.parentElement;
                }
                return {loaded: 0};
            }"""
        )
        if not isinstance(state, dict) or int(state.get("loaded") or 0) < 2:
            return {
                "success": False,
                "cover_status": "failed",
                "safe_to_continue": True,
                "error_code": "SMZDM_COVER_PERSISTENCE_UNVERIFIED",
                "error": "什么值得买草稿重开后长图或方图封面未完整显示",
            }
        return {
            **apply_result,
            "success": True,
            "cover_status": "completed",
            "cover_mode": "EXPLICIT_LONG_AND_SQUARE",
        }

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """smzdm 草稿自动保存不需要话题；公开话题选择尚未接入，如实报告。"""

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
            "error": "smzdm 话题选择尚未接入（自动保存草稿不需要话题）",
            "selection": {},
        }

    async def preflight_delivery(self, title: str) -> None:
        """在写入编辑器前冻结草稿箱实体基线。

        什么值得买允许同名草稿，因此不能再用标题唯一性证明本次副作用。
        这里记录所有可验证的 ``/edit/{draft_id}``，保存后只认唯一新增 ID。
        """

        expected_title = " ".join(str(title or "").split())
        if not expected_title:
            raise DraftResultUnknownError(
                "DRAFT_BASELINE_UNAVAILABLE: smzdm 缺少可核验标题"
            )
        entities = await self._load_draft_entities_once()
        self._preflight_title = expected_title
        self._preflight_draft_ids = frozenset(entities)

    async def save_draft(self, title: str = "") -> str:
        """强制刷新自动保存，按实体 ID 差集定位并重开核验完整图文。"""

        self._require_page_alive("smzdm 保存草稿")
        expected_title = " ".join(str(title or "").split())
        if not expected_title:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 自动保存结果缺少可核验标题"
            )
        if self._expected_persisted_blocks is None:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 缺少冻结内容核验快照"
            )
        if (
            self._preflight_draft_ids is None
            or self._preflight_title != expected_title
        ):
            raise DraftResultUnknownError(
                "DRAFT_BASELINE_UNAVAILABLE: smzdm 缺少与本次一致的草稿 ID 基线"
            )
        captured: dict = {}

        async def _on_response(response) -> None:
            try:
                if response.request.method in ("POST", "PUT", "PATCH") and (
                    "save" in response.url.lower()
                    or "draft" in response.url.lower()
                    or "edit" in response.url.lower()
                ):
                    captured["status"] = response.status
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            captured["error_code"] = body.get("error_code")
                    except Exception:
                        pass
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", _on_response)
            editor = await self._current_body_editor()
            await editor.press("Control+End")
            await self.simulator.random_delay(0.3, 0.8)
            await self.page.keyboard.type(" ", delay=50)
            await self.page.keyboard.press("Backspace")
            await editor.evaluate("el => el.blur()")
            await self.simulator.random_delay(1, 2)
            for _ in range(20):
                if captured.get("status"):
                    break
                await asyncio.sleep(1)
            await self.simulator.random_delay(1, 2)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: smzdm 自动保存期间浏览器已关闭"
                ) from exc
            if isinstance(exc, DraftResultUnknownError):
                raise
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 无法触发可核验的自动保存"
            ) from exc
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        status = captured.get("status")
        if not isinstance(status, int) or not 200 <= status < 300:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 本次未捕获到成功自动保存响应"
            )
        try:
            edit_url = await self._find_unique_new_draft()
            await self._verify_persisted_draft(expected_title, edit_url)
            return edit_url
        except DraftResultUnknownError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: smzdm 自动保存后浏览器已关闭"
                ) from exc
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 持久化草稿核验失败"
            ) from exc

    @staticmethod
    def _validated_draft_entity(raw_url: str) -> tuple[str, str]:
        """返回安全的 ``(draft_id, canonical_url)``，拒绝跨域和带参数地址。"""

        edit_url = urljoin(DRAFTS_URL, str(raw_url or ""))
        parts = urlsplit(edit_url)
        match = re.fullmatch(r"/edit/([A-Za-z0-9_-]{1,128})", parts.path)
        if (
            parts.scheme != "https"
            or parts.netloc != "post.smzdm.com"
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or match is None
        ):
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 草稿实体编辑地址无效"
            )
        draft_id = match.group(1)
        return draft_id, f"https://post.smzdm.com/edit/{draft_id}"

    async def _load_draft_entities_once(
        self,
        *,
        wait_for_new_ids: frozenset[str] | None = None,
    ) -> dict[str, str]:
        """只导航一次草稿箱，在当前 DOM 内等待实体集合稳定。"""

        try:
            await self.page.goto(
                DRAFTS_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            previous_ids: frozenset[str] | None = None
            stable_samples = 0
            latest: dict[str, str] = {}
            for attempt in range(20):
                raw_urls = await self.page.evaluate(
                    """() => Array.from(document.querySelectorAll('.draft-list li'))
                        .map(item => Array.from(item.querySelectorAll('a'))
                            .find(link => (link.innerText || '').trim() === '继续编辑')
                            ?.href || '')
                        .filter(Boolean)"""
                )
                if not isinstance(raw_urls, list):
                    raw_urls = []
                latest = {}
                for raw_url in raw_urls:
                    draft_id, edit_url = self._validated_draft_entity(raw_url)
                    latest[draft_id] = edit_url

                current_ids = frozenset(latest)
                has_new_entity = (
                    wait_for_new_ids is None
                    or bool(current_ids - wait_for_new_ids)
                )
                if current_ids == previous_ids and has_new_entity:
                    stable_samples += 1
                else:
                    stable_samples = 0
                if stable_samples >= 1:
                    return latest
                previous_ids = current_ids
                if attempt + 1 < 20:
                    await asyncio.sleep(0.5)
            return latest
        except DraftResultUnknownError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: smzdm 打开草稿箱时浏览器已关闭"
                ) from exc
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 草稿实体查询失败"
            ) from exc

    async def _find_unique_new_draft(self) -> str:
        """从保存前后 ID 集合差值中锁定本次唯一新增草稿。"""

        baseline = self._preflight_draft_ids
        if baseline is None:
            raise DraftResultUnknownError(
                "DRAFT_BASELINE_UNAVAILABLE: smzdm 缺少保存前草稿 ID 基线"
            )
        entities = await self._load_draft_entities_once(wait_for_new_ids=baseline)
        new_ids = frozenset(entities) - baseline
        if len(new_ids) != 1:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 无法唯一确认本次新增草稿实体"
            )
        return entities[next(iter(new_ids))]

    async def _verify_persisted_draft(
        self,
        expected_title: str,
        edit_url: str,
    ) -> None:
        blocks = self._expected_persisted_blocks
        if blocks is None:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 缺少冻结内容核验快照"
            )
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
            await self.page.wait_for_selector(
                BODY_SELECTOR,
                state="visible",
                timeout=20000,
            )
            expected_tokens = self._expected_content_tokens(blocks)
            actual_tokens: list[dict] = []
            title_matched = False
            for attempt in range(15):
                title_field = self.page.locator(TITLE_SELECTOR).first
                actual_title = " ".join((await title_field.input_value()).split())
                title_matched = actual_title == expected_title
                if title_matched:
                    actual_tokens = await self._read_editor_dom_tokens()
                    if actual_tokens == expected_tokens:
                        return
                if attempt + 1 < 15:
                    await asyncio.sleep(2)
            if not title_matched:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: smzdm 草稿重开后标题不一致"
                )
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 草稿重开后图文结构不完整; "
                f"expected={self._token_shape(expected_tokens)}; "
                f"actual={self._token_shape(actual_tokens)}"
            )
        except DraftResultUnknownError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: smzdm 重开草稿时浏览器已关闭"
                ) from exc
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 草稿重开核验失败"
            ) from exc

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
