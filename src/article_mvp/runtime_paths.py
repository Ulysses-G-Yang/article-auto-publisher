"""article_mvp 独立运行目录定义。"""

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_data_dir() -> Path:
    """返回新模块专属数据根目录，不与旧系统 ``data/`` 直接共用文件。"""

    configured = os.getenv("ARTICLE_MVP_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (project_root() / "data" / "article_mvp").resolve()


def database_path() -> Path:
    return runtime_data_dir() / "article_mvp.db"


def legacy_database_path() -> Path:
    """仅供显式迁移工具读取；运行时业务代码不得连接此路径。"""

    return (project_root() / "data" / "app.db").resolve()
