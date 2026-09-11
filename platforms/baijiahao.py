"""百家号（百度创作平台）账号会话适配器。

登录态与身份验证链路（真实扫码 → BDUSS 会话 cookie → 身份捕获/DOM）。
正文使用 UEditor iframe，图片必须经正文图片弹窗上传并确认。
公开投稿仅接受当前已核验草稿的单次原生提交及明确接收回执。

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
    DraftVerificationEvidence,
    LoginRequiredError,
    PlatformAutomationError,
    PublishResultUnknownError,
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
    """百家号账号会话、图文草稿与受控公开投稿适配器。"""

    platform_name = "baijiahao"
    SESSION_COOKIE_NAMES = frozenset({"BDUSS"})
    LOGIN_POLL_ATTEMPTS = 40
    LOGIN_POLL_INTERVAL_SECONDS = 3
    PUBLICATION_RESPONSE_PATH = "/pcui/article/publish"
    PUBLICATION_TIMEOUT_MS = 45000
    PUBLICATION_EXTRA_MODES = (
        "timer_time", "online_modify", "only_modify_goods", "replace_publish",
        "is_pay", "is_pay_training_camp", "is_pay_subscribe", "is_pay_mvp", "pay_read_type",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None
        self._expected_persisted_blocks: list[dict] | None = None
        self._expected_persisted_cover = False
        self._bound_draft_id = ""
        self._publication_preview_required: bool | None = None
        self._publication_preview_draft_id = ""
        self._preflight_title = ""
        self._preflight_matching_draft_count = 0
        self._preflight_draft_ids: frozenset[str] = frozenset()
        self._last_draft_entity_bound: bool | None = None
        self._last_draft_entity_source: str | None = None
        self._last_draft_match_count: int | None = None
        self._media_progress_state: dict[str, int] | None = None

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None
        self._publication_preview_required = None
        self._publication_preview_draft_id = ""
        self.page.on("response", self._capture_publication_capability)

    async def _capture_publication_capability(self, response) -> None:
        """从当前草稿的原生只读响应确认是否要求额外的预览保存。"""
        try:
            parsed = urlsplit(response.url)
            if (
                parsed.scheme != "https" or parsed.netloc != "baijiahao.baidu.com"
                or parsed.path != "/pcui/article/edit" or response.request.method != "GET"
                or not 200 <= response.status < 300
            ):
                return
            pairs = parse_qsl(parsed.query)
            ids = [v for k, v in pairs if k == "article_id"]
            if len(ids) != 1 or [v for k, v in pairs if k == "type"] != ["news"]:
                return
            payload = await response.json()
            if not isinstance(payload, dict) or type(payload.get("errno")) not in {int, str}:
                return
            if payload["errno"] not in (0, "0"):
                return
            data = payload.get("data")
            ability = data.get("ability") if isinstance(data, dict) else None
            value = ability.get("is_preview_gray") if isinstance(ability, dict) else None
            if type(value) not in {int, str} or value not in (0, 1, "0", "1"):
                return
            self._publication_preview_required = str(value) == "1"
            self._publication_preview_draft_id = ids[0]
        except Exception:
            # Missing capability is a pre-click stop, never a guess about publication.
            return

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
        """记录同名草稿实体基线，但允许平台创建同名新草稿。"""

        self._require_page_alive("百家号草稿基线检查")
        self._preflight_title = ""
        self._preflight_matching_draft_count = 0
        self._preflight_draft_ids = frozenset()
        self._last_draft_entity_bound = None
        self._last_draft_entity_source = None
        self._last_draft_match_count = None
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
        self._preflight_matching_draft_count = len(matches)
        self._preflight_draft_ids = frozenset(
            draft_id
            for draft_id in (
                self._preview_article_id(str(match.get("preview_href") or ""))
                for match in matches
            )
            if draft_id
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
        await self._select_draft_tab()
        await search.wait_for(state="visible", timeout=20000)

    async def _select_draft_tab(self) -> None:
        """进入唯一的草稿子页，防止在默认作品列表中误判保存结果。"""

        tabs = self.page.get_by_role("tab", name="草稿", exact=True)
        visible = [
            tabs.nth(index)
            for index in range(await tabs.count())
            if await tabs.nth(index).is_visible()
        ]
        if len(visible) != 1:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 百家号草稿标签不存在或不唯一"
            )
        tab = visible[0]
        await tab.click(timeout=5000)
        for _ in range(30):
            selected = str(await tab.get_attribute("aria-selected") or "").lower()
            class_name = str(await tab.get_attribute("class") or "").lower()
            if selected == "true" or "active" in class_name:
                return
            await asyncio.sleep(0.1)
        raise DraftBaselineError(
            "DRAFT_BASELINE_FAILED: 百家号草稿标签点击后未激活"
        )

    async def _search_works(self, title: str) -> None:
        search = self.page.locator(
            'input[placeholder*="输入标题关键字"]'
        ).first
        if await search.count() != 1 or not await search.is_visible():
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 百家号作品搜索框不可用"
            )
        await search.fill(title)
        # 真实页面是受控输入 + 防抖查询。按 Enter 会触发表单默认行为并把
        # 已正确过滤的草稿行清空；填值后等待防抖完成即可。
        await asyncio.sleep(4)

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
                const rows = Array.from(document.querySelectorAll(
                    'div[class*="articleItem"]'
                )).filter((row) => {
                    if (!visible(row)) return false;
                    const hasExactTitle = Array.from(row.querySelectorAll(
                        'a, span, p, div, h1, h2, h3, h4'
                    )).some((el) => visible(el) && text(el) === title
                        && !Array.from(el.children).some(
                            (child) => text(child) === title
                        ));
                    const actions = Array.from(row.querySelectorAll(
                        'button, a, [role="button"], span'
                    )).filter(visible).map(text);
                    return hasExactTitle && actions.includes('修改');
                });
                return rows.map((row, index) => {
                    const preview = Array.from(row.querySelectorAll('a[href]'))
                        .find((link) => {
                            try {
                                const rawHref = link.getAttribute('href') || '';
                                const parsed = new URL(rawHref, location.href);
                                const values = Array.from(parsed.searchParams.entries());
                                return /^https?:\\/\\//i.test(rawHref)
                                    && ['http:', 'https:'].includes(parsed.protocol)
                                    && parsed.hostname === 'baijiahao.baidu.com'
                                    && parsed.port === ''
                                    && parsed.username === ''
                                    && parsed.password === ''
                                    && parsed.pathname === '/builder/preview/s'
                                    && parsed.hash === ''
                                    && values.length === 1
                                    && values[0][0] === 'id'
                                    && /^[A-Za-z0-9_-]{1,128}$/.test(values[0][1]);
                            } catch (_) {
                                return false;
                            }
                        });
                    return {
                        index,
                        title,
                        preview_href: preview
                            ? (preview.getAttribute('href') || '')
                            : '',
                    };
                });
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
                else:
                    await self._apply_body_to_current_block()
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

    async def _current_body_editor(self, *, timeout_seconds: float = 10):
        """重新解析当前 UEditor iframe，容忍格式/图片操作造成的短暂重建。"""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            self._require_page_alive("百家号定位当前正文编辑器")
            try:
                editor = await self._body_editor_locator()
                if (
                    editor is not None
                    and await editor.count() > 0
                    and await editor.is_visible()
                ):
                    return editor
            except BrowserLifecycleError:
                raise
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 百家号定位正文时页面已关闭"
                    ) from exc
            remaining = deadline - loop.time()
            if remaining > 0:
                await asyncio.sleep(min(0.25, remaining))
        raise SelectorError("百家号正文编辑器在 10 秒内未重新就绪")

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
        """把 Word H2 映射到百家号当前正文段落的 21px 标题样式。

        百家号 2026 年编辑器的字号下拉框由 React 门户渲染。真实验收确认，
        点击“标题”菜单项会重置整块编辑器并短暂移除正文 iframe，导致随后
        的段落无法继续写入。UEditor 的正文仍以 iframe ``body`` DOM 作为
        保存源，因此这里把样式严格施加到当前选区所属的根级段落，并派发
        ``input``/``selectionchange``，避免依赖不稳定的外围菜单实现。

        该方法只改变当前段落样式，不改文字、不移动段落，也不影响冻结内容
        版本的哈希。若无法唯一确定当前根级段落则失败关闭。
        """

        try:
            applied = await self._set_current_block_font_size("21px")
            if applied is not True:
                raise RuntimeError("当前标题段落无法唯一定位")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号设置标题样式时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "BAIJIAHAO_HEADING_APPLY_FAILED: 二级标题样式未能应用"
            ) from exc

    async def _apply_body_to_current_block(self) -> None:
        """清除 Enter 从上一标题段继承的字号，恢复平台正文样式。"""

        try:
            applied = await self._set_current_block_font_size(None)
            if applied is not True:
                raise RuntimeError("当前正文段落无法唯一定位")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号恢复正文样式时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "BAIJIAHAO_BODY_STYLE_RESET_FAILED: 正文样式未能恢复"
            ) from exc

    async def _set_current_block_font_size(self, font_size: str | None) -> bool:
        """只修改当前选区所属的根级段落字号。"""

        editor = await self._current_body_editor()
        return bool(
            await editor.evaluate(
                r"""(root, fontSize) => {
                    const doc = root.ownerDocument;
                    const selection = doc.getSelection();
                    let node = selection && selection.anchorNode;
                    if (node && node.nodeType === Node.TEXT_NODE) {
                        node = node.parentElement;
                    }
                    let block = node instanceof Element ? node : null;
                    while (block && block.parentElement !== root) {
                        block = block.parentElement;
                    }
                    if (!block || block.parentElement !== root
                            || !(block.innerText || block.textContent || '').trim()) {
                        return false;
                    }
                    if (fontSize) {
                        block.style.fontSize = fontSize;
                    } else {
                        block.style.removeProperty('font-size');
                        for (const child of block.querySelectorAll('[style]')) {
                            child.style.removeProperty('font-size');
                            if (!child.getAttribute('style')) {
                                child.removeAttribute('style');
                            }
                        }
                    }
                    root.dispatchEvent(new InputEvent('input', {
                        bubbles: true,
                        composed: true,
                        inputType: 'formatFontSize',
                    }));
                    doc.dispatchEvent(new Event('selectionchange', {bubbles: true}));
                    return fontSize
                        ? block.style.fontSize === fontSize
                        : block.style.fontSize === '';
                }""",
                font_size,
            )
        )

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
                r"""root => {
                    const tokens = [];
                    for (const node of root.children) {
                        const className = typeof node.className === 'string'
                            ? node.className : '';
                        if (className.includes('bjh-image-caption')) {
                            continue;
                        }
                        const images = node.querySelectorAll('img');
                        if (images.length) {
                            for (const _image of images) tokens.push({kind: 'image'});
                            continue;
                        }
                        const sentinelText = (node.textContent || '').replace(
                            /[\s\p{M}\p{Cf}\p{Cc}\uFFFC]/gu,
                            '',
                        );
                        const touchesImage = Boolean(
                            node.previousElementSibling?.querySelector('img')
                            || node.nextElementSibling?.querySelector('img')
                        );
                        const range = root.ownerDocument.createRange();
                        range.selectNodeContents(node);
                        const renderedWidth = Math.max(
                            0,
                            ...Array.from(range.getClientRects()).map(
                                (rect) => rect.width
                            ),
                        );
                        const shortTextLength = Array.from(
                            node.innerText || node.textContent || ''
                        ).length;
                        const zeroWidthParagraph = node.tagName.toLowerCase() === 'p'
                            && shortTextLength <= 2 && renderedWidth < 0.5;
                        if ((!sentinelText && (node.querySelector('br') || touchesImage))
                                || zeroWidthParagraph) {
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
            # UEditor 会在图片相邻段落中残留 U+FFFC（对象替换字符）。图片本身
            # 已由独立 image token 严格计数；若把该占位符混进正文，会造成
            # “平台比冻结文本多 1 字符”的假失败。只在 DOM 比较视图中移除，
            # 不修改冻结 ContentVersion，也不放宽真实文字比较。
            comparison_text = str(item.get("text") or "").replace("\uFFFC", "")
            paragraphs = extract_expected_paragraphs(
                [{"type": "text", "text": comparison_text}]
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

    async def apply_cover(self, cover: dict | None = None) -> dict:
        """把内容版本冻结的封面素材交给百家号封面弹窗。"""

        self._expected_persisted_cover = False
        strategy = str((cover or {}).get("strategy") or "NONE").upper()
        if strategy == "NONE":
            return {"success": True, "cover_status": "not_required"}
        if strategy not in {"FIRST_BODY_IMAGE", "EXPLICIT"}:
            return {
                "success": False,
                "cover_status": "failed",
                "error_code": "BAIJIAHAO_COVER_STRATEGY_INVALID",
                "error": "百家号收到不受支持的封面策略",
            }
        local_path = str((cover or {}).get("local_path") or "")
        if not local_path or not Path(local_path).is_file():
            return {
                "success": False,
                "cover_status": "failed",
                "error_code": "BAIJIAHAO_COVER_ASSET_UNAVAILABLE",
                "error": "百家号封面素材不可用",
            }
        result = await self.set_cover(local_path)
        self._expected_persisted_cover = bool(
            result.get("success") and result.get("cover_status") == "completed"
        )
        return result

    async def set_cover(self, image_path: str) -> dict:
        """通过「选择封面」弹窗上传本地图片设为封面（3:2 预览后确定）。

        2026-08-20 真实探测确认：编辑器存在唯一「选择封面」入口；已有
        正文图片时弹窗会同时挂载 cropper 与 local-upload 两个 image input。
        上传后 React 会清空 ``input.files``，确认文案为「确定 (N)」；因此
        必须以视觉指纹变化、唯一启用确认动作与弹窗完整关闭为判据。
        """

        self._require_page_alive("百家号设置封面")
        try:
            trigger = await self._unique_visible_text("选择封面", self.page)
            if trigger is None:
                return await self._cover_failure(
                    "BAIJIAHAO_COVER_TRIGGER_NOT_FOUND",
                    "百家号「选择封面」按钮未找到或候选不唯一",
                )
            await trigger.click(timeout=5000)
            modal = await self._wait_for_cover_modal()
            if modal is None:
                return await self._cover_failure(
                    "BAIJIAHAO_COVER_MODAL_NOT_READY",
                    "百家号封面弹窗未唯一就绪",
                )

            # 真实 DOM 已确认弹窗内有唯一隐藏 image file input。直接向这个
            # 受限控件传入冻结素材，避免点击装饰性上传区后等待一个不会触发的
            # filechooser，也绝不回退到页面上的视频/其他 file input。
            cover_input = await self._wait_for_unique_cover_input(modal)
            if cover_input is None:
                return await self._cover_failure(
                    "BAIJIAHAO_COVER_INPUT_AMBIGUOUS",
                    "百家号封面图片控件不存在或候选不唯一",
                )
            baseline = await self._cover_preview_state(modal)
            await cover_input.set_input_files(str(image_path), timeout=15000)
            if not await self._wait_for_cover_preview(modal, baseline):
                return await self._cover_failure(
                    "BAIJIAHAO_COVER_PREVIEW_NOT_READY",
                    "百家号封面素材已选择，但 3:2 预览未就绪",
                )
            if not await self._confirm_cover_dialogs():
                return await self._cover_failure(
                    "BAIJIAHAO_COVER_RESULT_UNVERIFIED",
                    "百家号封面确认流程未完整关闭",
                )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号设置封面时页面已关闭"
                ) from exc
            return await self._cover_failure(
                "BAIJIAHAO_COVER_UPLOAD_FAILED",
                safe_media_error(exc, fallback="百家号封面上传失败"),
            )

        logger.info("百家号封面已设置（冻结封面素材）")
        return {
            "success": True,
            "cover_status": "completed",
            "safe_to_continue": True,
            "error_code": None,
            "error": "",
        }

    @staticmethod
    async def _visible_items(locator) -> list:
        return [
            locator.nth(index)
            for index in range(await locator.count())
            if await locator.nth(index).is_visible()
        ]

    async def _unique_visible_text(self, text: str, root):
        candidates = await self._visible_items(root.get_by_text(text, exact=True))
        return candidates[0] if len(candidates) == 1 else None

    async def _wait_for_cover_modal(self, *, timeout_seconds: float = 10):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            self._require_page_alive("百家号等待封面弹窗")
            candidates = []
            for modal in await self._visible_items(
                self.page.locator(".cheetah-modal")
            ):
                text = re.sub(r"\s+", "", str(await modal.inner_text()))
                if "封面预览" in text and "本地上传" in text:
                    candidates.append(modal)
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                return None
            await asyncio.sleep(0.25)
        return None

    async def _wait_for_unique_cover_input(
        self, modal, *, timeout_seconds: float = 10
    ):
        """等待 React 在唯一封面 modal 中延迟挂载 image file input。"""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        current = modal
        while loop.time() < deadline:
            self._require_page_alive("百家号等待封面图片控件")
            try:
                inputs = current.locator('input[type="file"][accept*="image"]')
                count = await inputs.count()
                if count == 1:
                    return inputs.first
                if count > 1:
                    local_uploads = []
                    for index in range(count):
                        candidate = inputs.nth(index)
                        role = await candidate.evaluate(
                            r"""input => {
                                let node = input.parentElement;
                                while (node) {
                                    const tokens = typeof node.className === 'string'
                                        ? node.className.split(/\s+/).filter(Boolean)
                                        : [];
                                    if (tokens.some((token) =>
                                        token.startsWith('FeEditorApp-')
                                        && token.endsWith('-upload'))) {
                                        return 'LOCAL_UPLOAD';
                                    }
                                    if (tokens.some((token) =>
                                        token.startsWith('FeEditorApp-')
                                        && token.endsWith('-cropper'))) {
                                        return 'BODY_CROPPER';
                                    }
                                    node = node.parentElement;
                                }
                                return 'UNKNOWN';
                            }"""
                        )
                        if role == "LOCAL_UPLOAD":
                            local_uploads.append(candidate)
                    if len(local_uploads) == 1:
                        return local_uploads[0]
                    if len(local_uploads) > 1:
                        return None
            except Exception:
                pass
            refreshed = await self._wait_for_cover_modal(timeout_seconds=1)
            if refreshed is not None:
                current = refreshed
            await asyncio.sleep(0.25)
        return None

    @staticmethod
    async def _cover_preview_state(modal) -> dict:
        state = await modal.evaluate(
            r"""root => {
                const visible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const parts = [];
                for (const image of root.querySelectorAll('img')) {
                    if (visible(image) && image.complete && image.naturalWidth > 0) {
                        parts.push(`img:${image.currentSrc || image.src || ''}`);
                    }
                }
                for (const canvas of root.querySelectorAll('canvas')) {
                    if (!visible(canvas) || !canvas.width || !canvas.height) continue;
                    let sample = `${canvas.width}x${canvas.height}`;
                    try { sample += `:${canvas.toDataURL().slice(0, 512)}`; }
                    catch (_) { /* cross-origin canvas: dimensions remain evidence */ }
                    parts.push(`canvas:${sample}`);
                }
                for (const element of root.querySelectorAll('*')) {
                    if (!visible(element)) continue;
                    const background = getComputedStyle(element).backgroundImage || '';
                    if (background && background !== 'none') parts.push(`bg:${background}`);
                }
                let hash = 2166136261;
                for (const char of parts.sort().join('|')) {
                    hash ^= char.codePointAt(0);
                    hash = Math.imul(hash, 16777619) >>> 0;
                }
                const inputs = Array.from(root.querySelectorAll(
                    'input[type="file"][accept*="image"]'
                ));
                const confirms = Array.from(root.querySelectorAll(
                    'button, [role="button"], [class*="btn" i]'
                )).filter((element) => visible(element)
                    && /^(?:确认|确定)(?:\s*\(\d+\))?$/.test(
                        (element.innerText || '').replace(/\s+/g, ' ').trim()
                    )
                    && !element.disabled
                    && element.getAttribute('aria-disabled') !== 'true');
                return {
                    selected_files: inputs.reduce(
                        (total, input) => total + (input.files?.length || 0), 0
                    ),
                    visual_count: parts.length,
                    visual_hash: hash,
                    confirm_count: confirms.length,
                };
            }"""
        )
        return state if isinstance(state, dict) else {}

    async def _wait_for_cover_preview(self, modal, baseline: dict) -> bool:
        for _ in range(40):
            self._require_page_alive("百家号等待封面预览")
            try:
                state = await self._cover_preview_state(modal)
            except Exception:
                # 上传后平台可能重建整个 modal；只重新解析同一个封面弹窗，
                # 不能沿用失效句柄，更不能扩大到页面级 file input。
                modal = await self._wait_for_cover_modal(timeout_seconds=1)
                if modal is None:
                    await asyncio.sleep(0.5)
                    continue
                state = await self._cover_preview_state(modal)
            visual_changed = (
                int(state.get("visual_count") or 0) > 0
                and (
                    int(state.get("visual_hash") or 0)
                    != int(baseline.get("visual_hash") or 0)
                    or int(baseline.get("visual_count") or 0) == 0
                )
            )
            # 真实 React 流程在接收文件后会立即清空 input.files。这里
            # 不把瞬时 FileList 当成成功证据，而要求冻结素材导致可见
            # 预览指纹变化，且当前封面弹窗只有一个可执行确认动作。
            if visual_changed and int(state.get("confirm_count") or 0) == 1:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _confirm_cover_dialogs(self) -> bool:
        """只在当前可见 cheetah modal 内完成「确认→确定」两步。"""

        for _ in range(5):
            dialogs = await self._visible_items(self.page.locator(".cheetah-modal"))
            if not dialogs:
                return True
            topmost = dialogs[-1]
            candidate = await self._unique_cover_confirm_action(topmost)
            if candidate is None:
                return False
            try:
                if not await candidate.is_enabled():
                    return False
            except AttributeError:
                pass
            await candidate.click(timeout=5000)
            await asyncio.sleep(1)
        return not await self._visible_items(self.page.locator(".cheetah-modal"))

    @staticmethod
    def _is_cover_confirm_label(value: object) -> bool:
        normalized = re.sub(r"\s+", " ", str(value or "")).strip()
        return bool(re.fullmatch(r"(?:确认|确定)(?:\s*\(\d+\))?", normalized))

    async def _unique_cover_confirm_action(self, root):
        actions = root.locator('button, [role="button"], [class*="btn" i]')
        matches = []
        for action in await self._visible_items(actions):
            try:
                label = await action.inner_text()
                enabled = await action.is_enabled()
            except Exception:
                continue
            if enabled and self._is_cover_confirm_label(label):
                matches.append(action)
        return matches[0] if len(matches) == 1 else None

    async def _dismiss_cover_dialogs(self) -> bool:
        """失败时只点击顶层弹窗内唯一「取消」，保证后续不会点穿遮罩。"""

        for _ in range(5):
            dialogs = await self._visible_items(self.page.locator(".cheetah-modal"))
            if not dialogs:
                return True
            cancel = await self._unique_visible_text("取消", dialogs[-1])
            if cancel is None:
                return False
            try:
                await cancel.click(timeout=3000)
            except Exception:
                return False
            await asyncio.sleep(0.5)
        return not await self._visible_items(self.page.locator(".cheetah-modal"))

    async def _cover_failure(self, error_code: str, error: str) -> dict:
        try:
            clean = await self._dismiss_cover_dialogs()
        except Exception:
            clean = False
        return {
            "success": False,
            "cover_status": "failed" if clean else "unverified",
            "safe_to_continue": clean,
            "error_code": error_code if clean else "BAIJIAHAO_COVER_DIALOG_STUCK",
            "error": safe_media_error(error, fallback="百家号封面上传失败"),
        }

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
        evidence = DraftVerificationEvidence()
        self._last_draft_evidence = evidence
        expected_title = self._normalize_title(title)
        if not expected_title or expected_title != self._preflight_title:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号缺少与本次一致的唯一标题基线",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
            )
        if self._expected_persisted_blocks is None:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号缺少冻结内容核验快照",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
            )
        save_responses: list[dict[str, object]] = []

        async def capture_save_response(response) -> None:
            try:
                evidence = self._safe_save_response_evidence(response)
                if evidence is not None and len(save_responses) < 40:
                    if evidence.get("path") == "/pcui/article/save":
                        try:
                            payload = await response.json()
                        except Exception:
                            payload = None
                        evidence["json"] = self._safe_save_response_payload(payload)
                    save_responses.append(evidence)
            except Exception:
                return

        try:
            self.page.on("response", capture_save_response)
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
        finally:
            try:
                self.page.remove_listener("response", capture_save_response)
            except Exception:
                pass

        # 点击后可能立即导航到作品页，原页面 execution context 会被销毁。
        # toast 只是辅助证据，读取失败绝不能阻断下面权威的草稿箱搜索与重开。
        await asyncio.sleep(12)
        safe_notices: list[str] = []
        try:
            notices = await self.page.evaluate(
                r"""() => Array.from(document.querySelectorAll(
                    '[role="alert"], [class*="message"], [class*="toast"], '
                    + '[class*="notice"], [class*="error"]'
                )).filter((element) => element.offsetParent !== null)
                    .map((element) => (element.innerText || element.textContent || '')
                        .replace(/\s+/g, ' ').trim())
                    .filter((text) => text && text.length <= 160)
                    .slice(0, 12)"""
            )
            safe_notices = [
                safe_media_error(item, fallback="")
                for item in notices
                if isinstance(item, str)
            ]
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 百家号存草稿后浏览器已关闭"
                ) from exc
            logger.info("百家号存草稿提示读取不可用，继续草稿箱核验")
        logger.info(
            "百家号存草稿响应证据: responses={} notices={}",
            save_responses,
            safe_notices,
        )

        try:
            edit_url = await self._find_unique_exact_draft(expected_title)
            evidence.mark_draft_list(match_count=self._last_draft_match_count)
            evidence.mark_entity_binding(
                bound=self._last_draft_entity_bound is True,
                source=self._last_draft_entity_source
                or "title_match_without_baseline",
                id_match=(
                    True
                    if self._last_draft_entity_bound is True
                    else False
                ),
            )
            evidence.set_draft_url(edit_url)
            if self._last_draft_entity_bound is not True:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 百家号缺少本次新增草稿的稳定 ID 绑定",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                )
            try:
                await self._verify_persisted_draft(expected_title, edit_url)
            except DraftResultUnknownError as exc:
                message = str(exc)
                if "标题不一致" in message:
                    evidence.mark_reopen(title_match=False, dom_blocks_match=None)
                elif "图文结构不完整" in message:
                    evidence.mark_reopen(title_match=True, dom_blocks_match=False)
                raise
            evidence.mark_reopen(title_match=True, dom_blocks_match=True)
            evidence.finalize()
            return edit_url
        except DraftResultUnknownError as exc:
            if getattr(exc, "evidence", None) is None:
                if (
                    evidence.draft_list_match_count is None
                    and self._last_draft_match_count is not None
                ):
                    evidence.mark_draft_list(
                        match_count=self._last_draft_match_count
                    )
                if evidence.draft_entity_bound is None:
                    evidence.mark_entity_binding(
                        bound=False,
                        source="baseline_new_id",
                        id_match=False,
                    )
                exc.evidence = evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN")
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 百家号核验草稿时浏览器已关闭",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                ) from exc
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号持久化草稿核验失败",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
            ) from exc

    @staticmethod
    def _safe_save_response_evidence(response) -> dict[str, object] | None:
        """仅保留同源 POST 路径和状态码，不记录查询参数或响应正文。"""

        parsed = urlsplit(str(response.url or ""))
        if (
            parsed.hostname != "baijiahao.baidu.com"
            or str(response.request.method or "").upper() != "POST"
        ):
            return None
        return {"path": parsed.path[:180], "status": int(response.status)}

    @staticmethod
    def _safe_save_response_payload(payload) -> dict:
        """投影保存响应的业务码和结构，不返回正文、URL 或凭据字段。"""

        if not isinstance(payload, dict):
            return {"type": type(payload).__name__}
        result: dict = {"top_level_keys": sorted(str(key)[:80] for key in payload)[:80]}
        for key in ("errno", "error_code", "code", "status", "success"):
            value = payload.get(key)
            if isinstance(value, (bool, int)) or (
                isinstance(value, str) and len(value) <= 40
            ):
                result[key] = value
        for key in ("errmsg", "error_msg", "message", "msg"):
            value = payload.get(key)
            if isinstance(value, str):
                result[key] = safe_media_error(value, fallback="")[:160]
        data = payload.get("data")
        if isinstance(data, dict):
            result["data_keys"] = sorted(str(key)[:80] for key in data)[:120]
            result["candidate_ids"] = [
                {"key": str(key)[:80], "length": len(str(value))}
                for key, value in data.items()
                if str(key).lower() in {"id", "nid", "article_id", "draft_id"}
                and isinstance(value, (str, int))
            ][:20]
        return result

    async def _find_unique_exact_draft(self, title: str) -> str:
        self._last_draft_entity_bound = None
        self._last_draft_entity_source = None
        self._last_draft_match_count = None
        await self._open_works_page()
        matches: list[dict] = []
        baseline_count = getattr(self, "_preflight_matching_draft_count", 0)
        baseline_ids = getattr(self, "_preflight_draft_ids", frozenset())
        if not isinstance(baseline_count, int) or baseline_count < 0:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号缺少保存前同名草稿数量基线"
            )
        if not isinstance(baseline_ids, frozenset):
            baseline_ids = frozenset(baseline_ids or ())
        created_match: dict | None = None
        for attempt in range(6):
            await self._search_works(title)
            matches = await self._matching_work_rows(title)
            self._last_draft_match_count = len(matches)
            if len(matches) == baseline_count + 1:
                post_by_id: dict[str, dict] = {}
                post_ids_complete = True
                for match in matches:
                    draft_id = self._preview_article_id(
                        str(match.get("preview_href") or "")
                    )
                    if draft_id is None or draft_id in post_by_id:
                        post_ids_complete = False
                        break
                    post_by_id[draft_id] = match
                baseline_ids_complete = len(baseline_ids) == baseline_count
                new_ids = set(post_by_id).difference(baseline_ids)
                if (
                    baseline_ids_complete
                    and post_ids_complete
                    and len(post_by_id) == len(matches)
                    and len(new_ids) == 1
                ):
                    created_match = post_by_id[new_ids.pop()]
                    self._last_draft_entity_bound = True
                    self._last_draft_entity_source = "baseline_new_id"
                    break
            if attempt + 1 < 6:
                await self.page.reload(wait_until="domcontentloaded", timeout=30000)
                await self.simulator.random_delay(1, 2)
        if created_match is None:
            self._last_draft_entity_bound = False
            self._last_draft_entity_source = "baseline_new_id"
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号无法从同名草稿中唯一识别本次新增实体"
            )
        # 真实作品行的预览链接可能是 HTTP，但这里只提取通过严格校验的
        # 稳定 ID；编辑页始终由 _edit_url_from_preview_href 规范为 HTTPS。
        preview_href = str(created_match.get("preview_href") or "")
        return self._edit_url_from_preview_href(preview_href)

    async def verify_draft_readonly(self, title: str) -> dict:
        """只读核验：在百家号草稿作品页按标题搜索并匹配唯一草稿行。

        只读操作：打开作品页 → 切到草稿 tab → 搜索框填标题（防抖查询）→
        读匹配行数；不点击“修改”、不打开编辑页、不保存。
        """
        expected_title = self._normalize_title(title)
        if not expected_title:
            return {"error_code": "PROBE_TITLE_MISSING", "error_message": "缺少可核验标题"}
        try:
            await self._open_works_page()
            await self._search_works(expected_title)
            matches = await self._matching_work_rows(expected_title)
            count = len(matches)
            if count == 1:
                preview_href = str(matches[0].get("preview_href") or "")
                draft_url = (
                    self._edit_url_from_preview_href(preview_href)
                    if preview_href
                    else None
                )
                return {
                    "title_matched": True,
                    "match_count": 1,
                    "draft_url": draft_url,
                    "structure": {"source": "works_list"},
                }
            if count > 1:
                return {
                    "error_code": "PROBE_TITLE_AMBIGUOUS",
                    "error_message": f"草稿箱存在 {count} 个同名草稿",
                }
            return {
                "error_code": "PROBE_NOT_FOUND",
                "error_message": "草稿箱未找到该标题草稿",
            }
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                return {
                    "error_code": "PROBE_RESULT_UNKNOWN",
                    "error_message": "核验期间浏览器已关闭",
                }
            return {
                "error_code": "PROBE_RESULT_UNKNOWN",
                "error_message": "草稿箱核验失败",
            }

    async def _open_exact_draft_via_modify(
        self,
        title: str,
        *,
        row_index: int = 0,
    ) -> str:
        """点击指定精确标题行的 React“修改”动作并捕获同源编辑页。

        百家号新版作品行不再提供 ``/builder/preview/s`` 链接。这里不猜
        React 私有属性，也不拼接未知 ID；只允许保存前后数量证明得到的
        精确标题行、该行唯一“修改”动作和最终通过 ``_safe_draft_url`` 的
        同源编辑页。
        """

        if self.context is None:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号缺少草稿重开浏览器上下文"
            )
        if isinstance(row_index, bool) or not isinstance(row_index, int) or row_index < 0:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号目标草稿行索引无效"
            )
        before_page_ids = {id(page) for page in self.context.pages}
        click_result = await self.page.evaluate(
            """({title, rowIndex}) => {
                const visible = (el) => !!(
                    el && (el.offsetWidth || el.offsetHeight
                        || el.getClientRects().length)
                );
                const text = (el) => (el?.innerText || el?.textContent || '')
                    .replace(/\\s+/g, ' ').trim();
                const rows = Array.from(document.querySelectorAll(
                    'div[class*="articleItem"]'
                )).filter((row) => {
                    if (!visible(row)) return false;
                    const exactTitle = Array.from(row.querySelectorAll(
                        'a, span, p, div, h1, h2, h3, h4'
                    )).some((el) => visible(el) && text(el) === title
                        && !Array.from(el.children).some(
                            (child) => text(child) === title
                        ));
                    return exactTitle;
                });
                if (rowIndex < 0 || rowIndex >= rows.length) {
                    return {status: 'ROW_INDEX_INVALID', rows: rows.length};
                }
                const actions = Array.from(rows[rowIndex].querySelectorAll(
                    'button, a, [role="button"], span'
                )).filter((el) => visible(el) && text(el) === '修改'
                    && !Array.from(el.children).some(
                        (child) => text(child) === '修改'
                    ));
                if (actions.length !== 1) {
                    return {status: 'ACTION_NOT_UNIQUE', actions: actions.length};
                }
                actions[0].click();
                return {status: 'CLICKED'};
            }""",
            {"title": title, "rowIndex": row_index},
        )
        if not isinstance(click_result, dict) or click_result.get("status") != "CLICKED":
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号目标草稿修改动作不可用"
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 12
        while loop.time() < deadline:
            self._require_page_alive("百家号等待草稿修改页")
            new_pages = [
                page
                for page in self.context.pages
                if id(page) not in before_page_ids and not page.is_closed()
            ]
            candidates = new_pages or [self.page]
            if len(new_pages) <= 1:
                for candidate in candidates:
                    try:
                        edit_url = self._safe_draft_url(str(candidate.url or ""))
                    except DraftResultUnknownError:
                        continue
                    if candidate is not self.page:
                        try:
                            await candidate.close()
                        except Exception:
                            pass
                    return edit_url
            await asyncio.sleep(0.25)
        raise DraftResultUnknownError(
            "DRAFT_RESULT_UNKNOWN: 百家号修改动作未打开唯一同源编辑页"
        )

    @classmethod
    def _edit_url_from_preview_href(cls, value: str) -> str:
        article_id = cls._preview_article_id(value)
        if article_id is None:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号草稿预览 ID 无法唯一验证"
            )
        return cls._safe_draft_url(
            urlunsplit(
                (
                    "https",
                    "baijiahao.baidu.com",
                    "/builder/rc/edit",
                    urlencode(
                        {
                            "type": "news",
                            "article_id": article_id,
                            "is_pay_training_camp": "",
                        }
                    ),
                    "",
                )
            )
        )

    @staticmethod
    def _preview_article_id(value: str) -> str | None:
        """从同源预览地址提取非敏感草稿 ID；无可靠 ID 时返回 ``None``。"""

        parts = urlsplit(str(value or ""))
        try:
            port = parts.port
        except ValueError:
            return None
        if (
            parts.scheme not in {"http", "https"}
            or parts.hostname != "baijiahao.baidu.com"
            or port is not None
            or parts.path != "/builder/preview/s"
            or parts.username
            or parts.password
            or parts.fragment
        ):
            return None
        values = parse_qsl(parts.query, keep_blank_values=True)
        article_ids = [item for key, item in values if key == "id"]
        if (
            len(values) != 1
            or len(article_ids) != 1
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", article_ids[0]) is None
        ):
            return None
        return article_ids[0]

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
        self._bound_draft_id = ""
        self._publication_preview_required = None
        self._publication_preview_draft_id = ""
        await self.page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
        await self._wait_for_editor_ready(timeout_seconds=45)
        expected_tokens = self._expected_content_tokens(blocks)
        actual_tokens: list[dict] = []
        title_matches = False
        content_matches = False
        for attempt in range(20):
            title_editor = await self._title_editor_locator()
            if title_editor is not None:
                actual_title = self._normalize_title(await title_editor.inner_text())
                title_matches = actual_title == title
            if title_matches:
                actual_tokens = await self._read_editor_dom_tokens()
                content_matches = actual_tokens == expected_tokens
                if content_matches:
                    if (
                        not self._expected_persisted_cover
                        or await self._has_persisted_cover()
                    ):
                        article_id = self._editor_article_id(edit_url)
                        if not article_id or self._editor_article_id(self.page.url) != article_id:
                            raise DraftResultUnknownError(
                                "DRAFT_RESULT_UNKNOWN: 百家号重开后的草稿 ID 不匹配"
                            )
                        self._bound_draft_id = article_id
                        return
            if attempt + 1 < 20:
                await asyncio.sleep(1.5)
        if not title_matches:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号草稿重开后标题不一致"
            )
        if content_matches and self._expected_persisted_cover:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 百家号草稿重开后封面未持久化"
            )
        raise DraftResultUnknownError(
            "DRAFT_RESULT_UNKNOWN: 百家号草稿重开后图文结构不完整; "
            f"expected={self._token_shape(expected_tokens)}; "
            f"actual={self._token_shape(actual_tokens)}"
        )

    async def _has_persisted_cover(self) -> bool:
        """只读核验已保存编辑页的唯一封面缩略图。"""

        self._require_page_alive("百家号核验持久化封面")
        try:
            count = await self.page.evaluate(
                r"""() => {
                    const visible = (element) => {
                        const rect = element.getBoundingClientRect();
                        const style = getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0
                            && style.display !== 'none'
                            && style.visibility !== 'hidden';
                    };
                    const hasClassSuffix = (element, suffix) =>
                        typeof element?.className === 'string'
                        && element.className.split(/\s+/).some((token) =>
                            token.startsWith('FeEditorApp-')
                            && token.endsWith(suffix));
                    return Array.from(document.querySelectorAll('img'))
                        .filter((image) => visible(image)
                            && image.complete && image.naturalWidth > 0
                            && hasClassSuffix(image, '-coverImg')
                            && Array.from(document.querySelectorAll('*')).some(
                                (wrapper) => hasClassSuffix(wrapper, '-coverWrapper')
                                    && wrapper.contains(image)
                            )).length;
                }"""
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号核验持久化封面时页面已关闭"
                ) from exc
            return False
        return int(count or 0) == 1

    @classmethod
    def _editor_article_id(cls, url: str) -> str:
        try:
            safe = cls._safe_draft_url(url)
        except (DraftResultUnknownError, ValueError):
            return ""
        if not safe:
            return ""
        pairs = parse_qsl(urlsplit(safe).query, keep_blank_values=True)
        ids = [value for key, value in pairs if key == "article_id"]
        types = [value for key, value in pairs if key == "type"]
        if len(ids) != 1 or not re.fullmatch(r"\d{1,30}", ids[0]) or types != ["news"]:
            return ""
        if any(k in cls.PUBLICATION_EXTRA_MODES and v not in {"", "0"} for k, v in pairs):
            return ""
        return ids[0]

    async def prepare_publication_options(self) -> None:
        """关闭原生表单默认开启的附加播客，不改变文章或创作声明。"""
        labels = self.page.locator("label:visible").filter(
            has_text=re.compile(r"^自动生成播客$"),
        )
        if await labels.count() != 1:
            raise SelectorError("百家号自动生成播客选项不存在或不唯一")
        checkbox = labels.locator('input[type="checkbox"]')
        if await checkbox.count() != 1 or not await checkbox.is_enabled():
            raise SelectorError("百家号自动生成播客状态无法确认")
        if await checkbox.is_checked():
            # Controlled React inputs may briefly revert before the parent commits.
            # Click once, then observe settlement; never toggle again on a timeout.
            await checkbox.click(timeout=5000)
        for _ in range(50):
            if not await checkbox.is_checked():
                return
            await asyncio.sleep(0.1)
        raise SelectorError("百家号自动生成播客未关闭")

    async def _assert_publication_ready(self, title: str) -> str:
        expected_title = self._normalize_title(title)
        if (
            not expected_title or not self._bound_draft_id
            or self._expected_persisted_blocks is None
        ):
            raise SelectorError("百家号发布前缺少已核验草稿")
        self._require_page_alive("百家号发布前核验")
        draft_id = self._editor_article_id(self.page.url)
        titles = self._editors()
        if (
            draft_id != self._bound_draft_id or await titles.count() != 1
            or self._normalize_title(await titles.inner_text()) != expected_title
        ):
            raise SelectorError("百家号发布前草稿或标题不匹配")
        if (
            self._publication_preview_required is not False
            or self._publication_preview_draft_id != draft_id
        ):
            raise SelectorError("百家号直接发布能力未确认，预览保存流程尚未接入")
        blocks = self._expected_persisted_blocks
        if await self._read_editor_dom_tokens() != self._expected_content_tokens(blocks):
            raise SelectorError("百家号发布前正文图文不完整")
        image_count = sum(block.get("type") == "image" for block in blocks)
        if await self._count_body_images() != image_count or (
            image_count and not await self._new_body_images_ready(0)
        ):
            raise SelectorError("百家号发布前正文图片尚未全部加载")
        if not await self._has_persisted_cover():
            raise SelectorError("百家号发布前缺少唯一已加载封面")
        if await self.page.locator('.cheetah-modal:visible, [role="dialog"]:visible').count():
            raise SelectorError("百家号发布前仍有未完成弹窗")
        return draft_id

    @classmethod
    def _is_publication_response(cls, response) -> bool:
        try:
            parsed = urlsplit(response.url)
            return (
                parsed.scheme == "https" and parsed.netloc == "baijiahao.baidu.com"
                and parsed.path == cls.PUBLICATION_RESPONSE_PATH
                and [v for k, v in parse_qsl(parsed.query) if k == "type"] == ["news"]
                and response.request.method == "POST"
            )
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _publication_fields(request) -> dict:
        """只在内存解码已观察到的原生表单；拒绝重复字段和其他编码。"""
        content_type = (getattr(request, "headers", {}) or {}).get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            raise ValueError("unsupported publication encoding")
        fields = {}
        pairs = parse_qsl(request.post_data or "", keep_blank_values=True, errors="strict")
        for key, value in pairs:
            if key in fields:
                raise ValueError("duplicate field")
            fields[key] = value
        return fields

    @classmethod
    def _publication_request_matches(cls, request, draft_id: str, title: str) -> bool:
        try:
            fields = cls._publication_fields(request)
            podcast_indexes = [
                match[1] for key, value in fields.items()
                if (match := re.fullmatch(r"activity_list\[([0-9]+)\]\[id\]", key))
                and value == "ai_tts"
            ]
            return (
                isinstance(fields, dict) and fields.get("type") == "news"
                and str(fields.get("article_id", "")) == draft_id
                and cls._normalize_title(str(fields.get("title", ""))) == title
                and all(fields.get(k) in (None, "", "0") for k in cls.PUBLICATION_EXTRA_MODES)
                and len(podcast_indexes) == 1
                and fields.get(f"activity_list[{podcast_indexes[0]}][is_checked]") == "0"
            )
        except (TypeError, ValueError, AttributeError):
            return False

    async def publish_now(self, title: str = "") -> dict:
        """只提交一次已核验原草稿，明确回执只证明接收，不证明审核通过。"""
        if getattr(self, "_public_publish_attempted", False):
            raise PublishResultUnknownError("百家号本次发布已尝试，不会自动重发")
        await self._assert_publication_ready(title)
        await self.prepare_publication_options()
        draft_id = await self._assert_publication_ready(title)
        buttons = self.page.get_by_role("button", name="发布", exact=True)
        if (
            await buttons.count() != 1 or not await buttons.is_visible()
            or not await buttons.is_enabled()
        ):
            raise SelectorError("百家号发布按钮不可用或不唯一")
        forwarded = 0
        pattern = "**/pcui/article/publish*"

        async def guard(route, request):
            nonlocal forwarded
            parsed = urlsplit(request.url)
            allowed = (
                not forwarded and request.method == "POST"
                and parsed.scheme == "https" and parsed.netloc == "baijiahao.baidu.com"
                and parsed.path == self.PUBLICATION_RESPONSE_PATH
                and [v for k, v in parse_qsl(parsed.query) if k == "type"] == ["news"]
                and self._publication_request_matches(
                    request, draft_id, self._normalize_title(title),
                )
            )
            if not allowed:
                await route.abort()
                return
            forwarded += 1
            await route.fallback()

        await self.page.route(pattern, guard)
        self._public_publish_attempted = True
        try:
            async with self.page.expect_response(
                self._is_publication_response, timeout=self.PUBLICATION_TIMEOUT_MS,
            ) as pending:
                await buttons.click(timeout=8000)
            response = await pending.value
            if not self._publication_request_matches(
                response.request, draft_id, self._normalize_title(title),
            ):
                raise ValueError("发布请求与当前草稿不匹配")
            payload = await response.json()
            if (
                isinstance(payload, dict) and type(payload.get("errno")) in {int, str}
                and str(payload["errno"]) == "10000015"
            ):
                raise PublishResultUnknownError(
                    "百家号触发百度安全验证（平台代码 10000015），需本人处理；不会自动重发"
                )
            if (
                not 200 <= response.status < 300 or not isinstance(payload, dict)
                or type(payload.get("errno")) not in {int, str}
                or payload["errno"] not in (0, "0")
                or not isinstance(payload.get("ret"), dict)
            ):
                raise ValueError("平台未明确确认接收")
            article_id = payload["ret"].get("nid")
            if type(article_id) not in {int, str} or not re.fullmatch(
                r"[0-9]{1,30}", str(article_id),
            ):
                raise ValueError("平台回执缺少有效文章 ID")
            return {
                "status": "SUBMITTED", "platform_article_id": str(article_id),
                "verification_evidence": {
                    "submit_acknowledged": True,
                    "submission_source": "baijiahao_publish_response",
                    "submission_scope": "PUBLIC", "submission_article_id": str(article_id),
                    "submission_draft_id": draft_id,
                },
            }
        except PublishResultUnknownError:
            raise
        except Exception as exc:
            raise PublishResultUnknownError(
                "百家号已尝试发布，但未确认本篇接收回执；不会自动重发"
            ) from exc
