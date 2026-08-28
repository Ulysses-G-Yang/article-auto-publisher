"""投递前账号身份绑定与 fail-closed 门测试。"""

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.account_service import AccountSessionService
from account_sessions.contracts import DeliveryRequest
from account_sessions.database import AccountDatabase
from account_sessions.delivery_service import DeliveryService
from account_sessions.errors import (
    AccountIdentityError,
    AccountIdentityMismatchError,
)
from account_sessions.models import PlatformAccount
from account_sessions.permissions import LOCAL_WEB_CONTEXT


def run(coroutine):
    return asyncio.run(coroutine)


def database_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}"


class _FakeContext:
    async def clear_cookies(self) -> None:
        return None


class _IdentityPlatform:
    platform_name = "xiaoheihe"

    def __init__(
        self,
        observed_id: str,
        *,
        login: bool = True,
        identity_ok: bool = True,
        display_name: str = "运行时昵称",
    ) -> None:
        self.observed_id = observed_id
        self.login = login
        self.identity_ok = identity_ok
        self.display_name = display_name
        self.context = _FakeContext()
        self.publish_calls = 0
        self.cleaned = False

    async def initialize(self) -> None:
        return None

    async def check_login(self) -> bool:
        return self.login

    async def fetch_identity_payload(self) -> dict:
        return {
            "ok": self.identity_ok,
            "user_id": self.observed_id if self.identity_ok else "",
            "display_name": self.display_name if self.identity_ok else "",
        }

    async def publish(self, **_kwargs) -> dict:
        self.publish_calls += 1
        return {
            "success": True,
            "draft_url": "https://example.invalid/xiaoheihe/draft/verified",
            "post_url": "",
            "verification_evidence": {
                "draft_entity_bound": True,
                "draft_entity_source": "save_response_id",
                "draft_entity_id_match": True,
                "reopen_title_match": True,
                "reopen_dom_blocks_match": True,
            },
        }

    async def cleanup(self) -> None:
        self.cleaned = True


async def add_account(
    database: AccountDatabase,
    profile: Path,
    *,
    platform_user_id: str | None,
    display_name: str = "已绑定昵称",
) -> PlatformAccount:
    account = PlatformAccount(
        account_id=str(uuid.uuid4()),
        platform="xiaoheihe",
        platform_user_id=platform_user_id,
        display_name=display_name,
        profile_path=str(profile.resolve()),
        status="ACTIVE",
        session_status="VALID",
        persist_login=True,
        last_verified_at=datetime.now(timezone.utc),
    )
    async with database.session() as session:
        session.add(account)
        await session.flush()
    return account


def request_for(account_id: str) -> DeliveryRequest:
    return DeliveryRequest.model_validate(
        {
            "article": {"title": "身份门测试", "body": "正文"},
            "platform": "xiaoheihe",
            "account_id": account_id,
            "mode": "DRAFT",
        }
    )


def make_services(
    tmp_path: Path,
    platform: _IdentityPlatform,
) -> tuple[AccountDatabase, AccountSessionService, Path]:
    database = AccountDatabase(database_url(tmp_path))
    profile_root = tmp_path / "profiles"
    profile = profile_root / "xiaoheihe" / str(uuid.uuid4())
    profile.mkdir(parents=True)
    accounts = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: platform,
        allowed_profile_roots=(profile_root,),
    )
    run(accounts.initialize())
    return database, accounts, profile


def test_same_bound_identity_allows_publish_and_does_not_change_operation_snapshot(
    tmp_path: Path,
) -> None:
    platform = _IdentityPlatform("bound-id", display_name="页面新昵称")
    database, accounts, profile = make_services(tmp_path, platform)
    try:
        account = run(
            add_account(
                database,
                profile,
                platform_user_id="bound-id",
                display_name="执行单昵称",
            )
        )
        delivery = DeliveryService(accounts, platform_factory=lambda _account: platform)
        queued = run(
            delivery.request_delivery(request_for(account.account_id), LOCAL_WEB_CONTEXT)
        )

        completed = run(
            delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT)
        )

        assert completed["status"] == "DRAFT_SAVED"
        assert platform.publish_calls == 1
        stored = run(accounts.get_account(account.account_id))
        assert stored.display_name == "执行单昵称"
        operation = run(delivery._load_operation(queued["operation_id"]))[0]
        assert operation.account_display_name_snapshot == "执行单昵称"
    finally:
        run(database.dispose())


