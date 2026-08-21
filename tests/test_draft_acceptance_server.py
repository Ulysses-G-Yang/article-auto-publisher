from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

import scripts.run_draft_acceptance_server as acceptance
from account_sessions.platform_catalog import PLATFORM_CATALOG
from content_studio.content_document import FEATURE_KEYS
from content_studio.platform_format_capabilities import (
    DEFAULT_PLATFORM_FORMAT_CAPABILITIES,
)


def _fake_app(factory=None) -> Flask:
    app = Flask("acceptance-test")
    app.extensions["content_studio"] = SimpleNamespace(
        service=SimpleNamespace(platform_format_capabilities=None)
    )
    if factory is not None:
        account_state = SimpleNamespace(
            accounts=SimpleNamespace(platform_factory=factory),
            delivery=SimpleNamespace(platform_factory=factory),
        )
        app.extensions["account_sessions"] = account_state
    return app


def _fake_create_app() -> Flask:
    return _fake_app(lambda _account: "production-platform")


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
        name: (
            declaration.supported
            if (declaration := DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get(name))
            else None
        )
        for name in acceptance._delivery_platforms()
    }

    monkeypatch.setattr(acceptance, "_load_create_app", _fake_create_app, raising=False)
    app = acceptance.build_acceptance_app(("xiaoheihe", "zol"), "DRAFT_ONLY")
    registry = app.extensions["content_studio"].service.platform_format_capabilities

    assert registry.get("xiaoheihe").supported == FEATURE_KEYS
    assert registry.get("zol").supported == FEATURE_KEYS
    assert registry.get("zol").heading_levels == frozenset({2})
    assert registry.get("xiaoheihe").heading_levels == frozenset({2, 3})
    assert registry.get("zhihu").supported == frozenset()
    assert {
        name: (
            declaration.supported
            if (declaration := DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get(name))
            else None
        )
        for name in acceptance._delivery_platforms()
    } == before
    assert registry is not DEFAULT_PLATFORM_FORMAT_CAPABILITIES


def test_zhihu_acceptance_declares_only_observed_h2() -> None:
    app = acceptance.build_acceptance_app(("zhihu",), "DRAFT_ONLY")
    registry = app.extensions["content_studio"].service.platform_format_capabilities

    assert registry.get("zhihu").supported == FEATURE_KEYS
    assert registry.get("zhihu").heading_levels == frozenset({2})
    assert registry.get("xiaoheihe").supported == frozenset()


def test_smzdm_acceptance_declares_only_observed_h2_and_image_order() -> None:
    app = acceptance.build_acceptance_app(("smzdm",), "DRAFT_ONLY")
    registry = app.extensions["content_studio"].service.platform_format_capabilities

    assert registry.get("smzdm").supported == frozenset(
        {"heading", "image_order"}
    )
    assert registry.get("smzdm").heading_levels == frozenset({2})
    assert registry.get("xiaoheihe").supported == frozenset()


def test_xiaohongshu_acceptance_temporarily_declares_observed_h2() -> None:
    app = acceptance.build_acceptance_app(("xiaohongshu",), "DRAFT_ONLY")
    registry = app.extensions["content_studio"].service.platform_format_capabilities

    assert registry.get("xiaohongshu").supported == FEATURE_KEYS
    assert registry.get("xiaohongshu").heading_levels == frozenset({2})
    assert DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get(
        "xiaohongshu"
    ).heading_levels == frozenset({2})


def test_weibo_acceptance_is_process_local_and_production_stays_disabled() -> None:
    app = acceptance.build_acceptance_app(("weibo",), "DRAFT_ONLY")
    registry = app.extensions["content_studio"].service.platform_format_capabilities

    declaration = registry.get("weibo")
    assert declaration.supported == frozenset({"heading", "image_order"})
    assert declaration.heading_levels == frozenset({2})
    assert registry.get("xiaoheihe").supported == frozenset()
    assert DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get("weibo") is None

    production_weibo = next(item for item in PLATFORM_CATALOG if item.id == "weibo")
    assert production_weibo.account_enabled is True
    assert production_weibo.delivery_enabled is False


