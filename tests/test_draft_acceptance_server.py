from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

import scripts.run_draft_acceptance_server as acceptance
from content_studio.content_document import FEATURE_KEYS
from content_studio.platform_format_capabilities import (
    DEFAULT_PLATFORM_FORMAT_CAPABILITIES,
)


def _fake_app() -> Flask:
    app = Flask("acceptance-test")
    app.extensions["content_studio"] = SimpleNamespace(
        service=SimpleNamespace(platform_format_capabilities=None)
    )
    return app


def _fake_create_app() -> Flask:
    return _fake_app()


@pytest.fixture(autouse=True)
def safe_environment(monkeypatch: pytest.MonkeyPatch):
    for name in acceptance._SAFETY_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        acceptance,
        "_load_config",
        lambda: {"app": {"publish_after_draft": False, "legacy_upload_queue_enabled": False}},
    )
    monkeypatch.setattr(acceptance, "_load_create_app", _fake_create_app, raising=False)


def test_build_injects_only_selected_platform_capabilities(monkeypatch: pytest.MonkeyPatch):
    before = {
        name: DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get(name).supported
        for name in acceptance._delivery_platforms()
    }

    monkeypatch.setattr(acceptance, "_load_create_app", _fake_create_app, raising=False)
    app = acceptance.build_acceptance_app(("xiaoheihe", "zol"), "DRAFT_ONLY")
    registry = app.extensions["content_studio"].service.platform_format_capabilities

    assert registry.get("xiaoheihe").supported == FEATURE_KEYS
    assert registry.get("zol").supported == FEATURE_KEYS
    assert registry.get("zhihu").supported == frozenset()
    assert {
        name: DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get(name).supported
        for name in acceptance._delivery_platforms()
    } == before
    assert registry is not DEFAULT_PLATFORM_FORMAT_CAPABILITIES


@pytest.mark.parametrize(
    "platforms",
    [(), ("",), ("not-a-platform",), ("xiaoheihe", "xiaoheihe")],
)
def test_platform_selection_is_nonempty_known_and_unique(platforms):
    with pytest.raises(ValueError):
        acceptance.build_acceptance_app(platforms, "DRAFT_ONLY")


def test_confirmation_word_is_required():
    with pytest.raises(ValueError):
        acceptance.build_acceptance_app(("xiaoheihe",), "PUBLISH")


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on", " TRUE "])
@pytest.mark.parametrize("name", acceptance._SAFETY_SWITCHES)
def test_each_dangerous_switch_rejects_enabled_value(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
):
    monkeypatch.setenv(name, raw)
    with pytest.raises(RuntimeError, match=name):
        acceptance.build_acceptance_app(("xiaoheihe",), "DRAFT_ONLY")


@pytest.mark.parametrize("raw", ["", "maybe", "enabled", "2"])
def test_ambiguous_safety_switch_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
):
    monkeypatch.setenv("PUBLISH_AFTER_DRAFT", raw)
    with pytest.raises(RuntimeError, match="PUBLISH_AFTER_DRAFT"):
        acceptance.build_acceptance_app(("xiaoheihe",), "DRAFT_ONLY")


def test_configured_publish_or_legacy_queue_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        acceptance,
        "_load_config",
        lambda: {"app": {"publish_after_draft": True, "legacy_upload_queue_enabled": False}},
    )
    with pytest.raises(RuntimeError, match="publish_after_draft"):
        acceptance.build_acceptance_app(("xiaoheihe",), "DRAFT_ONLY")

    monkeypatch.setattr(
        acceptance,
        "_load_config",
        lambda: {"app": {"publish_after_draft": False, "legacy_upload_queue_enabled": True}},
    )
    with pytest.raises(RuntimeError, match="legacy_upload_queue_enabled"):
        acceptance.build_acceptance_app(("xiaoheihe",), "DRAFT_ONLY")


def test_rejected_confirmation_and_switch_never_create_app(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def forbidden_create_app():
        calls.append("create_app")
        return _fake_app()

    monkeypatch.setattr(acceptance, "_load_create_app", forbidden_create_app, raising=False)
    with pytest.raises(ValueError):
        acceptance.build_acceptance_app(("xiaoheihe",), "PUBLISH")
    assert calls == []

    monkeypatch.setenv("PUBLISH_AFTER_DRAFT", "true")
    with pytest.raises(RuntimeError):
        acceptance.build_acceptance_app(("xiaoheihe",), "DRAFT_ONLY")
    assert calls == []


def test_help_runs_from_non_repository_cwd_without_importing_app(tmp_path: Path):
    script = Path(__file__).parents[1] / "scripts" / "run_draft_acceptance_server.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "DRAFT_ONLY" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr


def test_run_server_forces_local_host_and_disables_debug_reload():
    calls = {}

    class FakeApp:
        config = {"DRAFT_ACCEPTANCE_PLATFORMS": ("xiaoheihe",)}

        def run(self, **kwargs):
            calls.update(kwargs)

    acceptance.run_acceptance_server(FakeApp(), 54321)

    assert calls == {
        "host": "127.0.0.1",
        "port": 54321,
        "debug": False,
        "use_reloader": False,
    }
