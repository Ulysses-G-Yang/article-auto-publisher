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

2026-08 历史验收曾出现：**头条自动化草稿保存被平台拒绝**——
- 自动保存请求（带正文，save=0）返回 code 7050「保存失败」；
  空正文保存可通过（深诊空草稿成功落库）。
- 当时排查过：封面模式（选无封面 coverType=1 仍 7050）、输入节奏
  （超真人慢速/随机停顿/打错重打仍 7050）、频率（冷却后仍 7050）、
  发布路径（点「预览并发布」同样 save=0 → 7050）。
- 人工浏览器保存/发布正常，提示启动环境可能有关；历史记录没有平台侧诊断，
  不能据此确定具体风控算法或排除所有其他原因。
- 当前适配器不绕过平台接口，只实现正常编辑器操作与严格证据链：保存前建立
  ``pgc_id`` 基线，按 Word 顺序写入文字/H2/图片，等待自动保存，绑定唯一
  新增实体并重开核对标题、图文和图片指纹。平台明确拒绝或证据不足时停止
  且不自动重试。

2026-09-01 真实验收：改为启动普通系统 Chrome，再通过固定非零本地调试端口
连接 CDP；同一七图 Word 已产生唯一新 ``pgc_id``，重开后标题、29 个图文块及
7 张图片顺序一致。头条适配器因此必须使用本文件的原生 Chrome + CDP 初始化，
不退回此前失败的 ``launch_persistent_context`` 路线；CDP 本身不是风控保证。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from loguru import logger
from playwright.async_api import async_playwright

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
from platforms.toutiao_publication import ToutiaoPublicationMixin