def test_xiaohongshu_resume_title_is_isolated_to_acceptance_factory() -> None:
    app = acceptance.build_acceptance_app(
        ("xiaohongshu",),
        "DRAFT_ONLY",
        xhs_resume_title=" 唯一恢复标题 ",
    )
    account = SimpleNamespace(
        platform="xiaohongshu",
        profile_path="D:/isolated/xhs-profile",
    )
    platform = app.extensions["account_sessions"].accounts.platform_factory(account)

    assert platform._resume_existing_title == "唯一恢复标题"
    assert platform.strict_profile_lock is True
    with pytest.raises(ValueError, match="单平台"):
        acceptance.build_acceptance_app(
            ("xiaohongshu", "zol"),
            "DRAFT_ONLY",
            xhs_resume_title="唯一恢复标题",
        )


def test_weibo_resume_identity_is_isolated_to_acceptance_factory() -> None:
    app = acceptance.build_acceptance_app(
        ("weibo",),
        "DRAFT_ONLY",
        weibo_resume_title=" 唯一恢复标题 ",
        weibo_resume_draft_id="4183864",
    )
    account = SimpleNamespace(
        platform="weibo",
        profile_path="D:/isolated/weibo-profile",
    )
    platform = app.extensions["account_sessions"].accounts.platform_factory(account)

    assert platform._resume_existing_title == "唯一恢复标题"
    assert platform._resume_existing_draft_id == "4183864"
    assert platform.strict_profile_lock is True

    with pytest.raises(ValueError, match="单平台"):
        acceptance.build_acceptance_app(
            ("weibo", "zol"),
            "DRAFT_ONLY",
            weibo_resume_title="唯一恢复标题",
            weibo_resume_draft_id="4183864",
        )
    with pytest.raises(ValueError, match="同时指定"):
        acceptance.build_acceptance_app(
            ("weibo",),
            "DRAFT_ONLY",
            weibo_resume_title="唯一恢复标题",
        )
    with pytest.raises(ValueError, match="正整数"):
        acceptance.build_acceptance_app(
            ("weibo",),
            "DRAFT_ONLY",
            weibo_resume_title="唯一恢复标题",
            weibo_resume_draft_id="not-an-id",
        )


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


def test_zol_acceptance_factory_isolated_and_production_factory_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls = []

    def original_factory(account):
        calls.append(account.platform)
        return "production-platform"

    monkeypatch.setattr(
        acceptance,
        "_load_create_app",
        lambda: _fake_app(original_factory),
        raising=False,
    )
    app = acceptance.build_acceptance_app(("zol",), "DRAFT_ONLY")
    state = app.extensions["account_sessions"]
    zol_account = SimpleNamespace(platform="zol", profile_path=str(tmp_path / "zol"))
    other_account = SimpleNamespace(platform="xiaoheihe", profile_path=str(tmp_path / "xhh"))

    zol_platform = state.accounts.platform_factory(zol_account)
    assert zol_platform.enable_heading_experiment is True
    assert zol_platform.strict_profile_lock is True
    assert zol_platform.profile_dir == (tmp_path / "zol").resolve()
    assert state.delivery.platform_factory(zol_account).enable_heading_experiment is True
    assert state.accounts.platform_factory(other_account) == "production-platform"
    assert calls == ["xiaoheihe"]


def test_non_zol_acceptance_keeps_original_factory(monkeypatch: pytest.MonkeyPatch):
    sentinel = object()

    def original_factory(_account):
        return sentinel

    app = _fake_app(original_factory)
    monkeypatch.setattr(acceptance, "_load_create_app", lambda: app, raising=False)

    acceptance.build_acceptance_app(("xiaoheihe",), "DRAFT_ONLY")

    state = app.extensions["account_sessions"]
    assert state.accounts.platform_factory is original_factory
    assert state.delivery.platform_factory is original_factory


def test_production_platform_factory_never_enables_zol_heading_experiment(
    tmp_path: Path,
):
    from account_sessions.account_service import _platform_instance

    account = SimpleNamespace(platform="zol", profile_path=str(tmp_path / "zol"))
    platform = _platform_instance(account)

    assert platform.enable_heading_experiment is False


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
