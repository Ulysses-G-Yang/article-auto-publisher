"""什么值得买（smzdm）账号会话适配器。

登录方式说明（2026-08 真实页面探测）：
- smzdm Web 登录页（https://zhiyou.smzdm.com/user/login）只有
  手机号/邮箱 + 密码 + 「60 天内免登录」表单，**没有扫码登录**
  （页面上 120x120 二维码是 App 下载码，不是登录码）。
- 因此本适配器采用「原生 Chrome 人工凭据登录」：显式登录时先释放
  空白 Playwright context，再以同一账号 Profile 启动不带自动化或远程
  调试参数的系统 Chrome，只打开登录页。用户完成操作并关闭整个窗口后，
  才重新建立 Playwright context，执行一次只读会话与身份确认。

登录成功信号：.smzdm.com 出现 sess 会话 cookie（登录后才下发）。
身份提取采用「捕获页面自身响应」模式 + 首页 DOM 兜底。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    DraftResultUnknownError,
    DraftVerificationEvidence,
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
RATE_LIMIT_NOTICE = "您的操作过于频繁，请稍后再试"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name", "nick"}
IDENTITY_UID_KEYS = {"smzdm_id", "uid", "user_id", "id"}
TITLE_SELECTOR = "textarea.article-title"
BODY_SELECTOR = "div.ProseMirror"
DRAFTS_URL = "https://post.smzdm.com/tougao/"
DRAFT_LIST_URL = "https://zhiyou.smzdm.com/user/article/"
SAVE_DRAFT_ENDPOINT = "https://post.smzdm.com/api/draft/save"
BODY_IMAGE_TRIGGER = ".right-menu-bar:has(svg.zicon-picture)"
BODY_IMAGE_INPUT = 'input[type="file"][accept*="image"]'
CHROME_PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
SAVE_RESPONSE_TOP_LEVEL_ID_KEYS = ("draft_id", "draftId", "article_id", "articleId")
SAVE_RESPONSE_DATA_ID_KEYS = (*SAVE_RESPONSE_TOP_LEVEL_ID_KEYS, "id")
SAVE_RESPONSE_CODE_KEYS = ("code", "error_code", "errorCode", "errno", "errcode")

NativeChromeRunner = Callable[[Path, str, float], Awaitable[None]]


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class SmzdmRateLimitedError(PlatformAutomationError):
    """登录页明确提示当前操作被限频。"""

    error_code = "RATE_LIMITED"


class SmzdmLoginCheckError(PlatformAutomationError):
    """SMZDM 本地会话检查发生技术异常。"""

    error_code = "SMZDM_LOGIN_CHECK_ERROR"


class SmzdmNativeChromeNotFoundError(PlatformAutomationError):
    """系统没有找到可供人工登录使用的原生 Chrome。"""

    error_code = "SMZDM_NATIVE_CHROME_NOT_FOUND"


class SmzdmLoginWindowStillOpenError(PlatformAutomationError):
    """原生 Chrome 登录窗口仍在运行，禁止自动结束用户进程。"""

    error_code = "LOGIN_WINDOW_STILL_OPEN"


class SmzdmProfileNotReleasedError(PlatformAutomationError):
    """原生或 Playwright Chrome 尚未释放账号 Profile。"""

    error_code = "SMZDM_PROFILE_NOT_RELEASED"


class SmzdmNativeLoginLaunchError(PlatformAutomationError):
    """原生 Chrome 人工登录交接无法安全完成。"""

    error_code = "SMZDM_NATIVE_LOGIN_FAILED"


def _find_system_chrome() -> Path | None:
    """定位 Windows 系统 Chrome；不记录任何候选绝对路径。"""

    candidates: list[Path] = []
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


async def _run_native_chrome_login(
    profile_dir: Path,
    login_url: str,
    timeout_seconds: float,
) -> None:
    """用同一 Profile 启动无自动化参数的系统 Chrome，并等待整个窗口退出。"""

    chrome = _find_system_chrome()
    if chrome is None:
        raise SmzdmNativeChromeNotFoundError(
            "SMZDM_NATIVE_CHROME_NOT_FOUND: 未找到系统 Chrome"
        )
    try:
        process = await asyncio.create_subprocess_exec(
            str(chrome),
            f"--user-data-dir={profile_dir}",
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            login_url,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise SmzdmNativeChromeNotFoundError(
            "SMZDM_NATIVE_CHROME_NOT_FOUND: 未找到系统 Chrome"
        ) from exc
    except Exception as exc:
        raise SmzdmNativeLoginLaunchError(
            "SMZDM_NATIVE_LOGIN_FAILED: 无法启动原生 Chrome 登录窗口"
        ) from exc

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise SmzdmLoginWindowStillOpenError(
            "LOGIN_WINDOW_STILL_OPEN: 人工登录等待超时，请关闭整个 Chrome 窗口"
        ) from exc
    except asyncio.CancelledError as exc:
        raise SmzdmLoginWindowStillOpenError(
            "LOGIN_WINDOW_STILL_OPEN: 登录任务已取消，请关闭整个 Chrome 窗口"
        ) from exc

    if process.returncode not in (0, None):
        raise SmzdmNativeLoginLaunchError(
            "SMZDM_NATIVE_LOGIN_FAILED: 原生 Chrome 异常退出"
        )


class SmzdmPlatform(BasePlatform):
    """什么值得买账号会话与 DRAFT-only 图文投递适配器。"""

    platform_name = "smzdm"
    SESSION_COOKIE_NAMES = frozenset({"sess"})
    NATIVE_LOGIN_TIMEOUT_SECONDS = 15 * 60
    PROFILE_RELEASE_POLL_ATTEMPTS = 60
    PROFILE_RELEASE_POLL_INTERVAL_SECONDS = 0.5
    POST_IMAGE_PARAGRAPH_POLL_ATTEMPTS = 20
    POST_IMAGE_PARAGRAPH_POLL_INTERVAL_SECONDS = 0.25

    def __init__(
        self,
        *,
        native_chrome_runner: NativeChromeRunner | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._native_chrome_runner = native_chrome_runner or _run_native_chrome_login
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None
        self._expected_persisted_blocks: list[dict] | None = None
        self._media_progress_state: dict[str, int] | None = None
        self._pending_cover_path = ""
        self._preflight_title = ""
        self._preflight_draft_ids: frozenset[str] | None = None
        self._preflight_editor_draft_id: str | None = None

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None

    # ==================== 登录态与身份 ====================

    async def _read_site_cookie_state(self) -> tuple[bool, bool]:
        """仅读取站点 Cookie 名称，返回(有任意Cookie, 有sess)。"""

        if self.context is None:
            raise BrowserLifecycleError(
                "BROWSER_CONTEXT_CLOSED: smzdm 会话检查时浏览器上下文不存在"
            )
        try:
            cookies = await self.context.cookies([
                "https://www.smzdm.com/",
                "https://zhiyou.smzdm.com/",
            ])
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 会话检查时浏览器上下文已关闭"
                ) from exc
            raise SmzdmLoginCheckError(
                "SMZDM_LOGIN_CHECK_ERROR: smzdm 会话检查失败"
            ) from exc
        if not isinstance(cookies, list):
            raise SmzdmLoginCheckError(
                "SMZDM_LOGIN_CHECK_ERROR: smzdm 会话检查返回无效结果"
            )
        names = {
            str(item.get("name") or "")
            for item in cookies
            if isinstance(item, dict)
        }
        return bool(names), bool(names & self.SESSION_COOKIE_NAMES)

    async def _has_session_cookie_signal(self) -> bool:
        """sess 会话 cookie 作为登录成功信号；不返回、不记录 cookie 值。"""

        _has_any_cookie, has_session_cookie = await self._read_site_cookie_state()
        return has_session_cookie

    async def check_login(self) -> bool:
        """只读验证现有 Profile；会话信号触发同源身份检查后才认定有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("smzdm 登录态检测")
            # 先做本地只读信号检查；无任何站点 Cookie 的新候选直接交给
            # 唯一的 login() 导航，避免一次无意义的主页请求。
            await self._raise_if_rate_limited()
            has_any_cookie, _ = await self._read_site_cookie_state()
            if not has_any_cookie:
                self.last_login_error = "LOGIN_REQUIRED: smzdm 账号需要登录"
                return False
            # 有 sess 或其它历史站点 Cookie 时保留一次首页身份检查，兼容
            # 旧 Profile 由其它持久 Cookie 恢复会话的情况；身份仍需同源确认。
            identity = await self.fetch_identity_payload()
            _has_any_cookie_after_home, has_session_cookie = (
                await self._read_site_cookie_state()
            )
            if has_session_cookie and identity.get("ok"):
                return True
            self.last_login_error = "SMZDM_IDENTITY_MISSING: 会话存在但身份未确认"
            return False
        except (BrowserLifecycleError, SmzdmRateLimitedError, SmzdmLoginCheckError):
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 登录态检测时页面已关闭"
                ) from exc
            raise SmzdmLoginCheckError(
                "SMZDM_LOGIN_CHECK_ERROR: smzdm 登录态验证失败"
            ) from exc

    async def login(self):
        """交接给原生 Chrome 人工登录；关闭窗口后恢复 Playwright 只读验证。"""

        profile_dir = self.profile_dir
        if profile_dir is None or not profile_dir.is_dir():
            raise SmzdmNativeLoginLaunchError(
                "SMZDM_NATIVE_LOGIN_FAILED: 账号 Profile 不存在"
            )

        await self._close_playwright_for_native_login()
        await self._wait_for_profile_release(profile_dir)
        await self._native_chrome_runner(
            profile_dir,
            LOGIN_URL,
            float(self.NATIVE_LOGIN_TIMEOUT_SECONDS),
        )
        await self._wait_for_profile_release(profile_dir)
        try:
            await self.initialize()
        except PlatformAutomationError as exc:
            if "PROFILE_IN_USE" in str(exc):
                raise SmzdmProfileNotReleasedError(
                    "SMZDM_PROFILE_NOT_RELEASED: 原生 Chrome 未释放账号 Profile"
                ) from exc
            raise
        self.last_login_error = ""

    async def _close_playwright_for_native_login(self) -> None:
        """关闭当前空白 Playwright context，确保原生 Chrome 独占 Profile。"""

        context = self.context
        playwright = self.playwright
        close_error: Exception | None = None
        try:
            if context is not None:
                await context.close()
        except Exception as exc:  # noqa: BLE001
            if not self._exception_means_browser_closed(exc):
                close_error = exc
        finally:
            self.context = None
            self.page = None
            self.browser = None
        try:
            if playwright is not None:
                await playwright.stop()
        except Exception as exc:  # noqa: BLE001
            if close_error is None and not self._exception_means_browser_closed(exc):
                close_error = exc
        finally:
            self.playwright = None
        if close_error is not None:
            raise SmzdmNativeLoginLaunchError(
                "SMZDM_NATIVE_LOGIN_FAILED: 无法安全释放自动化浏览器"
            ) from close_error

    async def _wait_for_profile_release(self, profile_dir: Path) -> None:
        """等待 Chrome 自己移除 Profile 锁；绝不删除锁文件。"""

        for _ in range(self.PROFILE_RELEASE_POLL_ATTEMPTS):
            if not any(
                (profile_dir / name).exists() for name in CHROME_PROFILE_LOCK_NAMES
            ):
                return
            await asyncio.sleep(self.PROFILE_RELEASE_POLL_INTERVAL_SECONDS)
        raise SmzdmProfileNotReleasedError(
            "SMZDM_PROFILE_NOT_RELEASED: Chrome 未释放账号 Profile"
        )

    async def _visible_rate_limit_notice(self) -> bool:
        """只读可见页面/iframe 元素，不读取表单、Cookie 或网络响应。"""

        self._require_page_alive("smzdm 限频提示检测")
        page = self.page
        if page is None:
            return False
        contexts = [page]
        frames = getattr(page, "frames", None)
        if frames:
            contexts.extend(frame for frame in frames if frame is not page)
        for context in contexts:
            get_by_text = getattr(context, "get_by_text", None)
            locator_factory = getattr(context, "locator", None)
            if not callable(get_by_text) and not callable(locator_factory):
                continue
            try:
                matches = (
                    get_by_text(RATE_LIMIT_NOTICE, exact=False)
                    if callable(get_by_text)
                    else locator_factory(f"text={RATE_LIMIT_NOTICE}")
                )
                count = min(await matches.count(), 8)
                for index in range(count):
                    if await matches.nth(index).is_visible(timeout=500):
                        return True
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: smzdm 限频提示检测时页面已关闭"
                    ) from exc
                continue
        return False

    async def _raise_if_rate_limited(self) -> None:
        if await self._visible_rate_limit_notice():
            self.last_login_error = "RATE_LIMITED"
            raise SmzdmRateLimitedError("RATE_LIMITED")

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
                await self._raise_if_rate_limited()
                for _ in range(8):
                    if self._identity_payload is not None:
                        break
                    await self._raise_if_rate_limited()
                    await asyncio.sleep(1)
            finally:
                try:
                    self.page.remove_listener("response", _on_response)
                except Exception:  # noqa: BLE001
                    pass
        except (BrowserLifecycleError, SmzdmRateLimitedError):
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 身份捕获时页面已关闭"
                ) from exc
            raise SmzdmLoginCheckError(
                "SMZDM_LOGIN_CHECK_ERROR: smzdm 身份捕获失败"
            ) from exc

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
            except Exception as exc:  # noqa: BLE001
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: smzdm 身份兜底时页面已关闭"
                    ) from exc
                raise SmzdmLoginCheckError(
                    "SMZDM_LOGIN_CHECK_ERROR: smzdm 身份兜底失败"
                ) from exc

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
        # preflight 发生在打开本次编辑器之前；旧页面上的 /edit/{id} 绝不能被
        # 当作本次草稿壳。只有点击“发布新文章”并看到标题框后才允许绑定。
        self._preflight_editor_draft_id = None
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
            if self._preflight_draft_ids is not None:
                current_entity = self._current_draft_entity()
                self._preflight_editor_draft_id = (
                    current_entity[0] if current_entity is not None else None
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
        这里只冻结草稿箱实体；本次“发布新文章”生成的 ``/edit/{draft_id}``
        必须等 ``navigate_to_editor()`` 完成后、写入标题前再绑定。
        """

        expected_title = " ".join(str(title or "").split())
        if not expected_title:
            raise DraftResultUnknownError(
                "DRAFT_BASELINE_UNAVAILABLE: smzdm 缺少可核验标题"
            )
        self._preflight_editor_draft_id = None
        entities = await self._load_draft_entities_once()
        self._preflight_title = expected_title
        self._preflight_draft_ids = frozenset(entities)

    async def save_draft(self, title: str = "") -> str:
        """强制刷新自动保存，绑定精确实体并重开核验完整图文。"""

        self._require_page_alive("smzdm 保存草稿")
        evidence = DraftVerificationEvidence()
        self._last_draft_evidence = evidence
        expected_title = " ".join(str(title or "").split())
        if not expected_title:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 自动保存结果缺少可核验标题",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
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
        captured_records: set[tuple[int | None, str | None, frozenset[str]]] = set()

        async def _on_response(response) -> None:
            try:
                if (
                    response.request.method in ("POST", "PUT", "PATCH")
                    and str(response.url or "") == SAVE_DRAFT_ENDPOINT
                ):
                    status = response.status if isinstance(response.status, int) else None
                    platform_code = None
                    draft_ids: frozenset[str] = frozenset()
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            platform_code = self._save_response_code(body)
                            draft_ids = self._save_response_draft_ids(body)
                    except Exception:
                        pass
                    captured_records.add((status, platform_code, draft_ids))
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
                if captured_records:
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

        if not captured_records:
            evidence.mark_save_response(status=None)
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 本次未捕获到成功自动保存响应",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
            )
        if len(captured_records) != 1:
            evidence.mark_save_response(status=None, code="ambiguous")
            evidence.mark_entity_binding(
                bound=False,
                source="save_response_id",
                id_match=False,
            )
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 同一触发窗口捕获到冲突的保存响应",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
            )
        status, platform_code, response_ids = next(iter(captured_records))
        evidence.mark_save_response(status=status, code=platform_code)
        if not isinstance(status, int) or not 200 <= status < 300:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 本次未捕获到成功自动保存响应",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
            )
        if platform_code not in (None, "0"):
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 自动保存平台码未确认成功",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
            )
        try:
            if len(response_ids) > 1:
                evidence.mark_entity_binding(
                    bound=False,
                    source="save_response_id",
                    id_match=False,
                )
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: smzdm 保存响应包含冲突的草稿 ID",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                )
            response_id = next(iter(response_ids)) if len(response_ids) == 1 else None
            current_entity = self._current_draft_entity()
            current_id = current_entity[0] if current_entity is not None else None
            baseline_ids = self._preflight_draft_ids or frozenset()
            preflight_shell_id = self._preflight_editor_draft_id
            if (
                response_id is not None
                and current_id is not None
                and response_id != current_id
            ):
                evidence.mark_entity_binding(
                    bound=False,
                    source="save_response_id",
                    id_match=False,
                )
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: smzdm 保存响应与当前编辑页草稿 ID 不一致",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                )

            candidate_id = response_id or current_id
            binding_source = (
                "save_response_id" if response_id is not None else "existing_draft_id"
            )
            if candidate_id is None:
                edit_url = await self._find_unique_new_draft()
                evidence.mark_draft_list(match_count=1)
                binding_source = "baseline_new_id"
            else:
                if candidate_id in baseline_ids and candidate_id != preflight_shell_id:
                    evidence.mark_entity_binding(
                        bound=False,
                        source=binding_source,
                        id_match=False,
                    )
                    candidate_source = (
                        "保存响应"
                        if binding_source == "save_response_id"
                        else "当前编辑页"
                    )
                    raise DraftResultUnknownError(
                        f"DRAFT_RESULT_UNKNOWN: smzdm {candidate_source}指向无关的保存前草稿",
                        evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                    )
                _, edit_url = self._validated_draft_entity(f"/edit/{candidate_id}")
            evidence.mark_entity_binding(
                bound=True,
                source=binding_source,
                id_match=True,
            )
            evidence.set_draft_url(edit_url)
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
                # ID 差集失败时明确记录“本次实体未绑定”，防止基类仅凭
                # 标题唯一把旧同名草稿降级为成功。
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
                    "DRAFT_RESULT_UNKNOWN: smzdm 自动保存后浏览器已关闭",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                ) from exc
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: smzdm 持久化草稿核验失败",
                evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
            ) from exc

    @staticmethod
    def _safe_draft_id(value: object) -> str | None:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        draft_id = str(value).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", draft_id):
            return None
        return draft_id

    @classmethod
    def _save_response_draft_ids(cls, payload: dict) -> frozenset[str]:
        """从精确保存端点响应的白名单标量字段提取 ID 集合。

        调用方已锁定 ``SAVE_DRAFT_ENDPOINT``，所以只在这里允许 ``data.id``。
        """

        candidates = {
            draft_id
            for key in SAVE_RESPONSE_TOP_LEVEL_ID_KEYS
            if (draft_id := cls._safe_draft_id(payload.get(key))) is not None
        }
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.update(
                draft_id
                for key in SAVE_RESPONSE_DATA_ID_KEYS
                if (draft_id := cls._safe_draft_id(data.get(key))) is not None
            )
        return frozenset(candidates)

    @staticmethod
    def _save_response_code(payload: dict) -> str | None:
        """将平台码压缩成非隐私标签；不保留响应正文或任意字符串。"""

        codes: set[str] = set()
        for key in SAVE_RESPONSE_CODE_KEYS:
            if key not in payload or payload[key] in (None, ""):
                continue
            value = payload[key]
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                codes.add("invalid")
            elif str(value).strip() == "0":
                codes.add("0")
            else:
                codes.add("nonzero")
        if not codes:
            return None
        return next(iter(codes)) if len(codes) == 1 else "ambiguous"

    def _current_draft_entity(self) -> tuple[str, str] | None:
        """仅接受当前页精确、无参数的同源 ``/edit/{id}`` 地址。"""

        try:
            return self._validated_draft_entity(getattr(self.page, "url", ""))
        except DraftResultUnknownError:
            return None

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

    @classmethod
    def _normalize_draft_list_rows(
        cls,
        raw_items: object,
    ) -> dict[str, tuple[str, str]]:
        """把真实内容管理页卡片收敛为 ``id -> (编辑地址, 标题)``。

        内容管理页同时包含已发布文章和草稿。只有状态文本精确为“草稿”的
        ``.pandect-content-common`` 卡片才参与核验；编辑链接、标题链接中的
        ``/edit/{id}``（若存在）以及删除按钮 ``data-postid`` 必须彼此一致。
        """

        if not isinstance(raw_items, list):
            return {}
        entities: dict[str, tuple[str, str]] = {}
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            status = " ".join(str(raw.get("status") or "").split())
            if status != "草稿":
                continue
            title = " ".join(str(raw.get("title") or "").split())
            edit_href = str(raw.get("edit_href") or "").strip()
            if not title or not edit_href:
                continue
            draft_id, edit_url = cls._validated_draft_entity(edit_href)

            title_href = str(raw.get("title_href") or "").strip()
            if title_href:
                title_parts = urlsplit(urljoin(DRAFT_LIST_URL, title_href))
                if re.fullmatch(
                    r"/edit/[A-Za-z0-9_-]{1,128}",
                    title_parts.path,
                ):
                    title_id, _ = cls._validated_draft_entity(title_href)
                    if title_id != draft_id:
                        raise DraftResultUnknownError(
                            "DRAFT_RESULT_UNKNOWN: smzdm 草稿卡片编辑链接 ID 冲突"
                        )

            delete_id_raw = str(raw.get("delete_id") or "").strip()
            if delete_id_raw:
                delete_id = cls._safe_draft_id(delete_id_raw)
                if delete_id is None or delete_id != draft_id:
                    raise DraftResultUnknownError(
                        "DRAFT_RESULT_UNKNOWN: smzdm 草稿卡片删除标识与编辑链接不一致"
                    )

            previous = entities.get(draft_id)
            current = (edit_url, title)
            if previous is not None and previous != current:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: smzdm 同一草稿 ID 对应冲突卡片"
                )
            entities[draft_id] = current
        return entities

    async def _read_draft_list_rows(self) -> dict[str, tuple[str, str]]:
        raw_items = await self.page.evaluate(
            """() => Array.from(
                    document.querySelectorAll('.pandect-content-common')
                ).map(card => {
                    const titleRoot = card.querySelector('.p-pandect-content-title');
                    const titleLink = titleRoot?.querySelector('a') || null;
                    const status = titleRoot?.querySelector('em')?.textContent || '';
                    const editLink = card.querySelector('a.isEdit_[href*="/edit/"]');
                    const deleteButton = card.querySelector('.isDel_[data-postid]');
                    return {
                        title: (titleLink?.textContent || '').trim(),
                        title_href: titleLink?.href || '',
                        status: status.trim(),
                        edit_href: editLink?.href || '',
                        delete_id: deleteButton?.getAttribute('data-postid') || '',
                    };
                })"""
        )
        return self._normalize_draft_list_rows(raw_items)

    async def _load_draft_entities_once(
        self,
        *,
        wait_for_new_ids: frozenset[str] | None = None,
    ) -> dict[str, str]:
        """只导航一次草稿箱，在当前 DOM 内等待实体集合稳定。"""

        try:
            await self.page.goto(
                DRAFT_LIST_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            previous_ids: frozenset[str] | None = None
            stable_samples = 0
            latest: dict[str, str] = {}
            for attempt in range(20):
                rows = await self._read_draft_list_rows()
                latest = {
                    draft_id: edit_url
                    for draft_id, (edit_url, _title) in rows.items()
                }

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

    async def verify_draft_readonly(self, title: str) -> dict:
        """只读核验真实内容管理页中的云端草稿卡片。

        该探针仍只按标题服务于人工查询，所以同名时明确返回歧义；正式投递
        使用保存前 ID 基线、保存响应 ID 和精确编辑页回读，不依赖标题唯一。
        """
        expected_title = " ".join(str(title or "").split())
        if not expected_title:
            return {"error_code": "PROBE_TITLE_MISSING", "error_message": "缺少可核验标题"}
        try:
            await self.page.goto(
                DRAFT_LIST_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            previous_ids: frozenset[str] | None = None
            stable_samples = 0
            entities: dict[str, tuple[str, str]] = {}
            for attempt in range(20):
                entities = await self._read_draft_list_rows()

                current_ids = frozenset(entities)
                if current_ids == previous_ids:
                    stable_samples += 1
                else:
                    stable_samples = 0
                if stable_samples >= 1:
                    break
                previous_ids = current_ids
                if attempt + 1 < 20:
                    await asyncio.sleep(0.5)

            matches = [
                (draft_id, edit_url)
                for draft_id, (edit_url, title_text) in entities.items()
                if title_text == expected_title
            ]
            if len(matches) == 1:
                return {
                    "title_matched": True,
                    "match_count": 1,
                    "draft_url": matches[0][1],
                    "structure": {
                        "source": "cloud_draft_list",
                        "draft_id": matches[0][0],
                    },
                }
            if len(matches) > 1:
                return {
                    "error_code": "PROBE_TITLE_AMBIGUOUS",
                    "error_message": f"草稿箱存在 {len(matches)} 个同名草稿",
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

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
