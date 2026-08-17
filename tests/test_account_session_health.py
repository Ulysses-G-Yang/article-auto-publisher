from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

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
    HeartbeatService,
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
    status: str = "ACTIVE",
    session_status: str = "VALID",
    heartbeat_enabled: bool = True,
    last_verified_at: datetime | None = None,
    next_heartbeat_at: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
    heartbeat_failures: int = 0,
    last_heartbeat_error_code: str | None = None,
    profile_path: str | Path | None = None,
) -> PlatformAccount:
    account_id = str(uuid.uuid4())
    account = PlatformAccount(
        account_id=account_id,
        platform="xiaoheihe",
        platform_user_id=f"platform-user-{account_id}",
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
    )
    async with database.session() as session:
        session.add(account)
        await session.flush()
    return account


def test_policy_validates_ranges_and_uses_default_singleton_batch() -> None:
    assert HeartbeatPolicy().max_per_scan == 1
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

    async def seed() -> None:
        await database.initialize()
        await add_account(database, last_verified_at=None)

    run(seed())
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
        account = await add_account(database, profile_path=profile, session_status="UNVERIFIED")
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
