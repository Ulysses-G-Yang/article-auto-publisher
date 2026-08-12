from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.account_service import AccountSessionService
from account_sessions.contracts import DeliveryRequest
from account_sessions.database import AccountDatabase
from account_sessions.delivery_service import DeliveryService
from account_sessions.errors import (
    AccountBusyError,
    AccountPlatformMismatchError,
    ConfirmationRequiredError,
    PublicPublishDisabledError,
)
from account_sessions.leases import AccountProfileLease
from account_sessions.identity import _clean
from account_sessions.models import AccountActivity, PlatformAccount
from account_sessions.permissions import LOCAL_WEB_CONTEXT, AccessContext
from account_sessions.web import create_account_session_blueprint
from article_mvp.errors import PlatformBusyError
from platforms.base import PlatformAutomationError
from platforms.xiaoheihe import XiaoheihePlatform

SAMPLE_ARTICLE_TITLE = "账号域投递契约样例"
SAMPLE_ARTICLE_BODY = "这是一段仅用于账号投递服务测试的样例正文。"


def run(coroutine):
    return asyncio.run(coroutine)


def sqlite_database_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}"


def make_profile(tmp_path: Path, platform: str, account_id: str) -> Path:
    profile = tmp_path / "runtime" / "profiles" / platform / account_id
    profile.mkdir(parents=True)
    return profile


async def insert_account(
    database: AccountDatabase,
    profile: Path,
    *,
    platform: str = "xiaoheihe",
    user_id: str = "10001234",
    display_name: str = "夜航员",
    session_status: str = "VALID",
) -> PlatformAccount:
    account = PlatformAccount(
        account_id=str(uuid.uuid4()),
        platform=platform,
        platform_user_id=user_id,
        display_name=display_name,
        profile_path=str(profile.resolve()),
        session_status=session_status,
        persist_login=True,
    )
    async with database.session() as session:
        session.add(account)
        await session.flush()
    return account


def delivery_payload(account_id: str, *, mode: str = "DRAFT") -> dict:
    return {
        "article": {"title": SAMPLE_ARTICLE_TITLE, "body": SAMPLE_ARTICLE_BODY},
        "platform": "xiaoheihe",
        "account_id": account_id,
        "mode": mode,
        "confirmation_token": None,
    }


def test_legacy_javascript_escaped_nickname_is_decoded() -> None:
    assert _clean("%u73A9%u5BB6102503316") == "玩家102503316"
    assert _clean("%E6%B5%8B%E8%AF%95+account") == "测试 account"


def test_database_schema_pragmas_and_idempotent_initialization(tmp_path: Path) -> None:
    database_path = tmp_path / "accounts.db"
    database = AccountDatabase(sqlite_database_url(tmp_path))
    run(database.initialize())
    run(database.initialize())
    run(database.dispose())

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "platform_accounts",
            "delivery_operations",
            "account_activity",
            "publish_confirmations",
        }.issubset(tables)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_platform_filter_is_dynamic_and_never_leaks_sensitive_fields(
    tmp_path: Path,
) -> None:
    database = AccountDatabase(sqlite_database_url(tmp_path))
    service = AccountSessionService(database, seed_legacy_profiles=False)
    run(service.initialize())
    root = tmp_path / "runtime" / "profiles"
    xhh_profile = root / "xiaoheihe" / "one"
    zol_profile = root / "zol" / "two"
    xhh_profile.mkdir(parents=True)
    zol_profile.mkdir(parents=True)
    run(insert_account(database, xhh_profile, display_name="小黑盒真实昵称"))
    run(
        insert_account(
            database,
            zol_profile,
            platform="zol",
            user_id="zol-9876",
            display_name="ZOL真实昵称",
        )
    )

    accounts = run(service.list_accounts("xiaoheihe", LOCAL_WEB_CONTEXT, usable_only=True))
    assert [item["display_name"] for item in accounts] == ["小黑盒真实昵称"]
    assert accounts[0]["masked_platform_user_id"].endswith("1234")
    assert "platform_user_id" not in accounts[0]
    assert "profile_path" not in accounts[0]
    run(database.dispose())


def test_platform_account_mismatch_is_rejected(tmp_path: Path) -> None:
    database = AccountDatabase(sqlite_database_url(tmp_path))
    service = AccountSessionService(database, seed_legacy_profiles=False)
    run(service.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "mismatch")
    account = run(insert_account(database, profile))
    with pytest.raises(AccountPlatformMismatchError):
        run(
            service.require_account(
                account.account_id,
                "zol",
                LOCAL_WEB_CONTEXT,
                "draft.create",
            )
        )
    run(database.dispose())


def test_access_context_scopes_account_permissions() -> None:
    access = AccessContext(
        actor_id="mcp-client",
        source="MCP",
        capabilities=frozenset({"draft.create"}),
        allowed_account_ids=frozenset({"allowed"}),
    )
    access.require("draft.create", "allowed")
    with pytest.raises(RuntimeError):
        access.require("draft.create", "denied")
    with pytest.raises(RuntimeError):
        access.require("publish.execute", "allowed")


