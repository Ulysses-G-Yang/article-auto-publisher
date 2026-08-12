"""Content Studio 独立运行目录。"""

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_root() -> Path:
    configured = os.getenv("CONTENT_STUDIO_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    app_data = os.getenv("APP_DATA_DIR", "").strip()
    if app_data:
        return (Path(app_data).expanduser().resolve() / "content_studio").resolve()
    return (project_root() / "data" / "content_studio").resolve()


def default_database_path() -> Path:
    return default_root() / "content_studio.db"


def default_asset_root() -> Path:
    return default_root() / "assets"


def default_work_root() -> Path:
    return default_root() / "work"
