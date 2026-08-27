from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest

from platforms.smzdm import (
    CHROME_PROFILE_LOCK_NAMES,
    HOME_URL,
    LOGIN_URL,
    SmzdmLoginCheckError,
    SmzdmLoginWindowStillOpenError,
    SmzdmNativeChromeNotFoundError,
    SmzdmPlatform,
    SmzdmProfileNotReleasedError,
    SmzdmRateLimitedError,
    _run_native_chrome_login,
)


def run(coroutine):
    return asyncio.run(coroutine)


class FakeTextLocator:
    def __init__(self, visible: bool) -> None:
        self.visible = visible

    async def count(self) -> int:
        return 1

    def nth(self, _index: int) -> FakeTextLocator:
        return self

    async def is_visible(self, **_kwargs) -> bool:
        return self.visible


class FakeTextContext:
    def __init__(self, *, notice_visible: bool = False) -> None:
        self.notice_visible = notice_visible

    def get_by_text(self, _text: str, **_kwargs) -> FakeTextLocator:
        return FakeTextLocator(self.notice_visible)


class FakeLoginPage(FakeTextContext):
    def __init__(self, *, notice_visible: bool = False, frames=None) -> None:
        super().__init__(notice_visible=notice_visible)
        self.frames = list(frames or [])
        self.wait_calls = 0
        self.goto_calls = []
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)

    async def wait_for_selector(self, *_args, **_kwargs) -> None:
        self.wait_calls += 1

    async def evaluate(self, *_args, **_kwargs) -> None:
        return None