def test_profile_lease_refuses_singleton_and_never_deletes_it(
    tmp_path: Path,
) -> None:
    account_id = str(uuid.uuid4())
    profile = make_profile(tmp_path, "xiaoheihe", account_id)
    singleton = profile / "SingletonLock"
    singleton.write_text("occupied", encoding="utf-8")
    account = PlatformAccount(
        account_id=account_id,
        platform="xiaoheihe",
        display_name="账号",
        profile_path=str(profile.resolve()),
        is_legacy_profile=False,
    )

    with pytest.raises(AccountBusyError):
        AccountProfileLease(
            account,
            purpose="VERIFY",
            allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
        ).acquire()
    assert singleton.read_text(encoding="utf-8") == "occupied"


def test_legacy_platform_entry_never_deletes_singleton(tmp_path: Path) -> None:
    profile = tmp_path / "legacy-profile"
    profile.mkdir()
    singleton = profile / "SingletonLock"
    singleton.write_text("active-or-unknown", encoding="utf-8")
    platform = XiaoheihePlatform(profile_dir=profile)

    with pytest.raises(PlatformAutomationError, match="PROFILE_IN_USE"):
        run(platform.initialize())
    assert singleton.read_text(encoding="utf-8") == "active-or-unknown"
    assert platform.playwright is None


def test_profile_lease_is_cross_process_keyed_by_profile(
    tmp_path: Path,
) -> None:
    account_id = str(uuid.uuid4())
    profile = make_profile(tmp_path, "xiaoheihe", account_id)
    account = PlatformAccount(
        account_id=account_id,
        platform="xiaoheihe",
        display_name="账号",
        profile_path=str(profile.resolve()),
    )
    allowed = (tmp_path / "runtime" / "profiles",)
    first = AccountProfileLease(
        account,
        purpose="DRAFT",
        allowed_profile_roots=allowed,
    )
    second = AccountProfileLease(
        account,
        purpose="VERIFY",
        allowed_profile_roots=allowed,
    )
    first.acquire()
    try:
        with pytest.raises(AccountBusyError) as caught:
            second.acquire()
        assert isinstance(caught.value.__cause__, PlatformBusyError)
    finally:
        first.release()


def test_draft_operation_is_account_bound_and_logs_are_isolated(
    tmp_path: Path,
) -> None:
    database = AccountDatabase(sqlite_database_url(tmp_path))
    accounts = AccountSessionService(database, seed_legacy_profiles=False)
    run(accounts.initialize())
    first_profile = make_profile(tmp_path, "xiaoheihe", "first")
    second_profile = make_profile(tmp_path, "xiaoheihe", "second")
    first = run(insert_account(database, first_profile, display_name="甲账号"))
    second = run(
        insert_account(
            database,
            second_profile,
            user_id="20005678",
            display_name="乙账号",
        )
    )
    delivery = DeliveryService(accounts, public_publish_enabled=False)
    request = DeliveryRequest.model_validate(delivery_payload(first.account_id))
    result = run(delivery.request_delivery(request, LOCAL_WEB_CONTEXT))

    assert result["account"]["account_id"] == first.account_id
    assert result["account"]["display_name"] == "甲账号"
    assert result["status"] == "QUEUED"
    first_logs = run(accounts.list_activity(first.account_id, LOCAL_WEB_CONTEXT))
    second_logs = run(accounts.list_activity(second.account_id, LOCAL_WEB_CONTEXT))
    assert [item["action"] for item in first_logs] == ["DELIVERY_QUEUED"]
    assert second_logs == []
    run(database.dispose())


def test_draft_executor_records_platform_logs_and_success(tmp_path: Path) -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.clear_calls = 0

        async def clear_cookies(self) -> None:
            self.clear_calls += 1

    class FakePlatform:
        platform_name = "xiaoheihe"

        def __init__(self) -> None:
            self.cleaned = False
            self.context = FakeContext()

        async def initialize(self) -> None:
            return None

        async def publish(self, **kwargs) -> dict:
            assert kwargs["delivery_mode"] == "DRAFT"
            assert kwargs["auto_login"] is False
            assert kwargs["content_blocks"][0]["text"] == SAMPLE_ARTICLE_BODY
            kwargs["db"].add_task_log(0, "INFO", "平台草稿保存完成")
            return {
                "success": True,
                "draft_url": "https://example.invalid/drafts/1",
                "post_url": "",
            }

        async def cleanup(self) -> None:
            self.cleaned = True

    database = AccountDatabase(sqlite_database_url(tmp_path))
    profile_root = tmp_path / "runtime" / "profiles"
    fake = FakePlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(profile_root,),
    )
    run(accounts.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "execute")
    account = run(insert_account(database, profile))
    run(accounts.set_session_policy(account.account_id, False, LOCAL_WEB_CONTEXT))
    account.persist_login = False
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(delivery.request_delivery(request, LOCAL_WEB_CONTEXT))
    completed = run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))

    assert completed["status"] == "DRAFT_SAVED"
    assert completed["draft_url"].endswith("/drafts/1")
    assert fake.cleaned is True
    assert fake.context.clear_calls == 1
    logs = run(accounts.list_activity(account.account_id, LOCAL_WEB_CONTEXT))
    assert {row["action"] for row in logs} >= {
        "DELIVERY_QUEUED",
        "DELIVERY_STARTED",
        "PLATFORM_LOG",
        "DRAFT_SAVED",
        "SESSION_CLEARED_AFTER_OPERATION",
    }
    run(database.dispose())


