from __future__ import annotations

from typing import Any

import pytest

import run_flask_production


def _configure_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        run_flask_production,
        "get_config",
        lambda: {
            "app": {
                "environment": "production",
                "host": "127.0.0.1",
                "port": 5001,
                "publish_after_draft": False,
            }
        },
    )
    monkeypatch.setattr(run_flask_production.signal, "signal", lambda *_args: None)


def test_production_starts_account_domain_before_worker_and_waitress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeAccountState:
        def start(self) -> None:
            events.append("account.start")

    class FakeApplication:
        extensions = {"account_sessions": FakeAccountState()}

    application = FakeApplication()

    def create_app() -> FakeApplication:
        events.append("create_app")
        return application

    def start_queue_worker() -> None:
        events.append("worker.start")

    def serve(served_application: Any, **kwargs: Any) -> None:
        assert served_application is application
        assert kwargs == {
            "host": "127.0.0.1",
            "port": 5001,
            "threads": 4,
            "ident": "article-publisher-flask",
        }
        events.append("serve")

    _configure_production(monkeypatch)
    monkeypatch.setattr(run_flask_production, "create_app", create_app)
    monkeypatch.setattr(run_flask_production, "start_queue_worker", start_queue_worker)
    monkeypatch.setattr(run_flask_production, "serve", serve)

    run_flask_production.main()

    assert events == ["create_app", "account.start", "worker.start", "serve"]


def test_production_start_failure_blocks_worker_and_waitress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class StartupFailure(RuntimeError):
        pass

    class FailingAccountState:
        def start(self) -> None:
            events.append("account.start")
            raise StartupFailure("database recovery failed")

    class FakeApplication:
        extensions = {"account_sessions": FailingAccountState()}

    def forbidden() -> None:
        events.append("forbidden")

    _configure_production(monkeypatch)
    monkeypatch.setattr(
        run_flask_production,
        "create_app",
        lambda: FakeApplication(),
    )
    monkeypatch.setattr(run_flask_production, "start_queue_worker", forbidden)
    monkeypatch.setattr(run_flask_production, "serve", forbidden)

    with pytest.raises(StartupFailure, match="database recovery failed"):
        run_flask_production.main()

    assert events == ["account.start"]


def test_production_worker_failure_blocks_waitress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class WorkerFailure(RuntimeError):
        pass

    class FakeAccountState:
        def start(self) -> None:
            events.append("account.start")

    class FakeApplication:
        extensions = {"account_sessions": FakeAccountState()}

    def failing_worker() -> None:
        events.append("worker.start")
        raise WorkerFailure("queue worker failed")

    _configure_production(monkeypatch)
    monkeypatch.setattr(
        run_flask_production,
        "create_app",
        lambda: FakeApplication(),
    )
    monkeypatch.setattr(run_flask_production, "start_queue_worker", failing_worker)
    monkeypatch.setattr(
        run_flask_production,
        "serve",
        lambda *_args, **_kwargs: pytest.fail("Waitress must not start"),
    )

    with pytest.raises(WorkerFailure, match="queue worker failed"):
        run_flask_production.main()

    assert events == ["account.start", "worker.start"]


def test_production_requires_account_sessions_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApplication:
        extensions: dict[str, object] = {}

    _configure_production(monkeypatch)
    monkeypatch.setattr(
        run_flask_production,
        "create_app",
        lambda: FakeApplication(),
    )
    monkeypatch.setattr(
        run_flask_production,
        "start_queue_worker",
        lambda: pytest.fail("worker must not start"),
    )
    monkeypatch.setattr(
        run_flask_production,
        "serve",
        lambda *_args, **_kwargs: pytest.fail("Waitress must not start"),
    )

    with pytest.raises(
        RuntimeError,
        match="生产 Flask 应用缺少 account_sessions 扩展",
    ):
        run_flask_production.main()
