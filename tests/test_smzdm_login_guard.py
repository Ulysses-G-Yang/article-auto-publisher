from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.errors import LoginInProgressError  # noqa: E402
from account_sessions.models import PlatformAccount  # noqa: E402
from account_sessions.web import (  # noqa: E402
    AccountSessionRuntimeState,
    SmzdmLoginGuard,
    create_account_session_blueprint,
)


def run(coroutine):
    return asyncio.run(coroutine)


def build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setenv("ACCOUNT_SESSION_DATA_DIR", str(tmp_path / "runtime"))
    app = Flask("smzdm-login-guard-test")
    app.secret_key = "test"
    app.register_blueprint(
        create_account_session_blueprint(
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}",
            seed_legacy_profiles=False,
            auto_execute=False,
            public_publish_enabled=False,
            allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
        )
    )
    return app


def test_guard_scopes_active_and_result_state_to_the_account() -> None:
    guard = SmzdmLoginGuard()
    token = guard.reserve("smzdm", "account-a")

    assert guard.public_state("account-a") == {
        "login_in_progress": True,
        "platform_login_in_progress": True,
        "login_error_code": None,
    }
    assert guard.public_state("account-b") == {
        "login_in_progress": False,
        "platform_login_in_progress": True,
        "login_error_code": None,
    }

    guard.release(token, "RATE_LIMITED")
    assert guard.public_state("account-a") == {
        "login_in_progress": False,
        "platform_login_in_progress": False,
        "login_error_code": "RATE_LIMITED",
    }
    assert guard.public_state("account-b")["login_error_code"] is None


def test_smzdm_candidate_duplicate_is_rejected_before_profile_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(tmp_path, monkeypatch)
    state = app.extensions["account_sessions"]

    async def hold_login(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(state.accounts, "verify_account", hold_login)
    client = app.test_client()

    first = client.post("/api/platforms/smzdm/accounts/login")
    assert first.status_code == 202
    account_id = first.get_json()["account_id"]

    second = client.post("/api/platforms/smzdm/accounts/login")
    assert second.status_code == 409
    assert second.get_json()["error"] == "LOGIN_IN_PROGRESS"
    profiles = list((tmp_path / "runtime" / "profiles" / "smzdm").iterdir())
    assert [path.name for path in profiles] == [account_id]

    state.close()


def test_smzdm_duplicate_existing_login_does_not_rewrite_active_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(tmp_path, monkeypatch)
    state = app.extensions["account_sessions"]
    account_id = "smzdm-existing"
    profile = tmp_path / "runtime" / "profiles" / "smzdm" / account_id
    profile.mkdir(parents=True)

    async def insert_account() -> None:
        await state.accounts.initialize()
        async with state.database.session() as session:
            session.add(
                PlatformAccount(
                    account_id=account_id,
                    platform="smzdm",
                    display_name="测试账号",
                    profile_path=str(profile.resolve()),
                    session_status="LOGIN_REQUIRED",
                    persist_login=True,
                )
            )

    run(insert_account())

    async def hold_login(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(state.accounts, "verify_account", hold_login)
    client = app.test_client()

    first = client.post(f"/api/account-sessions/{account_id}/login")
    assert first.status_code == 202
    second = client.post(f"/api/account-sessions/{account_id}/login")
    assert second.status_code == 409
    assert second.get_json()["error"] == "LOGIN_IN_PROGRESS"

    async def read_status() -> str:
        async with state.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            return account.session_status

    assert run(read_status()) == "VERIFYING"
    state.close()


def test_submit_failure_closes_unscheduled_work_releases_guard_and_restores_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACCOUNT_SESSION_DATA_DIR", str(tmp_path / "runtime"))
    state = AccountSessionRuntimeState(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}",
        seed_legacy_profiles=False,
        allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
    )
    account_id = "smzdm-submit-failure"
    profile = tmp_path / "runtime" / "profiles" / "smzdm" / account_id
    profile.mkdir(parents=True)

    async def insert_account() -> None:
        await state.accounts.initialize()
        async with state.database.session() as session:
            session.add(
                PlatformAccount(
                    account_id=account_id,
                    platform="smzdm",
                    display_name="测试账号",
                    profile_path=str(profile.resolve()),
                    session_status="VERIFYING",
                    persist_login=True,
                )
            )

    run(insert_account())
    token = state.smzdm_login_guard.reserve("smzdm", account_id)

    async def work() -> None:
        await asyncio.sleep(0)

    def fail_submit(_coroutine) -> None:
        raise RuntimeError("runtime submit failed")

    monkeypatch.setattr(state, "submit", fail_submit)
    with pytest.raises(RuntimeError, match="runtime submit failed"):
        state.submit_login(token, work(), account_id=account_id)

    assert state.smzdm_login_guard.public_state(account_id) == {
        "login_in_progress": False,
        "platform_login_in_progress": False,
        "login_error_code": "ERROR",
    }

    async def read_status() -> str:
        async with state.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            return account.session_status

    assert run(read_status()) == "ERROR"
    state.close()


def test_cancel_waits_for_delayed_cleanup_before_releasing_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AccountSessionRuntimeState(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}",
        seed_legacy_profiles=False,
    )
    token = state.smzdm_login_guard.reserve("smzdm", "smzdm-cancel")
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    tasks: list[asyncio.Task] = []

    async def delayed_restore(*_args, **_kwargs) -> None:
        cleanup_started.set()
        await cleanup_release.wait()

    async def work() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(state.accounts, "restore_verifying_after_login_failure", delayed_restore)

    def submit(coroutine) -> None:
        tasks.append(asyncio.create_task(coroutine))

    monkeypatch.setattr(state, "submit", submit)

    async def scenario() -> None:
        state.submit_login(token, work(), account_id="smzdm-cancel")
        await asyncio.sleep(0)
        tasks[0].cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        with pytest.raises(LoginInProgressError):
            state.smzdm_login_guard.reserve("smzdm", "smzdm-other")
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        released = state.smzdm_login_guard.reserve("smzdm", "smzdm-other")
        state.smzdm_login_guard.release(released)

    run(scenario())


def test_other_platform_login_route_does_not_use_smzdm_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(tmp_path, monkeypatch)
    state = app.extensions["account_sessions"]

    async def hold_login(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(state.accounts, "verify_account", hold_login)
    client = app.test_client()

    first = client.post("/api/platforms/zhihu/accounts/login")
    second = client.post("/api/platforms/zhihu/accounts/login")
    assert first.status_code == 202
    assert second.status_code == 202
    assert state.smzdm_login_guard.public_state("unrelated")["platform_login_in_progress"] is False
    state.close()
