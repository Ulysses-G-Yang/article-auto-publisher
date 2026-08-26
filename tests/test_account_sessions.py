from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sqlite3
import sys
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.account_service import AccountSessionService, public_account
from account_sessions.contracts import DeliveryRequest
from account_sessions.database import AccountDatabase
from account_sessions.delivery_service import DeliveryService
from account_sessions.errors import (
    AccountBusyError,
    AccountNotFoundError,
    AccountPlatformMismatchError,
    AccountSessionError,
    AccountUnavailableError,
    ConfirmationRequiredError,
    PublicPublishDisabledError,
)
from account_sessions.leases import AccountProfileLease
from account_sessions.identity import _clean
from account_sessions.models import (
    AccountActivity,
    DeliveryOperation,
    PlatformAccount,
)
from account_sessions.permissions import LOCAL_WEB_CONTEXT, AccessContext
from account_sessions.web import create_account_session_blueprint
from article_mvp.errors import PlatformBusyError
from platforms.base import LoginRequiredError, PlatformAutomationError, SelectorError
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


def test_heartbeat_schema_fields_defaults_and_index_are_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "accounts.db"
    database = AccountDatabase(sqlite_database_url(tmp_path))
    run(database.initialize())
    run(database.initialize())
    run(database.dispose())

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(platform_accounts)")
        }
        assert {
            "heartbeat_enabled",
            "next_heartbeat_at",
            "last_heartbeat_at",
            "heartbeat_failures",
            "last_heartbeat_error_code",
            "heartbeat_claim_owner",
            "heartbeat_claimed_at",
            "heartbeat_claim_expires_at",
        }.issubset(columns)
        assert columns["heartbeat_enabled"][3] == 1
        assert columns["heartbeat_enabled"][4] == "1"
        assert columns["heartbeat_failures"][3] == 1
        assert columns["heartbeat_failures"][4] == "0"

        indexes = {
            row[1]: row
            for row in connection.execute("PRAGMA index_list(platform_accounts)")
        }
        assert "ix_account_heartbeat_due" in indexes
        assert "ix_account_heartbeat_claim" in indexes
        index_columns = [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(ix_account_heartbeat_due)"
            )
        ]
        assert index_columns == ["status", "heartbeat_enabled", "next_heartbeat_at"]
        claim_columns = [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(ix_account_heartbeat_claim)"
            )
        ]
        assert claim_columns == [
            "heartbeat_claim_expires_at",
            "heartbeat_claim_owner",
        ]