class FakeCookieContext:
    def __init__(
        self,
        cookies=None,
        *,
        responses=None,
        error: Exception | None = None,
    ) -> None:
        self.cookies_data = list(cookies or [])
        self.responses = [list(item) for item in (responses or [])]
        self.error = error
        self.pages = []
        self.calls = 0

    async def cookies(self, *_args, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        return list(self.cookies_data)


class FakeIdentityPage(FakeLoginPage):
    def __init__(self, *, notice_after_goto: bool = False) -> None:
        super().__init__()
        self.notice_after_goto = notice_after_goto
        self.response_handlers = []

    def on(self, _event, handler) -> None:
        self.response_handlers.append(handler)

    def remove_listener(self, _event, handler) -> None:
        if handler in self.response_handlers:
            self.response_handlers.remove(handler)

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        if self.notice_after_goto:
            self.notice_visible = True

    async def evaluate(self, *_args, **_kwargs):
        return "值友昵称"


def make_platform(page: FakeLoginPage) -> SmzdmPlatform:
    platform = SmzdmPlatform()
    platform.page = page
    return platform


def test_native_login_handoff_uses_same_profile_and_only_login_url(
    tmp_path: Path,
) -> None:
    events: list[object] = []

    async def runner(profile_dir: Path, url: str, timeout: float) -> None:
        events.append(("native", profile_dir, url, timeout))

    platform = SmzdmPlatform(
        profile_dir=tmp_path,
        native_chrome_runner=runner,
    )
    platform._close_playwright_for_native_login = AsyncMock(
        side_effect=lambda: events.append("close-playwright")
    )
    platform._wait_for_profile_release = AsyncMock(
        side_effect=lambda _path: events.append("profile-released")
    )
    platform.initialize = AsyncMock(side_effect=lambda: events.append("initialize"))

    run(platform.login())

    assert events == [
        "close-playwright",
        "profile-released",
        ("native", tmp_path.resolve(), LOGIN_URL, 900.0),
        "profile-released",
        "initialize",
    ]
    platform._wait_for_profile_release.assert_has_awaits(
        [call(tmp_path.resolve()), call(tmp_path.resolve())]
    )


def test_native_login_closes_playwright_context_before_runner(tmp_path: Path) -> None:
    events: list[str] = []

    class Context:
        async def close(self) -> None:
            events.append("context-close")

    class Playwright:
        async def stop(self) -> None:
            events.append("playwright-stop")

    async def runner(_profile_dir: Path, _url: str, _timeout: float) -> None:
        events.append("native-runner")

    platform = SmzdmPlatform(
        profile_dir=tmp_path,
        native_chrome_runner=runner,
    )
    platform.context = Context()
    platform.playwright = Playwright()
    platform.page = object()
    platform._wait_for_profile_release = AsyncMock()
    platform.initialize = AsyncMock()

    run(platform.login())

    assert events == ["context-close", "playwright-stop", "native-runner"]
    assert platform.context is None
    assert platform.page is None
    assert platform.playwright is None


def test_native_chrome_command_has_no_automation_or_remote_debugging_flags(
    tmp_path: Path,
) -> None:
    captured: list[tuple] = []

    class Process:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def create_process(*args, **kwargs):
        captured.append((args, kwargs))
        return Process()

    chrome = tmp_path / "chrome.exe"
    chrome.touch()
    with (
        patch("platforms.smzdm._find_system_chrome", return_value=chrome),
        patch("platforms.smzdm.asyncio.create_subprocess_exec", new=create_process),
    ):
        run(_run_native_chrome_login(tmp_path, LOGIN_URL, 10))

    args, _kwargs = captured[0]
    assert args == (
        str(chrome),
        f"--user-data-dir={tmp_path}",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        LOGIN_URL,
    )
    assert not any("automation" in str(arg).lower() for arg in args)
    assert not any("remote-debugging" in str(arg).lower() for arg in args)


def test_native_login_reports_missing_chrome_without_profile_path(tmp_path: Path) -> None:
    with patch("platforms.smzdm._find_system_chrome", return_value=None):
        with pytest.raises(SmzdmNativeChromeNotFoundError) as caught:
            run(_run_native_chrome_login(tmp_path, LOGIN_URL, 10))

    assert caught.value.error_code == "SMZDM_NATIVE_CHROME_NOT_FOUND"
    assert str(tmp_path) not in str(caught.value)


def test_native_login_timeout_never_terminates_user_chrome(tmp_path: Path) -> None:
    class Process:
        returncode = None

        def __init__(self) -> None:
            self.terminate_calls = 0
            self.kill_calls = 0

        async def wait(self) -> int:
            if self.returncode is not None:
                return self.returncode
            await asyncio.sleep(60)
            return 0

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.returncode = 0

        def kill(self) -> None:
            self.kill_calls += 1

    process = Process()
    chrome = tmp_path / "chrome.exe"
    chrome.touch()
    with (
        patch("platforms.smzdm._find_system_chrome", return_value=chrome),
        patch(
            "platforms.smzdm.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    ):
        with pytest.raises(SmzdmLoginWindowStillOpenError) as caught:
            run(_run_native_chrome_login(tmp_path, LOGIN_URL, 0.001))

    assert caught.value.error_code == "LOGIN_WINDOW_STILL_OPEN"
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


def test_native_login_cancellation_never_terminates_user_chrome(tmp_path: Path) -> None:
    class Process:
        returncode = None

        def __init__(self) -> None:
            self.terminate_calls = 0
            self.kill_calls = 0

        async def wait(self) -> int:
            raise asyncio.CancelledError

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1

    process = Process()
    chrome = tmp_path / "chrome.exe"
    chrome.touch()
    with (
        patch("platforms.smzdm._find_system_chrome", return_value=chrome),
        patch(
            "platforms.smzdm.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    ):
        with pytest.raises(SmzdmLoginWindowStillOpenError) as caught:
            run(_run_native_chrome_login(tmp_path, LOGIN_URL, 10))

    assert caught.value.error_code == "LOGIN_WINDOW_STILL_OPEN"
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


def test_profile_lock_timeout_never_deletes_chrome_lock(tmp_path: Path) -> None:
    lock = tmp_path / CHROME_PROFILE_LOCK_NAMES[0]
    lock.touch()
    platform = SmzdmPlatform(profile_dir=tmp_path)
    platform.PROFILE_RELEASE_POLL_ATTEMPTS = 2
    platform.PROFILE_RELEASE_POLL_INTERVAL_SECONDS = 0

    with (
        patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()),
        pytest.raises(SmzdmProfileNotReleasedError) as caught,
    ):
        run(platform._wait_for_profile_release(tmp_path))

    assert caught.value.error_code == "SMZDM_PROFILE_NOT_RELEASED"
    assert lock.exists()


def test_hidden_rate_limit_text_and_normal_captcha_do_not_trigger() -> None:
    hidden_frame = FakeTextContext(notice_visible=False)
    page = FakeLoginPage(notice_visible=False, frames=[hidden_frame])
    platform = make_platform(page)

    assert run(platform._visible_rate_limit_notice()) is False

    captcha_page = FakeLoginPage(notice_visible=False)
    captcha_platform = make_platform(captcha_page)
    assert run(captcha_platform._visible_rate_limit_notice()) is False


def test_visible_rate_limit_text_in_iframe_triggers_without_page_text() -> None:
    page = FakeLoginPage(
        notice_visible=False,
        frames=[FakeTextContext(notice_visible=True)],
    )
    platform = make_platform(page)

    assert run(platform._visible_rate_limit_notice()) is True


def test_new_candidate_without_site_cookies_skips_home_before_login() -> None:
    page = FakeLoginPage()
    platform = make_platform(page)
    platform.context = FakeCookieContext()
    platform.fetch_identity_payload = AsyncMock(
        side_effect=AssertionError("new candidate must not navigate HOME")
    )

    assert run(platform.check_login()) is False
    assert page.goto_calls == []
    platform.fetch_identity_payload.assert_not_awaited()


def test_existing_site_cookie_uses_one_home_identity_navigation() -> None:
    page = FakeIdentityPage()
    platform = make_platform(page)
    platform.context = FakeCookieContext(
        responses=[
            [
                {"name": "legacy_site_cookie", "value": "opaque"},
                {"name": "user", "value": "user%3A0|123"},
            ],
            [
                {"name": "legacy_site_cookie", "value": "opaque"},
                {"name": "user", "value": "user%3A0|123"},
            ],
            [
                {"name": "sess", "value": "opaque"},
                {"name": "user", "value": "user%3A0|123"},
            ],
        ]
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        assert run(platform.check_login()) is True

    assert page.goto_calls == [HOME_URL]


def test_identity_without_session_cookie_stays_unverified() -> None:
    page = FakeIdentityPage()
    platform = make_platform(page)
    platform.context = FakeCookieContext(
        cookies=[
            {"name": "legacy_site_cookie", "value": "opaque"},
            {"name": "user", "value": "user%3A0|123"},
        ]
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        assert run(platform.check_login()) is False

    assert page.goto_calls == [HOME_URL]


def test_identity_navigation_propagates_rate_limit_without_login_fallback() -> None:
    page = FakeIdentityPage(notice_after_goto=True)
    platform = make_platform(page)
    platform.context = FakeCookieContext(cookies=[{"name": "sess"}])
    platform.login = AsyncMock(side_effect=AssertionError("must not open login twice"))

    with pytest.raises(SmzdmRateLimitedError):
        run(platform.check_login())

    assert page.goto_calls == [HOME_URL]
    platform.login.assert_not_awaited()


def test_cookie_read_technical_error_is_not_treated_as_login_required() -> None:
    page = FakeLoginPage()
    platform = make_platform(page)
    platform.context = FakeCookieContext(error=TimeoutError("cookie backend unavailable"))

    with pytest.raises(SmzdmLoginCheckError) as caught:
        run(platform.check_login())

    assert caught.value.error_code == "SMZDM_LOGIN_CHECK_ERROR"
    assert page.goto_calls == []


def test_identity_navigation_technical_error_is_not_treated_as_login_required() -> None:
    page = FakeIdentityPage()
    page.goto = AsyncMock(side_effect=TimeoutError("navigation timeout"))
    platform = make_platform(page)
    platform.context = FakeCookieContext(cookies=[{"name": "sess"}])

    with pytest.raises(SmzdmLoginCheckError) as caught:
        run(platform.fetch_identity_payload())

    assert caught.value.error_code == "SMZDM_LOGIN_CHECK_ERROR"


def test_check_login_propagates_identity_technical_error_without_login_fallback() -> None:
    page = FakeLoginPage()
    platform = make_platform(page)
    platform.context = FakeCookieContext(cookies=[{"name": "sess"}])
    platform.fetch_identity_payload = AsyncMock(
        side_effect=TimeoutError("identity capture timeout")
    )
    platform.login = AsyncMock(side_effect=AssertionError("must not retry login"))

    with pytest.raises(SmzdmLoginCheckError):
        run(platform.check_login())

    platform.login.assert_not_awaited()


def test_check_login_propagates_preflight_rate_limit_without_fallback_login() -> None:
    page = FakeLoginPage(notice_visible=True)
    platform = make_platform(page)
    platform.login = AsyncMock(side_effect=AssertionError("login fallback must not run"))

    with pytest.raises(SmzdmRateLimitedError):
        run(platform.check_login())

    platform.login.assert_not_awaited()
