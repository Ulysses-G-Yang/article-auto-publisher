from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import update

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.database import AccountDatabase
from account_sessions.errors import AccountBusyError, AccountIdentityError
from account_sessions.models import AccountActivity, PlatformAccount
from account_sessions.permissions import AccessContext
from account_sessions.session_health import (
    HeartbeatPolicy,
    HeartbeatScheduler,
    HeartbeatService,
    HEARTBEAT_CLAIM_NOT_ACQUIRED,
    HEARTBEAT_ACCESS_CONTEXT,
    as_utc,
    classify_verification_failure,
)
from platforms.base import LoginRequiredError, PlatformAutomationError


def run(coroutine):
    return asyncio.run(coroutine)


def database_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}"


def make_policy() -> HeartbeatPolicy:
    return HeartbeatPolicy(
        success_ttl=timedelta(hours=1),
        scan_interval=timedelta(seconds=5),
        max_per_scan=1,
        busy_backoff=timedelta(seconds=10),
        error_backoff_base=timedelta(seconds=20),
        error_backoff_max=timedelta(minutes=2),
        login_required_backoff=timedelta(minutes=3),
        jitter_ratio=0,
    )


async def add_account(
    database: AccountDatabase,
    *,
    platform_user_id: str | None = None,
    status: str = "ACTIVE",
    session_status: str = "VALID",
    heartbeat_enabled: bool = True,
    last_verified_at: datetime | None = None,
    next_heartbeat_at: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
    heartbeat_failures: int = 0,
    last_heartbeat_error_code: str | None = None,
    heartbeat_claim_owner: str | None = None,
    heartbeat_claimed_at: datetime | None = None,
    heartbeat_claim_expires_at: datetime | None = None,
    updated_at: datetime | None = None,
    profile_path: str | Path | None = None,
) -> PlatformAccount:
    account_id = str(uuid.uuid4())
    account = PlatformAccount(
        account_id=account_id,
        platform="xiaoheihe",
        platform_user_id=platform_user_id or f"platform-user-{account_id}",
        display_name="测试账号",
        profile_path=str(profile_path or f"/not-used-by-fake-verify/{uuid.uuid4()}"),
        status=status,
        session_status=session_status,
        heartbeat_enabled=heartbeat_enabled,
        last_verified_at=last_verified_at,
        next_heartbeat_at=next_heartbeat_at,
        last_heartbeat_at=last_heartbeat_at,
        heartbeat_failures=heartbeat_failures,
        last_heartbeat_error_code=last_heartbeat_error_code,
        heartbeat_claim_owner=heartbeat_claim_owner,
        heartbeat_claimed_at=heartbeat_claimed_at,
        heartbeat_claim_expires_at=heartbeat_claim_expires_at,
        updated_at=updated_at or datetime.now(timezone.utc),
    )
    async with database.session() as session:
        session.add(account)
        await session.flush()
    return account


def test_policy_validates_ranges_and_uses_default_singleton_batch() -> None:
    assert HeartbeatPolicy().max_per_scan == 1
    assert HeartbeatPolicy().claim_ttl == timedelta(minutes=10)
    with pytest.raises(ValueError):
        HeartbeatPolicy(success_ttl=0)
    with pytest.raises(ValueError):
        HeartbeatPolicy(scan_interval=-1)
    with pytest.raises(ValueError):
        HeartbeatPolicy(max_per_scan=0)
    with pytest.raises(ValueError):
        HeartbeatPolicy(max_per_scan=101)
    with pytest.raises(ValueError):
        HeartbeatPolicy(error_backoff_base=10, error_backoff_max=9)
    with pytest.raises(ValueError):
        HeartbeatPolicy(jitter_ratio=1.1)
    with pytest.raises(ValueError):
        HeartbeatPolicy(claim_ttl=29)
    with pytest.raises(ValueError):
        HeartbeatPolicy(claim_ttl=timedelta(hours=1, seconds=1))


def test_policy_due_stale_backoff_and_deterministic_jitter() -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    policy = HeartbeatPolicy(
        success_ttl=timedelta(hours=1),
        scan_interval=timedelta(minutes=1),
        busy_backoff=timedelta(seconds=10),
        error_backoff_base=timedelta(seconds=20),
        error_backoff_max=timedelta(seconds=30),
        jitter_ratio=0.5,
    )
    assert policy.is_stale(None, now)
    assert policy.is_stale(now - timedelta(hours=1), now)
    assert not policy.is_stale(now - timedelta(minutes=59), now)
    assert policy.is_due(None, last_verified_at=now, now=now)
    assert not policy.is_due(
        now + timedelta(minutes=1), last_verified_at=now, now=now
    )
    assert not policy.is_due(
        now, last_verified_at=now, now=now, session_status="LOGIN_REQUIRED"
    )
    first = policy.calculate_next(now, "account-a", outcome="success")
    second = policy.calculate_next(now, "account-a", outcome="success")
    other = policy.calculate_next(now, "account-b", outcome="success")
    assert first == second
    assert first >= now + policy.success_ttl
    assert other != first
    assert policy.calculate_next(
        now, "account-a", error_code="RATE_LIMITED", failures=2
    ) >= now + timedelta(seconds=30)


def test_failure_classifier_uses_stable_type_code_and_prefix() -> None:
    busy = classify_verification_failure(AccountBusyError("/secret/profile"))
    assert (busy.category, busy.disposition, busy.preserve_session) == (
        "BUSY",
        "DEFERRED",
        True,
    )
    profile_busy = classify_verification_failure(
        PlatformAutomationError("PROFILE_IN_USE: /secret/profile")
    )
    assert profile_busy.error_code == "PROFILE_IN_USE"
    zol_challenge = classify_verification_failure(
        PlatformAutomationError("ZOL_SECURITY_CHALLENGE: 安全验证")
    )
    assert (zol_challenge.category, zol_challenge.error_code) == ("CHALLENGE", "CHALLENGE")
    login = classify_verification_failure(LoginRequiredError("anything"))
    assert login.category == "LOGIN_REQUIRED"

    class RateError(RuntimeError):
        error_code = "RATE_LIMITED"

    class ChallengeError(RuntimeError):
        error_code = "CHALLENGE"

    assert classify_verification_failure(RateError("token=secret")).category == "RATE_LIMITED"
    assert classify_verification_failure(ChallengeError("captcha")).category == "CHALLENGE"
    unknown = classify_verification_failure(RuntimeError("登录字样但没有稳定码"))
    assert (unknown.category, unknown.error_code) == ("ERROR", "UNKNOWN")

    class LeakyCodeError(RuntimeError):
        error_code = "TOKEN=secret"

    leaky = classify_verification_failure(LeakyCodeError("TOKEN=secret /profile"))
    assert (leaky.category, leaky.error_code) == ("ERROR", "UNKNOWN")