def test_publish_requires_single_use_confirmation_and_gate_stays_closed(
    tmp_path: Path,
) -> None:
    database = AccountDatabase(sqlite_database_url(tmp_path))
    accounts = AccountSessionService(database, seed_legacy_profiles=False)
    run(accounts.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "publish")
    account = run(insert_account(database, profile))
    delivery = DeliveryService(accounts, public_publish_enabled=False)
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id, mode="PUBLISH"))

    with pytest.raises(ConfirmationRequiredError) as required:
        run(delivery.request_delivery(request, LOCAL_WEB_CONTEXT))
    confirmed = request.model_copy(update={"confirmation_token": required.value.token})
    with pytest.raises(PublicPublishDisabledError):
        run(delivery.request_delivery(confirmed, LOCAL_WEB_CONTEXT))
    with pytest.raises(Exception) as reused:
        run(delivery.request_delivery(confirmed, LOCAL_WEB_CONTEXT))
    assert getattr(reused.value, "error_code", "") == "PUBLISH_CONFIRMATION_INVALID"
    run(database.dispose())


def test_blueprint_matches_frontend_contract_and_injects_article(tmp_path: Path) -> None:
    template_root = PROJECT_ROOT / "web" / "templates"
    static_root = template_root.parent / "static"
    database_url = sqlite_database_url(tmp_path)
    database = AccountDatabase(database_url)
    run(database.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "web")
    account = run(insert_account(database, profile, display_name="网页账号"))
    run(database.dispose())

    from flask import Flask

    app = Flask(
        "account-session-test",
        template_folder=str(template_root),
        static_folder=str(static_root),
    )
    app.secret_key = "test"
    app.register_blueprint(
        create_account_session_blueprint(
            database_url=database_url,
            seed_legacy_profiles=False,
            auto_execute=False,
            public_publish_enabled=False,
            allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
        )
    )
    client = app.test_client()

    page = client.get("/delivery/new", follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["Location"] == "/upload"

    accounts_response = client.get("/api/platforms/xiaoheihe/accounts?usable=true")
    assert accounts_response.status_code == 200
    accounts_payload = accounts_response.get_json()
    assert accounts_payload["platform"] == "xiaoheihe"
    assert accounts_payload["accounts"][0]["display_name"] == "网页账号"
    assert "profile_path" not in accounts_payload["accounts"][0]

    operation = client.post(
        "/api/delivery-operations",
        json=delivery_payload(account.account_id),
    )
    assert operation.status_code == 202
    assert operation.get_json()["account"]["account_id"] == account.account_id
    assert operation.get_json()["status"] == "QUEUED"
    activity = client.get(f"/api/account-sessions/{account.account_id}/activity")
    assert activity.status_code == 200
    assert activity.get_json()["account_id"] == account.account_id
    assert [event["action"] for event in activity.get_json()["activities"]] == ["DELIVERY_QUEUED"]

    publish_payload = delivery_payload(account.account_id, mode="PUBLISH")
    confirmation = client.post("/api/delivery-operations", json=publish_payload)
    assert confirmation.status_code == 428
    assert confirmation.get_json()["error"] == "PUBLISH_CONFIRMATION_REQUIRED"
    assert confirmation.get_json()["confirmation_token"]

    state = app.extensions["account_sessions"]
    state.close()


def test_account_activity_table_contains_no_cross_account_rows(tmp_path: Path) -> None:
    database = AccountDatabase(sqlite_database_url(tmp_path))
    service = AccountSessionService(database, seed_legacy_profiles=False)
    run(service.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "activity")
    account = run(insert_account(database, profile))
    run(service.set_session_policy(account.account_id, False, LOCAL_WEB_CONTEXT))

    async def query_rows() -> list[AccountActivity]:
        async with database.session() as session:
            return list(
                (
                    await session.scalars(
                        select(AccountActivity).where(
                            AccountActivity.account_id == account.account_id
                        )
                    )
                ).all()
            )

    rows = run(query_rows())
    assert len(rows) == 1
    assert rows[0].display_name_snapshot == "夜航员"
    assert rows[0].actor_id == "local-web-user"
    assert rows[0].source == "WEB"
    run(database.dispose())