LOGIN_URL = "https://mp.toutiao.com/auth/page/login"
HOME_URL = "https://mp.toutiao.com/profile_v4/"
PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
DRAFT_BOX_URL = "https://mp.toutiao.com/profile_v4/manage/draft"
TITLE_SELECTOR = "textarea[placeholder*='文章标题']"
BODY_SELECTOR = "div.ProseMirror[contenteditable='true']"
CREATE_ARTICLE_LINK_SELECTOR = "a[href='/profile_v4/graphic/publish']"
EDITOR_EXIT_SELECTOR = (
    "div.menu-tab-stick-header-fixer > svg:has("
    "circle[cx='12'][cy='12'][r='11.5'])"
)
HEADING_BUTTON_SELECTOR = (
    ".syl-editor-toolbar .syl-toolbar-tool.header.static "
    "button.syl-toolbar-button"
)
IMAGE_BUTTON_SELECTOR = (
    ".syl-editor-toolbar .syl-toolbar-tool.image.static "
    "button.syl-toolbar-button"
)
IMAGE_DRAWER_SELECTOR = (
    ".byte-drawer-wrapper:has(.byte-tabs-header-title):visible"
)
IMAGE_UPLOAD_TAB_IN_DRAWER_SELECTOR = ".byte-tabs-header-title"
BODY_IMAGE_INPUT_IN_DRAWER_SELECTOR = (
    ".upload-image-panel [data-e2e='image-upload'] "
    "input[type='file'][accept*='image']"
)
IMAGE_CONFIRM_IN_DRAWER_SELECTOR = "button[data-e2e='imageUploadConfirm-btn']"
IMAGE_DRAWER_CLOSE_IN_DRAWER_SELECTOR = ".byte-drawer-close-icon"
IMAGE_UPLOAD_ERROR_IN_DRAWER_SELECTOR = (
    ".upload-image-wrapper .pic-select-image-item .error, "
    ".upload-image-wrapper .pic-select-image-item .size-err"
)
IMAGE_UPLOAD_TAB_SELECTOR = (
    f"{IMAGE_DRAWER_SELECTOR} {IMAGE_UPLOAD_TAB_IN_DRAWER_SELECTOR}"
)
BODY_IMAGE_INPUT_SELECTOR = (
    f"{IMAGE_DRAWER_SELECTOR} {BODY_IMAGE_INPUT_IN_DRAWER_SELECTOR}"
)
IMAGE_DRAWER_CLOSE_SELECTOR = (
    f"{IMAGE_DRAWER_SELECTOR} {IMAGE_DRAWER_CLOSE_IN_DRAWER_SELECTOR}"
)
IMAGE_CONFIRM_SELECTOR = (
    f"{IMAGE_DRAWER_SELECTOR} {IMAGE_CONFIRM_IN_DRAWER_SELECTOR}"
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
CHROME_PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
CDP_READY_ATTEMPTS = 40
CDP_READY_INTERVAL_SECONDS = 0.25


def _find_system_chrome() -> Path | None:
    """定位 Windows 系统 Chrome；不记录候选路径。"""

    candidates: list[Path] = []
    configured = os.environ.get("ARTICLEOPS_CHROMIUM_EXECUTABLE", "").strip()
    if configured:
        candidates.append(Path(configured))
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(variable, "").strip()
        if root:
            candidates.append(
                Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
    for command in ("chrome.exe", "chrome"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


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


class ToutiaoPlatform(ToutiaoPublicationMixin, BasePlatform):
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
        self._autosave_request_handler = None
        self._autosave_request_generations: dict[int, int] = {}
        self._mutation_generation = 0
        self._last_editor_mutation_at = 0.0
        self._active_draft_id: str | None = None
        self._cdp_browser = None
        self._cdp_owner_verified = False
        self._native_chrome_process: asyncio.subprocess.Process | None = None
        self._lifecycle_stage = "未初始化"
        self._lifecycle_events: list[dict[str, object]] = []
        self._cleanup_started = False

    def _record_lifecycle_event(self, event: str) -> None:
        """记录不含账号和页面内容的浏览器生命周期证据。"""

        process = self._native_chrome_process
        record = {
            "event": event,
            "stage": self._lifecycle_stage,
            "native_process_returncode": (
                process.returncode if process is not None else None
            ),
            "expected_cleanup": self._cleanup_started,
        }
        self._lifecycle_events.append(record)
        logger.warning(
            "头条号浏览器生命周期事件: event={}, stage={}, process_returncode={}",
            record["event"],
            record["stage"],
            record["native_process_returncode"],
        )

    def _require_page_alive(self, stage: str = ""):
        """在基类检查前保存阶段，便于区分页面关闭来源。"""

        if stage:
            self._lifecycle_stage = stage
        return super()._require_page_alive(stage)

    async def initialize(self):
        """以普通系统 Chrome 启动，再经 CDP 驱动同一个账号 Profile。"""
        self.invalidate_delivery_identity()

        os.environ.pop("NODE_OPTIONS", None)
        chrome_profile_dir = self.profile_dir or Path(
            self.cfg["paths"].get(
                "data",
                os.path.join(os.path.dirname(__file__), "..", "data"),
            )
        ) / "chrome_profiles" / self.platform_name
        chrome_profile_dir = chrome_profile_dir.resolve()
        if self.strict_profile_lock and not chrome_profile_dir.is_dir():
            raise PlatformAutomationError(
                "PROFILE_NOT_FOUND: 账号浏览器 Profile 不存在"
            )
        chrome_profile_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = chrome_profile_dir
        if any(
            (chrome_profile_dir / name).exists()
            for name in CHROME_PROFILE_LOCK_NAMES
        ):
            raise PlatformAutomationError(
                "PROFILE_IN_USE: 账号浏览器 Profile 正在被其他流程占用"
            )

        chrome = _find_system_chrome()
        if chrome is None:
            raise PlatformAutomationError(
                "TOUTIAO_NATIVE_CHROME_NOT_FOUND: 未找到系统 Chrome"
            )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            debug_port = int(probe.getsockname()[1])

        try:
            self.playwright = await async_playwright().start()
            self._native_chrome_process = await asyncio.create_subprocess_exec(
                str(chrome),
                f"--remote-debugging-port={debug_port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={chrome_profile_dir}",
                "--profile-directory=Default",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-proxy-server",
                "--new-window",
                "about:blank",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            endpoint = f"http://127.0.0.1:{debug_port}"
            last_error: Exception | None = None
            for _ in range(CDP_READY_ATTEMPTS):
                if self._native_chrome_process.returncode is not None:
                    raise RuntimeError("native Chrome exited before CDP became ready")
                try:
                    self._cdp_browser = (
                        await self.playwright.chromium.connect_over_cdp(
                            endpoint,
                            timeout=2000,
                        )
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - bounded local readiness
                    last_error = exc
                    await asyncio.sleep(CDP_READY_INTERVAL_SECONDS)
            else:
                raise RuntimeError("CDP endpoint was not ready") from last_error

            listener_pid = await self._read_cdp_listener_pid(debug_port)
            if listener_pid != self._native_chrome_process.pid:
                raise RuntimeError("CDP listener does not belong to launched Chrome")
            self._cdp_owner_verified = True
            if len(self._cdp_browser.contexts) != 1:
                raise RuntimeError("unexpected CDP browser context count")
            self.context = self._cdp_browser.contexts[0]
            if len(self.context.pages) != 1 or self.context.pages[0].url != "about:blank":
                raise RuntimeError("unexpected restored Chrome pages")
            self.page = self.context.pages[0]
            self.page.on(
                "close",
                lambda *_: self._record_lifecycle_event("page_close"),
            )
            self.page.on(
                "crash",
                lambda *_: self._record_lifecycle_event("page_crash"),
            )
            self.context.on(
                "close",
                lambda *_: self._record_lifecycle_event("context_close"),
            )
            self._cdp_browser.on(
                "disconnected",
                lambda *_: self._record_lifecycle_event("browser_disconnected"),
            )
            if await self.page.evaluate("() => navigator.webdriver === true"):
                raise RuntimeError("Chrome exposed navigator.webdriver")
            self.browser = self._cdp_browser
        except BaseException as exc:
            await self.cleanup()
            if not isinstance(exc, Exception):
                raise
            raise PlatformAutomationError(
                "TOUTIAO_CDP_START_FAILED: 头条号普通 Chrome 启动失败"
            ) from exc

        self._identity_payload = None
        self._preflight_title = None
        self._draft_baseline = None
        self._expected_persisted_tokens = None
        self._autosave_records = []
        self._autosave_handler = None
        self._autosave_request_handler = None
        self._autosave_request_generations = {}
        self._mutation_generation = 0
        self._last_editor_mutation_at = 0.0
        self._active_draft_id = None
        self._lifecycle_stage = "初始化完成"
        self._cleanup_started = False

    async def _safe_simulate_scroll(
        self,
        scroll_times: int | None = None,
        stage: str = "滚动",
    ) -> None:
        """头条草稿依赖退出自动保存；保存前不做无关滚动。"""

        self._require_page_alive(stage)

    async def _safe_random_mouse_movement(
        self,
        stage: str = "鼠标移动",
    ) -> None:
        """头条草稿依赖退出自动保存；保存前不做无关鼠标移动。"""

        self._require_page_alive(stage)

    @staticmethod
    async def _read_cdp_listener_pid(port: int) -> int | None:
        """只读核对本地调试端口归属，避免误连或误关其它 Chrome。"""

        command = (
            "$connection = Get-NetTCPConnection "
            f"-LocalAddress 127.0.0.1 -LocalPort {port} -State Listen "
            "-ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if ($connection) { $connection.OwningProcess }"
        )
        process = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate()
        value = output.decode("ascii", errors="ignore").strip()
        return int(value) if process.returncode == 0 and value.isdigit() else None

    async def _wait_for_profile_release(self) -> None:
        """等待本次 Chrome 自然释放 Profile；绝不删除 Singleton 文件。"""

        profile_dir = self.profile_dir
        if profile_dir is None:
            return
        for _ in range(40):
            if not any(
                (profile_dir / name).exists()
                for name in CHROME_PROFILE_LOCK_NAMES
            ):
                return
            await asyncio.sleep(0.1)
        raise PlatformAutomationError(
            "TOUTIAO_PROFILE_NOT_RELEASED: 头条号 Chrome 未释放账号 Profile"
        )

    async def cleanup(self):
        """关闭本次 CDP 会话和本次启动的 Chrome，不触碰其它进程。"""
        self.invalidate_delivery_identity()

        self._cleanup_started = True
        self._lifecycle_stage = "主动清理"
        try:
            if self._cdp_browser is not None and self._cdp_owner_verified:
                await self._cdp_browser.close()
        except Exception:  # noqa: BLE001 - cleanup steps must be independent
            pass
        try:
            if self._native_chrome_process is not None:
                try:
                    await asyncio.wait_for(
                        self._native_chrome_process.wait(),
                        timeout=8,
                    )
                except TimeoutError:
                    self._native_chrome_process.terminate()
                    try:
                        await asyncio.wait_for(
                            self._native_chrome_process.wait(),
                            timeout=8,
                        )
                    except TimeoutError:
                        self._native_chrome_process.kill()
                        await asyncio.wait_for(
                            self._native_chrome_process.wait(),
                            timeout=8,
                        )
                await self._wait_for_profile_release()
        finally:
            if self.playwright is not None:
                try:
                    await self.playwright.stop()
                except Exception:  # noqa: BLE001 - owned Chrome already handled
                    pass
            self.browser = None
            self.context = None
            self.page = None
            self.playwright = None
            self._cdp_browser = None
            self._cdp_owner_verified = False
            self._native_chrome_process = None

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
            if not await self._has_session_cookie_signal():
                self.last_login_error = await self.login_obstacle_code()
                return False
            # 身份捕获本身会打开一次工作台；这里不再提前重复导航 HOME。
            identity = await self.fetch_identity_payload()
            if identity.get("ok"):
                return True
            self.last_login_error = "TOUTIAO_IDENTITY_MISSING: 会话存在但身份未确认"
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
        await self.actions.perform(
            self.page.goto,
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        # 确保「扫码登录」Tab 激活
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

        if self._identity_payload and self._identity_payload.get("ok") is True:
            return self._identity_payload
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
            await self.actions.perform(
                self.page.goto,
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

    async def _fetch_draft_snapshot(
        self,
        *,
        exit_editor: bool = False,
        expected_title: str | None = None,
    ) -> _ToutiaoDraftSnapshot:
        """加载草稿列表，只对本次目标标题要求完整 ID 证据。"""

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
            if exit_editor:
                await self._exit_editor_via_native_control()
                if urlparse(str(getattr(self.page, "url", ""))).path != urlparse(
                    DRAFT_BOX_URL
                ).path:
                    await self.actions.perform(
                        self.page.goto,
                        DRAFT_BOX_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
            else:
                await self.actions.perform(
                    self.page.goto,
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

            if exit_editor and expected_title and self._draft_baseline is not None:
                # 原生退出触发的云端落库与草稿列表更新存在延迟。最多等待
                # 60 秒；只在第 10/30/50 秒刷新草稿页，绝不再次点击退出。
                normalized_expected = self._normalize_platform_title(expected_title)
                baseline_count = self._draft_baseline.title_counts.get(
                    normalized_expected,
                    0,
                )
                active_draft_id = self._active_draft_id
                active_response_bound = bool(
                    active_draft_id
                    and any(
                        active_draft_id in (record.get("draft_ids") or ())
                        for record in self._autosave_records
                    )
                )

                def expected_entity_ready(candidate: dict) -> bool:
                    title_counts = candidate.get("titleCounts") or {}
                    current_count = int(title_counts.get(normalized_expected) or 0)
                    matching_ids: set[str] = set()
                    for title, draft_id in candidate.get("capturedItems") or []:
                        if title == normalized_expected and self._safe_pgc_id(draft_id):
                            matching_ids.add(str(draft_id))
                    for item in candidate.get("hrefItems") or []:
                        if not isinstance(item, dict):
                            continue
                        if self._normalize_platform_title(item.get("title")) != normalized_expected:
                            continue
                        draft_id = self._pgc_id_from_url(item.get("href"))
                        if draft_id:
                            matching_ids.add(draft_id)
                    count_advanced = current_count >= baseline_count + 1
                    if active_draft_id:
                        # 标题初始化已从平台自动保存响应绑定新 pgc_id 时，
                        # 草稿列表可能延迟几十秒才渲染该卡片。此时立即结束
                        # 列表轮询，后续仍必须按该 ID 重开并精确核对全部
                        # 图文 token，不能仅凭响应 ID 宣告成功。
                        return (
                            count_advanced and active_draft_id in matching_ids
                        ) or active_response_bound
                    return count_advanced and len(matching_ids) == current_count

                state = await read_complete_state()
                for attempt in range(60):
                    if expected_entity_ready(state):
                        break
                    if attempt in {9, 29, 49}:
                        await self.actions.perform(
                            self.page.goto,
                            DRAFT_BOX_URL,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await self.page.wait_for_selector(
                            ".draft-page .list-count",
                            timeout=20000,
                        )
                    await asyncio.sleep(1)
                    state = await read_complete_state()
            else:
                # 基线读取仍要求快速稳定，避免把正在变化的旧列表当作基线。
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
        normalized_expected = self._normalize_platform_title(expected_title)
        expected_count = title_counts.get(normalized_expected, 0)
        expected_ids = frozen_title_ids.get(normalized_expected, frozenset())
        if normalized_expected:
            active_draft_id = self._active_draft_id
            active_is_new = bool(
                active_draft_id
                and self._draft_baseline is not None
                and active_draft_id not in self._draft_baseline.draft_ids
            )
            if active_is_new:
                active_response_bound = any(
                    active_draft_id in (record.get("draft_ids") or ())
                    for record in self._autosave_records
                )
                if active_draft_id not in expected_ids and not active_response_bound:
                    raise DraftBaselineError(
                        "DRAFT_BASELINE_UNAVAILABLE: 头条号本次新草稿 ID 未出现在草稿列表"
                    )
            elif len(expected_ids) != expected_count:
                raise DraftBaselineError(
                    "DRAFT_BASELINE_UNAVAILABLE: 头条号目标标题的草稿 ID 未完整稳定加载"
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
        self._draft_baseline = await self._fetch_draft_snapshot(
            expected_title=expected_title
        )

    def _mark_editor_mutation(self) -> int:
        """为下一次平台自动保存建立不可混淆的内容代次。"""

        self._mutation_generation += 1
        self._last_editor_mutation_at = time.monotonic()
        return self._mutation_generation

    def _capture_autosave_request(self, request) -> None:
        """在请求发出时绑定内容代次，避免旧响应晚到污染新版本。"""

        try:
            parsed = urlparse(str(request.url or ""))
            if (
                str(request.method or "").upper() == "POST"
                and parsed.netloc == "mp.toutiao.com"
                and parsed.path == AUTOSAVE_PATH
            ):
                self._autosave_request_generations[id(request)] = (
                    self._mutation_generation
                )
        except Exception:  # noqa: BLE001
            return

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
                    "generation": self._autosave_request_generations.pop(
                        id(response.request),
                        None,
                    ),
                    "received_at": time.monotonic(),
                    "status": status,
                    "code": payload.get("code"),
                    "err_no": payload.get("err_no"),
                    "reason": safe_media_error(
                        payload.get("reason")
                        or payload.get("message")
                        or payload.get("msg")
                        or payload.get("err_msg")
                        or payload.get("err_tips"),
                        fallback="",
                    )[:120],
                    "draft_ids": self._extract_save_ids(payload),
                }
            )
        except Exception:  # noqa: BLE001
            return

    def _ensure_autosave_listener(self) -> None:
        if self._autosave_handler is not None:
            return
        self._autosave_request_handler = self._capture_autosave_request
        self._autosave_handler = self._capture_autosave_response
        self.page.on("request", self._autosave_request_handler)
        self.page.on("response", self._autosave_handler)

    async def _wait_for_autosave_barrier(
        self,
        *,
        generation: int,
        stage: str,
        require_draft_id: bool = False,
    ) -> dict[str, object]:
        """等待本次变更后的平台自动保存稳定，失败即停止后续写入。"""

        previous_count = -1
        stable_rounds = 0
        last_records: list[dict[str, object]] = []
        for _ in range(80):
            self._require_page_alive(f"头条号等待{stage}自动保存")
            records = [
                record
                for record in self._autosave_records
                if record.get("generation") == generation
            ]
            if records:
                if len(records) == previous_count:
                    stable_rounds += 1
                else:
                    previous_count = len(records)
                    stable_rounds = 0
                last_records = records
                if stable_rounds >= 4:
                    latest = records[-1]
                    platform_code = self._autosave_platform_code(latest)
                    if platform_code != "0":
                        code = platform_code or "未知"
                        reason = str(latest.get("reason") or "").strip()
                        suffix = f"：{reason}" if reason else ""
                        raise ToutiaoDraftSaveRejectedError(
                            f"TOUTIAO_DRAFT_SAVE_REJECTED: 头条号{stage}自动保存失败"
                            f"（平台码 {code}）{suffix}"
                        )
                    response_ids = frozenset(
                        draft_id
                        for record in records
                        for draft_id in (record.get("draft_ids") or ())
                    )
                    if len(response_ids) > 1:
                        raise DraftResultUnknownError(
                            "DRAFT_RESULT_UNKNOWN: 头条号自动保存返回多个草稿 ID"
                        )
                    response_id = next(iter(response_ids)) if response_ids else None
                    current_id = self._pgc_id_from_url(
                        str(getattr(self.page, "url", "") or "")
                    )
                    candidate_id = response_id or current_id or self._active_draft_id
                    if (
                        response_id
                        and current_id
                        and response_id != current_id
                    ):
                        raise DraftResultUnknownError(
                            "DRAFT_RESULT_UNKNOWN: 头条号页面与保存响应的草稿 ID 不一致"
                        )
                    if (
                        candidate_id
                        and self._active_draft_id
                        and candidate_id != self._active_draft_id
                    ):
                        raise DraftResultUnknownError(
                            "DRAFT_RESULT_UNKNOWN: 头条号自动保存切换到了另一草稿实体"
                        )
                    if require_draft_id and not candidate_id:
                        await asyncio.sleep(0.25)
                        continue
                    if candidate_id:
                        self._active_draft_id = candidate_id
                    return latest
            await asyncio.sleep(0.25)

        if last_records and self._autosave_rejected(last_records[-1]):
            raise ToutiaoDraftSaveRejectedError(
                f"TOUTIAO_DRAFT_SAVE_REJECTED: 头条号{stage}自动保存失败"
            )
        raise DraftResultUnknownError(
            f"DRAFT_RESULT_UNKNOWN: 头条号{stage}后未取得成功的自动保存证据"
        )

    async def _exit_editor_via_native_control(self) -> None:
        """点击平台原生退出一次；禁止使用浏览器历史回退代替保存流程。"""

        self._require_page_alive("头条号点击原生退出")
        control = self.page.locator(EDITOR_EXIT_SELECTOR)
        if await control.count() != 1 or not await control.is_visible():
            raise SelectorError(
                "SELECTOR_ERROR: 头条号编辑器原生退出控件不存在或不唯一"
            )
        await self.actions.perform(control.click, timeout=8000)
        publish_path = urlparse(PUBLISH_URL).path
        previous_count = -1
        stable_rounds = 0
        for _ in range(80):
            self._require_page_alive("头条号等待原生退出")
            current = urlparse(str(getattr(self.page, "url", "") or ""))
            records = self._autosaves_after_last_mutation()
            if len(records) == previous_count:
                stable_rounds += 1
            else:
                previous_count = len(records)
                stable_rounds = 0
            same_origin = current.netloc == urlparse(PUBLISH_URL).netloc
            left_editor = current.path != publish_path
            login_redirect = "/login" in current.path or "/auth/" in current.path
            if same_origin and left_editor and not login_redirect and stable_rounds >= 8:
                return
            await asyncio.sleep(0.25)
        raise DraftResultUnknownError(
            "DRAFT_RESULT_UNKNOWN: 头条号点击原生退出后仍停留在编辑器"
        )

    async def _wait_for_editor_settle_before_exit(self) -> None:
        """完整图文静置 12 秒；期间结构漂移则禁止退出。"""

        expected_tokens = self._expected_persisted_tokens
        if expected_tokens is None:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 头条号缺少退出前图文核验快照"
            )
        for _ in range(12):
            self._require_page_alive("头条号等待完整图文稳定")
            await asyncio.sleep(1)
            if await self._read_editor_tokens() != expected_tokens:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 头条号等待退出期间图文结构发生变化"
                )

    async def navigate_to_editor(self):
        """从草稿页点击平台原生文章入口，避免恢复旧编辑会话。"""

        self._require_page_alive("头条号打开编辑器")
        self._ensure_autosave_listener()
        current = urlparse(str(getattr(self.page, "url", "") or ""))
        expected_draft = urlparse(DRAFT_BOX_URL)
        if (
            current.netloc != expected_draft.netloc
            or current.path != expected_draft.path
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_UNAVAILABLE: 头条号创建新稿前未停留在草稿页"
            )
        create_link = self.page.locator(CREATE_ARTICLE_LINK_SELECTOR)
        if await create_link.count() != 1 or not await create_link.is_visible():
            raise SelectorError(
                "SELECTOR_ERROR: 头条号草稿页的文章创作入口不存在或不唯一"
            )
        await self.actions.perform(create_link.click, timeout=8000)
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
        current_id = self._pgc_id_from_url(getattr(self.page, "url", ""))
        if (
            current_id
            and self._draft_baseline is not None
            and current_id in self._draft_baseline.draft_ids
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_UNAVAILABLE: 头条号文章入口恢复了已有草稿，已停止写入"
            )

    async def fill_title(self, title: str):
        """在原生新稿会话中写入标题，并等待平台绑定新草稿 ID。"""

        self._require_page_alive("头条号填写标题")
        expected = self._normalize_platform_title(title)
        if len(expected) < 2:
            raise SelectorError("SELECTOR_ERROR: 头条号标题不足 2 个字")
        title_input = self.page.locator(TITLE_SELECTOR)
        if await title_input.count() != 1:
            raise SelectorError("SELECTOR_ERROR: 头条号编辑器标题输入框不存在或不唯一")
        generation = self._mark_editor_mutation()
        await self.actions.fill(title_input, expected)
        actual = (await title_input.input_value()).strip()
        if actual != expected:
            raise SelectorError("SELECTOR_ERROR: 头条号标题回读不一致")
        if expected != " ".join(str(title or "").split()):
            logger.info("头条号标题已按平台 30 字上限安全截断")
        # 原生“创作 → 文章”先通过标题自动保存创建 pgc_id。必须等这次
        # 成功回执绑定到当前会话后再写正文，否则后续正文请求会在草稿实体
        # 尚未稳定时被平台拒绝。这里只等待同页网络回执，禁止 blur、刷新、
        # 跳转或退出，因此标题与正文仍属于同一个原生新稿会话。
        await self._wait_for_autosave_barrier(
            generation=generation,
            stage="标题初始化",
            require_draft_id=True,
        )
        if not self._active_draft_id:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 头条号标题初始化未绑定草稿 ID"
            )
        if (
            self._draft_baseline is not None
            and self._active_draft_id in self._draft_baseline.draft_ids
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_UNAVAILABLE: 头条号标题初始化绑定了已有草稿"
            )
        logger.info(
            "头条号标题已填写并绑定新草稿实体: {} 字",
            len(actual),
        )

    async def fill_content(self, content_blocks: list, images: list):
        """按冻结块顺序写入文字、H2 与正文图片，并在保存前核对 DOM。"""

        self._require_page_alive("头条号填写正文")
        editor = self.page.locator(BODY_SELECTOR)
        try:
            await self.actions.perform(editor.click, timeout=8000)
        except Exception:
            pass
        self._mark_editor_mutation()
        await self.actions.perform(editor.press, "Control+A")
        await self.actions.perform(editor.press, "Backspace")

        expected_tokens: list[dict[str, str]] = []
        expected_images = 0
        uploaded_images = 0
        failed_images: list[dict[str, str]] = []
        wrote_any = False
        previous_block_type: str | None = None
        for block_number, block in enumerate(content_blocks, start=1):
            block_type = str(block.get("type") or "")
            text = str(block.get("text") or "").strip()
            if block_type not in {"text", "heading", "image"}:
                continue
            if block_type != "image" and not text:
                continue
            first_plain_text = not wrote_any and block_type in {"text", "heading"}
            native_text_continuation = (
                wrote_any
                and block_type in {"text", "heading"}
                and previous_block_type in {"text", "heading", "image"}
            )
            native_image_continuation = (
                wrote_any
                and block_type == "image"
                and previous_block_type in {"text", "heading"}
            )
            if (
                first_plain_text
                or native_text_continuation
                or native_image_continuation
            ):
                # 清空后的原生选区已经位于正文。实测再次 focus、Control+End
                # 和重建选区会让头条把首段自动保存判为失败；第一段必须沿用
                # 清空动作留下的原生选区直接输入。连续文字块也只使用原生
                # Enter 换段，避免再次重建选区触发保存失败。
                if native_text_continuation or native_image_continuation:
                    await self.actions.perform(self.page.keyboard.press, "Enter")
                self._mark_editor_mutation()
            else:
                # 工具栏和上传面板会移动焦点；后续块重新锚定正文末尾。
                await self.actions.perform(editor.focus, timeout=8000)
                await self.actions.perform(editor.press, "Control+End")
                self._mark_editor_mutation()
                if wrote_any and not await self._can_reuse_existing_empty_tail():
                    await self.actions.perform(self.page.keyboard.press, "Enter")
            if block_type in {"text", "heading"}:
                if (
                    not (first_plain_text or native_text_continuation)
                    and not await self._stabilize_text_insertion_point()
                ):
                    raise SelectorError(
                        "SELECTOR_ERROR: 头条号正文文字插入位置未稳定"
                    )
                lines = text.splitlines() or [text]
                for index, line in enumerate(lines):
                    if line:
                        await self.actions.insert_text(self.page.keyboard, line)
                    if index < len(lines) - 1:
                        await self.actions.perform(self.page.keyboard.press, "Shift+Enter")
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
                elif (
                    not native_image_continuation
                    and not await self._stabilize_image_insertion_point()
                ):
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
                        if await self._read_editor_tokens() != expected_tokens:
                            failed_images.append(
                                {
                                    "filename": Path(img_path).name,
                                    "error": "头条号正文图片未插入到预期图文位置",
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
            previous_block_type = block_type
            await self._wait_for_content_block_autosave(block_number)
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

    async def _wait_for_content_block_autosave(self, block_number: int) -> None:
        """等待当前块的自动保存窗口；中间拒绝交由最终退出核验裁决。"""

        try:
            await self._wait_for_autosave_barrier(
                generation=self._mutation_generation,
                stage=f"第 {block_number} 个图文块",
            )
        except ToutiaoDraftSaveRejectedError:
            # 头条没有显式保存按钮，新稿编辑期间偶尔会对中间版本返回
            # “保存失败”，但平台原生退出仍可能把完整编辑器状态写入草稿。
            # 这里只结束当前防抖窗口，最终是否成功仍必须由唯一新增 ID
            # 与精确重开后的完整图文结构共同证明。
            logger.warning(
                "头条号第 {} 个图文块的中间自动保存未通过；"
                "继续写入并由原生退出后的云端重开结果裁决",
                block_number,
            )

    async def _can_reuse_existing_empty_tail(self) -> bool:
        """复用编辑器自动生成的末尾空段，避免每块之间多插一行。"""

        try:
            return bool(
                await self.page.evaluate(
                    """() => {
                        const reuseExistingTail = true;
                        const root = document.querySelector('.ProseMirror');
                        const tail = root?.lastElementChild;
                        if (!reuseExistingTail || !tail || tail.tagName !== 'P') {
                            return false;
                        }
                        const isEmptyText = child =>
                            child.nodeType === Node.TEXT_NODE &&
                            (child.nodeValue || '')
                                .replace(/[\u200B-\u200D\uFEFF]/g, '')
                                .trim() === '';
                        const isPlaceholderBreak = child =>
                            child.nodeType === Node.ELEMENT_NODE &&
                            child.tagName === 'BR';
                        const isEditorPlaceholder = child =>
                            child.nodeType === Node.ELEMENT_NODE &&
                            child.matches(
                                'span.syl-placeholder.ProseMirror-widget' +
                                '[contenteditable="false"][ignoreel]'
                            );
                        const children = Array.from(tail.childNodes);
                        const breaks = children.filter(isPlaceholderBreak);
                        const placeholders = children.filter(isEditorPlaceholder);
                        return breaks.length <= 1 && placeholders.length <= 1 &&
                            children.every(
                                child => isEmptyText(child) ||
                                    isPlaceholderBreak(child) ||
                                    isEditorPlaceholder(child)
                            );
                    }"""
                )
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号检查末尾空段时页面已关闭"
                ) from exc
            return False

    async def _stabilize_text_insertion_point(self) -> bool:
        """确认键盘输入会落到唯一的末尾空文本段，而非图片组件前的旧选区。"""

        editor = self.page.locator(BODY_SELECTOR)
        if await editor.count() != 1:
            return False
        try:
            await self.actions.perform(editor.focus, timeout=8000)
            await self.actions.perform(editor.press, "Control+End")
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
                    const isPlaceholderBreak = child =>
                        child.nodeType === Node.ELEMENT_NODE &&
                        child.tagName === 'BR';
                    const isEditorPlaceholder = child =>
                        child.nodeType === Node.ELEMENT_NODE &&
                        child.matches(
                            'span.syl-placeholder.ProseMirror-widget' +
                            '[contenteditable="false"][ignoreel]'
                        );
                    const children = Array.from(node.childNodes);
                    const breaks = children.filter(isPlaceholderBreak);
                    const placeholders = children.filter(isEditorPlaceholder);
                    return breaks.length <= 1 && placeholders.length <= 1 &&
                        children.every(
                        child => isEmptyText(child) || isPlaceholderBreak(child) ||
                            isEditorPlaceholder(child),
                    );
                }"""
            )
            if not tail_is_empty:
                return False
            return await self._wait_for_stable_empty_tail_selection()
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号稳定文字插入位置时页面已关闭"
                ) from exc
            return False

    async def _stabilize_image_insertion_point(self) -> bool:
        """在打开图片抽屉前确认 ProseMirror 选区已落到正文末尾。"""

        editor = self.page.locator(BODY_SELECTOR)
        if await editor.count() != 1:
            return False
        try:
            await self.actions.perform(editor.focus, timeout=8000)
            await self.actions.perform(editor.press, "Control+End")
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
                    const isPlaceholderBreak = child =>
                        child.nodeType === Node.ELEMENT_NODE &&
                        child.tagName === 'BR';
                    const isEditorPlaceholder = child =>
                        child.nodeType === Node.ELEMENT_NODE &&
                        child.matches(
                            'span.syl-placeholder.ProseMirror-widget' +
                            '[contenteditable="false"][ignoreel]'
                        );
                    const children = Array.from(node.childNodes);
                    const breaks = children.filter(isPlaceholderBreak);
                    const placeholders = children.filter(isEditorPlaceholder);
                    return breaks.length <= 1 && placeholders.length <= 1 &&
                        children.every(
                        child => isEmptyText(child) || isPlaceholderBreak(child) ||
                            isEditorPlaceholder(child),
                    );
                }"""
            )
            if not tail_is_empty:
                return False
            return await self._wait_for_stable_empty_tail_selection()
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号稳定图片插入位置时页面已关闭"
                ) from exc
            return False

    async def _wait_for_stable_empty_tail_selection(self) -> bool:
        """等待上传后的 ProseMirror 重绘完成，并重新锚定末尾空段。"""

        editor = self.page.locator(BODY_SELECTOR)
        async def selection_is_stable() -> bool:
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
                        const isEmptyText = child =>
                            child.nodeType === Node.TEXT_NODE &&
                            (child.nodeValue || '')
                                .replace(/[\u200B-\u200D\uFEFF]/g, '')
                                .trim() === '';
                        const isPlaceholderBreak = child =>
                            child.nodeType === Node.ELEMENT_NODE &&
                            child.tagName === 'BR';
                        const isEditorPlaceholder = child =>
                            child.nodeType === Node.ELEMENT_NODE &&
                            child.matches(
                                'span.syl-placeholder.ProseMirror-widget' +
                                '[contenteditable="false"][ignoreel]'
                            );
                        const children = Array.from(tail.childNodes);
                        const breaks = children.filter(isPlaceholderBreak);
                        const placeholders = children.filter(isEditorPlaceholder);
                        return Boolean(
                            selection.isCollapsed && anchor && focus &&
                            (anchor === tail || tail.contains(anchor)) &&
                            (focus === tail || tail.contains(focus)) &&
                            breaks.length <= 1 && placeholders.length <= 1 &&
                            children.every(
                                child => isEmptyText(child) ||
                                    isPlaceholderBreak(child) ||
                                    isEditorPlaceholder(child)
                            )
                        );
                    }"""
                )
            )

        # Control+End 通常已经给出正确选区；先纯等待，禁止用正文根中心点击。
        for _ in range(4):
            if await selection_is_stable():
                return True
            await asyncio.sleep(0.25)

        # 仅当 ProseMirror 重绘后仍未同步时，点击唯一的末尾空 P 一次兜底。
        tail = editor.locator(":scope > p:last-child")
        if await tail.count() != 1:
            return False
        await self.actions.perform(tail.click, timeout=8000, position={"x": 4, "y": 4})
        await self.actions.perform(editor.press, "End")
        for attempt in range(20):
            if await selection_is_stable():
                return True
            if attempt < 19:
                await asyncio.sleep(0.25)
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

        target = await self._capture_selected_block_signature()
        if target is None:
            raise SelectorError("SELECTOR_ERROR: 头条号无法绑定待转换的 H2 段落")
        button = self.page.locator(HEADING_BUTTON_SELECTOR)
        if await button.count() != 1:
            raise SelectorError("SELECTOR_ERROR: 头条号 H2 工具按钮不存在或不唯一")
        await self.actions.perform(button.click, timeout=8000)
        await self.simulator.random_delay(0.2, 0.5)
        is_h2 = await self._block_signature_is_h2(target)
        if not is_h2:
            # 工具栏点击后可能夺走浏览器选区；先用真实点击重新锚定原文字块，
            # 再使用编辑器标准 H2 快捷键一次，最后仍按同一块的 DOM 回读。
            if not await self._restore_block_selection(target):
                raise SelectorError("SELECTOR_ERROR: 头条号 H2 段落重新定位失败")
            await self.actions.perform(self.page.keyboard.press, "Control+Alt+2")
            await self.simulator.random_delay(0.2, 0.5)
            is_h2 = await self._block_signature_is_h2(target)
        if not is_h2:
            raise SelectorError("SELECTOR_ERROR: 头条号 H2 按钮点击后段落层级未生效")

    async def _capture_selected_block_signature(self) -> dict[str, object] | None:
        value = await self.page.evaluate(
            r"""() => {
                const root = document.querySelector('.ProseMirror');
                const selection = window.getSelection();
                let currentBlock = selection?.anchorNode || null;
                if (currentBlock?.nodeType === Node.TEXT_NODE) {
                    currentBlock = currentBlock.parentElement;
                }
                while (currentBlock && currentBlock.parentElement !== root) {
                    currentBlock = currentBlock.parentElement;
                }
                if (!root || !currentBlock) return null;
                const index = Array.from(root.children).indexOf(currentBlock);
                const text = String(currentBlock.innerText || '')
                    .replace(/\s+/g, ' ')
                    .trim();
                return index >= 0 && text ? {index, text} : null;
            }"""
        )
        if not isinstance(value, dict):
            return None
        index = value.get("index")
        text = str(value.get("text") or "").strip()
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or not text:
            return None
        return {"index": index, "text": text}

    async def _block_signature_is_h2(self, target: dict[str, object]) -> bool:
        return bool(
            await self.page.evaluate(
                r"""expected => {
                    const root = document.querySelector('.ProseMirror');
                    const block = root?.children?.[expected.index];
                    if (!block) return false;
                    const text = String(block.innerText || '')
                        .replace(/\s+/g, ' ')
                        .trim();
                    return text === expected.text && Boolean(
                        block.matches('h1, h2') || block.querySelector('h1, h2')
                    );
                }""",
                target,
            )
        )

    async def _restore_block_selection(self, target: dict[str, object]) -> bool:
        try:
            editor = self.page.locator(BODY_SELECTOR)
            block = editor.locator(
                f":scope > :nth-child({int(target['index']) + 1})"
            )
            if await block.count() != 1:
                return False
            await self.actions.perform(block.click, timeout=8000, position={"x": 4, "y": 4})
            await self.actions.perform(self.page.keyboard.press, "End")
            await self.page.evaluate(
                """() => new Promise((resolve) => {
                    requestAnimationFrame(() => requestAnimationFrame(resolve));
                })"""
            )
            return bool(
                await self.page.evaluate(
                    r"""expected => {
                        const root = document.querySelector('.ProseMirror');
                        const block = root?.children?.[expected.index];
                        const selection = window.getSelection();
                        if (!root || !block || !selection ||
                            selection.rangeCount !== 1 || !selection.isCollapsed ||
                            document.activeElement !== root) {
                            return false;
                        }
                        const text = String(block.innerText || '')
                            .replace(/\s+/g, ' ')
                            .trim();
                        const anchor = selection.anchorNode;
                        const focus = selection.focusNode;
                        return text === expected.text && anchor && focus &&
                            (anchor === block || block.contains(anchor)) &&
                            (focus === block || block.contains(focus));
                    }""",
                    target,
                )
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号重新定位 H2 段落时页面已关闭"
                ) from exc
            return False

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
                    const observedImageNode =
                        child.matches('[__syl_tag][contenteditable="false"]') &&
                        Boolean(child.querySelector('templ img')) &&
                        Boolean(child.querySelector('mask img'));
                    const legacyImageNode = child.matches('.pgc-image, .pgc-img');
                    if (observedImageNode || legacyImageNode) {
                        const fingerprints = Array.from(child.querySelectorAll('img'))
                            .map(imageFingerprint)
                            .filter(Boolean);
                        const unique = Array.from(new Set(fingerprints));
                        tokens.push({
                            kind: 'I',
                            text: '',
                            fingerprint: unique.length === 1 ? unique[0] : '',
                        });
                        continue;
                    }
                    // 头条编辑器只有一个“标题”按钮，真实 DOM 生成 H1；
                    // 在统一内容契约中仍投影为正文 H2，避免与文章标题栏混淆。
                    const kind = child.matches('h1, h2') ||
                        Boolean(child.querySelector('h1, h2'))
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
                        if (node.tagName === 'BR') {
                            textBuffer += ' ';
                            return;
                        }
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
            r"""() => {
                const root = document.querySelector('.ProseMirror');
                if (!root) return null;
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
                const fingerprintsForBlock = child => {
                    const observedImageNode =
                        child.matches('[__syl_tag][contenteditable="false"]') &&
                        Boolean(child.querySelector('templ img')) &&
                        Boolean(child.querySelector('mask img'));
                    const legacyImageNode = child.matches('.pgc-image, .pgc-img');
                    if (!observedImageNode && !legacyImageNode) return null;
                    const fingerprints = Array.from(child.querySelectorAll('img'))
                        .map(imageFingerprint)
                        .filter(Boolean);
                    const unique = Array.from(new Set(fingerprints));
                    return unique.length === 1 ? unique[0] : '';
                };
                return Array.from(root.children)
                    .map(fingerprintsForBlock)
                    .filter(value => value !== null);
            }"""
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
            await self.actions.perform(image_button.click, timeout=8000)
            drawer = self.page.locator(IMAGE_DRAWER_SELECTOR)
            try:
                await drawer.wait_for(state="visible", timeout=10000)
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: 头条号打开图片抽屉时页面已关闭"
                    ) from exc
                return {"success": False, "error": "头条号正文图片上传面板未加载"}
            if await drawer.count() != 1:
                return {"success": False, "error": "头条号正文图片抽屉不存在或不唯一"}
            if not await self._activate_local_image_upload_tab(drawer):
                close_error = await self._close_exact_image_drawer(drawer)
                suffix = f"；{close_error}" if close_error else ""
                return {
                    "success": False,
                    "error": (
                        "头条号图片面板未切换到本地上传，已停止本次图片写入"
                        f"{suffix}"
                    ),
                }

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

    async def _activate_local_image_upload_tab(self, drawer) -> bool:
        """强制使用抽屉内“上传图片”，禁止沿用搜图/图库标签页。"""

        try:
            tabs = drawer.locator(IMAGE_UPLOAD_TAB_IN_DRAWER_SELECTOR)
            texts = [str(value or "").strip() for value in await tabs.all_inner_texts()]
            matches = [index for index, value in enumerate(texts) if value == "上传图片"]
            if len(matches) != 1:
                return False
            upload_tab = tabs.nth(matches[0])
            await self.actions.perform(upload_tab.click, timeout=8000)
            active = False
            for _ in range(20):
                class_names = str(await upload_tab.get_attribute("class") or "")
                aria_selected = str(
                    await upload_tab.get_attribute("aria-selected") or ""
                ).lower()
                if "active" in class_names.split() or aria_selected == "true":
                    active = True
                    break
                await asyncio.sleep(0.1)
            if not active:
                return False
            panel = drawer.locator(".upload-image-panel")
            if await panel.count() != 1 or not await panel.is_visible():
                return False
            file_input = drawer.locator(BODY_IMAGE_INPUT_IN_DRAWER_SELECTOR)
            await file_input.wait_for(state="attached", timeout=8000)
            return await file_input.count() == 1
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号切换本地图片上传时页面已关闭"
                ) from exc
            return False

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
            file_inputs = drawer.locator(BODY_IMAGE_INPUT_IN_DRAWER_SELECTOR)
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
            await self.actions.perform(file_inputs.set_input_files, str(image_path), timeout=15000)

            # 头条本地上传只把文件放进图片抽屉；必须等待上传项全部成功，
            # 再点击抽屉内的“确定”，图片才会真正插入 ProseMirror 正文。
            confirm_button = drawer.locator(IMAGE_CONFIRM_IN_DRAWER_SELECTOR)
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
                        drawer=drawer,
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
                drawer_error = await self._read_image_upload_error(drawer)
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
                        drawer=drawer,
                        fallback="头条号图片上传未完成，确认按钮仍不可用",
                    ),
                }
            await self.actions.perform(confirm_button.click, timeout=8000)
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

    async def _read_image_upload_error(self, drawer=None) -> str:
        """仅读取正文图片抽屉内当前上传项的可见错误文本。"""

        try:
            error_nodes = (
                drawer.locator(IMAGE_UPLOAD_ERROR_IN_DRAWER_SELECTOR)
                if drawer is not None
                else self.page.locator(IMAGE_UPLOAD_ERROR_SELECTOR)
            )
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
        drawer=None,
        fallback: str,
    ) -> str:
        drawer_error = await self._read_image_upload_error(drawer)
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
        close_button = drawer.locator(IMAGE_DRAWER_CLOSE_IN_DRAWER_SELECTOR)
        if await close_button.count() != 1:
            return "头条号图片抽屉关闭按钮不存在或不唯一"
        await self.actions.perform(close_button.click, timeout=8000)
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
        """点击原生退出一次，绑定新增 ``pgc_id`` 并重开核对完整图文。"""

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
            # 完整图文已在本地编辑器逐块回读通过；头条没有显式“保存草稿”
            # 按钮，平台约定由原生退出触发最终保存。禁止在这里先 blur 或
            # 强制等待中间自动保存，否则平台的短暂“保存失败”会阻断真正的
            # 退出保存流程。
            actual_title = (
                await self.page.locator(TITLE_SELECTOR).input_value()
            ).strip()
            if actual_title != expected_title:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 头条号退出前标题回读不一致"
                )

            await self._wait_for_editor_settle_before_exit()

            # 离开编辑器既是头条的正常自动保存触发，也是草稿实体的只读核验入口。
            current_id = self._pgc_id_from_url(getattr(self.page, "url", ""))
            latest = await self._fetch_draft_snapshot(
                exit_editor=True,
                expected_title=expected_title,
            )
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
            title_ids = latest.title_to_ids.get(expected_title, frozenset())
            baseline_title_ids = baseline.title_to_ids.get(
                expected_title,
                frozenset(),
            )
            new_ids = title_ids - baseline_title_ids
            candidate_id: str | None = None
            binding_source = "baseline_new_id"
            total_delta = latest.total_count - baseline.total_count
            unique_new_id = next(iter(new_ids)) if len(new_ids) == 1 else None
            active_draft_id = self._active_draft_id
            active_entity_proven = bool(
                active_draft_id
                and active_draft_id not in baseline.draft_ids
                and (
                    (active_draft_id in title_ids and title_delta >= 1)
                    or response_id == active_draft_id
                )
            )
            if active_entity_proven:
                if response_id is not None and response_id != active_draft_id:
                    raise DraftResultUnknownError(
                        "DRAFT_RESULT_UNKNOWN: 头条号保存响应 ID 与标题初始化 ID 冲突"
                    )
                candidate_id = active_draft_id
                binding_source = (
                    "save_response_id"
                    if response_id == active_draft_id
                    else "baseline_new_id"
                )
            else:
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
            if record.get("generation") == self._mutation_generation
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

        await self.actions.perform(
            self.page.goto,
            edit_url,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await self.page.wait_for_selector(TITLE_SELECTOR, timeout=20000)
        await self.page.wait_for_selector(BODY_SELECTOR, timeout=20000)
        expected_tokens = self._expected_persisted_tokens or []
        actual_tokens: list[dict[str, str]] = []
        title_match = False
        for _ in range(20):
            actual_title = (
                await self.page.locator(TITLE_SELECTOR).input_value()
            ).strip()
            title_match = actual_title == expected_title
            actual_tokens = await self._read_editor_tokens()
            if title_match and actual_tokens == expected_tokens:
                break
            await asyncio.sleep(1)
        return title_match, actual_tokens == expected_tokens

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
        snapshot = await self._fetch_draft_snapshot(expected_title=expected_title)
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