def test_scan_success_is_serial_and_limited_to_one(tmp_path: Path) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    database = AccountDatabase(database_url(tmp_path))
    policy = make_policy()
    calls: list[tuple[str, bool]] = []

    async def verify(account_id, _access, *, allow_interactive_login=False):
        calls.append((account_id, allow_interactive_login))

    async def scenario() -> tuple[PlatformAccount, PlatformAccount, dict]:
        await database.initialize()
        first = await add_account(database, last_verified_at=None)
        second = await add_account(database, last_verified_at=None)
        service = HeartbeatService(database, verify, policy=policy)
        result = await service.scan_once(now=now)
        return first, second, result

    first, second, result = run(scenario())
    assert result["processed"] == 1
    assert result["succeeded"] == 1
    assert [item[1] for item in calls] == [False]
    stored_first = run(_get_account(database, first.account_id))
    stored_second = run(_get_account(database, second.account_id))
    updated = stored_first if stored_first.last_heartbeat_at is not None else stored_second
    untouched = stored_second if updated.account_id == stored_first.account_id else stored_first
    assert updated.account_id == calls[0][0]
    assert as_utc(updated.last_heartbeat_at) == now
    assert as_utc(updated.next_heartbeat_at) == now + policy.success_ttl
    assert updated.heartbeat_failures == 0
    assert untouched.last_heartbeat_at is None
    run(database.dispose())


def test_two_heartbeat_services_only_one_verifies_due_account(tmp_path: Path) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    first_database = AccountDatabase(database_url(tmp_path))
    second_database = AccountDatabase(database_url(tmp_path))
    calls: list[str] = []

    async def verify(account_id, _access, *, allow_interactive_login=False):
        assert allow_interactive_login is False
        calls.append(account_id)
        await asyncio.sleep(0.05)

    async def scenario() -> tuple[dict, dict, str]:
        await first_database.initialize()
        await second_database.initialize()
        account = await add_account(
            first_database,
            session_status="VALID",
            next_heartbeat_at=now - timedelta(seconds=1),
        )
        policy = HeartbeatPolicy(
            claim_ttl=timedelta(minutes=1),
            max_per_scan=1,
            jitter_ratio=0,
        )
        first = HeartbeatService(
            first_database,
            verify,
            policy=policy,
            owner_id="heartbeat-a",
        )
        second = HeartbeatService(
            second_database,
            verify,
            policy=policy,
            owner_id="heartbeat-b",
        )
        result_a, result_b = await asyncio.gather(
            first.scan_once(now=now),
            second.scan_once(now=now),
        )
        return result_a, result_b, account.account_id

    result_a, result_b, account_id = run(scenario())
    assert len(calls) == 1
    assert result_a["succeeded"] + result_b["succeeded"] == 1
    assert result_a["processed"] + result_b["processed"] == 2
    skipped = [item for result in (result_a, result_b) for item in result["results"]]
    assert sum(item["error_code"] == HEARTBEAT_CLAIM_NOT_ACQUIRED for item in skipped) == 1
    stored = run(_get_account(first_database, account_id))
    assert stored.heartbeat_claim_owner is None
    assert stored.heartbeat_claim_expires_at is None
    run(first_database.dispose())
    run(second_database.dispose())


