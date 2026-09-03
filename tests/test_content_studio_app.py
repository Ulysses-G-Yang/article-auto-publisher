"""完整 Flask 组合层的 Content Studio 冒烟测试。"""

import importlib
import re
import sys
from pathlib import Path


def test_create_app_mounts_studio_without_touching_legacy_queue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("ACCOUNT_SESSION_DATA_DIR", str(tmp_path / "accounts"))
    monkeypatch.setenv("CONTENT_STUDIO_DATA_DIR", str(tmp_path / "content"))
    monkeypatch.setenv("LEGACY_UPLOAD_QUEUE_ENABLED", "false")

    import config
    import models.database

    config._config = None
    models.database.Database._instance = None
    sys.modules.pop("app", None)
    app_module = importlib.import_module("app")
    flask_app = app_module.create_app()
    client = flask_app.test_client()

    # 新工作台默认不能隐式启动旧 worker；否则启动迁移会改写历史任务与日志。
    assert app_module.start_queue_worker() is False

    assert client.get("/upload").status_code == 200
    settings_page = client.get("/settings/ai")
    assert settings_page.status_code == 200
    assert settings_page.headers["Cache-Control"] == "no-store"
    assert 'id="ai-settings"' in settings_page.get_data(as_text=True)
    first_token = re.search(
        r'data-csrf-token="([^"]+)"', settings_page.get_data(as_text=True)
    ).group(1)
    second_settings_page = client.get("/settings/ai")
    second_token = re.search(
        r'data-csrf-token="([^"]+)"', second_settings_page.get_data(as_text=True)
    ).group(1)
    assert second_token == first_token
    public_settings = client.get("/api/settings/publication-ai")
    assert public_settings.status_code == 200
    assert public_settings.headers["Cache-Control"] == "no-store"
    assert set(public_settings.get_json()) == {
        "enabled",
        "base_url",
        "model",
        "api_key_configured",
        "api_key_source",
        "persistence",
    }
    redirect = client.get("/delivery/new?draft_id=abc", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["Location"] == "/upload?draft_id=abc"

    drafts = client.get("/api/content-drafts")
    assert drafts.status_code == 200
    rows = drafts.get_json()["drafts"]
    assert len(rows) == 1
    assert rows[0]["source_type"] == "SYSTEM_SEED"

    legacy = client.post("/api/upload")
    assert legacy.status_code == 410
    assert legacy.get_json()["error"] == "LEGACY_UPLOAD_QUEUE_DISABLED"

    legacy_database = tmp_path / "app-data" / "app.db"
    before = legacy_database.read_bytes()
    client.get("/api/content-drafts")
    assert legacy_database.read_bytes() == before

    flask_app.extensions["content_studio"].close()
    flask_app.extensions["account_sessions"].close()
    models.database.Database._instance = None
    config._config = None