def test_mismatched_runtime_identity_blocks_publish_and_marks_account_error(
    tmp_path: Path,
) -> None:
    platform = _IdentityPlatform("wrong-runtime-id")
    database, accounts, profile = make_services(tmp_path, platform)
    try:
        account = run(
            add_account(
                database,
                profile,
                platform_user_id="bound-id",
            )
        )
        delivery = DeliveryService(accounts, platform_factory=lambda _account: platform)
        queued = run(
            delivery.request_delivery(request_for(account.account_id), LOCAL_WEB_CONTEXT)
        )

        with pytest.raises(AccountIdentityMismatchError) as exc_info:
            run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))

        assert exc_info.value.error_code == "ACCOUNT_IDENTITY_MISMATCH"
        assert "bound-id" not in str(exc_info.value)
        assert "wrong-runtime-id" not in str(exc_info.value)
        assert platform.publish_calls == 0
        stored = run(accounts.get_account(account.account_id))
        assert stored.session_status == "ERROR"
        assert stored.last_verified_at is None
        assert stored.platform_user_id == "bound-id"
        operation = run(delivery._load_operation(queued["operation_id"]))[0]
        assert operation.status == "FAILED"
        assert operation.error_code == "ACCOUNT_IDENTITY_MISMATCH"
        logs = run(accounts.list_activity(account.account_id, LOCAL_WEB_CONTEXT))
        messages = " ".join(row["message"] for row in logs)
        assert "bound-id" not in messages
        assert "wrong-runtime-id" not in messages
    finally:
        run(database.dispose())


def test_unverified_runtime_identity_blocks_publish_with_stable_code(
    tmp_path: Path,
) -> None:
    platform = _IdentityPlatform("", identity_ok=False)
    database, accounts, profile = make_services(tmp_path, platform)
    try:
        account = run(
            add_account(
                database,
                profile,
                platform_user_id="bound-id",
            )
        )
        delivery = DeliveryService(accounts, platform_factory=lambda _account: platform)
        queued = run(
            delivery.request_delivery(request_for(account.account_id), LOCAL_WEB_CONTEXT)
        )

        with pytest.raises(AccountIdentityError) as exc_info:
            run(delivery.execute_operation(queued["operation_id"], LOCAL_WEB_CONTEXT))

        assert getattr(exc_info.value, "error_code", None) == "ACCOUNT_IDENTITY_UNVERIFIED"
        assert platform.publish_calls == 0
        stored = run(accounts.get_account(account.account_id))
        assert stored.session_status == "ERROR"
        operation = run(delivery._load_operation(queued["operation_id"]))[0]
        assert operation.error_code == "ACCOUNT_IDENTITY_UNVERIFIED"
    finally:
        run(database.dispose())


def test_verify_account_does_not_drift_existing_identity_or_nickname(
    tmp_path: Path,
) -> None:
    platform = _IdentityPlatform("new-runtime-id", display_name="攻击者昵称")
    database, accounts, profile = make_services(tmp_path, platform)
    try:
        account = run(
            add_account(
                database,
                profile,
                platform_user_id="bound-id",
                display_name="原始昵称",
            )
        )

        with pytest.raises(AccountIdentityMismatchError) as exc_info:
            run(accounts.verify_account(account.account_id, LOCAL_WEB_CONTEXT))

        assert exc_info.value.error_code == "ACCOUNT_IDENTITY_MISMATCH"
        assert "bound-id" not in str(exc_info.value)
        assert "new-runtime-id" not in str(exc_info.value)
        stored = run(accounts.get_account(account.account_id))
        assert stored.platform_user_id == "bound-id"
        assert stored.display_name == "原始昵称"
        assert stored.session_status == "ERROR"
        assert stored.last_verified_at is None
    finally:
        run(database.dispose())
