"""账号会话域的独立运行目录。"""

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_root() -> Path:
    configured = os.getenv("ACCOUNT_SESSION_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (project_root() / "data" / "account_sessions").resolve()


def default_database_path() -> Path:
    return default_root() / "account_sessions.db"


def managed_profile_path(platform: str, account_id: str) -> Path:
    return default_root() / "profiles" / platform / account_id


def legacy_profile_root() -> Path:
    """返回现役发布流程的 Profile 根目录。

    独立 worktree 中没有旧 Profile，因此允许测试/迁移工具显式注入绝对
    根目录。正式合并回主工作树后，不设置环境变量仍会解析到原有目录。
    """

    configured = os.getenv("LEGACY_CHROME_PROFILE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (project_root() / "data" / "chrome_profiles").resolve()


def legacy_profile_path(platform: str) -> Path:
    """现役发布流程已经持有的 Profile；只登记，不复制、不删除。"""

    return (legacy_profile_root() / platform).resolve()
