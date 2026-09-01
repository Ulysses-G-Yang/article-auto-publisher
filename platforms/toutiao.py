"""头条号（mp.toutiao.com）账号会话与草稿投递适配器。

登录（2026-08 真实探测）：
- 登录页 https://mp.toutiao.com/auth/page/login（**不能带 redirect_url 参数**，
  带参数会触发页面 JS atob 解码崩溃导致白屏；无参数正常）。
- 「扫码登录」用抖音 App 扫码；登录成功信号 cookie：
  sessionid / sessionid_ss / sid_tt / uid_tt / sid_guard / toutiao_sso_user 等。
- 新账号首次登录后需完成创作者账号类型选择（personal/landing-type →
  landing-complete/success），完成后才能访问创作页与草稿箱。

身份（捕获页面自身响应）：
- 登录后访问工作台，捕获 /user/profile/auth/info/v2/ 响应：
  data.user_name 为昵称，URL 参数 __user_id 为平台用户 ID。

编辑器（https://mp.toutiao.com/profile_v4/graphic/publish）：
- 标题：textarea[placeholder*="文章标题"]（2~30 字，最少 2 字）。
- 正文：div.ProseMirror（contenteditable，TipTap/ProseMirror 系）。
- 封面模式：单图(2)/三图(3)/无封面(1)；纯文字文章需选「无封面」。
- 草稿：无独立存草稿按钮，「草稿将自动保存」（自动保存接口
  POST /mp/agw/article/publish?type=article，save=0）。

2026-08 历史验收曾出现：**头条自动化草稿保存被风控拒绝**——
- 自动保存请求（带正文，save=0）返回 code 7050「保存失败」；
  空正文保存可通过（深诊空草稿成功落库）。
- 已排除：封面模式（选无封面 coverType=1 仍 7050）、输入节奏
  （超真人慢速/随机停顿/打错重打仍 7050）、频率（冷却后仍 7050）、
  发布路径（点「预览并发布」同样 save=0 → 7050）。
- 人工手动（真人浏览器）保存/发布正常——是**字节系风控判定
  Playwright 启动的 Chrome 为自动化环境**，带内容（高风险）保存被拒。
- 当前适配器不绕过风控，只实现正常编辑器操作与严格证据链：保存前建立
  ``pgc_id`` 基线，按 Word 顺序写入文字/H2/图片，等待自动保存，绑定唯一
  新增实体并重开核对标题、图文和图片指纹。真实验收通过前投递仍保持关闭；
  平台明确拒绝或证据不足时停止且不自动重试。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    DraftBaselineError,
    DraftResultUnknownError,
    DraftVerificationEvidence,
    LoginRequiredError,
    PlatformAutomationError,
    SelectorError,
)
from platforms.content_validation import ensure_valid_content, safe_media_error

LOGIN_URL = "https://mp.toutiao.com/auth/page/login"
HOME_URL = "https://mp.toutiao.com/profile_v4/"
PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
DRAFT_BOX_URL = "https://mp.toutiao.com/profile_v4/manage/draft"
TITLE_SELECTOR = "textarea[placeholder*='文章标题']"
BODY_SELECTOR = "div.ProseMirror[contenteditable='true']"
HEADING_BUTTON_SELECTOR = ".syl-toolbar-tool.header button"
IMAGE_BUTTON_SELECTOR = ".syl-toolbar-tool.image button"
BODY_IMAGE_INPUT_SELECTOR = (
    ".upload-image-panel [data-e2e='image-upload'] "
    "input[type='file'][accept*='image']"
)
IMAGE_DRAWER_SELECTOR = (
    ".byte-drawer-wrapper:has(" f"{BODY_IMAGE_INPUT_SELECTOR}" ")"
)
IMAGE_DRAWER_CLOSE_SELECTOR = (
    f"{IMAGE_DRAWER_SELECTOR} .byte-drawer-close-icon"
)
IMAGE_CONFIRM_SELECTOR = (
    f"{IMAGE_DRAWER_SELECTOR} button[data-e2e='imageUploadConfirm-btn']"
)
IMAGE_UPLOAD_ERROR_SELECTOR = (
    f"{IMAGE_DRAWER_SELECTOR} .upload-image-wrapper "
    ".pic-select-image-item .error, "
    f"{IMAGE_DRAWER_SELECTOR} .upload-image-wrapper "
    ".pic-select-image-item .size-err"
)
IMAGE_UPLOAD_PATH = "/spice/image"
DRAFT_CARD_SELECTOR = ".article-draft-item.draft-item"
AUTOSAVE_PATH = "/mp/agw/article/publish"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name"}
IDENTITY_UID_KEYS = {"user_id", "uid", "id"}


@dataclass(frozen=True, slots=True)
class _ToutiaoDraftSnapshot:
    """保存前后只记录草稿实体摘要，不保留响应正文或账号数据。"""

    loaded: bool
    total_count: int
    title_counts: dict[str, int]
    draft_ids: frozenset[str]
    title_to_ids: dict[str, frozenset[str]]


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class ToutiaoDraftSaveRejectedError(PlatformAutomationError):
    """头条自动保存接口明确拒绝了本次草稿。"""

    error_code = "TOUTIAO_DRAFT_SAVE_REJECTED"


class ToutiaoPlatform(BasePlatform):
    """头条号账号会话适配器；投递开放需通过真实草稿验收。"""

    platform_name = "toutiao"
    SESSION_COOKIE_NAMES = frozenset(
        {
            "sessionid",
            "sessionid_ss",
            "sid_tt",
            "sid_guard",
            "uid_tt",
            "uid_tt_ss",
            "toutiao_sso_user",
            "passport_auth_status",
        }
    )
    LOGIN_POLL_ATTEMPTS = 100
    LOGIN_POLL_INTERVAL_SECONDS = 3

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None
        self._preflight_title: str | None = None
        self._draft_baseline: _ToutiaoDraftSnapshot | None = None
        self._expected_persisted_tokens: list[dict[str, str]] | None = None
        self._autosave_records: list[dict[str, object]] = []
        self._autosave_handler = None
        self._last_editor_mutation_at = 0.0

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None
        self._preflight_title = None
        self._draft_baseline = None
        self._expected_persisted_tokens = None
        self._autosave_records = []
        self._autosave_handler = None
        self._last_editor_mutation_at = 0.0

    # ==================== 登录态与身份 ====================

    async def _has_session_cookie_signal(self) -> bool:
        """会话 cookie 作为登录成功信号；不返回、不记录 cookie 值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies(["https://mp.toutiao.com/"])
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def check_login(self) -> bool:
        """只读验证现有 Profile；会话 cookie 出现且身份可确认才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("头条号登录态检测")
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
                self.last_login_error = "TOUTIAO_IDENTITY_MISSING: 会话存在但身份未确认"
                return False
            self.last_login_error = "LOGIN_REQUIRED: 头条号账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "TOUTIAO_LOGIN_CHECK_ERROR: 头条号登录态验证失败"
            return False

    async def login(self):
        """打开头条号登录页（无 redirect_url 参数），等待抖音 App 扫码登录。"""

        self._require_page_alive("头条号打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        # 确保「扫码登录」Tab 激活
        try:
            await self.page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('span, div'));
                    const tab = nodes.find(
                        (el) => (el.innerText || '').trim() === '扫码登录'
                    );
                    if (tab) tab.click();
                }"""
            )
        except Exception:
            pass
        await self._show_scan_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("头条号等待登录")
            if await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        raise LoginRequiredError("等待头条号扫码登录超时")

    async def _show_scan_hint(self):
        logger.info(
            "请使用抖音 App 扫描头条号登录二维码（隔离 Profile: {}）",
            getattr(self, "profile_dir", "?"),
        )

    async def fetch_identity_payload(self) -> dict:
        """捕获页面自身 /user/profile/auth/info/v2 响应（裸 fetch 会被签名拒绝）。"""

        captured: list[dict] = []

        async def _on_response(response) -> None:
            try:
                url = response.url
                if "/user/profile/auth/info/" in url and "__user_id=" in url:
                    body = await response.text()
                    if len(body) < 200000:
                        captured.append({"url": url, "body": body})
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", _on_response)
            await self.page.goto(
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.simulator.random_delay(4, 7)
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号身份捕获时页面已关闭"
                ) from exc
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        for item in captured:
            try:
                payload = json.loads(item["body"])
                data = payload.get("data") or {}
            except Exception:
                continue
            user_name = str(data.get("user_name") or "").strip()
            match = re.search(r"__user_id=(\d+)", item["url"])
            if user_name and match:
                self._identity_payload = {
                    "ok": True,
                    "user_id": match.group(1),
                    "display_name": user_name,
                }
                return self._identity_payload

        # DOM 兜底：工作台头部昵称
        try:
            nickname = await self.page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    const texts = nodes
                        .filter((el) => el.children.length === 0)
                        .map((el) => (el.innerText || '').trim())
                        .filter((t) => t && t.length <= 40);
                    return texts[0] || '';
                }"""
            )
        except Exception:  # noqa: BLE001
            nickname = ""
        # DOM 只能提供昵称，不能证明稳定平台 ID；保留展示信息但不得把它
        # 晋级成可投递身份。否则账号页会先显示有效，执行层随后又拒绝。
        if nickname:
            self._identity_payload = {
                "ok": False,
                "user_id": "",
                "display_name": nickname,
            }
            return self._identity_payload
        self._identity_payload = {"ok": False}
        return self._identity_payload

    # ==================== 草稿投递链 ====================

    @staticmethod
    def _normalize_platform_title(value: object) -> str:
        """头条标题栏上限为 30 字；所有基线、写入和回读使用同一值。"""

        return " ".join(str(value or "").split())[:30]

    @staticmethod
    def _safe_pgc_id(value: object) -> str | None:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        candidate = str(value).strip()
        return candidate if re.fullmatch(r"\d{6,32}", candidate) else None

    @classmethod
    def _edit_url(cls, pgc_id: object) -> str | None:
        safe_id = cls._safe_pgc_id(pgc_id)
        if safe_id is None:
            return None
        parsed = urlparse(PUBLISH_URL)
        return urlunparse(parsed._replace(query=urlencode({"pgc_id": safe_id})))

    @classmethod
    def _pgc_id_from_url(cls, value: object) -> str | None:
        try:
            parsed = urlparse(str(value or ""))
        except Exception:
            return None
        if parsed.scheme != "https" or parsed.netloc != "mp.toutiao.com":
            return None
        if parsed.path != "/profile_v4/graphic/publish":
            return None
        values = parse_qs(parsed.query, keep_blank_values=False).get("pgc_id", [])
        return cls._safe_pgc_id(values[0]) if len(values) == 1 else None

    @classmethod
    def _extract_draft_items(cls, payload: object) -> list[tuple[str, str]]:
        """从草稿列表 JSON 中只提取白名单标题和数字草稿 ID。"""

        items: list[tuple[str, str]] = []

        def walk(value: object) -> None:
            if isinstance(value, dict):
                title = next(
                    (
                        cls._normalize_platform_title(value.get(key))
                        for key in ("title", "abstract_title", "article_title")
                        if cls._normalize_platform_title(value.get(key))
                    ),
                    "",
                )
                draft_id = next(
                    (
                        cls._safe_pgc_id(value.get(key))
                        for key in (
                            "pgc_id",
                            "pgcId",
                            "group_id",
                            "gid",
                            "item_id",
                        )
                        if cls._safe_pgc_id(value.get(key))
                    ),
                    None,
                )
                if title and draft_id:
                    items.append((title, draft_id))
                for nested in value.values():
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(payload)
        return items

    @classmethod
    def _extract_save_ids(cls, payload: object) -> frozenset[str]:
        """保存端点已锁定，只接受明确文章 ID 字段，不接受泛化 ``id``。"""

        found: set[str] = set()

        def walk(value: object, *, depth: int = 0) -> None:
            if depth > 5:
                return
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key in {"pgc_id", "pgcId", "group_id", "gid"}:
                        safe_id = cls._safe_pgc_id(nested)
                        if safe_id:
                            found.add(safe_id)
                    elif isinstance(nested, (dict, list)):
                        walk(nested, depth=depth + 1)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested, depth=depth + 1)

        walk(payload)
        return frozenset(found)

    async def _fetch_draft_snapshot(self) -> _ToutiaoDraftSnapshot:
        """只读加载草稿列表，建立标题计数和可见/接口草稿 ID 摘要。"""

        self._require_page_alive("头条号读取草稿基线")
        captured_items: list[tuple[str, str]] = []

        async def on_response(response) -> None:
            try:
                parsed = urlparse(str(response.url or ""))
                request_method = str(response.request.method or "").upper()
                if (
                    request_method != "GET"
                    or parsed.netloc != "mp.toutiao.com"
                    or not (
                        parsed.path.startswith("/mp/agw/article")
                        or any(
                            marker in parsed.path.lower()
                            for marker in ("draft", "manage")
                        )
                    )
                ):
                    return
                payload = await response.json()
                captured_items.extend(self._extract_draft_items(payload))
            except Exception:  # noqa: BLE001
                return

        self.page.on("response", on_response)
        try:
            await self.page.goto(
                DRAFT_BOX_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.page.wait_for_selector(
                ".draft-page .list-count",
                timeout=20000,
            )

            async def read_state() -> dict:
                return await self.page.evaluate(
                r"""() => {
                    const cards = Array.from(
                        document.querySelectorAll('.article-draft-item.draft-item')
                    );
                    const titleCounts = {};
                    const hrefItems = [];
                    for (const card of cards) {
                        const title = (card.querySelector('.main .title')?.innerText || '')
                            .replace(/\s+/g, ' ').trim().slice(0, 30);
                        if (title) titleCounts[title] = (titleCounts[title] || 0) + 1;
                        for (const link of card.querySelectorAll('a[href]')) {
                            const href = link.href || '';
                            if (title && href.includes('/profile_v4/graphic/publish') &&
                                href.includes('pgc_id=')) {
                                hrefItems.push({title, href});
                            }
                        }
                    }
                    const countText = document.querySelector('.list-count')?.innerText || '';
                    const match = countText.match(/共\s*(\d+)\s*条/);
                    return {
                        loaded: Boolean(document.querySelector('.draft-page')),
                        totalCount: match ? Number(match[1]) : cards.length,
                        titleCounts,
                        hrefItems,
                    };
                }"""
            )
            async def read_complete_state() -> dict:
                state = await read_state()
                state["capturedItems"] = sorted(set(captured_items))
                return state

            # DOM 数量稳定并不代表草稿 ID 已经加载完整。把页面自身接口中
            # 捕获到的标题/pgc_id 也纳入两次稳定性比较，避免漏掉旧 ID 后
            # 将其误认成本次新增草稿。
            first_state = await read_complete_state()
            await asyncio.sleep(1)
            state = await read_complete_state()
            if first_state != state:
                await asyncio.sleep(1)
                stable_state = await read_complete_state()
                if state != stable_state:
                    raise DraftBaselineError(
                        "DRAFT_BASELINE_UNAVAILABLE: 头条号草稿列表仍在变化"
                    )
                state = stable_state
        finally:
            try:
                self.page.remove_listener("response", on_response)
            except Exception:  # noqa: BLE001
                pass

        if not isinstance(state, dict) or not state.get("loaded"):
            raise DraftBaselineError(
                "DRAFT_BASELINE_UNAVAILABLE: 头条号草稿列表未稳定加载"
            )
        title_counts = {
            self._normalize_platform_title(title): int(count)
            for title, count in (state.get("titleCounts") or {}).items()
            if self._normalize_platform_title(title) and isinstance(count, int)
        }
        title_to_ids: dict[str, set[str]] = {}
        for title, draft_id in state.get("capturedItems") or []:
            title_to_ids.setdefault(title, set()).add(draft_id)
        for item in state.get("hrefItems") or []:
            if not isinstance(item, dict):
                continue
            title = self._normalize_platform_title(item.get("title"))
            draft_id = self._pgc_id_from_url(item.get("href"))
            if title and draft_id:
                title_to_ids.setdefault(title, set()).add(draft_id)
        frozen_title_ids = {
            title: frozenset(ids) for title, ids in title_to_ids.items()
        }
        total_count = max(0, int(state.get("totalCount") or 0))
        if total_count > 0 and not title_counts:
            raise DraftBaselineError(
                "DRAFT_BASELINE_UNAVAILABLE: 头条号草稿列表有记录但标题摘要为空"
            )
        all_draft_ids = frozenset(
            draft_id for ids in frozen_title_ids.values() for draft_id in ids
        )
        if len(all_draft_ids) != total_count:
            raise DraftBaselineError(
                "DRAFT_BASELINE_UNAVAILABLE: 头条号草稿 ID 未完整稳定加载"
            )
        return _ToutiaoDraftSnapshot(
            loaded=True,
            total_count=total_count,
            title_counts=title_counts,
            draft_ids=all_draft_ids,
            title_to_ids=frozen_title_ids,
        )

    async def preflight_delivery(self, title: str) -> None:
        """任何编辑器写入前冻结头条草稿箱基线。"""

        expected_title = self._normalize_platform_title(title)
        if len(expected_title) < 2:
            raise DraftBaselineError(
                "DRAFT_BASELINE_UNAVAILABLE: 头条号标题不足 2 个字"
            )
        self._preflight_title = expected_title
        self._draft_baseline = await self._fetch_draft_snapshot()

    async def _capture_autosave_response(self, response) -> None:
        try:
            parsed = urlparse(str(response.url or ""))
            if (
                str(response.request.method or "").upper() != "POST"
                or parsed.netloc != "mp.toutiao.com"
                or parsed.path != AUTOSAVE_PATH
            ):
                return
            status = response.status if isinstance(response.status, int) else None
            payload: dict = {}
            try:
                candidate = await response.json()
                if isinstance(candidate, dict):
                    payload = candidate
            except Exception:  # noqa: BLE001
                pass
            self._autosave_records.append(
                {
                    "received_at": time.monotonic(),
                    "status": status,
                    "code": payload.get("code"),
                    "err_no": payload.get("err_no"),
                    "reason": str(payload.get("reason") or "")[:120],
                    "draft_ids": self._extract_save_ids(payload),
                }
            )
        except Exception:  # noqa: BLE001
            return

    def _ensure_autosave_listener(self) -> None:
        if self._autosave_handler is not None:
            return
        self._autosave_handler = self._capture_autosave_response
        self.page.on("response", self._autosave_handler)

    async def navigate_to_editor(self):
        """直接打开头条号文章编辑器（graphic/publish）。"""

        self._require_page_alive("头条号打开编辑器")
        self._ensure_autosave_listener()
        await self.page.goto(
            PUBLISH_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        try:
            await self.page.wait_for_selector(
                TITLE_SELECTOR,
                timeout=20000,
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号编辑器打开时页面已关闭"
                ) from exc
            raise SelectorError(
                "SELECTOR_ERROR: 头条号编辑器标题输入框未出现"
            ) from exc
        await self.simulator.random_delay(2, 4)

    async def fill_title(self, title: str):
        self._require_page_alive("头条号填写标题")
        expected = self._normalize_platform_title(title)
        if len(expected) < 2:
            raise SelectorError("SELECTOR_ERROR: 头条号标题不足 2 个字")
        title_input = self.page.locator(TITLE_SELECTOR)
        try:
            await title_input.click(timeout=8000)
        except Exception:
            pass
        self._last_editor_mutation_at = time.monotonic()
        await title_input.fill(expected)
        await self.simulator.random_delay(1, 2)
        actual = (await title_input.input_value()).strip()
        if actual != expected:
            raise SelectorError("SELECTOR_ERROR: 头条号标题回读不一致")
        if expected != " ".join(str(title or "").split()):
            logger.info("头条号标题已按平台 30 字上限安全截断")
        logger.info("头条号标题已填写并回读验证: {} 字", len(actual))

    async def fill_content(self, content_blocks: list, images: list):
        """按冻结块顺序写入文字、H2 与正文图片，并在保存前核对 DOM。"""

        self._require_page_alive("头条号填写正文")
        editor = self.page.locator(BODY_SELECTOR)
        try:
            await editor.click(timeout=8000)
        except Exception:
            pass
        self._last_editor_mutation_at = time.monotonic()
        await editor.press("Control+A")
        await editor.press("Backspace")

        expected_tokens: list[dict[str, str]] = []
        expected_images = 0
        uploaded_images = 0
        failed_images: list[dict[str, str]] = []
        wrote_any = False
        for block in content_blocks:
            block_type = str(block.get("type") or "")
            text = str(block.get("text") or "").strip()
            if block_type not in {"text", "heading", "image"}:
                continue
            if block_type != "image" and not text:
                continue
            # 工具栏和上传面板会移动焦点；每个块开始前都重新把选区固定到
            # 正文末尾，避免后续段落误写进按钮、弹窗或上一张图片说明区。
            await editor.click(timeout=8000)
            await editor.press("Control+End")
            if wrote_any:
                self._last_editor_mutation_at = time.monotonic()
                await self.page.keyboard.press("Enter")
            if block_type in {"text", "heading"}:
                self._last_editor_mutation_at = time.monotonic()
                lines = text.splitlines() or [text]
                for index, line in enumerate(lines):
                    if line:
                        await self.page.keyboard.insert_text(line)
                    if index < len(lines) - 1:
                        await self.page.keyboard.press("Shift+Enter")
                if block_type == "heading":
                    await self._apply_h2_to_current_block()
                expected_tokens.append(
                    {
                        "kind": "H2" if block_type == "heading" else "P",
                        "text": " ".join(text.split()),
                    }
                )
            else:
                expected_images += 1
                img_path = self._image_path_for_block(block, images)
                if img_path is None:
                    failed_images.append(
                        {"filename": "", "error": "文章图片块没有对应本地文件"}
                    )
                elif not await self._stabilize_image_insertion_point():
                    failed_images.append(
                        {
                            "filename": Path(img_path).name,
                            "error": "头条号正文图片插入位置未稳定",
                        }
                    )
                else:
                    upload_result = await self._upload_image(img_path)
                    if upload_result.get("success"):
                        uploaded_images += 1
                        expected_tokens.append(
                            {
                                "kind": "I",
                                "text": "",
                                "fingerprint": str(
                                    upload_result.get("fingerprint") or ""
                                ),
                            }
                        )
                    else:
                        failed_images.append(
                            {
                                "filename": Path(img_path).name,
                                "error": safe_media_error(
                                    upload_result.get("error"),
                                    fallback="图片上传失败",
                                ),
                            }
                        )
                if failed_images:
                    failure_reason = safe_media_error(
                        failed_images[-1].get("error"),
                        fallback="头条号正文图片未完成",
                    )
                    error = DraftResultUnknownError(
                        f"DRAFT_RESULT_UNKNOWN: {failure_reason}；已停止后续写入"
                    )
                    error.media_progress = {
                        "expected_images": expected_images,
                        "uploaded_images": uploaded_images,
                        "failed_image_count": len(failed_images),
                        "media_status": self._media_progress_status(
                            expected_images,
                            uploaded_images,
                            len(failed_images),
                        ),
                    }
                    raise error
            wrote_any = True
            await self.simulator.random_delay(0.2, 0.5)

        actual_text = await editor.inner_text()
        expected_count = ensure_valid_content(
            content_blocks,
            actual_text,
            platform="toutiao",
            phase="输入后",
        )
        actual_tokens = await self._read_editor_tokens()
        if failed_images or actual_tokens != expected_tokens:
            error = DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 头条号编辑器图文顺序写入后无法完整核对"
            )
            error.media_progress = {
                "expected_images": expected_images,
                "uploaded_images": uploaded_images,
                "failed_image_count": len(failed_images),
                "media_status": self._media_progress_status(
                    expected_images,
                    uploaded_images,
                    len(failed_images),
                ),
            }
            raise error
        self._expected_persisted_tokens = list(expected_tokens)
        logger.info(
            "头条号正文写入并验证成功: {} 个文字块，{} 张图片",
            expected_count,
            uploaded_images,
        )

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

    async def _stabilize_image_insertion_point(self) -> bool:
        """在打开图片抽屉前确认 ProseMirror 选区已落到正文末尾。"""

        editor = self.page.locator(BODY_SELECTOR)
        if await editor.count() != 1:
            return False
        try:
            await editor.click(timeout=8000)
            await editor.press("Control+End")
            # Enter 产生的新段落要先经过 ProseMirror 事务和浏览器绘制，
            # 否则 last-child 仍可能是上一段或图片节点。
            await self.page.evaluate(
                """() => new Promise((resolve) => {
                    requestAnimationFrame(() => requestAnimationFrame(resolve));
                })"""
            )
            tail = editor.locator(":scope > p:last-child")
            if await tail.count() != 1:
                return False
            tail_is_empty = await tail.evaluate(
                """(node) => {
                    const isEmptyText = child => child.nodeType === Node.TEXT_NODE &&
                        (child.nodeValue || '')
                            .replace(/[\u200B-\u200D\uFEFF]/g, '')
                            .trim() === '';
                    const isTrailingBreak = child =>
                        child.nodeType === Node.ELEMENT_NODE &&
                        child.tagName === 'BR' &&
                        child.classList.contains('ProseMirror-trailingBreak');
                    return Array.from(node.childNodes).every(
                        child => isEmptyText(child) || isTrailingBreak(child),
                    );
                }"""
            )
            if not tail_is_empty:
                return False
            # 必须使用真实点击让 ProseMirror 同步内部 TextSelection；直接用
            # DOM Range 看似有光标，但平台 editor.insert() 仍可能读到旧选区。
            await tail.click(timeout=8000, position={"x": 4, "y": 4})
            await editor.press("End")
            await self.page.evaluate(
                """() => new Promise((resolve) => {
                    requestAnimationFrame(() => requestAnimationFrame(resolve));
                })"""
            )
            return bool(
                await self.page.evaluate(
                    """() => {
                        const root = document.querySelector('.ProseMirror');
                        const tail = root?.lastElementChild;
                        const selection = window.getSelection();
                        if (!root || !tail || tail.tagName !== 'P' ||
                            !selection || selection.rangeCount !== 1 ||
                            document.activeElement !== root) {
                            return false;
                        }
                        const anchor = selection.anchorNode;
                        const focus = selection.focusNode;
                        if (!(
                            selection.isCollapsed && anchor && focus &&
                            (anchor === tail || tail.contains(anchor)) &&
                            (focus === tail || tail.contains(focus))
                        )) {
                            return false;
                        }
                        const isEmptyText = child =>
                            child.nodeType === Node.TEXT_NODE &&
                            (child.nodeValue || '')
                                .replace(/[\u200B-\u200D\uFEFF]/g, '')
                                .trim() === '';
                        const isTrailingBreak = child =>
                            child.nodeType === Node.ELEMENT_NODE &&
                            child.tagName === 'BR' &&
                            child.classList.contains('ProseMirror-trailingBreak');
                        return Array.from(tail.childNodes).every(
                            child => isEmptyText(child) || isTrailingBreak(child),
                        );
                    }"""
                )
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号稳定图片插入位置时页面已关闭"
                ) from exc
            return False

    @staticmethod
    def _media_progress_status(
        expected_images: int,
        uploaded_images: int,
        failed_image_count: int,
    ) -> str:
        """与安全进度投影使用同一状态推导，避免诊断字段被拒绝。"""

        if expected_images == 0:
            return "not_required"
        if uploaded_images == expected_images and failed_image_count == 0:
            return "completed"
        if uploaded_images == 0 and failed_image_count == expected_images:
            return "failed"
        if uploaded_images + failed_image_count == expected_images:
            return "partial"
        return "in_progress"

    @staticmethod
    def _image_path_for_block(block: dict, images: list) -> str | None:
        local_path = str(block.get("local_path") or "").strip()
        if local_path:
            return local_path
        position = block.get("position")
        for image in images:
            if image.get("position_index") == position:
                candidate = str(image.get("local_path") or "").strip()
                if candidate:
                    return candidate
        return None

    async def _apply_h2_to_current_block(self) -> None:
        """只使用头条编辑器自己的标题按钮，将当前段落切换为 H2。"""

        button = self.page.locator(HEADING_BUTTON_SELECTOR)
        if await button.count() != 1:
            raise SelectorError("SELECTOR_ERROR: 头条号 H2 工具按钮不存在或不唯一")
        self._last_editor_mutation_at = time.monotonic()
        await button.click(timeout=8000)
        await self.simulator.random_delay(0.2, 0.5)
        is_h2 = await self.page.evaluate(
            """() => {
                const root = document.querySelector('.ProseMirror');
                const block = root?.lastElementChild;
                return Boolean(block && (block.matches('h2') || block.querySelector('h2')));
            }"""
        )
        if not is_h2:
            raise SelectorError("SELECTOR_ERROR: 头条号 H2 按钮点击后段落层级未生效")

    async def _read_editor_tokens(self) -> list[dict[str, str]]:
        value = await self.page.evaluate(
            r"""() => {
                const root = document.querySelector('.ProseMirror');
                if (!root) return null;
                const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
                const imageFingerprint = image => {
                    const candidates = [
                        image.currentSrc,
                        image.getAttribute('src'),
                        image.getAttribute('data-src'),
                        image.getAttribute('data-original'),
                    ];
                    for (const candidate of candidates) {
                        if (!candidate) continue;
                        try {
                            const url = new URL(candidate, location.href);
                            if (url.protocol === 'https:' || url.protocol === 'http:') {
                                return `${url.hostname}${url.pathname}`;
                            }
                        } catch (_error) {}
                    }
                    return '';
                };
                const tokens = [];
                for (const child of Array.from(root.children)) {
                    const kind = child.matches('h2') || Boolean(child.querySelector('h2'))
                        ? 'H2' : 'P';
                    let textBuffer = '';
                    const flushText = () => {
                        const text = normalize(textBuffer);
                        if (text) tokens.push({kind, text});
                        textBuffer = '';
                    };
                    const visit = node => {
                        if (node.nodeType === Node.TEXT_NODE) {
                            textBuffer += node.nodeValue || '';
                            return;
                        }
                        if (node.nodeType !== Node.ELEMENT_NODE) return;
                        if (node.tagName === 'IMG') {
                            flushText();
                            tokens.push({
                                kind: 'I',
                                text: '',
                                fingerprint: imageFingerprint(node),
                            });
                            return;
                        }
                        for (const nested of Array.from(node.childNodes)) visit(nested);
                    };
                    visit(child);
                    flushText();
                }
                return tokens;
            }"""
        )
        if not isinstance(value, list):
            raise SelectorError("SELECTOR_ERROR: 头条号正文 DOM 无法读取")
        normalized: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            token = {
                "kind": str(item.get("kind") or ""),
                "text": str(item.get("text") or ""),
            }
            if token["kind"] == "I":
                token["fingerprint"] = str(item.get("fingerprint") or "")
            normalized.append(token)
        return normalized

    async def _read_editor_image_fingerprints(self) -> list[str]:
        value = await self.page.evaluate(
            r"""() => Array.from(document.querySelectorAll(
                    '.ProseMirror > .pgc-image img, .ProseMirror > .pgc-img img'
                ))
                .map(image => {
                    const candidates = [
                        image.currentSrc,
                        image.getAttribute('src'),
                        image.getAttribute('data-src'),
                        image.getAttribute('data-original'),
                    ];
                    for (const candidate of candidates) {
                        if (!candidate) continue;
                        try {
                            const url = new URL(candidate, location.href);
                            if (url.protocol === 'https:' || url.protocol === 'http:') {
                                return `${url.hostname}${url.pathname}`;
                            }
                        } catch (_error) {}
                        }
                    return '';
                })"""
        )
        if not isinstance(value, list):
            return []
        return [str(item or "") for item in value]

    async def _upload_image(self, image_path: str) -> dict:
        """打开正文图片面板并上传一次；绝不回退封面或页面首个文件框。"""

        self._require_page_alive("头条号上传图片")
        try:
            before_fingerprints = await self._read_editor_image_fingerprints()
            image_button = self.page.locator(IMAGE_BUTTON_SELECTOR)
            if await image_button.count() != 1:
                return {"success": False, "error": "头条号正文图片按钮不存在或不唯一"}
            await image_button.click(timeout=8000)
            drawer = self.page.locator(IMAGE_DRAWER_SELECTOR)
            try:
                await drawer.wait_for(state="attached", timeout=10000)
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 头条号打开图片抽屉时页面已关闭"
                    ) from exc
                return {"success": False, "error": "头条号正文图片上传面板未加载"}
            if await drawer.count() != 1:
                return {"success": False, "error": "头条号正文图片抽屉不存在或不唯一"}

            primary_result: dict | None = None
            primary_exception: Exception | None = None
            try:
                primary_result = await self._upload_image_from_open_drawer(
                    image_path,
                    before_fingerprints,
                    drawer,
                )
            except Exception as exc:  # 关闭抽屉后再按明确优先级处理
                primary_exception = exc

            close_error: str | None = None
            close_exception: Exception | None = None
            try:
                close_error = await self._close_exact_image_drawer(drawer)
            except Exception as exc:  # 不允许清理异常覆盖主流程生命周期异常
                close_exception = exc

            if isinstance(primary_exception, BrowserLifecycleError):
                raise primary_exception
            if isinstance(close_exception, BrowserLifecycleError):
                raise close_exception
            if close_exception is not None:
                if self._exception_means_browser_closed(close_exception):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 头条号清理图片抽屉时页面已关闭"
                    ) from close_exception
                close_error = safe_media_error(
                    str(close_exception),
                    fallback="头条号图片抽屉清理失败",
                )
            if primary_exception is not None:
                if self._exception_means_browser_closed(primary_exception):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 头条号上传图片时页面已关闭"
                    ) from primary_exception
                primary_result = {
                    "success": False,
                    "error": safe_media_error(
                        str(primary_exception),
                        fallback="头条号图片上传失败",
                    ),
                }
            if primary_result is None:
                primary_result = {"success": False, "error": "头条号图片上传未返回结果"}
            if close_error:
                primary_error = str(primary_result.get("error") or "").strip()
                message = close_error
                if primary_error:
                    message = f"{primary_error}；{close_error}"
                return {"success": False, "error": message}
            return primary_result
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号上传图片时页面已关闭"
                ) from exc
            return {
                "success": False,
                "error": safe_media_error(
                    str(exc),
                    fallback="头条号图片上传失败",
                ),
            }

    async def _upload_image_from_open_drawer(
        self,
        image_path: str,
        before_fingerprints: list[str],
        drawer,
    ) -> dict:
        """上传后点击抽屉内唯一“确定”，再绑定正文新增图片指纹。"""

        upload_records: list[dict[str, str | int | bool]] = []

        async def on_upload_response(response) -> None:
            if not await self._is_body_image_upload_response(response):
                return
            record: dict[str, str | int | bool] = {
                "status": int(getattr(response, "status", 0) or 0),
                "code": "",
                "reason": "",
                "payload_parsed": False,
                "origin_present": False,
            }
            try:
                payload = await response.json()
            except Exception:  # 响应体不可解析时仅保留 HTTP 状态
                payload = None
            if isinstance(payload, dict):
                record["payload_parsed"] = True
                code = payload.get("code")
                if isinstance(code, (str, int)) and not isinstance(code, bool):
                    record["code"] = str(code)[:32]
                reason = payload.get("message") or payload.get("msg")
                record["reason"] = safe_media_error(reason, fallback="")
                data = payload.get("data")
                record["origin_present"] = bool(
                    isinstance(data, dict) and data.get("origin_image_url")
                )
            upload_records.append(record)

        self.page.on("response", on_upload_response)
        try:
            file_inputs = self.page.locator(BODY_IMAGE_INPUT_SELECTOR)
            try:
                await file_inputs.wait_for(state="attached", timeout=10000)
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 头条号等待图片上传控件时页面已关闭"
                    ) from exc
                return {"success": False, "error": "头条号正文图片上传面板未加载"}
            if await file_inputs.count() != 1:
                return {"success": False, "error": "头条号正文图片上传控件不存在或不唯一"}
            self._last_editor_mutation_at = time.monotonic()
            await file_inputs.set_input_files(str(image_path), timeout=15000)

            # 头条本地上传只把文件放进图片抽屉；必须等待上传项全部成功，
            # 再点击抽屉内的“确定”，图片才会真正插入 ProseMirror 正文。
            confirm_button = self.page.locator(IMAGE_CONFIRM_SELECTOR)
            try:
                await confirm_button.wait_for(state="attached", timeout=20000)
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 头条号等待图片确认按钮时页面已关闭"
                    ) from exc
                return {
                    "success": False,
                    "error": await self._describe_image_upload_failure(
                        upload_records,
                        fallback="头条号图片上传后未出现确认按钮",
                    ),
                }
            if await confirm_button.count() != 1:
                return {"success": False, "error": "头条号图片确认按钮不存在或不唯一"}
            confirm_enabled = False
            for _ in range(40):
                if await confirm_button.is_enabled():
                    confirm_enabled = True
                    break
                drawer_error = await self._read_image_upload_error()
                if drawer_error:
                    return {
                        "success": False,
                        "error": f"头条号图片上传失败：{drawer_error}",
                    }
                conclusive_error = self._conclusive_image_upload_error(upload_records)
                if conclusive_error:
                    return {"success": False, "error": conclusive_error}
                await asyncio.sleep(0.5)
            if not confirm_enabled:
                return {
                    "success": False,
                    "error": await self._describe_image_upload_failure(
                        upload_records,
                        fallback="头条号图片上传未完成，确认按钮仍不可用",
                    ),
                }
            await confirm_button.click(timeout=8000)
            try:
                await drawer.wait_for(state="hidden", timeout=10000)
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 头条号确认图片时页面已关闭"
                    ) from exc
                return {"success": False, "error": "头条号确认图片后抽屉未关闭"}

            after_fingerprints = before_fingerprints
            for _ in range(20):
                await asyncio.sleep(1)
                after_fingerprints = await self._read_editor_image_fingerprints()
                if (
                    len(after_fingerprints) == len(before_fingerprints) + 1
                    and all(after_fingerprints)
                ):
                    break
            if (
                len(after_fingerprints) != len(before_fingerprints) + 1
            ):
                return {"success": False, "error": "上传后编辑器图片数量未增加"}
            if not all(after_fingerprints):
                return {
                    "success": False,
                    "error": "图片已插入，但远程图片指纹尚未生成",
                }
            stable = 0
            for _ in range(6):
                await asyncio.sleep(0.5)
                current = await self._read_editor_image_fingerprints()
                stable = stable + 1 if current == after_fingerprints else 0
                after_fingerprints = current
                if stable >= 2:
                    break
            if stable < 2:
                return {"success": False, "error": "上传后编辑器图片顺序未稳定"}
            remaining = Counter(before_fingerprints)
            added: list[str] = []
            for fingerprint in after_fingerprints:
                if remaining[fingerprint] > 0:
                    remaining[fingerprint] -= 1
                else:
                    added.append(fingerprint)
            if len(added) != 1 or not added[0]:
                return {"success": False, "error": "无法绑定本次上传图片指纹"}
            return {"success": True, "error": "", "fingerprint": added[0]}
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号上传图片时页面已关闭"
                ) from exc
            return {
                "success": False,
                "error": safe_media_error(
                    str(exc),
                    fallback="头条号图片上传失败",
                ),
            }
        finally:
            try:
                self.page.remove_listener("response", on_upload_response)
            except Exception:  # 诊断监听清理不得覆盖主流程结果
                pass

    @staticmethod
    async def _is_body_image_upload_response(response) -> bool:
        """只匹配 multipart 本地上传；不读取 Cookie、请求体或完整请求头。"""

        try:
            parsed = urlparse(str(response.url or ""))
            query = parse_qs(parsed.query, keep_blank_values=False)
            content_type = str(
                await response.request.header_value("content-type") or ""
            ).lower()
            return (
                str(response.request.method or "").upper() == "POST"
                and parsed.netloc == "mp.toutiao.com"
                and parsed.path == IMAGE_UPLOAD_PATH
                and query.get("aid") == ["1231"]
                and query.get("device_platform") == ["web"]
                and bool(query.get("upload_source"))
                and "need_enhance" not in query
                and content_type.startswith("multipart/form-data;")
            )
        except Exception:
            return False

    @staticmethod
    def _conclusive_image_upload_error(
        records: list[dict[str, str | int | bool]],
    ) -> str:
        """把明确 HTTP/平台拒绝转换为不含响应正文的安全错误。"""

        if not records:
            return ""
        record = records[-1]
        status = int(record.get("status") or 0)
        code = str(record.get("code") or "").strip()
        reason = str(record.get("reason") or "").strip()
        if status < 200 or status >= 300:
            message = f"头条号图片上传请求失败（HTTP {status or '未知'}）"
        elif code and code != "0":
            message = f"头条号图片上传被拒绝（平台码 {code}）"
        else:
            return ""
        return f"{message}：{reason}" if reason else message

    async def _read_image_upload_error(self) -> str:
        """仅读取正文图片抽屉内当前上传项的可见错误文本。"""

        try:
            error_nodes = self.page.locator(IMAGE_UPLOAD_ERROR_SELECTOR)
            if await error_nodes.count() == 0:
                return ""
            texts = await error_nodes.all_inner_texts()
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号读取图片上传错误时页面已关闭"
                ) from exc
            return ""
        for text in texts:
            safe_text = safe_media_error(text, fallback="")
            if safe_text:
                return safe_text
        return ""

    async def _describe_image_upload_failure(
        self,
        records: list[dict[str, str | int | bool]],
        *,
        fallback: str,
    ) -> str:
        drawer_error = await self._read_image_upload_error()
        if drawer_error:
            return f"头条号图片上传失败：{drawer_error}"
        conclusive_error = self._conclusive_image_upload_error(records)
        if conclusive_error:
            return conclusive_error
        if records and str(records[-1].get("code") or "") == "0":
            if records[-1].get("origin_present") is True:
                return "头条号图片上传接口已成功，但确认按钮仍不可用"
            return "头条号图片上传返回成功码，但未形成可确认的图片"
        return fallback

    async def _close_exact_image_drawer(self, drawer) -> str | None:
        """只关闭包含正文图片 input 的唯一抽屉；绝不点击页面级通用关闭按钮。"""

        drawer_count = await drawer.count()
        if drawer_count == 0:
            return None
        if drawer_count != 1:
            return "头条号正文图片抽屉不存在或不唯一"
        if not await drawer.is_visible():
            return None
        close_button = self.page.locator(IMAGE_DRAWER_CLOSE_SELECTOR)
        if await close_button.count() != 1:
            return "头条号图片抽屉关闭按钮不存在或不唯一"
        await close_button.click(timeout=8000)
        try:
            await drawer.wait_for(state="hidden", timeout=8000)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号关闭图片抽屉时页面已关闭"
                ) from exc
            return "头条号图片抽屉关闭后仍遮挡编辑器"
        return None

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """头条号草稿自动保存不需要话题；公开话题选择尚未接入，如实报告。"""

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
            "error": "头条号话题选择尚未接入（自动保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """等待自动保存，绑定唯一新增 ``pgc_id``，重开并核对完整图文。"""

        self._require_page_alive("头条号保存草稿")
        evidence = DraftVerificationEvidence()
        self._last_draft_evidence = evidence
        expected_title = self._normalize_platform_title(title)
        if not expected_title or self._expected_persisted_tokens is None:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 头条号缺少冻结标题或图文核验快照",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
            )
        if (
            self._draft_baseline is None
            or self._preflight_title != expected_title
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_UNAVAILABLE: 头条号缺少与本次一致的草稿基线",
                evidence=evidence.finalize(error_code="DRAFT_BASELINE_UNAVAILABLE"),
            )

        self._ensure_autosave_listener()
        try:
            editor = self.page.locator(BODY_SELECTOR)
            await editor.evaluate("element => element.blur()")
            # 等一个完整防抖窗口，不在第一条中间自动保存响应出现时就离开。
            for _ in range(12):
                await asyncio.sleep(1)

            # 离开编辑器既是头条的正常自动保存触发，也是草稿实体的只读核验入口。
            current_id = self._pgc_id_from_url(getattr(self.page, "url", ""))
            latest = await self._fetch_draft_snapshot()
            await asyncio.sleep(2)
            records = self._autosaves_after_last_mutation()
            latest_record = records[-1] if records else None
            if latest_record is not None:
                evidence.mark_save_response(
                    status=latest_record.get("status"),
                    code=self._autosave_platform_code(latest_record),
                )
            else:
                evidence.mark_save_response(status=None)

            latest_rejected = bool(
                latest_record is not None
                and self._autosave_rejected(latest_record)
            )

            baseline = self._draft_baseline
            baseline_title_count = baseline.title_counts.get(expected_title, 0)
            latest_title_count = latest.title_counts.get(expected_title, 0)
            title_delta = latest_title_count - baseline_title_count
            evidence.mark_draft_list(match_count=latest_title_count)

            response_ids = frozenset(
                draft_id
                for record in records
                for draft_id in (record.get("draft_ids") or ())
            )
            if len(response_ids) > 1:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 头条号自动保存响应包含冲突的草稿 ID"
                )
            response_id = next(iter(response_ids)) if response_ids else None
            new_ids = latest.draft_ids - baseline.draft_ids

            title_ids = latest.title_to_ids.get(expected_title, frozenset())
            candidate_id: str | None = None
            binding_source = "baseline_new_id"
            total_delta = latest.total_count - baseline.total_count
            unique_new_id = next(iter(new_ids)) if len(new_ids) == 1 else None
            new_entity_proven = (
                total_delta == 1
                and title_delta == 1
                and unique_new_id is not None
                and unique_new_id in title_ids
            )
            if response_id is not None:
                response_matches_new = (
                    new_entity_proven and response_id == unique_new_id
                )
                if not response_matches_new:
                    raise DraftResultUnknownError(
                        "DRAFT_RESULT_UNKNOWN: 头条号保存响应 ID 未绑定到唯一新增标题实体"
                    )
                candidate_id = unique_new_id
                binding_source = "save_response_id"
            else:
                candidate_id = unique_new_id if new_entity_proven else None
                if current_id and candidate_id != current_id:
                    candidate_id = None
            if candidate_id is None or candidate_id in baseline.draft_ids:
                evidence.mark_entity_binding(
                    bound=False,
                    source=binding_source,
                    id_match=False,
                )
                if latest_rejected:
                    raise ToutiaoDraftSaveRejectedError(
                        "TOUTIAO_DRAFT_SAVE_REJECTED: 头条号自动保存接口明确拒绝本次内容"
                    )
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 头条号未能绑定唯一新增草稿实体"
                )

            edit_url = self._edit_url(candidate_id)
            if edit_url is None:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 头条号新增草稿 ID 无法生成安全编辑地址"
                )
            evidence.set_draft_url(edit_url)
            title_match, blocks_match = await self._verify_persisted_draft(
                expected_title,
                edit_url,
            )
            evidence.mark_reopen(
                title_match=title_match,
                dom_blocks_match=blocks_match,
            )
            if not title_match or not blocks_match:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 头条号重开后标题或图文结构不完整"
                )
            # 只有重开标题与完整图文指纹都通过后才绑定实体，避免基类把
            # 错 ID 或不完整草稿降级成“已保存但有警告”。
            evidence.mark_entity_binding(
                bound=True,
                source=binding_source,
                id_match=True,
            )
            evidence.finalize()
            logger.info("头条号草稿已绑定并重开核验: pgc_id={}", candidate_id)
            return edit_url
        except (ToutiaoDraftSaveRejectedError, DraftBaselineError):
            raise
        except DraftResultUnknownError as exc:
            if getattr(exc, "evidence", None) is None:
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
                    "DRAFT_RESULT_UNKNOWN: 头条号自动保存期间浏览器已关闭",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                ) from exc
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 头条号草稿持久化核验失败",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
            ) from exc

    def _autosaves_after_last_mutation(self) -> list[dict[str, object]]:
        return [
            record
            for record in self._autosave_records
            if float(record.get("received_at") or 0) >= self._last_editor_mutation_at
        ]

    @staticmethod
    def _autosave_platform_code(record: dict[str, object]) -> str | None:
        values = [record.get("code"), record.get("err_no")]
        present = [str(value).strip() for value in values if value not in (None, "")]
        if not present:
            return None
        return "0" if all(value == "0" for value in present) else "nonzero"

    @classmethod
    def _autosave_rejected(cls, record: dict[str, object]) -> bool:
        status = record.get("status")
        return (
            not isinstance(status, int)
            or not 200 <= status < 300
            or cls._autosave_platform_code(record) == "nonzero"
        )

    async def _verify_persisted_draft(
        self,
        expected_title: str,
        edit_url: str,
    ) -> tuple[bool, bool]:
        """精确重开本次 ``pgc_id``，核对标题和全部 DOM token。"""

        await self.page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
        await self.page.wait_for_selector(TITLE_SELECTOR, timeout=20000)
        await self.page.wait_for_selector(BODY_SELECTOR, timeout=20000)
        actual_title = (
            await self.page.locator(TITLE_SELECTOR).input_value()
        ).strip()
        expected_tokens = self._expected_persisted_tokens or []
        actual_tokens: list[dict[str, str]] = []
        for _ in range(20):
            actual_tokens = await self._read_editor_tokens()
            if actual_tokens == expected_tokens:
                break
            await asyncio.sleep(1)
        return actual_title == expected_title, actual_tokens == expected_tokens

    async def verify_draft_readonly(self, title: str) -> dict:
        """按精确标题只读查询；同名多条时不猜测、不写平台。"""

        expected_title = self._normalize_platform_title(title)
        if not expected_title:
            return {
                "title_matched": False,
                "match_count": 0,
                "draft_url": None,
                "structure": {"draft_id": None},
            }
        snapshot = await self._fetch_draft_snapshot()
        match_count = snapshot.title_counts.get(expected_title, 0)
        ids = snapshot.title_to_ids.get(expected_title, frozenset())
        draft_id = next(iter(ids)) if match_count == 1 and len(ids) == 1 else None
        safe_url = self._edit_url(draft_id) if draft_id else None
        title_match = match_count == 1
        return {
            "title_matched": title_match,
            "match_count": match_count,
            "draft_url": safe_url if title_match else None,
            "dom_blocks_match": None,
            "structure": {"draft_id": draft_id},
        }

    async def publish_now(self, title: str = "") -> str:
        raise PlatformNotImplementedError("头条号公开发布未开启")