def test_claim_cannot_be_taken_before_expiry_but_can_be_taken_after(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    first_database = AccountDatabase(database_url(tmp_path))
    second_database = AccountDatabase(database_url(tmp_path))

    async def scenario() -> None:
        await first_database.initialize()
        await second_database.initialize()
        account = await add_account(
            first_database,
            next_heartbeat_at=now - timedelta(seconds=1),
        )
        policy = HeartbeatPolicy(claim_ttl=timedelta(minutes=1), jitter_ratio=0)
        first = HeartbeatService(
            first_database,
            lambda *_args, **_kwargs: None,
            policy=policy,
            owner_id="heartbeat-a",
        )
        second = HeartbeatService(
            second_database,
            lambda *_args, **_kwargs: None,
            policy=policy,
            owner_id="heartbeat-b",
        )
        assert await first._claim_account(account.account_id, now) is True
        assert (
            await second._claim_account(account.account_id, now + timedelta(seconds=59))
            is False
        )
        assert (
            await second._claim_account(account.account_id, now + timedelta(minutes=1))
            is True
        )
        assert await second._release_claim(account.account_id) is True

    run(scenario())
    run(first_database.dispose())
    run(second_database.dispose())


def test_recovery_only_resets_verifying_with_expired_heartbeat_claim(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    database = AccountDatabase(database_url(tmp_path))

    async def scenario() -> tuple[
        PlatformAccount,
        PlatformAccount,
        PlatformAccount,
        PlatformAccount,
        dict,
    ]:
        await database.initialize()
        policy = HeartbeatPolicy(claim_ttl=timedelta(minutes=1), jitter_ratio=0)
        expired = now - timedelta(seconds=1)
        future = now + timedelta(minutes=1)
        valid = await add_account(
            database,
            session_status="VERIFYING",
            last_verified_at=now - timedelta(hours=1),
            heartbeat_claim_owner="dead-heartbeat",
            heartbeat_claimed_at=now - timedelta(minutes=2),
            heartbeat_claim_expires_at=expired,
        )
        unverified = await add_account(
            database,
            session_status="VERIFYING",
            heartbeat_claim_owner="dead-heartbeat-2",
            heartbeat_claimed_at=now - timedelta(minutes=2),
            heartbeat_claim_expires_at=expired,
        )
        no_claim = await add_account(
            database,
            session_status="VERIFYING",
            updated_at=now - timedelta(hours=2),
        )
        active_claim = await add_account(
            database,
            session_status="VERIFYING",
            last_verified_at=now - timedelta(hours=1),
            heartbeat_claim_owner="live-heartbeat",
            heartbeat_claimed_at=now - timedelta(seconds=1),
            heartbeat_claim_expires_at=future,
        )
        service = HeartbeatService(
            database,
            lambda *_args, **_kwargs: None,
            policy=policy,
            owner_id="recovery-test",
            enabled=False,
        )
        result = await service.recover_stale_state(now=now)
        return (
            await _get_account(database, valid.account_id),
            await _get_account(database, unverified.account_id),
            await _get_account(database, no_claim.account_id),
            await _get_account(database, active_claim.account_id),
            result,
        )

    valid, unverified, no_claim, active_claim, result = run(scenario())
    assert valid.session_status == "VALID"
    assert as_utc(valid.next_heartbeat_at) == now
    assert valid.heartbeat_claim_owner is None
    assert unverified.session_status == "UNVERIFIED"
    assert as_utc(unverified.next_heartbeat_at) == now
    assert no_claim.session_status == "VERIFYING"
    assert no_claim.next_heartbeat_at is None
    assert active_claim.session_status == "VERIFYING"
    assert active_claim.heartbeat_claim_owner == "live-heartbeat"
    assert result["recovered_valid"] == 1
    assert result["recovered_unverified"] == 1
    assert result["recovered_verifying"] == 2
    run(database.dispose())


def test_claim_fencing_never_overwrites_a_claim_taken_by_another_owner(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    first_database = AccountDatabase(database_url(tmp_path))
    second_database = AccountDatabase(database_url(tmp_path))

    async def scenario() -> tuple[dict, PlatformAccount]:
        await first_database.initialize()
        await second_database.initialize()
        account = await add_account(
            first_database,
            session_status="VALID",
            next_heartbeat_at=now - timedelta(seconds=1),
        )
        service = HeartbeatService(
            first_database,
            lambda *_args, **_kwargs: None,
            policy=HeartbeatPolicy(claim_ttl=timedelta(minutes=1), jitter_ratio=0),
            owner_id="heartbeat-a",
        )
        assert await service._claim_account(account.account_id, now) is True
        async with second_database.session() as session:
            await session.execute(
                update(PlatformAccount)
                .where(PlatformAccount.account_id == account.account_id)
                .values(heartbeat_claim_owner="heartbeat-b")
            )
        result = await service._record_success(account.account_id, now)
        return result, await _get_account(first_database, account.account_id)

    result, stored = run(scenario())
    assert result == {
        "account_id": stored.account_id,
        "outcome": "CLAIM_LOST",
        "error_code": "HEARTBEAT_CLAIM_LOST",
    }
    assert stored.session_status == "VALID"
    assert stored.last_heartbeat_at is None
    assert stored.heartbeat_claim_owner == "heartbeat-b"
    run(first_database.dispose())
    run(second_database.dispose())


def test_success_failure_and_cancellation_clear_only_their_claims(tmp_path: Path) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    database = AccountDatabase(database_url(tmp_path))

    async def scenario() -> list[PlatformAccount]:
        await database.initialize()
        accounts = [
            await add_account(database, next_heartbeat_at=now - timedelta(seconds=1))
            for _ in range(3)
        ]
        policy = HeartbeatPolicy(claim_ttl=timedelta(minutes=1), jitter_ratio=0)

        async def success(*_args, **_kwargs):
            return True

        async def failure(*_args, **_kwargs):
            raise LoginRequiredError("SESSION_EXPIRED")

        async def cancelled(*_args, **_kwargs):
            raise asyncio.CancelledError()

        for verifier, account in zip(
            (success, failure, cancelled), accounts, strict=True
        ):
            service = HeartbeatService(
                database,
                verifier,
                policy=policy,
                owner_id=f"owner-{account.account_id}",
            )
            if verifier is cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await service.check_account(account.account_id, now=now)
            else:
                await service.check_account(account.account_id, now=now)
        return [await _get_account(database, account.account_id) for account in accounts]

    stored = run(scenario())
    assert all(account.heartbeat_claim_owner is None for account in stored)
    assert all(account.heartbeat_claimed_at is None for account in stored)
    assert all(account.heartbeat_claim_expires_at is None for account in stored)
    assert stored[0].session_status == "VALID"
    assert stored[1].session_status == "LOGIN_REQUIRED"
    assert stored[2].session_status == "VALID"
    run(database.dispose())


def test_disabled_scheduler_recovers_database_claims_without_verifying(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    database = AccountDatabase(database_url(tmp_path))
    calls: list[str] = []

    async def scenario() -> PlatformAccount:
        await database.initialize()
        account = await add_account(
            database,
            session_status="VERIFYING",
            heartbeat_claim_owner="dead-heartbeat",
            heartbeat_claimed_at=now - timedelta(minutes=2),
            heartbeat_claim_expires_at=now - timedelta(seconds=1),
        )

        async def verify(account_id, *_args, **_kwargs):
            calls.append(account_id)

        policy = HeartbeatPolicy(claim_ttl=timedelta(minutes=1), jitter_ratio=0)
        service = HeartbeatService(
            database,
            verify,
            policy=policy,
            enabled=False,
            owner_id="disabled-scheduler",
        )
        scheduler = HeartbeatScheduler(service, enabled=False, policy=policy)
        assert await scheduler.start() is False
        assert scheduler.running is False
        return await _get_account(database, account.account_id)

    stored = run(scenario())
    assert calls == []
    assert stored.heartbeat_claim_owner is None
    assert stored.session_status == "UNVERIFIED"
    run(database.dispose())


@pytest.mark.parametrize(
    ("failure", "category", "expected_status"),
    [
        (AccountBusyError("PROFILE_IN_USE: /secret/profile"), "DEFERRED", "VALID"),
        (LoginRequiredError("SESSION_EXPIRED"), "FAILED", "LOGIN_REQUIRED"),
    ],
)
def test_scan_busy_and_login_transitions(
    tmp_path: Path,
    failure: Exception,
    category: str,
    expected_status: str,
) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    verified = now - timedelta(minutes=5)
    database = AccountDatabase(database_url(tmp_path))

    async def verify(*_args, **_kwargs):
        raise failure

    async def scenario() -> str:
        await database.initialize()
        account = await add_account(
            database,
            session_status="VALID",
            last_verified_at=verified,
            next_heartbeat_at=now - timedelta(seconds=1),
        )
        service = HeartbeatService(database, verify, policy=make_policy())
        result = await service.check_account(account.account_id, now=now)
        assert result["outcome"] == category
        stored = await _get_account(database, account.account_id)
        assert stored.session_status == expected_status
        assert as_utc(stored.last_verified_at) == (
            verified if category == "DEFERRED" else None
        )
        assert as_utc(stored.last_heartbeat_at) == now
        assert stored.last_heartbeat_error_code in {"ACCOUNT_BUSY", "LOGIN_REQUIRED"}
        if category == "DEFERRED":
            assert stored.heartbeat_failures == 0
        else:
            assert stored.heartbeat_failures == 1
        return account.account_id

    run(scenario())
    actions = run(_activity_rows(database))
    expected_action = "HEARTBEAT_DEFERRED" if category == "DEFERRED" else "HEARTBEAT_FAILED"
    assert expected_action in {row.action for row in actions}
    assert all("/secret/profile" not in row.message for row in actions)
    run(database.dispose())


@pytest.mark.parametrize("error_code", ["RATE_LIMITED", "CHALLENGE", "UNKNOWN"])
def test_transient_and_unknown_failures_backoff_without_invalidating_valid(
    tmp_path: Path,
    error_code: str,
) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    verified = now - timedelta(minutes=5)
    database = AccountDatabase(database_url(tmp_path))

    class StableFailure(RuntimeError):
        pass

    async def verify(*_args, **_kwargs):
        if error_code == "UNKNOWN":
            raise RuntimeError("cookie=secret /secret/profile")
        error = StableFailure("opaque detail")
        error.error_code = error_code
        raise error

    async def scenario() -> str:
        await database.initialize()
        account = await add_account(
            database,
            last_verified_at=verified,
            next_heartbeat_at=now - timedelta(seconds=1),
        )
        service = HeartbeatService(database, verify, policy=make_policy())
        result = await service.check_account(account.account_id, now=now)
        assert result["outcome"] == "FAILED"
        stored = await _get_account(database, account.account_id)
        assert stored.session_status == "VALID"
        assert as_utc(stored.last_verified_at) == verified
        assert as_utc(stored.last_heartbeat_at) == now
        assert stored.heartbeat_failures == 1
        assert stored.last_heartbeat_error_code == error_code
        assert as_utc(stored.next_heartbeat_at) > now
        return account.account_id

    run(scenario())
    rows = run(_activity_rows(database))
    assert all("secret" not in row.message for row in rows)
    assert all("/secret/profile" not in row.message for row in rows)
    run(database.dispose())


async def _get_account(database: AccountDatabase, account_id: str) -> PlatformAccount:
    async with database.session() as session:
        account = await session.get(PlatformAccount, account_id)
        assert account is not None
        return account


async def _activity_rows(database: AccountDatabase) -> list[AccountActivity]:
    async with database.session() as session:
        return list((await session.scalars(select(AccountActivity))).all())


class _FakeSchedulerService:
    def __init__(self, policy: HeartbeatPolicy) -> None:
        self.policy = policy
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.first_scan = asyncio.Event()

    async def scan_once(self) -> dict:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.first_scan.set()
        try:
            await asyncio.sleep(0)
            return {"processed": 0}
        finally:
            self.active -= 1


def test_scheduler_is_disabled_by_default_and_stop_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACCOUNT_SESSION_HEARTBEAT_ENABLED", raising=False)

    async def scenario() -> None:
        from account_sessions.session_health import HeartbeatScheduler

        service = _FakeSchedulerService(make_policy())
        scheduler = HeartbeatScheduler(service)
        assert await scheduler.start() is False
        assert service.calls == 0
        assert await scheduler.stop() is False

    run(scenario())


def test_scheduler_start_twice_waits_before_scan_and_stop_wakes_immediately() -> None:
    async def scenario() -> None:
        from account_sessions.session_health import HeartbeatScheduler

        policy = HeartbeatPolicy(scan_interval=0.05, jitter_ratio=0)
        service = _FakeSchedulerService(policy)
        scheduler = HeartbeatScheduler(service, enabled=True)
        assert await scheduler.start() is True
        assert await scheduler.start() is False
        await asyncio.sleep(0)
        assert service.calls == 0
        await asyncio.wait_for(service.first_scan.wait(), timeout=1)
        assert service.calls == 1
        assert await scheduler.stop() is True
        assert scheduler.running is False
        assert await scheduler.stop() is False

    run(scenario())


def test_scheduler_stop_cancels_inflight_scan_and_releases_claim(tmp_path: Path) -> None:
    """stop() 等待取消清理完成，不留 busy 或数据库 claim。"""

    async def scenario() -> None:
        database = AccountDatabase(database_url(tmp_path))
        await database.initialize()
        now = datetime.now(timezone.utc)
        account = await add_account(
            database,
            next_heartbeat_at=now - timedelta(seconds=1),
        )
        verify_entered = asyncio.Event()
        cleanup_entered = asyncio.Event()
        allow_cleanup = asyncio.Event()
        verify_finalized = asyncio.Event()
        keep_verifying = asyncio.Event()

        async def verify(*_args, **_kwargs):
            verify_entered.set()
            try:
                await keep_verifying.wait()
            finally:
                cleanup_entered.set()
                try:
                    await allow_cleanup.wait()
                finally:
                    verify_finalized.set()

        policy = HeartbeatPolicy(
            scan_interval=0.01,
            claim_ttl=timedelta(seconds=30),
            jitter_ratio=0,
        )
        service = HeartbeatService(
            database,
            verify,
            policy=policy,
            owner_id="stop-cancellation-test",
        )
        scheduler = HeartbeatScheduler(service, enabled=True, policy=policy)
        try:
            assert await scheduler.start() is True
            await asyncio.wait_for(verify_entered.wait(), timeout=1)
            claimed = await _get_account(database, account.account_id)
            assert claimed.heartbeat_claim_owner == "stop-cancellation-test"

            stop_call = asyncio.create_task(scheduler.stop())
            await asyncio.wait_for(cleanup_entered.wait(), timeout=1)
            stop_call.cancel()
            await asyncio.sleep(0)
            assert stop_call.done() is False
            allow_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await stop_call

            assert verify_finalized.is_set()
            assert service.busy_account_ids == set()
            assert scheduler.running is False
            assert scheduler._task is None
            assert scheduler._stop_event is None
            assert await scheduler.stop() is False

            stored = await _get_account(database, account.account_id)
            assert stored.heartbeat_claim_owner is None
            assert stored.heartbeat_claimed_at is None
            assert stored.heartbeat_claim_expires_at is None
        finally:
            allow_cleanup.set()
            await scheduler.stop()
            await database.dispose()

    run(scenario())


def test_stop_cancelled_during_recovery_still_stops_started_scheduler() -> None:
    """stop 在 recovery 窗口被取消时，先停掉新 task 再传播取消。"""

    async def scenario() -> None:
        class RecoveringService:
            def __init__(self, policy: HeartbeatPolicy) -> None:
                self.policy = policy
                self.recover_entered = asyncio.Event()
                self.allow_recover = asyncio.Event()

            async def recover_stale_state(self) -> None:
                self.recover_entered.set()
                await self.allow_recover.wait()

            async def scan_once(self) -> dict:
                return {"processed": 0}

        policy = HeartbeatPolicy(scan_interval=60, jitter_ratio=0)
        service = RecoveringService(policy)
        scheduler = HeartbeatScheduler(service, enabled=True, policy=policy)
        start_call = asyncio.create_task(scheduler.start())
        await asyncio.wait_for(service.recover_entered.wait(), timeout=1)

        stop_call = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0)
        stop_call.cancel()
        await asyncio.sleep(0)
        assert stop_call.done() is False

        service.allow_recover.set()
        assert await start_call is True
        with pytest.raises(asyncio.CancelledError):
            await stop_call
        assert scheduler.running is False
        assert scheduler._task is None
        assert scheduler._stop_event is None
        assert scheduler._starting_task is None
        assert scheduler._stopping_task is None

    run(scenario())


def test_scheduler_concurrent_stop_and_start_restarts_after_cleanup() -> None:
    """并发 start 等旧 stop 清理完成，然后创建新 task。"""

    async def scenario() -> None:
        class RestartService:
            def __init__(self, policy: HeartbeatPolicy) -> None:
                self.policy = policy
                self.calls = 0
                self.first_scan_entered = asyncio.Event()
                self.first_cleanup_entered = asyncio.Event()
                self.allow_first_cleanup = asyncio.Event()

            async def scan_once(self) -> dict:
                self.calls += 1
                if self.calls == 1:
                    self.first_scan_entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.first_cleanup_entered.set()
                        await self.allow_first_cleanup.wait()
                return {"processed": 0}

        policy = HeartbeatPolicy(scan_interval=0.01, jitter_ratio=0)
        service = RestartService(policy)
        scheduler = HeartbeatScheduler(service, enabled=True, policy=policy)
        assert await scheduler.start() is True
        await asyncio.wait_for(service.first_scan_entered.wait(), timeout=1)
        first_task = scheduler._task

        stop_call = asyncio.create_task(scheduler.stop())
        await asyncio.wait_for(service.first_cleanup_entered.wait(), timeout=1)
        start_call = asyncio.create_task(scheduler.start())
        await asyncio.sleep(0)
        assert start_call.done() is False

        service.allow_first_cleanup.set()
        assert await stop_call is True
        assert await start_call is True
        assert scheduler.running is True
        assert scheduler._task is not first_task
        assert await scheduler.stop() is True

    run(scenario())


def test_scheduler_self_stop_keeps_handles_until_run_exits() -> None:
    """从 scheduler task 内 stop 时，句柄保留到 _run 真正退出。"""

    async def scenario() -> None:
        class SelfStoppingService:
            def __init__(self, policy: HeartbeatPolicy) -> None:
                self.policy = policy
                self.scheduler: HeartbeatScheduler | None = None
                self.self_stop_observed = asyncio.Event()
                self.allow_scan_return = asyncio.Event()

            async def scan_once(self) -> dict:
                assert self.scheduler is not None
                target_task = asyncio.current_task()
                assert self.scheduler._task is target_task
                assert await self.scheduler.stop() is True
                assert self.scheduler._task is target_task
                assert self.scheduler._stopping_task is not None
                self.self_stop_observed.set()
                await self.allow_scan_return.wait()
                return {"processed": 0}

        policy = HeartbeatPolicy(scan_interval=0.01, jitter_ratio=0)
        service = SelfStoppingService(policy)
        scheduler = HeartbeatScheduler(service, enabled=True, policy=policy)
        service.scheduler = scheduler
        assert await scheduler.start() is True
        await asyncio.wait_for(service.self_stop_observed.wait(), timeout=1)
        target_task = scheduler._task
        stopping_task = scheduler._stopping_task
        assert target_task is not None
        assert stopping_task is not None
        assert target_task.done() is False

        service.allow_scan_return.set()
        await asyncio.wait_for(asyncio.shield(stopping_task), timeout=1)
        assert scheduler._task is None
        assert scheduler._stop_event is None
        assert scheduler._stopping_task is None
        assert await scheduler.stop() is False

    run(scenario())


def test_scheduler_scan_is_non_reentrant() -> None:
    async def scenario() -> None:
        from account_sessions.session_health import HeartbeatScheduler

        class SlowService(_FakeSchedulerService):
            async def scan_once(self) -> dict:
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    await asyncio.sleep(0.02)
                    return {"processed": 1}
                finally:
                    self.active -= 1

        service = SlowService(make_policy())
        scheduler = HeartbeatScheduler(service, enabled=True)
        first, second = await asyncio.gather(scheduler.scan_once(), scheduler.scan_once())
        assert {first.get("skipped"), second.get("skipped")} == {None, "NON_REENTRANT"}
        assert service.max_active == 1

    run(scenario())


def test_scheduler_error_log_uses_safe_stable_code_without_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from account_sessions.session_health import HeartbeatScheduler

    class LeakyCodeError(RuntimeError):
        error_code = "TOKEN=secret"

    class FailingService(_FakeSchedulerService):
        async def scan_once(self) -> dict:
            self.calls += 1
            self.first_scan.set()
            raise LeakyCodeError("TOKEN=secret /secret/profile")

    async def scenario() -> None:
        policy = HeartbeatPolicy(scan_interval=0.01, jitter_ratio=0)
        service = FailingService(policy)
        scheduler = HeartbeatScheduler(service, enabled=True)
        await scheduler.start()
        await asyncio.wait_for(service.first_scan.wait(), timeout=1)
        await scheduler.stop()

    with caplog.at_level("WARNING"):
        run(scenario())
    assert "UNKNOWN" in caplog.text
    assert "secret" not in caplog.text


def test_health_summary_is_read_only_filtered_and_redacted(tmp_path: Path) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    policy = HeartbeatPolicy(success_ttl=timedelta(hours=1), scan_interval=10, jitter_ratio=0)
    database = AccountDatabase(database_url(tmp_path))

    async def scenario() -> tuple[dict, str]:
        await database.initialize()
        fresh = await add_account(
            database,
            last_verified_at=now - timedelta(minutes=5),
            next_heartbeat_at=now + timedelta(minutes=5),
            last_heartbeat_at=now - timedelta(minutes=1),
        )
        stale = await add_account(
            database,
            last_verified_at=now - timedelta(hours=2),
            next_heartbeat_at=None,
        )
        login = await add_account(
            database,
            session_status="LOGIN_REQUIRED",
            last_verified_at=None,
            next_heartbeat_at=now - timedelta(minutes=1),
        )
        busy = await add_account(
            database,
            last_verified_at=now - timedelta(minutes=5),
            next_heartbeat_at=now + timedelta(minutes=5),
            last_heartbeat_error_code="PROFILE_IN_USE",
        )
        disabled_heartbeat = await add_account(
            database,
            heartbeat_enabled=False,
            last_verified_at=now - timedelta(minutes=5),
            next_heartbeat_at=now - timedelta(minutes=10),
        )
        service = HeartbeatService(
            database,
            lambda *_args, **_kwargs: None,
            policy=policy,
            enabled=True,
        )
        access = AccessContext(
            actor_id="health-reader",
            source="MCP",
            capabilities=frozenset({"session.read"}),
            allowed_account_ids=frozenset(
                {
                    fresh.account_id,
                    stale.account_id,
                    login.account_id,
                    busy.account_id,
                    disabled_heartbeat.account_id,
                }
            ),
        )
        summary = await service.get_health_summary(access, now)
        return summary, busy.account_id

    summary, _busy_id = run(scenario())
    assert summary["heartbeat_enabled"] is True
    assert summary["total_accounts"] == 5
    assert summary["valid"] == 4
    assert summary["stale"] == 1
    assert summary["due"] == 1
    assert summary["login_required"] == 1
    assert summary["busy"] == 1
    assert summary["last_heartbeat_at"] is not None
    assert summary["next_heartbeat_at"].startswith("2026-08-17T12:05:00")
    serialized = str(summary)
    for forbidden in ("profile_path", "platform-user", "测试账号", "cookie", "token"):
        assert forbidden not in serialized.lower()
    assert set(summary) == {
        "heartbeat_enabled",
        "total_accounts",
        "valid",
        "stale",
        "due",
        "login_required",
        "busy",
        "last_heartbeat_at",
        "next_heartbeat_at",
        "policy",
    }
    run(database.dispose())


def test_health_endpoint_is_read_only_and_disabled_by_default(tmp_path: Path) -> None:
    from flask import Flask

    from account_sessions.web import create_account_session_blueprint

    database_url_value = database_url(tmp_path)
    database = AccountDatabase(database_url_value)

    async def seed() -> str:
        await database.initialize()
        account = await add_account(
            database,
            session_status="VERIFYING",
            heartbeat_claim_owner="dead-heartbeat",
            heartbeat_claimed_at=datetime.now(timezone.utc) - timedelta(minutes=2),
            heartbeat_claim_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        return account.account_id

    account_id = run(seed())
    run(database.dispose())

    def forbid_platform(*_args, **_kwargs):
        raise AssertionError("health API must not open a platform")

    app = Flask("heartbeat-health-test")
    app.register_blueprint(
        create_account_session_blueprint(
            database_url=database_url_value,
            seed_legacy_profiles=False,
            heartbeat_enabled=False,
            platform_factory=forbid_platform,
        )
    )
    client = app.test_client()
    response = client.get("/api/account-sessions/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["heartbeat_enabled"] is False
    assert payload["total_accounts"] == 1
    assert "accounts" not in payload
    assert "profile_path" not in str(payload)
    app.extensions["account_sessions"].close()

    recovered_database = AccountDatabase(database_url_value)

    async def read_recovered() -> PlatformAccount:
        await recovered_database.initialize()
        return await _get_account(recovered_database, account_id)

    # The runtime call above performs recovery even though the scheduler is disabled;
    # no platform factory call is needed for this database-only step.
    recovered = run(read_recovered())
    assert recovered.session_status == "UNVERIFIED"
    assert recovered.heartbeat_claim_owner is None
    run(recovered_database.dispose())


class _ServiceFakePlatform:
    platform_name = "xiaoheihe"

    def __init__(
        self,
        *,
        login_state: bool = True,
        initialize_error: Exception | None = None,
        cleanup_error: Exception | None = None,
    ) -> None:
        self.login_state = login_state
        self.initialize_error = initialize_error
        self.cleanup_error = cleanup_error
        self.cleaned = False
        self.login_calls = 0

    async def initialize(self) -> None:
        if self.initialize_error is not None:
            raise self.initialize_error

    async def check_login(self) -> bool:
        return self.login_state

    async def login(self) -> None:
        self.login_calls += 1

    async def fetch_identity_payload(self) -> dict:
        return {"ok": True, "user_id": "fake-id", "display_name": "Fake"}

    async def cleanup(self) -> None:
        self.cleaned = True
        if self.cleanup_error is not None:
            raise self.cleanup_error


def test_account_service_check_login_false_is_login_required_without_interactive_login(
    tmp_path: Path,
) -> None:
    from account_sessions.account_service import AccountSessionService

    async def scenario() -> tuple[
        AccountIdentityError,
        PlatformAccount,
        _ServiceFakePlatform,
        AccountDatabase,
    ]:
        database = AccountDatabase(database_url(tmp_path))
        await database.initialize()
        profile = tmp_path / "profiles" / "xiaoheihe" / "login-required"
        profile.mkdir(parents=True)
        account = await add_account(database, profile_path=profile)
        platform = _ServiceFakePlatform(login_state=False)
        service = AccountSessionService(
            database,
            seed_legacy_profiles=False,
            platform_factory=lambda _account: platform,
            allowed_profile_roots=(tmp_path / "profiles",),
        )
        try:
            with pytest.raises(AccountIdentityError) as caught:
                await service.verify_account(
                    account.account_id,
                    HEARTBEAT_ACCESS_CONTEXT,
                    allow_interactive_login=False,
                )
            stored = await service.get_account(account.account_id)
            return caught.value, stored, platform, database
        except BaseException:
            await database.dispose()
            raise

    error, stored, platform, database = run(scenario())
    try:
        assert error.error_code == "LOGIN_REQUIRED"
        assert stored.session_status == "LOGIN_REQUIRED"
        assert stored.last_verified_at is None
        assert platform.login_calls == 0
        assert platform.cleaned is True
    finally:
        run(database.dispose())


def test_account_service_busy_restores_valid_and_cleanup_failure_does_not_cover_success(
    tmp_path: Path,
) -> None:
    from account_sessions.account_service import AccountSessionService

    async def run_busy() -> tuple[PlatformAccount, AccountDatabase]:
        (tmp_path / "busy").mkdir()
        database = AccountDatabase(database_url(tmp_path / "busy"))
        await database.initialize()
        profile = tmp_path / "busy" / "profiles" / "xiaoheihe" / "busy"
        profile.mkdir(parents=True)
        verified = datetime(2026, 8, 17, 11, tzinfo=timezone.utc)
        account = await add_account(
            database,
            profile_path=profile,
            last_verified_at=verified,
            session_status="VERIFYING",
        )
        platform = _ServiceFakePlatform(
            initialize_error=AccountBusyError("PROFILE_IN_USE: /secret/profile")
        )
        service = AccountSessionService(
            database,
            seed_legacy_profiles=False,
            platform_factory=lambda _account: platform,
            allowed_profile_roots=(tmp_path / "busy" / "profiles",),
        )
        with pytest.raises(AccountBusyError):
            await service.verify_account(account.account_id, HEARTBEAT_ACCESS_CONTEXT)
        stored = await service.get_account(account.account_id)
        assert stored.session_status == "VALID"
        assert as_utc(stored.last_verified_at) == verified
        return stored, database

    stored, busy_database = run(run_busy())
    try:
        assert stored.session_status == "VALID"
    finally:
        run(busy_database.dispose())

    async def run_cleanup_failure() -> tuple[PlatformAccount, AccountDatabase]:
        (tmp_path / "cleanup").mkdir()
        database = AccountDatabase(database_url(tmp_path / "cleanup"))
        await database.initialize()
        profile = tmp_path / "cleanup" / "profiles" / "xiaoheihe" / "success"
        profile.mkdir(parents=True)
        account = await add_account(
            database,
            profile_path=profile,
            platform_user_id="fake-id",
            session_status="UNVERIFIED",
        )
        platform = _ServiceFakePlatform(cleanup_error=RuntimeError("cleanup-only"))
        service = AccountSessionService(
            database,
            seed_legacy_profiles=False,
            platform_factory=lambda _account: platform,
            allowed_profile_roots=(tmp_path / "cleanup" / "profiles",),
        )
        result = await service.verify_account(account.account_id, HEARTBEAT_ACCESS_CONTEXT)
        assert result["session_status"] == "VALID"
        stored = await service.get_account(account.account_id)
        assert stored.session_status == "VALID"
        return stored, database

    cleanup_stored, cleanup_database = run(run_cleanup_failure())
    try:
        assert cleanup_stored.session_status == "VALID"
    finally:
        run(cleanup_database.dispose())


def test_account_service_cancelled_error_is_not_recorded_as_verification_failure(
    tmp_path: Path,
) -> None:
    from account_sessions.account_service import AccountSessionService

    async def scenario() -> AccountDatabase:
        database = AccountDatabase(database_url(tmp_path))
        await database.initialize()
        profile = tmp_path / "profiles" / "xiaoheihe" / "cancelled"
        profile.mkdir(parents=True)
        account = await add_account(database, profile_path=profile, session_status="VALID")
        platform = _ServiceFakePlatform(initialize_error=asyncio.CancelledError())
        service = AccountSessionService(
            database,
            seed_legacy_profiles=False,
            platform_factory=lambda _account: platform,
            allowed_profile_roots=(tmp_path / "profiles",),
        )
        with pytest.raises(asyncio.CancelledError):
            await service.verify_account(account.account_id, HEARTBEAT_ACCESS_CONTEXT)
        stored = await service.get_account(account.account_id)
        assert stored.session_status == "VALID"
        return database

    database = run(scenario())
    run(database.dispose())


def test_runtime_state_start_is_idempotent_and_initializes_once() -> None:
    """应用启动钩子 start() 幂等；多次调用只初始化一次。"""

    from threading import Lock

    from account_sessions.web import AccountSessionRuntimeState

    events: list[str] = []

    class FakeRuntime:
        def run(self, coroutine, *, timeout=30):
            del timeout
            return asyncio.run(coroutine)

    class FakeAccounts:
        async def initialize(self):
            events.append("accounts.initialize")

    class FakeDelivery:
        async def reconcile_interrupted_operations(self):
            events.append("delivery.interrupted")

        async def reconcile_pending_article_mappings(self):
            events.append("delivery.article_mapping")

    class FakeHeartbeatScheduler:
        async def start(self):
            events.append("heartbeat.start")

    state = AccountSessionRuntimeState.__new__(AccountSessionRuntimeState)
    state._runtime = FakeRuntime()
    state._initialized = False
    state._lock = Lock()
    state.accounts = FakeAccounts()
    state.delivery = FakeDelivery()
    state.heartbeat_scheduler = FakeHeartbeatScheduler()

    state.start()
    state.start()
    state.start()

    assert events == [
        "accounts.initialize",
        "delivery.interrupted",
        "delivery.article_mapping",
        "heartbeat.start",
    ]


def test_runtime_state_start_is_thread_safe() -> None:
    """并发调用 start() 不会重复初始化（锁保护）。"""

    from account_sessions.web import AccountSessionRuntimeState

    events: list[str] = []
    errors: list[BaseException] = []
    error_lock = threading.Lock()
    all_lock_attempts = threading.Event()

    class TrackingLock:
        def __init__(self, expected_attempts: int) -> None:
            self._lock = threading.Lock()
            self._counter_lock = threading.Lock()
            self.expected_attempts = expected_attempts
            self.attempts = 0

        def __enter__(self):
            with self._counter_lock:
                self.attempts += 1
                if self.attempts == self.expected_attempts:
                    all_lock_attempts.set()
            self._lock.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            del exc_type, exc_value, traceback
            self._lock.release()

    class FakeRuntime:
        def run(self, coroutine, *, timeout=30):
            del timeout
            return asyncio.run(coroutine)

    class FakeAccounts:
        async def initialize(self):
            events.append("accounts.initialize")
            all_attempted = await asyncio.to_thread(all_lock_attempts.wait, 2)
            assert all_attempted is True

    class FakeDelivery:
        async def reconcile_interrupted_operations(self):
            events.append("delivery.interrupted")

        async def reconcile_pending_article_mappings(self):
            events.append("delivery.article_mapping")

    class FakeHeartbeatScheduler:
        async def start(self):
            events.append("heartbeat.start")

    state = AccountSessionRuntimeState.__new__(AccountSessionRuntimeState)
    state._runtime = FakeRuntime()
    state._initialized = False
    state._lock = TrackingLock(expected_attempts=4)
    state.accounts = FakeAccounts()
    state.delivery = FakeDelivery()
    state.heartbeat_scheduler = FakeHeartbeatScheduler()

    def call_start() -> None:
        try:
            state.start()
        except BaseException as exc:
            with error_lock:
                errors.append(exc)

    threads = [threading.Thread(target=call_start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert state._lock.attempts == 4
    assert events == [
        "accounts.initialize",
        "delivery.interrupted",
        "delivery.article_mapping",
        "heartbeat.start",
    ]


def test_runtime_state_close_blocks_start_until_old_runtime_is_disposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close 持有同步锁穿过 stop/dispose，不允许并发 start 抢跑。"""

    import account_sessions.web as web_module
    from account_sessions.web import AccountSessionRuntimeState

    events: list[str] = []
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    dispose_entered = threading.Event()
    allow_dispose = threading.Event()
    start_attempted = threading.Event()
    start_finished = threading.Event()
    new_runtime_created = threading.Event()

    class FakeDatabase:
        async def dispose(self) -> None:
            events.append("database.dispose")

    class FakeAccounts:
        async def initialize(self) -> None:
            events.append("accounts.initialize")

    class FakeDelivery:
        async def reconcile_interrupted_operations(self) -> None:
            events.append("delivery.interrupted")

        async def reconcile_pending_article_mappings(self) -> None:
            events.append("delivery.article_mapping")

    class FakeScheduler:
        async def start(self) -> None:
            events.append("heartbeat.start")

        async def stop(self) -> None:
            events.append("heartbeat.stop")

    class OldRuntime:
        def run(self, coroutine, *, timeout=30):
            del timeout
            return asyncio.run(coroutine)

        def close(self, dispose) -> None:
            events.append("old.close.enter")
            dispose_entered.set()
            if not allow_dispose.wait(timeout=2):
                dispose.close()
                raise AssertionError("test did not release dispose")
            asyncio.run(dispose)
            events.append("old.close.done")

    class NewRuntime:
        def __init__(self) -> None:
            events.append("new.runtime.created")
            new_runtime_created.set()

        def run(self, coroutine, *, timeout=30):
            del timeout
            return asyncio.run(coroutine)

    monkeypatch.setattr(web_module, "AccountRuntime", NewRuntime)
    state = AccountSessionRuntimeState.__new__(AccountSessionRuntimeState)
    state.database = FakeDatabase()
    state.accounts = FakeAccounts()
    state.delivery = FakeDelivery()
    state.heartbeat_scheduler = FakeScheduler()
    state._runtime = OldRuntime()
    state._owns_runtime = True
    state._initialized = True
    state._lock = threading.Lock()

    def capture(callable_) -> None:
        try:
            callable_()
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)

    close_thread = threading.Thread(target=lambda: capture(state.close))

    def start_state() -> None:
        start_attempted.set()
        state.start()
        start_finished.set()

    start_thread = threading.Thread(target=lambda: capture(start_state))
    close_thread.start()
    assert dispose_entered.wait(timeout=1)
    start_thread.start()
    assert start_attempted.wait(timeout=1)
    try:
        assert new_runtime_created.wait(timeout=0.1) is False
        assert start_finished.is_set() is False
    finally:
        allow_dispose.set()

    close_thread.join(timeout=3)
    start_thread.join(timeout=3)
    assert close_thread.is_alive() is False
    assert start_thread.is_alive() is False
    assert errors == []
    assert start_finished.is_set() is True
    assert events.index("old.close.done") < events.index("new.runtime.created")
