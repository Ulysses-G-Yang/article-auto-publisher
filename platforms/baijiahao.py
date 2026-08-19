"""百家号（百度创作平台）账号会话适配器。

登录态与身份验证链路（真实扫码 → BDUSS 会话 cookie → 身份捕获/DOM）。
图文投递只支持 DRAFT：正文使用 UEditor iframe，图片必须经正文图片弹窗
上传并确认；公开发布保持 fail-closed。

真实登录载体（百度 passport，扫码登录为默认 Tab）：
- 登录页：https://passport.baidu.com/v2/?login
- 二维码：约 138x138 的 ``img.tang-pass-qrcode-img``（passport 二维码接口）。
- 登录成功信号：.baidu.com 出现 BDUSS cookie（百度核心会话凭证）。
- 身份接口带 cookie/签名约束，采用「捕获页面自身响应」模式 + 首页 DOM 兜底。
"""

from __future__ import annotations

import asyncio
import copy
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    DraftBaselineError,
    DraftResultUnknownError,
    LoginRequiredError,
    PlatformAutomationError,
    SelectorError,
)
from platforms.content_validation import (
    ContentValidationError,
    extract_expected_paragraphs,
    safe_media_error,
)
from platforms.media_progress import safe_media_progress

LOGIN_URL = "https://passport.baidu.com/v2/?login"
HOME_URL = "https://baijiahao.baidu.com/"
CREATOR_HOME = "https://baijiahao.baidu.com/builder/rc/edit"
EDITOR_URL = "https://baijiahao.baidu.com/builder/rc/edit?type=news"
WORKS_URL = "https://baijiahao.baidu.com/builder/rc/content"
IMAGE_TRIGGER_SELECTOR = ".edui-for-insertimage"
IMAGE_MODAL_SELECTOR = ".cheetah-ui-pro-image-modal"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name", "nick"}
IDENTITY_UID_KEYS = {"uid", "user_id", "bjh_id", "id"}
IDENTITY_ENDPOINT_PATH = "/builder/app/appinfo"


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class BaijiahaoPlatform(BasePlatform):
    """百家号账号会话与 DRAFT-only 图文投递适配器。"""

    platform_name = "baijiahao"
    SESSION_COOKIE_NAMES = frozenset({"BDUSS"})
    LOGIN_POLL_ATTEMPTS = 40
    LOGIN_POLL_INTERVAL_SECONDS = 3

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None
        self._expected_persisted_blocks: list[dict] | None = None
        self._preflight_title = ""
        self._media_progress_state: dict[str, int] | None = None

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None

    # ==================== 登录态与身份 ====================

    async def _has_session_cookie_signal(self) -> bool:
        """BDUSS 会话 cookie 作为登录成功信号；不返回、不记录 cookie 值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies([
                "https://passport.baidu.com/",
                "https://baijiahao.baidu.com/",
            ])
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def check_login(self) -> bool:
        """只读验证现有 Profile；BDUSS 会话 cookie 出现才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("百家号登录态检测")
            await self.page.goto(
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(4)
            if await self._has_session_cookie_signal():
                return True
            self.last_login_error = "LOGIN_REQUIRED: 百家号账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "BAIJIAHAO_LOGIN_CHECK_ERROR: 百家号登录态验证失败"
            return False

    async def login(self):
        """打开百度扫码登录页，等待用户在隔离 Profile 中扫码登录。"""

        self._require_page_alive("百家号打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        try:
            await self.page.wait_for_function(
                """() => {
                    const imgs = Array.from(
                        document.querySelectorAll('img.tang-pass-qrcode-img')
                    );
                    return imgs.some(
                        (i) => i.naturalWidth > 100 && i.naturalWidth < 260
                    );
                }""",
                timeout=20000,
            )
        except Exception:
            pass
        await self._show_scan_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("百家号等待登录")
            if await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: 百家号登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_scan_hint(self):
        """页面顶部显示扫码提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'bjh-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#2932e1;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请用百度 App 扫码登录'
                        + '<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """从真实观察到的 appinfo 同源接口读取稳定 ID 与昵称。"""

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    parsed = urlsplit(response.url)
                    if (
                        response.request.resource_type not in ("xhr", "fetch")
                        or parsed.hostname != "baijiahao.baidu.com"
                        or parsed.path != IDENTITY_ENDPOINT_PATH
                        or not 200 <= response.status < 300
                    ):
                        return
                    found = self._extract_appinfo_identity(await response.json())
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
                    CREATOR_HOME,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                for _ in range(8):
                    if self._identity_payload is not None:
                        break
                    await asyncio.sleep(1)
                if self._identity_payload is None:
                    direct = await self.page.evaluate(
                        """async (path) => {
                            try {
                                const response = await fetch(path, {
                                    method: 'GET',
                                    credentials: 'include',
                                    headers: {Accept: 'application/json'},
                                });
                                if (!response.ok) return null;
                                return await response.json();
                            } catch (_) {
                                return null;
                            }
                        }""",
                        IDENTITY_ENDPOINT_PATH,
                    )
                    found = self._extract_appinfo_identity(direct)
                    if found:
                        self._identity_payload = {
                            "ok": True,
                            "user_id": found[0],
                            "display_name": found[1],
                        }
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
                    "BROWSER_CONTEXT_CLOSED: 百家号身份捕获时页面已关闭"
                ) from exc
            logger.warning("百家号身份捕获失败: {}", exc)

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        return {"ok": False, "user_id": "", "display_name": ""}

    @staticmethod
    def _extract_appinfo_identity(payload) -> tuple[str, str] | None:
        """只接受 ``data.user`` 中同时存在且互相一致的稳定身份。"""

        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            return None
        primary_id = str(user.get("userid") or "").strip()
        secondary_id = str(user.get("id") or "").strip()
        display_name = str(user.get("name") or "").strip()
        if primary_id and secondary_id and primary_id != secondary_id:
            return None
        user_id = primary_id or secondary_id
        if not user_id or not display_name:
            return None
        return user_id, display_name

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
            f"PLATFORM_NOT_IMPLEMENTED: 百家号{operation}能力尚未接入"
        )

    async def preflight_delivery(self, title: str) -> None:
        """在打开新编辑器前证明同名内容不存在，避免重复草稿。"""

        self._require_page_alive("百家号草稿基线检查")
        expected_title = self._normalize_title(title)
        if not expected_title:
            raise DraftBaselineError("DRAFT_BASELINE_FAILED: 百家号标题不能为空")
        try:
            await self._open_works_page()
            await self._search_works(expected_title)
            matches = await self._matching_work_rows(expected_title)
        except DraftBaselineError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号草稿基线检查时页面已关闭"
                ) from exc
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 百家号无法确认同名内容基线"
            ) from exc
        if matches:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 百家号已存在同名内容，禁止自动创建重复草稿"
            )
        self._preflight_title = expected_title

    @staticmethod
    def _normalize_title(value: str) -> str:
        return " ".join(str(value or "").split())

    async def _open_works_page(self) -> None:
        await self.page.goto(
            WORKS_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        search = self.page.locator(
            'input[placeholder*="输入标题关键字"]'
        ).first
        await search.wait_for(state="visible", timeout=20000)

    async def _search_works(self, title: str) -> None:
        search = self.page.locator(
            'input[placeholder*="输入标题关键字"]'
        ).first
        if await search.count() != 1 or not await search.is_visible():
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 百家号作品搜索框不可用"
            )
        await search.fill(title)
        await search.press("Enter")
        await self.simulator.random_delay(1.5, 2.5)

    async def _matching_work_rows(self, title: str) -> list[dict]:
        """返回标题精确匹配且同时包含“修改”动作的最小作品行。"""

        result = await self.page.evaluate(
            """title => {
                const visible = (el) => !!(
                    el && (el.offsetWidth || el.offsetHeight
                        || el.getClientRects().length)
                );
                const text = (el) => (el?.innerText || el?.textContent || '')
                    .replace(/\\s+/g, ' ').trim();
                const leaves = Array.from(
                    document.querySelectorAll('a, span, p, div, h1, h2, h3, h4')
                ).filter((el) => visible(el) && text(el) === title
                    && !Array.from(el.children).some((child) => text(child) === title));
                const rows = [];
                for (const leaf of leaves) {
                    let row = leaf;
                    while (row && row !== document.body) {
                        const actions = Array.from(
                            row.querySelectorAll('button, a, [role="button"], span')
                        ).filter(visible).map(text);
                        if (actions.includes('修改')) break;
                        row = row.parentElement;
                    }
                    if (!row || row === document.body || rows.includes(row)) continue;
                    rows.push(row);
                }
                return rows.map((row, index) => ({index, title}));
            }""",
            title,
        )
        if not isinstance(result, list):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 百家号作品列表结构无法验证"
            )
        return [item for item in result if isinstance(item, dict)]

    async def navigate_to_editor(self):
        """打开百家号图文编辑器（type=news），等待标题和正文都就绪。

        真实结构（2026-08 探测）：标题与正文均为 FeEditor contenteditable
        （标题占位「请输入标题（2 - 64字）」，正文占位「请输入正文」），
        底部有「存草稿」按钮，编辑器自动保存。存草稿按钮只能作为辅助
        页面信号，不能单独证明 UEditor iframe 正文 body 已经可编辑。
        """
        self._require_page_alive("百家号打开编辑器")
        try:
            await self.page.goto(
                "https://baijiahao.baidu.com/builder/rc/edit?type=news",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            # 多平台并发投递时页面加载变慢，保持 45 秒有界轮询；只有主页面
            # 标题编辑器可见且当前 UEditor body 可见/可编辑时才允许返回。
            await self._wait_for_editor_ready(timeout_seconds=45)
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号打开编辑器时页面已关闭"
                ) from exc
            raise SelectorError("百家号编辑器未就绪") from exc

    def _editors(self):
        return self.page.locator(
            "div[class*='FeEditorApp-'][contenteditable='true']:visible"
        )

    async def _title_editor_locator(self):
        """返回当前主页面可见标题编辑器；不以保存按钮代替正文就绪。"""

        editors = self._editors()
        count = await editors.count()
        for index in range(min(count, 30)):
            editor = editors.nth(index)
            if await editor.is_visible():
                return editor
        return None

    async def _wait_for_editor_ready(self, *, timeout_seconds: float):
        """有界等待标题编辑器和当前正文 iframe body 同时就绪。"""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            self._require_page_alive("百家号等待编辑器就绪")
            try:
                title_editor = await self._title_editor_locator()
                body_editor = await self._body_editor_locator()
                if title_editor is not None and body_editor is not None:
                    return title_editor, body_editor
            except BrowserLifecycleError:
                raise
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 百家号等待编辑器时页面已关闭"
                    ) from exc
                logger.debug(
                    "百家号编辑器仍未就绪: error_type={}",
                    type(exc).__name__,
                )
            remaining = deadline - loop.time()
            if remaining > 0:
                await asyncio.sleep(min(0.5, remaining))
        raise SelectorError(
            f"百家号标题或正文编辑器在 {timeout_seconds:g} 秒内未就绪"
        )

    async def _focus_editor(self, editor, label: str):
        if await editor.count() == 0 or not await editor.is_visible():
            raise RuntimeError(f"{label}编辑器不可见")
        try:
            await editor.click(timeout=5000)
        except Exception:
            await editor.evaluate("(el) => el.focus()")
        # 焦点检查必须在编辑器所属 frame 内执行（正文在 iframe 中）
        focused = await editor.evaluate(
            """() => {
                const el = document.activeElement;
                return el ? el.isContentEditable : false;
            }"""
        )
        if not focused:
            raise RuntimeError(f"{label}编辑器未能获得焦点")

    async def fill_title(self, title: str):
        """填写百家号标题（第一个 FeEditor contenteditable）。"""

        self._require_page_alive("百家号填写标题")
        try:
            editor = self._editors().first
            await self._focus_editor(editor, "标题")
            await self.simulator.random_delay(0.3, 0.8)
            try:
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
            except Exception:
                pass
            await self.page.keyboard.insert_text(str(title or "").strip())
            actual = await editor.inner_text()
            if (title or "").strip() and title.strip() not in actual:
                raise RuntimeError("标题回读不一致")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号填写标题时页面已关闭"
                ) from exc
            logger.error("百家号标题填写失败: {}", exc)
            raise SelectorError("百家号标题编辑器未找到或填写失败") from exc

    async def _body_editor_locator(self):
        """定位 UEditor 正文 iframe 内的可编辑 body。

        百家号正文是 UEditor，可编辑区在 iframe（body.view.news-editor-pc）
        内；标题在主页面的 FeEditor。返回 Playwright Locator 或 None。
        """
        try:
            frames = list(self.page.frames)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号读取正文 iframe 时页面已关闭"
                ) from exc
            return None
        for frame in frames:
            try:
                is_body = await frame.evaluate(
                    """() => {
                        const b = document.body;
                        return Boolean(
                            b && b.isContentEditable
                            && (b.className || '').includes('news-editor-pc')
                        );
                    }"""
                )
                if not is_body:
                    continue
                body = frame.locator("body")
                if await body.count() == 0 or not await body.is_visible():
                    continue
                try:
                    if not await body.is_editable():
                        continue
                except AttributeError:
                    # 真实 Playwright Locator 支持 is_editable；极简替身由
                    # frame.evaluate 的 isContentEditable 证据兜底。
                    pass
                return body
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 百家号读取正文 body 时页面已关闭"
                    ) from exc
                logger.debug(
                    "百家号正文 iframe 尚未可用: error_type={}",
                    type(exc).__name__,
                )
                continue
        return None

    async def fill_content(self, content_blocks: list, images: list):
        """按冻结 ContentVersion 的原始顺序写入 UEditor 图文并回读。"""

        self._require_page_alive("百家号填写正文")
        self._content_blocks = copy.deepcopy(content_blocks)
        self._expected_persisted_blocks = copy.deepcopy(content_blocks)
        expected_images = sum(
            1 for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "image"
        )
        self._media_progress_state = {
            "expected_images": expected_images,
            "uploaded_images": 0,
            "failed_image_count": 0,
        }
        editor = await self._current_body_editor()
        try:
            await self._focus_editor(editor, "正文")
            await editor.press("Control+A")
            await editor.press("Backspace")
            await self.simulator.random_delay(0.3, 0.8)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号清空正文时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "BAIJIAHAO_EDITOR_RESET_FAILED: 正文编辑器无法安全清空"
            ) from exc

        uploaded_images = 0
        failed_images: list[dict[str, str]] = []
        content_started = False
        paragraph_ready_after_image = False
        for block_index, block in enumerate(content_blocks):
            if not isinstance(block, dict):
                raise ContentValidationError(
                    "BAIJIAHAO_CONTENT_CONTRACT_INVALID: 正文块无效"
                )
            block_type = block.get("type")
            if block_type in {"text", "heading"}:
                text = str(block.get("text") or "").strip()
                if not text:
                    continue
                if block_type == "heading" and (
                    block.get("level") != 2 or "\n" in text or "\r" in text
                ):
                    raise ContentValidationError(
                        "BAIJIAHAO_HEADING_UNSUPPORTED: 仅支持单行二级标题"
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
                    "BAIJIAHAO_CONTENT_CONTRACT_INVALID: 未知正文块类型"
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
        return {
            "text_ok": True,
            "expected_images": expected_images,
            "uploaded_images": uploaded_images,
            "failed_images": failed_images,
            "media_status": media_status,
            "media_error": media_error,
        }

    async def _current_body_editor(self):
        editor = await self._body_editor_locator()
        if editor is None or await editor.count() == 0 or not await editor.is_visible():
            raise SelectorError("百家号正文编辑器未找到或当前不可见")
        return editor

    async def _place_body_caret_at_end(self) -> None:
        editor = await self._current_body_editor()
        try:
            await editor.press("Control+End")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号移动正文光标时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "BAIJIAHAO_CARET_POSITION_FAILED: 正文末尾光标定位失败"
            ) from exc

    async def _apply_h2_to_current_block(self) -> None:
        """把 Word H2 映射到百家号字号菜单中的“标题”。"""

        try:
            trigger = self.page.locator(".edui-for-customfontsize:visible")
            visible = [
                trigger.nth(index)
                for index in range(await trigger.count())
                if await trigger.nth(index).is_visible()
            ]
            if len(visible) != 1:
                raise RuntimeError("标题格式入口不唯一")
            await visible[0].click(timeout=5000)
            options = self.page.get_by_text("标题", exact=True)
            candidates = [
                options.nth(index)
                for index in range(await options.count())
                if await options.nth(index).is_visible()
            ]
            if len(candidates) != 1:
                raise RuntimeError("标题格式选项不唯一")
            await candidates[0].click(timeout=5000)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号设置标题样式时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "BAIJIAHAO_HEADING_APPLY_FAILED: 二级标题样式未能应用"
            ) from exc

    async def _create_paragraph_after_image(self) -> None:
        editor = await self._current_body_editor()
        try:
            await editor.press("Control+End")
            await self.page.keyboard.press("ArrowDown")
            await self.page.keyboard.press("ArrowRight")
            await self.page.keyboard.press("Enter")
            tail_ready = bool(
                await editor.evaluate(
                    """root => {
                        const tail = root.lastElementChild;
                        return !!tail && tail.tagName.toLowerCase() === 'p'
                            && tail.querySelectorAll('img').length === 0;
                    }"""
                )
            )
            if not tail_ready:
                raise RuntimeError("图片后正文段落未建立")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号图片后创建段落时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "BAIJIAHAO_POST_IMAGE_PARAGRAPH_FAILED: 图片后无法建立正文插入点"
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
                        {"kind": "heading", "level": 2,
                         "text": paragraph.comparison_text}
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
                        const sized = [node, ...node.querySelectorAll('*')].some((el) => {
                            const inline = parseFloat(el.style?.fontSize || '0');
                            return Number.isFinite(inline) && inline >= 20;
                        });
                        tokens.push({
                            kind: /^h[1-6]$/.test(tag) || sized ? 'heading' : 'text',
                            level: /^h[1-6]$/.test(tag) || sized ? 2 : 0,
                            text,
                        });
                    }
                    return tokens;
                }"""
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号读取正文 DOM 时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "BAIJIAHAO_CONTENT_DOM_VERIFY_FAILED: 正文 DOM 回读失败"
            ) from exc
        if not isinstance(raw, list):
            raise ContentValidationError(
                "BAIJIAHAO_CONTENT_DOM_VERIFY_FAILED: DOM 序列无效"
            )
        normalized: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ContentValidationError(
                    "BAIJIAHAO_CONTENT_DOM_VERIFY_FAILED: DOM token 无效"
                )
            kind = item.get("kind")
            if kind == "image":
                normalized.append({"kind": "image"})
                continue
            paragraphs = extract_expected_paragraphs(
                [{"type": "text", "text": str(item.get("text") or "")}]
            )
            for paragraph in paragraphs:
                if kind == "heading":
                    normalized.append(
                        {"kind": "heading", "level": 2,
                         "text": paragraph.comparison_text}
                    )
                elif kind == "text":
                    normalized.append(
                        {"kind": "text", "text": paragraph.comparison_text}
                    )
                else:
                    raise ContentValidationError(
                        "BAIJIAHAO_CONTENT_DOM_VERIFY_FAILED: DOM token 类型无效"
                    )
        return normalized

    @staticmethod
    def _token_shape(tokens: list[dict]) -> str:
        return ",".join(
            "I" if token.get("kind") == "image"
            else f"H2:{len(token.get('text') or '')}"
            if token.get("kind") == "heading"
            else f"T:{len(token.get('text') or '')}"
            for token in tokens[:40]
        ) or "EMPTY"

    async def _validate_dom_prefix(self, blocks: list[dict], *, phase: str) -> None:
        expected = self._expected_content_tokens(blocks)
        actual = await self._read_editor_dom_tokens()
        if actual[: len(expected)] != expected:
            raise ContentValidationError(
                f"CONTENT_VALIDATION_ERROR: 百家号{phase}图文顺序不完整; "
                f"expected={self._token_shape(expected)}; "
                f"actual={self._token_shape(actual)}"
            )

    async def _validate_dom_exact(self, blocks: list[dict], *, phase: str) -> None:
        expected = self._expected_content_tokens(blocks)
        actual = await self._read_editor_dom_tokens()
        if actual != expected:
            raise ContentValidationError(
                f"CONTENT_VALIDATION_ERROR: 百家号{phase}图文顺序不完整; "
                f"expected={self._token_shape(expected)}; "
                f"actual={self._token_shape(actual)}"
            )

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

    async def _upload_image(self, image_path: str) -> dict:
        """打开正文图片弹窗，单次上传并验证 UEditor 图片稳定增加。"""

        self._require_page_alive("百家号上传图片")
        try:
            before = await self._count_body_images()
            triggers = self.page.locator(f"{IMAGE_TRIGGER_SELECTOR}:visible")
            candidates = [
                triggers.nth(index)
                for index in range(await triggers.count())
                if await triggers.nth(index).is_visible()
            ]
            if len(candidates) != 1:
                return {
                    "success": False,
                    "error_code": "BAIJIAHAO_BODY_IMAGE_TRIGGER_AMBIGUOUS",
                    "error": "百家号正文图片入口不存在或候选不唯一，已安全停止",
                }
            await candidates[0].click(timeout=5000)
            await self.simulator.random_delay(0.5, 1)
            modals = self.page.locator(f"{IMAGE_MODAL_SELECTOR}:visible")
            visible_modals = [
                modals.nth(index)
                for index in range(await modals.count())
                if await modals.nth(index).is_visible()
            ]
            if len(visible_modals) != 1:
                return {
                    "success": False,
                    "error_code": "BAIJIAHAO_BODY_IMAGE_MODAL_AMBIGUOUS",
                    "error": "百家号正文图片弹窗不存在或候选不唯一，已安全停止",
                }
            modal = visible_modals[0]
            inputs = modal.locator('input[type="file"][accept*="image"]')
            if await inputs.count() != 1:
                return {
                    "success": False,
                    "error_code": "BAIJIAHAO_BODY_IMAGE_INPUT_AMBIGUOUS",
                    "error": "百家号正文图片控件不存在或候选不唯一，已安全停止",
                }
            await inputs.first.set_input_files(str(image_path), timeout=15000)

            confirm = modal.get_by_text("确认", exact=True)
            confirm_button = None
            for _ in range(40):
                visible = [
                    confirm.nth(index)
                    for index in range(await confirm.count())
                    if await confirm.nth(index).is_visible()
                ]
                enabled = []
                for candidate in visible:
                    try:
                        if await candidate.is_enabled():
                            enabled.append(candidate)
                    except AttributeError:
                        enabled.append(candidate)
                if len(enabled) == 1:
                    confirm_button = enabled[0]
                    break
                await asyncio.sleep(0.5)
            if confirm_button is None:
                return {
                    "success": False,
                    "error_code": "BAIJIAHAO_BODY_IMAGE_CONFIRM_NOT_READY",
                    "error": "百家号正文图片上传后确认控件未就绪",
                }
            await confirm_button.click(timeout=5000)

            stable_streak = 0
            observed = before
            for _ in range(40):
                await asyncio.sleep(0.5)
                observed = await self._count_body_images()
                remote_ready = await self._new_body_images_ready(before)
                if observed > before and remote_ready:
                    stable_streak += 1
                else:
                    stable_streak = 0
                if stable_streak >= 3:
                    break
            if observed <= before or stable_streak < 3:
                return {
                    "success": False,
                    "error_code": "BAIJIAHAO_EDITOR_IMAGE_COUNT_UNCHANGED",
                    "error": "上传后百家号正文图片数量未稳定增加",
                }
            return {"success": True, "error": ""}
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号上传图片时页面已关闭"
                ) from exc
            return {
                "success": False,
                "error_code": "BAIJIAHAO_IMAGE_UPLOAD_FAILED",
                "error": safe_media_error(exc, fallback="百家号图片上传失败"),
            }

    async def _count_body_images(self) -> int:
        editor = await self._current_body_editor()
        return int(await editor.locator("img").count())

    async def _new_body_images_ready(self, before: int) -> bool:
        editor = await self._current_body_editor()
        return bool(
            await editor.evaluate(
                """(root, before) => {
                    const images = Array.from(root.querySelectorAll('img')).slice(before);
                    return images.length > 0 && images.every((image) => {
                        const src = image.getAttribute('src') || '';
                        return image.complete && image.naturalWidth > 0
                            && (src.startsWith('http://')
                                || src.startsWith('https://')
                                || src.startsWith('//'));
                    });
                }""",
                before,
            )
        )

    async def set_cover(self) -> dict:
        """通过「选择封面」弹窗上传本地图片设为封面（3:2 预览后确定）。

        2026-08 真实验收结论：**自动化环境下封面上传不生效**——
        「点击本地上传」可触发 filechooser，set_files 后封面预览始终为空
        （弹窗保持「暂无标题」），点「确定」不关闭弹窗；换图重试一致。
        判定为百度侧对自动化环境的图片上传限制，如实失败、绝不假成功。
        用户手动操作可正常设置封面。
        """

        self._require_page_alive("百家号设置封面")
        clicked = await self.page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('*'));
                const target = nodes.find(el => {
                    const t = (el.innerText || '').trim();
                    return t === '选择封面' && el.children.length === 0;
                });
                if (!target) return 'not-found';
                target.click();
                return 'clicked';
            }"""
        )
        if clicked != "clicked":
            return {"success": False, "error": "百家号「选择封面」按钮未找到"}
        await self.simulator.random_delay(1, 2)

        # 弹窗出现后点「点击本地上传」触发系统文件选择，用 filechooser 上传
        # （隐藏控件直接 set_input_files 无效，2026-08 实测上传不落图）
        first_image = ""
        try:
            blocks = getattr(self, "_content_blocks", []) or []
            for block in blocks:
                if block.get("type") == "image" and block.get("local_path"):
                    first_image = str(block["local_path"])
                    break
        except Exception:  # noqa: BLE001
            pass
        if not first_image:
            return {"success": False, "error": "百家号封面缺少本地图片素材"}
        try:
            async with self.page.expect_file_chooser(timeout=15000) as fc_info:
                upload_clicked = await self.page.evaluate(
                    """() => {
                        const nodes = Array.from(document.querySelectorAll('*'));
                        const target = nodes.find(el => {
                            const t = (el.innerText || '').trim();
                            return t === '点击本地上传' && el.children.length === 0;
                        });
                        if (!target) return 'not-found';
                        const clickable = target.closest(
                            '[class*="btn" i], [role="button"], [class*="upload" i], label, div'
                        );
                        if (clickable && clickable !== target) {
                            clickable.click();
                            return 'clicked';
                        }
                        target.click();
                        return 'clicked';
                    }"""
                )
            file_chooser = await fc_info.value
            await file_chooser.set_files(first_image)
        except TimeoutError:
            return {
                "success": False,
                "error": f"百家号封面未触发文件选择（upload={upload_clicked}）",
            }
        await self.simulator.random_delay(3, 5)

        # 等待封面预览出现后点「确定」（可见按钮中最后一个；可能有图片确认+封面确定两步）
        confirmed = await self.page.evaluate(
            """() => {
                const nodes = Array.from(
                    document.querySelectorAll('button, [role="button"], [class*="btn" i]')
                );
                const visible = nodes.filter(
                    (el) => (el.innerText || '').trim() === '确定'
                        && el.offsetParent !== null
                );
                const target = visible[visible.length - 1] || null;
                if (target) { target.click(); return 'clicked'; }
                return 'no-confirm';
            }"""
        )
        await self.simulator.random_delay(2, 3)

        # 成功判据：封面上传弹窗关闭（弹窗含两步：图片「确认」→ 封面「确定」）
        dialog_closed = False
        for _ in range(6):
            dialog_open = await self.page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    return nodes.some(el => {
                        const t = (el.innerText || '').trim();
                        return t === '点击本地上传'
                            && el.children.length === 0
                            && el.offsetParent !== null;
                    });
                }"""
            )
            if not dialog_open:
                dialog_closed = True
                break
            # 弹窗仍在：依次点可见的「确认」/「确定」（最后一个可见按钮）
            await self.page.evaluate(
                """() => {
                    const nodes = Array.from(
                        document.querySelectorAll('button, [role="button"], [class*="btn" i]')
                    );
                    const visible = nodes.filter(
                        (el) => ['确认', '确定'].includes((el.innerText || '').trim())
                            && el.offsetParent !== null
                    );
                    const target = visible[visible.length - 1] || null;
                    if (target) target.click();
                }"""
            )
            await self.simulator.random_delay(2, 3)
        if not dialog_closed:
            return {
                "success": False,
                "error": f"百家号封面上传弹窗未关闭（confirm={confirmed}）",
            }
        logger.info("百家号封面已设置（本地首图）")
        return {"success": True, "error": ""}

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """百家号保存草稿不需要话题；公开话题选择尚未接入，如实报告。"""

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
            "error": "百家号话题选择尚未接入（保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """精确点击“存草稿”，再按唯一标题重开并核验冻结图文。"""

        self._require_page_alive("百家号保存草稿")
        expected_title = self._normalize_title(title)
        if not expected_title or expected_title != self._preflight_title:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号缺少与本次一致的唯一标题基线"
            )
        if self._expected_persisted_blocks is None:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号缺少冻结内容核验快照"
            )
        try:
            buttons = self.page.get_by_text("存草稿", exact=True)
            visible = [
                buttons.nth(index)
                for index in range(await buttons.count())
                if await buttons.nth(index).is_visible()
            ]
            if len(visible) != 1:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 百家号精确存草稿按钮不存在或不唯一"
                )
            await visible[0].click(timeout=5000)
            await self.simulator.random_delay(2, 4)
        except DraftResultUnknownError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 百家号存草稿期间浏览器已关闭"
                ) from exc
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号存草稿点击结果无法确认"
            ) from exc

        try:
            edit_url = await self._find_unique_exact_draft(expected_title)
            await self._verify_persisted_draft(expected_title, edit_url)
            return edit_url
        except DraftResultUnknownError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 百家号核验草稿时浏览器已关闭"
                ) from exc
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号持久化草稿核验失败"
            ) from exc

    async def _find_unique_exact_draft(self, title: str) -> str:
        await self._open_works_page()
        matches: list[dict] = []
        for attempt in range(6):
            await self._search_works(title)
            matches = await self._matching_work_rows(title)
            if len(matches) == 1:
                break
            if attempt + 1 < 6:
                await self.page.reload(wait_until="domcontentloaded", timeout=30000)
                await self.simulator.random_delay(1, 2)
        if len(matches) != 1:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号未找到标题精确匹配的唯一草稿"
            )
        clicked = await self.page.evaluate(
            """title => {
                const visible = (el) => !!(
                    el && (el.offsetWidth || el.offsetHeight
                        || el.getClientRects().length)
                );
                const text = (el) => (el?.innerText || el?.textContent || '')
                    .replace(/\\s+/g, ' ').trim();
                const leaf = Array.from(
                    document.querySelectorAll('a, span, p, div, h1, h2, h3, h4')
                ).find((el) => visible(el) && text(el) === title
                    && !Array.from(el.children).some((child) => text(child) === title));
                if (!leaf) return false;
                let row = leaf;
                while (row && row !== document.body) {
                    const action = Array.from(
                        row.querySelectorAll('button, a, [role="button"], span')
                    ).find((el) => visible(el) && text(el) === '修改');
                    if (action) { action.click(); return true; }
                    row = row.parentElement;
                }
                return false;
            }""",
            title,
        )
        if not clicked:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号唯一草稿缺少精确修改入口"
            )
        await self.page.wait_for_url(
            re.compile(r"https://baijiahao\.baidu\.com/builder/rc/edit(?:\?|$)"),
            timeout=20000,
        )
        return self._safe_draft_url(self.page.url)

    @staticmethod
    def _safe_draft_url(value: str) -> str:
        parts = urlsplit(str(value or ""))
        if (
            parts.scheme != "https"
            or parts.netloc != "baijiahao.baidu.com"
            or parts.path != "/builder/rc/edit"
            or parts.username
            or parts.password
        ):
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号草稿编辑地址无效"
            )
        safe_query: list[tuple[str, str]] = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            if re.search(r"token|cookie|auth|session|bduss", key, re.I):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 百家号草稿编辑地址包含敏感参数"
                )
            if len(key) > 80 or len(item) > 256:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 百家号草稿编辑地址参数异常"
                )
            safe_query.append((key, item))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(safe_query), "")
        )

    async def _verify_persisted_draft(self, title: str, edit_url: str) -> None:
        blocks = self._expected_persisted_blocks
        if blocks is None:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号缺少冻结内容核验快照"
            )
        await self.page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
        await self._wait_for_editor_ready(timeout_seconds=45)
        expected_tokens = self._expected_content_tokens(blocks)
        actual_tokens: list[dict] = []
        title_matches = False
        for attempt in range(20):
            title_editor = await self._title_editor_locator()
            if title_editor is not None:
                actual_title = self._normalize_title(await title_editor.inner_text())
                title_matches = actual_title == title
            if title_matches:
                actual_tokens = await self._read_editor_dom_tokens()
                if actual_tokens == expected_tokens:
                    return
            if attempt + 1 < 20:
                await asyncio.sleep(1.5)
        if not title_matches:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号草稿重开后标题不一致"
            )
        raise DraftResultUnknownError(
            "DRAFT_RESULT_UNKNOWN: 百家号草稿重开后图文结构不完整; "
            f"expected={self._token_shape(expected_tokens)}; "
            f"actual={self._token_shape(actual_tokens)}"
        )

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