def test_heartbeat_schema_upgrade_is_idempotent_and_backfills_old_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "accounts.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE platform_accounts (
                account_id VARCHAR(36) PRIMARY KEY,
                platform VARCHAR(32) NOT NULL,
                platform_user_id VARCHAR(255),
                display_name VARCHAR(255) NOT NULL,
                profile_path VARCHAR(2048) NOT NULL,
                is_legacy_profile BOOLEAN NOT NULL DEFAULT 0,
                status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
                session_status VARCHAR(24) NOT NULL DEFAULT 'UNVERIFIED',
                persist_login BOOLEAN NOT NULL DEFAULT 1,
                last_verified_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            INSERT INTO platform_accounts (
                account_id, platform, display_name, profile_path,
                created_at, updated_at
            ) VALUES (
                'old-account', 'xiaoheihe', '旧账号', '/tmp/old-profile',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );
            """
        )

    database = AccountDatabase(sqlite_database_url(tmp_path))
    run(database.initialize())
    run(database.initialize())

    async def read_old_row() -> PlatformAccount:
        async with database.session() as session:
            row = await session.get(PlatformAccount, "old-account")
            assert row is not None
            return row

    row = run(read_old_row())
    assert row.heartbeat_enabled is True
    assert row.heartbeat_failures == 0
    assert row.next_heartbeat_at is None
    assert row.last_heartbeat_at is None
    assert row.last_heartbeat_error_code is None
    assert row.heartbeat_claim_owner is None
    assert row.heartbeat_claimed_at is None
    assert row.heartbeat_claim_expires_at is None
    run(database.dispose())


def test_public_account_is_a_safe_health_projection_whitelist(tmp_path: Path) -> None:
    account = PlatformAccount(
        account_id="safe-account",
        platform="xiaoheihe",
        platform_user_id="raw-platform-id",
        display_name="公开昵称",
        profile_path=str(tmp_path / "secret-profile"),
        heartbeat_enabled=True,
        heartbeat_failures=2,
        last_heartbeat_error_code="RATE_LIMITED",
    )

    projection = public_account(account)
    assert set(projection) == {
        "account_id",
        "display_name",
        "masked_platform_user_id",
        "status",
        "session_status",
        "persist_login",
        "heartbeat_enabled",
        "next_heartbeat_at",
        "last_heartbeat_at",
        "heartbeat_failures",
        "last_heartbeat_error_code",
        "last_verified_at",
    }
    assert projection["masked_platform_user_id"] != "raw-platform-id"
    assert "profile_path" not in projection
    assert "platform_user_id" not in projection
    assert "cookie" not in {key.lower() for key in projection}
    assert "token" not in {key.lower() for key in projection}


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

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

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


def test_draft_result_unknown_is_persisted_without_automatic_retry(
    tmp_path: Path,
) -> None:
    class FakePlatform:
        platform_name = "xiaoheihe"
        context = None

        def __init__(self) -> None:
            self.publish_calls = 0

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

        async def publish(self, **_kwargs) -> dict:
            self.publish_calls += 1
            return {
                "success": False,
                "error_code": "DRAFT_RESULT_UNKNOWN",
                "error": "保存动作已触发但结果无法证明",
            }

        async def cleanup(self) -> None:
            return None

    database = AccountDatabase(sqlite_database_url(tmp_path))
    fake = FakePlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    run(accounts.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "unknown-result")
    account = run(insert_account(database, profile))
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(delivery.request_delivery(request, LOCAL_WEB_CONTEXT))

    with pytest.raises(AccountUnavailableError) as exc_info:
        run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))
    assert exc_info.value.error_code == "DRAFT_RESULT_UNKNOWN"

    async def load_operation() -> DeliveryOperation:
        async with database.session() as session:
            return await session.get(DeliveryOperation, queued["operation_id"])

    stored = run(load_operation())
    assert stored.status == "RESULT_UNKNOWN"
    assert stored.error_code == "DRAFT_RESULT_UNKNOWN"
    activity = run(accounts.list_activity(account.account_id, LOCAL_WEB_CONTEXT))
    terminal = [row for row in activity if row["action"] == "DELIVERY_RESULT_UNKNOWN"]
    assert len(terminal) == 1
    assert terminal[0]["level"] == "WARN"
    assert terminal[0]["operation_id"] == queued["operation_id"]

    # 终态不能重新进入平台执行；第二次调用只读取已结束执行单。
    second = run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))
    assert second["status"] == "RESULT_UNKNOWN"
    assert fake.publish_calls == 1
    run(database.dispose())


def test_cancelled_delivery_is_result_unknown_and_cancel_continues_upward(
    tmp_path: Path,
) -> None:
    class FakePlatform:
        platform_name = "xiaoheihe"
        context = None

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

        async def publish(self, **_kwargs) -> dict:
            error = asyncio.CancelledError()
            error.error_code = "DRAFT_RESULT_UNKNOWN"
            error.safe_message = "DRAFT_RESULT_UNKNOWN: 保存动作已触发，结果未知"
            raise error

        async def cleanup(self) -> None:
            return None

    database = AccountDatabase(sqlite_database_url(tmp_path))
    fake = FakePlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    run(accounts.initialize())
    account = run(
        insert_account(
            database,
            make_profile(tmp_path, "xiaoheihe", "cancelled"),
        )
    )
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(delivery.request_delivery(request, LOCAL_WEB_CONTEXT))

    with pytest.raises(asyncio.CancelledError):
        run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))

    async def load_operation() -> DeliveryOperation:
        async with database.session() as session:
            return await session.get(DeliveryOperation, queued["operation_id"])

    stored = run(load_operation())
    assert stored.status == "RESULT_UNKNOWN"
    assert stored.error_code == "DRAFT_RESULT_UNKNOWN"
    run(database.dispose())


def test_media_incomplete_with_saved_draft_is_with_warnings_not_failed(
    tmp_path: Path,
) -> None:
    """草稿已保存但图片未完整：状态 *_WITH_WARNINGS，不抹成失败。"""

    class FakePlatform:
        platform_name = "xiaoheihe"
        context = None

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

        async def publish(self, **kwargs) -> dict:
            return {
                "success": True,
                "draft_url": "https://example.invalid/drafts/media1",
                "post_url": "",
                "media_status": "failed",
                "media_error": "3 张图片全部上传失败",
                "expected_images": 3,
                "uploaded_images": 0,
                "failed_images": [{"filename": "a.png", "error": "风控"}],
            }

        async def cleanup(self) -> None:
            return None

    database = AccountDatabase(sqlite_database_url(tmp_path))
    fake = FakePlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    run(accounts.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "media-warning")
    account = run(insert_account(database, profile))
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(delivery.request_delivery(request, LOCAL_WEB_CONTEXT))
    completed = run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))

    assert completed["status"] == "DRAFT_SAVED_WITH_WARNINGS"
    assert completed["draft_url"].endswith("/drafts/media1")
    assert completed["error_code"] == "PLATFORM_MEDIA_INCOMPLETE"
    assert "图片" in (completed["error_message"] or "")
    logs = run(accounts.list_activity(account.account_id, LOCAL_WEB_CONTEXT))
    assert "DELIVERY_COMPLETED_WITH_WARNINGS" in {row["action"] for row in logs}
    run(database.dispose())


def test_media_incomplete_without_url_but_bound_entity_is_with_warnings(
    tmp_path: Path,
) -> None:
    """已有本次实体证据时，空草稿 URL 也不能被媒体 partial 改写为 FAILED。"""

    class FakePlatform:
        platform_name = "xiaoheihe"
        context = None

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

        async def publish(self, **_kwargs) -> dict:
            return {
                "success": True,
                "draft_url": "",
                "post_url": "",
                "media_status": "partial",
                "media_error": "正文图片未完整核验",
                "expected_images": 7,
                "uploaded_images": 5,
                "failed_images": [{"filename": "image.png", "error": "未确认"}],
                "verification_evidence": {
                    "draft_entity_bound": True,
                    "draft_entity_source": "save_response_id",
                    "draft_entity_id_match": True,
                },
                "degraded": "draft_list_confirmed",
            }

        async def cleanup(self) -> None:
            return None

    database = AccountDatabase(sqlite_database_url(tmp_path))
    fake = FakePlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    run(accounts.initialize())
    account = run(insert_account(database, make_profile(tmp_path, "xiaoheihe", "media-no-url")))
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(delivery.request_delivery(request, LOCAL_WEB_CONTEXT))
    completed = run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))

    assert completed["status"] == "DRAFT_SAVED_WITH_WARNINGS"
    assert completed["draft_url"] in {None, ""}
    assert completed["error_code"] == "PLATFORM_MEDIA_INCOMPLETE"
    run(database.dispose())


def test_frozen_cover_payload_reaches_platform_without_public_path_exposure(
    tmp_path: Path,
) -> None:
    """账号域必须把内容域冻结封面原样交给适配器，操作 API 不回显本机路径。"""

    class FakePlatform:
        platform_name = "xiaoheihe"
        context = None

        def __init__(self) -> None:
            self.publish_kwargs: dict = {}

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

        async def publish(self, **kwargs) -> dict:
            self.publish_kwargs = kwargs
            return {
                "success": True,
                "draft_url": "https://example.invalid/drafts/cover-ok",
                "post_url": "",
                "media_status": "not_required",
                "cover_status": "completed",
            }

        async def cleanup(self) -> None:
            return None

    cover_path = tmp_path / "controlled-assets" / "cover.png"
    cover_path.parent.mkdir(parents=True)
    cover_path.write_bytes(b"test-cover")
    cover = {
        "strategy": "EXPLICIT",
        "asset_id": "cover-asset-id",
        "local_path": str(cover_path),
        "filename": "cover.png",
        "media_type": "image/png",
        "width": 1200,
        "height": 800,
    }

    async def resolve_content(_version_id: str):
        return (
            "冻结封面标题",
            [{"type": "text", "text": "正文", "position": 0}],
            [],
            cover,
        )

    database = AccountDatabase(sqlite_database_url(tmp_path))
    fake = FakePlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    run(accounts.initialize())
    account = run(insert_account(database, make_profile(tmp_path, "xiaoheihe", "cover-ok")))
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
        content_resolver=resolve_content,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(
        delivery.request_delivery(
            request,
            LOCAL_WEB_CONTEXT,
            frozen_content_hash="frozen-cover-hash",
            content_reference="frozen-cover-version",
        )
    )
    completed = run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))

    assert completed["status"] == "DRAFT_SAVED"
    assert fake.publish_kwargs["cover"] == cover
    assert str(cover_path) not in str(completed)
    run(database.dispose())


def test_unsupported_frozen_cover_is_saved_with_explicit_warning(tmp_path: Path) -> None:
    """正文草稿存在但平台不支持封面时，不得伪装成完整 Word 成功。"""

    class FakePlatform:
        platform_name = "xiaoheihe"
        context = None

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

        async def publish(self, **_kwargs) -> dict:
            return {
                "success": True,
                "draft_url": "https://example.invalid/drafts/cover-warning",
                "post_url": "",
                "media_status": "completed",
                "cover_status": "unsupported",
                "cover_error_code": "PLATFORM_COVER_UNSUPPORTED",
                "cover_error": (
                    r"当前平台尚未实现封面投递: D:\Secret Folder\cover.png"
                ),
            }

        async def cleanup(self) -> None:
            return None

    async def resolve_content(_version_id: str):
        return (
            "冻结封面标题",
            [{"type": "text", "text": "正文", "position": 0}],
            [],
            {
                "strategy": "FIRST_BODY_IMAGE",
                "asset_id": "cover-asset-id",
                "local_path": str(tmp_path / "controlled-assets" / "cover.png"),
            },
        )

    database = AccountDatabase(sqlite_database_url(tmp_path))
    fake = FakePlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    run(accounts.initialize())
    account = run(
        insert_account(database, make_profile(tmp_path, "xiaoheihe", "cover-warning"))
    )
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
        content_resolver=resolve_content,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(
        delivery.request_delivery(
            request,
            LOCAL_WEB_CONTEXT,
            frozen_content_hash="unsupported-cover-hash",
            content_reference="unsupported-cover-version",
        )
    )
    completed = run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))

    assert completed["status"] == "DRAFT_SAVED_WITH_WARNINGS"
    assert completed["draft_url"].endswith("/drafts/cover-warning")
    assert completed["error_code"] == "PLATFORM_COVER_UNSUPPORTED"
    assert "封面" in (completed["error_message"] or "")
    assert "Secret Folder" not in (completed["error_message"] or "")
    assert "D:\\" not in (completed["error_message"] or "")
    run(database.dispose())


def test_invalid_cover_resolver_payload_fails_closed_before_platform_publish(
    tmp_path: Path,
) -> None:
    class FakePlatform:
        platform_name = "xiaoheihe"
        context = None
        publish_called = False

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

        async def publish(self, **_kwargs) -> dict:
            self.publish_called = True
            raise AssertionError("非法封面载荷不得进入平台")

        async def cleanup(self) -> None:
            return None

    async def resolve_content(_version_id: str):
        return "标题", [{"type": "text", "text": "正文"}], [], {
            "strategy": "EXPLICIT",
            "asset_id": "missing",
            "local_path": None,
        }

    database = AccountDatabase(sqlite_database_url(tmp_path))
    fake = FakePlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    run(accounts.initialize())
    account = run(
        insert_account(database, make_profile(tmp_path, "xiaoheihe", "cover-invalid"))
    )
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
        content_resolver=resolve_content,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(
        delivery.request_delivery(
            request,
            LOCAL_WEB_CONTEXT,
            frozen_content_hash="invalid-cover-hash",
            content_reference="invalid-cover-version",
        )
    )

    with pytest.raises(AccountUnavailableError) as exc_info:
        run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))
    assert exc_info.value.error_code == "CONTENT_VERSION_COVER_UNAVAILABLE"
    assert fake.publish_called is False
    run(database.dispose())


def test_media_incomplete_without_draft_still_fails(tmp_path: Path) -> None:
    """图片未完整且草稿也没保存：整体判失败，杜绝假成功。"""

    class FakePlatform:
        platform_name = "xiaoheihe"
        context = None

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

        async def publish(self, **kwargs) -> dict:
            return {
                "success": True,
                "draft_url": "",
                "post_url": "",
                "media_status": "failed",
                "media_error": "1 张图片全部上传失败",
                "expected_images": 1,
                "uploaded_images": 0,
                "failed_images": [],
            }

        async def cleanup(self) -> None:
            return None

    database = AccountDatabase(sqlite_database_url(tmp_path))
    fake = FakePlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    run(accounts.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "media-fail")
    account = run(insert_account(database, profile))
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(delivery.request_delivery(request, LOCAL_WEB_CONTEXT))

    with pytest.raises(AccountUnavailableError) as exc_info:
        run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))
    assert exc_info.value.error_code == "PLATFORM_MEDIA_INCOMPLETE"
    run(database.dispose())


def test_failed_media_progress_is_logged_as_safe_account_audit(
    tmp_path: Path,
) -> None:
    class FakePlatform:
        platform_name = "xiaoheihe"
        context = None

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

        async def publish(self, **_kwargs) -> dict:
            return {
                "success": False,
                "error_code": "CONTENT_VALIDATION_ERROR",
                "error": "正文校验失败",
                "media_progress": {
                    "expected_images": 7,
                    "uploaded_images": 2,
                    "failed_image_count": 0,
                    "media_status": "in_progress",
                    "path": r"D:\secret\image.png",
                    "token": "secret-token",
                    "body": "用户正文不应进入活动日志",
                },
            }

        async def cleanup(self) -> None:
            return None

    database = AccountDatabase(sqlite_database_url(tmp_path))
    fake = FakePlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    run(accounts.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "media-audit")
    account = run(insert_account(database, profile))
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(delivery.request_delivery(request, LOCAL_WEB_CONTEXT))

    with pytest.raises(AccountUnavailableError) as exc_info:
        run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))
    assert exc_info.value.error_code == "CONTENT_VALIDATION_ERROR"

    stored = run(delivery.get_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))
    assert stored["status"] == "FAILED"
    assert stored["error_code"] == "CONTENT_VALIDATION_ERROR"
    logs = run(accounts.list_activity(account.account_id, LOCAL_WEB_CONTEXT))
    progress_logs = [row for row in logs if "MEDIA_PROGRESS" in row["message"]]
    assert len(progress_logs) == 1
    message = progress_logs[0]["message"]
    assert message == (
        "MEDIA_PROGRESS 媒体进度：expected_images=7，uploaded_images=2，"
        "failed_image_count=0，media_status=in_progress"
    )
    all_messages = " ".join(row["message"] for row in logs)
    assert r"D:\secret" not in all_messages
    assert "secret-token" not in all_messages
    assert "用户正文不应进入活动日志" not in all_messages
    run(database.dispose())


@pytest.mark.parametrize(
    ("failure", "expected_session_status"),
    [
        (LoginRequiredError("登录态失效"), "LOGIN_REQUIRED"),
        (SelectorError("编辑器控件缺失"), "VALID"),
    ],
)
def test_delivery_failure_downgrades_only_invalid_sessions(
    tmp_path: Path,
    failure: PlatformAutomationError,
    expected_session_status: str,
) -> None:
    class FailingPlatform:
        platform_name = "xiaoheihe"
        context = None

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return True

        async def fetch_identity_payload(self) -> dict:
            return {"ok": True, "user_id": "10001234", "display_name": "夜航员"}

        async def publish(self, **_kwargs) -> dict:
            raise failure

        async def cleanup(self) -> None:
            return None

    async def stamp_verification(database: AccountDatabase, account_id: str) -> None:
        async with database.session() as session:
            stored = await session.get(PlatformAccount, account_id)
            assert stored is not None
            stored.last_verified_at = datetime.now(timezone.utc)

    database = AccountDatabase(sqlite_database_url(tmp_path))
    fake = FailingPlatform()
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: fake,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    run(accounts.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "failure-state")
    account = run(insert_account(database, profile))
    run(stamp_verification(database, account.account_id))
    delivery = DeliveryService(
        accounts,
        platform_factory=lambda _account: fake,
        public_publish_enabled=False,
    )
    request = DeliveryRequest.model_validate(delivery_payload(account.account_id))
    queued = run(delivery.request_delivery(request, LOCAL_WEB_CONTEXT))

    with pytest.raises(type(failure)):
        run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))

    stored = run(accounts.get_account(account.account_id))
    assert stored.session_status == expected_session_status
    if expected_session_status == "LOGIN_REQUIRED":
        assert stored.last_verified_at is None
    else:
        assert stored.last_verified_at is not None
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
    platforms_response = client.get("/api/platforms")
    assert platforms_response.status_code == 200
    platforms = platforms_response.get_json()["platforms"]
    assert len(platforms) == 10
    zhihu = next(item for item in platforms if item["id"] == "zhihu")
    assert zhihu == {
        "id": "zhihu",
        "display_name": "知乎",
        "logo_url": "/static/img/platforms/zhihu.svg",
        "status": "AVAILABLE",
        "delivery_enabled": True,
        "account_enabled": True,
        "sort_order": 30,
    }

    page = client.get("/delivery/new", follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["Location"] == "/upload"

    accounts_response = client.get("/api/platforms/xiaoheihe/accounts?usable=true")
    assert accounts_response.status_code == 200
    accounts_payload = accounts_response.get_json()
    assert accounts_payload["platform"] == "xiaoheihe"
    assert accounts_payload["accounts"][0]["display_name"] == "网页账号"
    assert "profile_path" not in accounts_payload["accounts"][0]

    summary_response = client.get("/api/account-sessions/summary")
    assert summary_response.status_code == 200
    summary_payload = summary_response.get_json()
    assert summary_payload["summary"] == {
        "total_accounts": 1,
        "valid_accounts": 1,
        "attention_required_accounts": 0,
        "platforms_with_accounts": 1,
    }
    assert summary_payload["accounts"] == [
        {
            "account_id": account.account_id,
            "platform": "xiaoheihe",
            "display_name": "网页账号",
            "masked_platform_user_id": "****1234",
            "status": "ACTIVE",
            "session_status": "VALID",
            "persist_login": True,
            "heartbeat_enabled": True,
            "next_heartbeat_at": None,
            "last_heartbeat_at": None,
            "heartbeat_failures": 0,
            "last_heartbeat_error_code": None,
            "last_verified_at": None,
        }
    ]
    account_keys = set(summary_payload["accounts"][0])
    assert "profile_path" not in account_keys
    assert "platform_user_id" not in account_keys

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


def test_existing_account_login_route_reuses_profile_and_enables_interaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flask import Flask

    template_root = PROJECT_ROOT / "web" / "templates"
    static_root = template_root.parent / "static"
    app = Flask(
        "account-relogin-test",
        template_folder=str(template_root),
        static_folder=str(static_root),
    )
    app.secret_key = "test"
    app.register_blueprint(
        create_account_session_blueprint(
            database_url=sqlite_database_url(tmp_path),
            seed_legacy_profiles=False,
            auto_execute=False,
            public_publish_enabled=False,
            allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
        )
    )
    state = app.extensions["account_sessions"]
    captured: dict = {}

    async def fake_mark_verifying(account_id: str, _access: AccessContext) -> dict:
        captured["marked_account_id"] = account_id
        return {"account_id": account_id, "session_status": "VERIFYING"}

    def fake_verify_account(
        account_id: str,
        _access: AccessContext,
        *,
        allow_interactive_login: bool = False,
    ) -> object:
        captured["verified_account_id"] = account_id
        captured["allow_interactive_login"] = allow_interactive_login
        return object()

    def forbid_new_account(*_args, **_kwargs):
        raise AssertionError("existing-account login must not create a new account")

    monkeypatch.setattr(state.accounts, "mark_verifying", fake_mark_verifying)
    monkeypatch.setattr(state.accounts, "verify_account", fake_verify_account)
    monkeypatch.setattr(state.accounts, "create_login_candidate", forbid_new_account)
    monkeypatch.setattr(state, "submit", lambda work: captured.setdefault("submitted", work))

    account_id = "existing-account"
    run(state.accounts.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", account_id)

    async def insert_existing_account() -> None:
        async with state.database.session() as session:
            session.add(
                PlatformAccount(
                    account_id=account_id,
                    platform="xiaoheihe",
                    display_name="现有账号",
                    profile_path=str(profile.resolve()),
                    session_status="LOGIN_REQUIRED",
                    persist_login=True,
                )
            )

    run(insert_existing_account())
    response = app.test_client().post(f"/api/account-sessions/{account_id}/login")

    assert response.status_code == 202
    assert response.get_json() == {"account_id": account_id, "session_status": "VERIFYING"}
    assert captured["marked_account_id"] == account_id
    assert captured["verified_account_id"] == account_id
    assert captured["allow_interactive_login"] is True
    assert captured["submitted"] is not None
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


def test_remove_account_deletes_row_activity_and_profile(tmp_path: Path) -> None:
    database = AccountDatabase(sqlite_database_url(tmp_path))
    profile_root = tmp_path / "runtime" / "profiles"
    service = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        allowed_profile_roots=(profile_root,),
    )
    run(service.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "remove-me")
    account = run(insert_account(database, profile, session_status="LOGIN_REQUIRED"))
    run(service.set_session_policy(account.account_id, False, LOCAL_WEB_CONTEXT))

    result = run(service.remove_account(account.account_id, LOCAL_WEB_CONTEXT))
    assert result["deleted"]["account_id"] == account.account_id
    assert result["deleted"]["session_status"] == "LOGIN_REQUIRED"
    assert not profile.exists()

    async def query_account() -> PlatformAccount | None:
        async with database.session() as session:
            return await session.get(PlatformAccount, account.account_id)

    assert run(query_account()) is None

    async def count_activity() -> int:
        async with database.session() as session:
            statement = select(AccountActivity).where(
                AccountActivity.account_id == account.account_id
            )
            return len((await session.scalars(statement)).all())

    assert run(count_activity()) == 0
    run(database.dispose())


def test_remove_account_refuses_delivery_history(tmp_path: Path) -> None:
    database = AccountDatabase(sqlite_database_url(tmp_path))
    profile_root = tmp_path / "runtime" / "profiles"
    service = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        allowed_profile_roots=(profile_root,),
    )
    run(service.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "with-history")
    account = run(insert_account(database, profile))

    async def attach_operation() -> None:
        async with database.session() as session:
            session.add(
                DeliveryOperation(
                    operation_id=str(uuid.uuid4()),
                    account_id=account.account_id,
                    platform="xiaoheihe",
                    mode="DRAFT",
                    source="WEB",
                    actor_id="local-web-user",
                    title=SAMPLE_ARTICLE_TITLE,
                    body=SAMPLE_ARTICLE_BODY,
                    content_version="test-v1",
                    account_display_name_snapshot=account.display_name,
                    status="DRAFT_SAVED",
                )
            )
            await session.flush()

    run(attach_operation())
    with pytest.raises(AccountSessionError) as exc_info:
        run(service.remove_account(account.account_id, LOCAL_WEB_CONTEXT))
    assert exc_info.value.error_code == "ACCOUNT_HAS_DELIVERY_HISTORY"
    assert profile.exists()

    async def query_account() -> PlatformAccount | None:
        async with database.session() as session:
            return await session.get(PlatformAccount, account.account_id)

    assert run(query_account()) is not None
    run(database.dispose())


def test_remove_account_unknown_account_returns_not_found(tmp_path: Path) -> None:
    database = AccountDatabase(sqlite_database_url(tmp_path))
    service = AccountSessionService(database, seed_legacy_profiles=False)
    run(service.initialize())
    with pytest.raises(AccountNotFoundError):
        run(service.remove_account("missing-account-id", LOCAL_WEB_CONTEXT))
    run(database.dispose())


def test_account_archive_clear_restore_preserves_delivery_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = AccountDatabase(sqlite_database_url(tmp_path))
    profile_root = tmp_path / "runtime" / "profiles"
    service = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        allowed_profile_roots=(profile_root,),
    )
    run(service.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "lifecycle")
    cookie_sentinel = profile / "Default" / "Cookies"
    cookie_sentinel.parent.mkdir(parents=True)
    cookie_sentinel.write_text("test-only-cookie-placeholder", encoding="utf-8")
    account = run(insert_account(database, profile))

    async def attach_completed_operation() -> str:
        operation_id = str(uuid.uuid4())
        async with database.session() as session:
            session.add(
                DeliveryOperation(
                    operation_id=operation_id,
                    account_id=account.account_id,
                    platform="xiaoheihe",
                    mode="DRAFT",
                    source="WEB",
                    actor_id="local-web-user",
                    title=SAMPLE_ARTICLE_TITLE,
                    body=SAMPLE_ARTICLE_BODY,
                    content_version="lifecycle-v1",
                    account_display_name_snapshot=account.display_name,
                    status="DRAFT_SAVED",
                )
            )
        return operation_id

    operation_id = run(attach_completed_operation())
    archived = run(service.archive_account(account.account_id, LOCAL_WEB_CONTEXT))
    assert archived["status"] == "ARCHIVED"
    assert archived["heartbeat_enabled"] is False
    assert run(service.list_accounts("xiaoheihe", LOCAL_WEB_CONTEXT)) == []
    archived_list = run(
        service.list_accounts(
            "xiaoheihe",
            LOCAL_WEB_CONTEXT,
            include_archived=True,
        )
    )
    assert [item["account_id"] for item in archived_list] == [account.account_id]

    with pytest.raises(AccountSessionError) as archived_error:
        run(service.mark_verifying(account.account_id, LOCAL_WEB_CONTEXT))
    assert archived_error.value.error_code == "ACCOUNT_ARCHIVED"

    monkeypatch.setattr(service, "_lease", lambda *_args, **_kwargs: nullcontext())
    cleared = run(service.clear_login_state(account.account_id, LOCAL_WEB_CONTEXT))
    assert cleared["status"] == "ARCHIVED"
    assert cleared["session_status"] == "LOGIN_REQUIRED"
    assert cleared["persist_login"] is False
    assert profile.is_dir()
    assert list(profile.iterdir()) == []

    async def retained_state() -> tuple[DeliveryOperation | None, list[str]]:
        async with database.session() as session:
            operation = await session.get(DeliveryOperation, operation_id)
            actions = list(
                (
                    await session.scalars(
                        select(AccountActivity.action).where(
                            AccountActivity.account_id == account.account_id
                        )
                    )
                ).all()
            )
            return operation, actions

    retained_operation, actions = run(retained_state())
    assert retained_operation is not None
    assert retained_operation.status == "DRAFT_SAVED"
    assert "ACCOUNT_ARCHIVED" in actions
    assert "LOGIN_STATE_CLEARED" in actions

    restored = run(service.restore_account(account.account_id, LOCAL_WEB_CONTEXT))
    assert restored["status"] == "ACTIVE"
    assert restored["session_status"] == "LOGIN_REQUIRED"
    assert restored["heartbeat_enabled"] is True

    with pytest.raises(AccountSessionError) as delete_error:
        run(service.remove_account(account.account_id, LOCAL_WEB_CONTEXT))
    assert delete_error.value.error_code == "ACCOUNT_HAS_DELIVERY_HISTORY"
    run(database.dispose())


def test_archive_refuses_account_with_active_delivery(tmp_path: Path) -> None:
    database = AccountDatabase(sqlite_database_url(tmp_path))
    service = AccountSessionService(database, seed_legacy_profiles=False)
    run(service.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "active-delivery")
    account = run(insert_account(database, profile))

    async def attach_queued_operation() -> None:
        async with database.session() as session:
            session.add(
                DeliveryOperation(
                    operation_id=str(uuid.uuid4()),
                    account_id=account.account_id,
                    platform="xiaoheihe",
                    mode="DRAFT",
                    source="WEB",
                    actor_id="local-web-user",
                    title=SAMPLE_ARTICLE_TITLE,
                    body=SAMPLE_ARTICLE_BODY,
                    content_version="queued-v1",
                    account_display_name_snapshot=account.display_name,
                    status="QUEUED",
                )
            )

    run(attach_queued_operation())
    with pytest.raises(AccountSessionError) as exc_info:
        run(service.archive_account(account.account_id, LOCAL_WEB_CONTEXT))
    assert exc_info.value.error_code == "ACCOUNT_HAS_ACTIVE_DELIVERY"
    run(database.dispose())


def test_account_lifecycle_http_contract_hides_archived_by_default(
    tmp_path: Path,
) -> None:
    from flask import Flask

    database_url = sqlite_database_url(tmp_path)
    database = AccountDatabase(database_url)
    run(database.initialize())
    profile = make_profile(tmp_path, "xiaoheihe", "lifecycle-http")
    account = run(insert_account(database, profile, display_name="归档接口账号"))
    run(database.dispose())

    app = Flask("account-lifecycle-http-test")
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

    archived = client.post(f"/api/account-sessions/{account.account_id}/archive")
    assert archived.status_code == 200
    assert archived.get_json()["status"] == "ARCHIVED"
    assert client.get("/api/platforms/xiaoheihe/accounts").get_json()["accounts"] == []
    archived_rows = client.get(
        "/api/platforms/xiaoheihe/accounts?include_archived=true"
    ).get_json()["accounts"]
    assert [row["account_id"] for row in archived_rows] == [account.account_id]

    missing_confirmation = client.post(
        f"/api/account-sessions/{account.account_id}/clear-login-state",
        json={},
    )
    assert missing_confirmation.status_code == 422
    assert missing_confirmation.get_json()["error"] == "REQUEST_VALIDATION_FAILED"
    assert profile.exists()

    restored = client.post(f"/api/account-sessions/{account.account_id}/restore")
    assert restored.status_code == 200
    assert restored.get_json()["status"] == "ACTIVE"
    app.extensions["account_sessions"].close()
