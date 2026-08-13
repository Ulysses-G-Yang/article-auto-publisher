from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.account_service import AccountSessionService
from account_sessions.database import AccountDatabase
from account_sessions.identity import extract_identity
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from platforms.zhihu import PlatformNotImplementedError, ZhihuPlatform


def run(coroutine):
    return asyncio.run(coroutine)


class FakeContext:
    def __init__(self, cookies: list[dict] | None = None) -> None:
        self._cookies = cookies or []

    async def cookies(self, *_args):
        return list(self._cookies)

    @property
    def pages(self):
        return []


class FakePage:
    def __init__(self, identities: list[dict]) -> None:
        self.identities = list(identities)
        self.url = "about:blank"
        self.goto_calls: list[str] = []
        self.identity_paths: list[str] = []

    def is_closed(self) -> bool:
        return False

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url
        self.goto_calls.append(url)

    async def evaluate(self, _script: str, identity_path: str) -> dict:
        self.identity_paths.append(identity_path)
        if not self.identities:
            return {
                "ok": False,
                "status": 401,
                "user_id": "",
                "url_token": "",
                "display_name": "",
            }
        return self.identities.pop(0)


def make_platform(
    identities: list[dict],
    *,
    cookies: list[dict] | None = None,
) -> ZhihuPlatform:
    platform = ZhihuPlatform()
    platform.page = FakePage(identities)
    platform.context = FakeContext(cookies)
    return platform


def valid_identity() -> dict:
    return {
        "ok": True,
        "status": 200,
        "user_id": "stable-id-123",
        "url_token": "night-sailor",
        "display_name": "夜航员",
    }


def invalid_identity(status: int = 401) -> dict:
    return {
        "ok": False,
        "status": status,
        "user_id": "",
        "url_token": "",
        "display_name": "",
    }


def test_check_login_requires_same_origin_identity_api_success() -> None:
    platform = make_platform([valid_identity()])

    assert run(platform.check_login()) is True
    assert platform.page.goto_calls == ["https://www.zhihu.com/"]
    assert platform.page.identity_paths == ["/api/v4/me"]
    assert platform._identity_payload["user_id"] == "stable-id-123"
    assert set(platform._identity_payload) == {
        "ok",
        "status",
        "user_id",
        "display_name",
    }


def test_cookie_is_only_weak_signal_and_cannot_prove_login() -> None:
    platform = make_platform(
        [invalid_identity()],
        cookies=[{"name": "z_c0", "value": "secret-must-not-leak"}],
    )

    assert run(platform.check_login()) is False
    assert platform.last_login_error.startswith("ZHIHU_SESSION_INVALID:")
    assert "secret-must-not-leak" not in platform.last_login_error
    assert platform._identity_payload is None


def test_extract_identity_uses_verified_api_fields_without_cookie_output() -> None:
    platform = make_platform([valid_identity()])

    identity = run(extract_identity(platform))

    assert identity.platform_user_id == "stable-id-123"
    assert identity.display_name == "夜航员"


def test_interactive_login_polls_until_identity_api_confirms_session(monkeypatch) -> None:
    platform = make_platform([invalid_identity(), valid_identity()])
    platform.LOGIN_POLL_INTERVAL_SECONDS = 0
    platform.LOGIN_POLL_ATTEMPTS = 2

    run(platform.login())

    assert platform.page.goto_calls == ["https://www.zhihu.com/signin"]
    assert len(platform.page.identity_paths) == 2
    assert platform.last_login_error == ""


def test_account_service_creates_and_verifies_isolated_zhihu_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_root = tmp_path / "account-sessions"
    monkeypatch.setenv("ACCOUNT_SESSION_DATA_DIR", str(data_root))
    database = AccountDatabase(
        f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}"
    )
    created_platforms = []

    class FakeZhihuSessionPlatform:
        platform_name = "zhihu"

        def __init__(self, checks: list[bool]) -> None:
            self.checks = checks
            self.context = FakeContext()
            self.page = FakePage([])
            self.login_calls = 0
            self.cleaned = False

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return self.checks.pop(0)

        async def login(self) -> None:
            self.login_calls += 1

        async def fetch_identity_payload(self) -> dict:
            return valid_identity()

        async def cleanup(self) -> None:
            self.cleaned = True

    check_sequences = [[False, True], [True]]

    def platform_factory(_account):
        platform = FakeZhihuSessionPlatform(check_sequences.pop(0))
        created_platforms.append(platform)
        return platform

    service = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=platform_factory,
        allowed_profile_roots=(data_root / "profiles",),
    )
    run(service.initialize())
    candidate = run(service.create_login_candidate("zhihu", LOCAL_WEB_CONTEXT))
    verified = run(
        service.verify_account(
            candidate["account_id"],
            LOCAL_WEB_CONTEXT,
            allow_interactive_login=True,
        )
    )
    read_only_verified = run(
        service.verify_account(
            candidate["account_id"],
            LOCAL_WEB_CONTEXT,
            allow_interactive_login=False,
        )
    )

    assert candidate["session_status"] == "VERIFYING"
    assert verified["display_name"] == "夜航员"
    assert verified["session_status"] == "VALID"
    assert read_only_verified["session_status"] == "VALID"
    assert created_platforms[0].login_calls == 1
    assert created_platforms[1].login_calls == 0
    assert all(platform.cleaned for platform in created_platforms)
    profile_dir = data_root / "profiles" / "zhihu" / candidate["account_id"]
    assert profile_dir.is_dir()
    run(database.dispose())

@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("publish", ()),
        ("navigate_to_editor", ()),
        ("fill_title", ("标题",)),
        ("fill_content", ([], [])),
        ("select_topic", ()),
        ("save_draft", ()),
        ("publish_now", ()),
    ],
)
def test_all_delivery_methods_fail_closed(method_name: str, args: tuple) -> None:
    platform = make_platform([])

    with pytest.raises(PlatformNotImplementedError) as error:
        run(getattr(platform, method_name)(*args))

    assert error.value.error_code == "PLATFORM_NOT_IMPLEMENTED"
