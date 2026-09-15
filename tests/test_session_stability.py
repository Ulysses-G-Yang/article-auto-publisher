"""Session failures and one-use identity receipts, with isolated databases."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from account_sessions.errors import AccountIdentityError
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from platforms.base import BasePlatform, SelectorError
from platforms.session_state import login_check_failure
from tests.test_account_identity_delivery import (
    _IdentityPlatform,
    add_account,
    make_services,
)
from tests.test_regression import FakePage


@pytest.mark.parametrize(
    "raw,code,expired",
    [
        ("", "SESSION_CHECK_FAILED", False),
        ("arbitrary cookie=secret", "SESSION_CHECK_FAILED", False),
        ("ZHIHU_LOGIN_CHECK_ERROR: token=secret", "ZHIHU_LOGIN_CHECK_ERROR", False),
        ("LOGIN_REQUIRED: private account", "LOGIN_REQUIRED", True),
        ("SESSION_EXPIRED", "LOGIN_REQUIRED", True),
        ("ZOL_SECURITY_CHALLENGE: details", "CHALLENGE", False),
        ("RATE_LIMITED", "RATE_LIMITED", False),
    ],
)
def test_false_login_result_keeps_reason_without_leaking_details(raw, code, expired):
    result = login_check_failure(SimpleNamespace(last_login_error=raw))
    assert result.code == code
    assert result.requires_login is expired
    assert "secret" not in result.message
    assert "private account" not in result.message


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("", "SESSION_CHECK_FAILED"),
        ("CHALLENGE", "CHALLENGE"),
        ("ZHIHU_LOGIN_CHECK_ERROR", "ZHIHU_LOGIN_CHECK_ERROR"),
        ("LOGIN_REQUIRED", "LOGIN_REQUIRED"),
    ],
)
def test_failed_delivery_check_never_publishes_or_requests_unnecessary_login(
    tmp_path,
    reason,
    expected,
):
    platform = _IdentityPlatform("bound-id", login=False)
    platform.last_login_error = reason
    database, accounts, profile = make_services(tmp_path, platform)

    async def scenario():
        try:
            account = await add_account(database, profile, platform_user_id="bound-id")
            with pytest.raises(AccountIdentityError) as caught:
                await accounts.assert_delivery_identity(account, platform, LOCAL_WEB_CONTEXT)
            assert caught.value.error_code == expected
            stored = await accounts.get_account(account.account_id)
            assert stored.session_status == (
                "LOGIN_REQUIRED" if expected == "LOGIN_REQUIRED" else "ERROR"
            )
            assert platform.publish_calls == 0
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["", "CHALLENGE", "ZHIHU_LOGIN_CHECK_ERROR"])
def test_interactive_permission_does_not_turn_check_errors_into_login(tmp_path, reason):
    platform = _IdentityPlatform("bound-id", login=False)
    platform.last_login_error = reason
    platform.login = AsyncMock()
    # _IdentityPlatform uses .login as a boolean, so provide an explicit check.
    platform.check_login = AsyncMock(return_value=False)
    database, accounts, profile = make_services(tmp_path, platform)

    async def scenario():
        try:
            account = await add_account(database, profile, platform_user_id="bound-id")
            with pytest.raises(AccountIdentityError):
                await accounts.verify_account(
                    account.account_id,
                    LOCAL_WEB_CONTEXT,
                    allow_interactive_login=True,
                )
            platform.login.assert_not_awaited()
            assert platform.cleaned
        finally:
            await database.dispose()

    asyncio.run(scenario())


class ReceiptPlatform(BasePlatform):
    platform_name = "xiaoheihe"

    async def check_login(self):
        return True

    async def login(self):
        raise AssertionError("No login allowed")

    async def navigate_to_editor(self):
        raise AssertionError("No navigation allowed")

    async def fill_title(self, title):
        raise AssertionError("No writes allowed")

    async def fill_content(self, content_blocks, images):
        raise AssertionError("No writes allowed")

    async def select_topic(self, topic):
        raise AssertionError("No writes allowed")

    async def save_draft(self):
        raise AssertionError("No writes allowed")

    async def publish_now(self, title=""):
        raise AssertionError("No writes allowed")


def test_bound_account_check_is_reused_once_in_same_live_task(tmp_path):
    platform = ReceiptPlatform()
    platform.page = FakePage()
    platform.context = SimpleNamespace(pages=[platform.page])
    platform.check_login = AsyncMock(return_value=True)
    platform.fetch_identity_payload = AsyncMock(
        return_value={
            "ok": True,
            "user_id": "bound-id",
            "display_name": "fixture",
        }
    )
    platform.preflight_delivery = AsyncMock(side_effect=SelectorError("FIXTURE_STOP"))
    database, accounts, profile = make_services(tmp_path, platform)

    async def scenario():
        try:
            account = await add_account(database, profile, platform_user_id="bound-id")
            await accounts.assert_delivery_identity(account, platform, LOCAL_WEB_CONTEXT)
            log = SimpleNamespace(add_task_log=lambda *_args: None)
            await platform.publish("fixture", [], [], auto_login=False, db=log)
            assert platform.check_login.await_count == 1
            await platform.publish("fixture", [], [], auto_login=False, db=log)
            assert platform.check_login.await_count == 2
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["page", "context", "profile", "page_url", "age", "task"])
def test_identity_receipt_cannot_cross_browser_or_task_boundary(monkeypatch, changed):
    platform = ReceiptPlatform()
    platform.page = FakePage()
    platform.context = SimpleNamespace(pages=[platform.page])

    async def scenario():
        platform.remember_delivery_identity()
        if changed == "page":
            platform.page = FakePage()
        elif changed == "context":
            platform.context = SimpleNamespace(pages=[platform.page])
        elif changed == "profile":
            platform.profile_dir = "different-profile"
        elif changed == "page_url":
            platform.page.url = "https://fixture.invalid/logout"
        elif changed == "age":
            receipt = platform._delivery_identity_receipt
            platform._delivery_identity_receipt = (*receipt[:-1], receipt[-1] - 31)
        elif changed == "task":

            async def other_task():
                return platform._consume_delivery_identity()

            assert await asyncio.create_task(other_task()) is False
            return
        assert platform._consume_delivery_identity() is False

    asyncio.run(scenario())
