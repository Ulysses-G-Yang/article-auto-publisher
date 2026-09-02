"""Profile 租约必须覆盖平台 cleanup 的生命周期回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from account_sessions.account_service import AccountSessionService
from account_sessions.delivery_service import DeliveryService
from account_sessions.draft_verify import DraftVerifyService
from account_sessions.errors import AccountSessionError
from account_sessions.permissions import AccessContext


class _RecordingLease:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self):
        self.events.append("lease-acquire")
        return self

    def __exit__(self, *_exc_info) -> None:
        self.events.append("lease-release")


def _access(*capabilities: str) -> AccessContext:
    return AccessContext(
        actor_id="lease-test",
        source="TEST",
        capabilities=frozenset(capabilities),
    )


@pytest.mark.asyncio
async def test_delivery_clears_and_cleans_before_releasing_profile_lease() -> None:
    events: list[str] = []

    class Context:
        async def clear_cookies(self) -> None:
            events.append("clear-cookies")

    class Platform:
        context = Context()

        async def initialize(self) -> None:
            events.append("initialize")

        async def publish(self, **_kwargs) -> dict:
            events.append("publish")
            return {
                "success": True,
                "draft_url": "https://example.invalid/draft/1",
                "verification_evidence": {
                    "draft_entity_bound": True,
                    "draft_entity_id_match": True,
                    "reopen_title_match": True,
                    "reopen_dom_blocks_match": True,
                },
            }

        async def cleanup(self) -> None:
            events.append("cleanup")

    account = SimpleNamespace(
        account_id="account-1",
        platform="toutiao",
        persist_login=False,
    )
    operation = SimpleNamespace(
        operation_id="operation-1",
        status="QUEUED",
        mode="DRAFT",
        title="标题",
        body="正文",
        content_reference=None,
        persist_login_snapshot=False,
    )
    accounts = MagicMock()
    accounts.database = MagicMock()
    accounts._lease.side_effect = lambda *_args, **_kwargs: _RecordingLease(events)

    async def assert_identity(*_args, **_kwargs) -> None:
        events.append("identity")

    accounts.assert_delivery_identity = assert_identity
    service = DeliveryService(accounts, platform_factory=lambda _account: Platform())
    service._load_operation = AsyncMock(return_value=(operation, account))
    service._mark_started = AsyncMock(return_value=True)
    service._mark_completed = AsyncMock(return_value={"status": "DRAFT_SAVED"})
    service._record_session_cleanup = AsyncMock()
    service._mark_session_login_required = AsyncMock()

    result = await service.execute_operation(
        operation.operation_id,
        _access("draft.create"),
    )

    assert result == {"status": "DRAFT_SAVED"}
    assert events == [
        "lease-acquire",
        "initialize",
        "identity",
        "publish",
        "clear-cookies",
        "cleanup",
        "lease-release",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_stage", "error_type"),
    [
        pytest.param("publish", RuntimeError, id="publish-error"),
        pytest.param("publish", asyncio.CancelledError, id="publish-cancelled"),
        pytest.param("initialize", RuntimeError, id="initialize-half-failed"),
    ],
)
async def test_delivery_failure_still_cleans_before_releasing_profile_lease(
    failure_stage: str,
    error_type: type[BaseException],
) -> None:
    events: list[str] = []

    class Context:
        async def clear_cookies(self) -> None:
            events.append("clear-cookies")

    class Platform:
        context = Context()

        async def initialize(self) -> None:
            events.append("initialize")
            if failure_stage == "initialize":
                raise error_type("initialize failed")

        async def publish(self, **_kwargs) -> dict:
            events.append("publish")
            raise error_type("publish failed")

        async def cleanup(self) -> None:
            events.append("cleanup")

    account = SimpleNamespace(
        account_id="account-1",
        platform="toutiao",
        persist_login=False,
    )
    operation = SimpleNamespace(
        operation_id="operation-1",
        status="QUEUED",
        mode="DRAFT",
        title="标题",
        body="正文",
        content_reference=None,
        persist_login_snapshot=False,
    )
    accounts = MagicMock()
    accounts.database = MagicMock()
    accounts._lease.side_effect = lambda *_args, **_kwargs: _RecordingLease(events)

    async def assert_identity(*_args, **_kwargs) -> None:
        events.append("identity")

    accounts.assert_delivery_identity = assert_identity
    service = DeliveryService(accounts, platform_factory=lambda _account: Platform())
    service._load_operation = AsyncMock(return_value=(operation, account))
    service._mark_started = AsyncMock(return_value=True)
    service._mark_failed = AsyncMock()
    service._record_session_cleanup = AsyncMock()
    service._mark_session_login_required = AsyncMock()

    with pytest.raises(error_type):
        await service.execute_operation(
            operation.operation_id,
            _access("draft.create"),
        )

    assert events[-3:] == ["clear-cookies", "cleanup", "lease-release"]
    assert events.index("cleanup") < events.index("lease-release")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [RuntimeError, asyncio.CancelledError],
    ids=["initialize-error", "initialize-cancelled"],
)
async def test_account_verification_initialize_failure_cleans_before_release(
    error_type: type[BaseException],
) -> None:
    events: list[str] = []
    account = SimpleNamespace(
        account_id="account-1",
        platform="toutiao",
        status="ACTIVE",
    )

    class Platform:
        async def initialize(self) -> None:
            events.append("initialize")
            raise error_type("initialize failed")

        async def cleanup(self) -> None:
            events.append("cleanup")

    service = AccountSessionService(
        MagicMock(),
        seed_legacy_profiles=False,
        platform_factory=lambda _account: Platform(),
    )
    service.get_account = AsyncMock(return_value=account)
    service._lease = lambda *_args, **_kwargs: _RecordingLease(events)
    service._mark_verification_failure = AsyncMock()

    with pytest.raises(error_type):
        await service.verify_account(
            account.account_id,
            _access("session.verify"),
        )

    assert events == ["lease-acquire", "initialize", "cleanup", "lease-release"]
    if error_type is asyncio.CancelledError:
        service._mark_verification_failure.assert_not_awaited()
    else:
        service._mark_verification_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_logout_initialize_failure_cleans_before_release() -> None:
    events: list[str] = []
    account = SimpleNamespace(
        account_id="account-1",
        platform="toutiao",
        status="ACTIVE",
    )

    class Platform:
        async def initialize(self) -> None:
            events.append("initialize")
            raise RuntimeError("initialize failed")

        async def cleanup(self) -> None:
            events.append("cleanup")

    service = AccountSessionService(
        MagicMock(),
        seed_legacy_profiles=False,
        platform_factory=lambda _account: Platform(),
    )
    service.get_account = AsyncMock(return_value=account)
    service._lease = lambda *_args, **_kwargs: _RecordingLease(events)
    service._mark_verification_failure = AsyncMock()

    with pytest.raises(RuntimeError, match="initialize failed"):
        await service.logout_account(
            account.account_id,
            _access("session.manage"),
        )

    assert events == ["lease-acquire", "initialize", "cleanup", "lease-release"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [RuntimeError, asyncio.CancelledError],
    ids=["initialize-error", "initialize-cancelled"],
)
async def test_readonly_verification_initialize_failure_cleans_before_release(
    error_type: type[BaseException],
) -> None:
    events: list[str] = []
    operation = SimpleNamespace(title="标题", platform="toutiao", status="RESULT_UNKNOWN")
    account = SimpleNamespace(
        account_id="account-1",
        platform="toutiao",
        status="ACTIVE",
    )

    class Platform:
        async def initialize(self) -> None:
            events.append("initialize")
            raise error_type("initialize failed")

        async def cleanup(self) -> None:
            events.append("cleanup")

    accounts = MagicMock()
    accounts._lease.side_effect = lambda *_args, **_kwargs: _RecordingLease(events)
    delivery = MagicMock()
    delivery._load_operation = AsyncMock(return_value=(operation, account))
    service = DraftVerifyService(
        accounts,
        delivery,
        platform_factory=lambda _account: Platform(),
    )

    if error_type is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await service.verify_draft(
                "op-1",
                _access("draft.create", "session.read"),
            )
    else:
        with pytest.raises(AccountSessionError) as caught:
            await service.verify_draft(
                "op-1",
                _access("draft.create", "session.read"),
            )
        assert caught.value.error_code == "PROBE_RESULT_UNKNOWN"

    assert events == ["lease-acquire", "initialize", "cleanup", "lease-release"]
