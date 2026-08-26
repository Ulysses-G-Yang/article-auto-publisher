from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from platforms.base import BrowserLifecycleError, LoginRequiredError
from platforms.smzdm import (
    HOME_URL,
    LOGIN_URL,
    SmzdmLoginCheckError,
    SmzdmPlatform,
    SmzdmRateLimitedError,
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
    platform.LOGIN_POLL_INTERVAL_SECONDS = 0
    return platform


def test_initial_visible_rate_limit_stops_before_login_form_wait() -> None:
    page = FakeLoginPage(notice_visible=True)
    platform = make_platform(page)

    with pytest.raises(SmzdmRateLimitedError) as caught:
        run(platform.login())

    assert caught.value.error_code == "RATE_LIMITED"
    assert page.wait_calls == 0


def test_rate_limit_appearing_during_login_form_wait_stops_promptly() -> None:
    page = FakeLoginPage()

    async def wait_then_show_notice(*_args, **_kwargs) -> None:
        page.wait_calls += 1
        page.notice_visible = True
        raise TimeoutError("selector not ready")

    page.wait_for_selector = wait_then_show_notice
    platform = make_platform(page)

    with pytest.raises(SmzdmRateLimitedError):
        run(platform.login())

    assert page.wait_calls == 1


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


def test_normal_manual_login_signal_still_succeeds() -> None:
    page = FakeLoginPage()
    platform = make_platform(page)
    platform._has_session_cookie_signal = AsyncMock(side_effect=[False, True])

    run(platform.login())

    assert page.goto_calls == [LOGIN_URL]
    assert platform.last_login_error == ""


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


def test_manual_login_timeout_and_closed_window_remain_stable() -> None:
    timeout_page = FakeLoginPage()
    timeout_platform = make_platform(timeout_page)
    timeout_platform.LOGIN_POLL_ATTEMPTS = 2
    timeout_platform._has_session_cookie_signal = AsyncMock(return_value=False)

    with pytest.raises(LoginRequiredError) as timed_out:
        run(timeout_platform.login())
    assert timed_out.value.error_code == "LOGIN_REQUIRED"

    closed_page = FakeLoginPage()
    closed_page.closed = True
    closed_platform = make_platform(closed_page)
    with pytest.raises(BrowserLifecycleError) as closed:
        run(closed_platform.login())
    assert closed.value.error_code == "BROWSER_CONTEXT_CLOSED"


def test_check_login_propagates_preflight_rate_limit_without_fallback_login() -> None:
    page = FakeLoginPage(notice_visible=True)
    platform = make_platform(page)
    platform.login = AsyncMock(side_effect=AssertionError("login fallback must not run"))

    with pytest.raises(SmzdmRateLimitedError):
        run(platform.check_login())

    platform.login.assert_not_awaited()
