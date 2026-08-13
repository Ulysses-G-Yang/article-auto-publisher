from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import sys
import uuid
from pathlib import Path

import pytest
from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.account_service import _platform_instance
from account_sessions.errors import AccountPlatformMismatchError
from account_sessions.models import PlatformAccount
from account_sessions.platform_catalog import (
    ACCOUNT_ENABLED_PLATFORMS,
    DELIVERY_ENABLED_PLATFORMS,
    PLATFORM_CATALOG,
)
from account_sessions.web import create_account_session_blueprint


EXPECTED_IDS = [
    "xiaoheihe",
    "zol",
    "zhihu",
    "weibo",
    "smzdm",
    "toutiao",
    "baijiahao",
    "xiaohongshu",
    "douyin",
    "wechat_mp",
]


def test_platform_catalog_freezes_public_capability_contract() -> None:
    assert [item.id for item in PLATFORM_CATALOG] == EXPECTED_IDS
    assert [item.sort_order for item in PLATFORM_CATALOG] == list(range(10, 101, 10))
    assert ACCOUNT_ENABLED_PLATFORMS == ("xiaoheihe", "zol", "zhihu")
    assert DELIVERY_ENABLED_PLATFORMS == ("xiaoheihe", "zol")

    by_id = {item.id: item for item in PLATFORM_CATALOG}
    assert by_id["zhihu"].status == "AVAILABLE"
    assert by_id["zhihu"].account_enabled is True
    assert by_id["zhihu"].delivery_enabled is False
    assert all(
        by_id[platform_id].status == "COMING_SOON"
        and by_id[platform_id].account_enabled is False
        and by_id[platform_id].delivery_enabled is False
        for platform_id in EXPECTED_IDS[3:]
    )
    assert all(
        item.logo_url == f"/static/img/platforms/{item.id}.svg"
        for item in PLATFORM_CATALOG
    )


def test_platform_catalog_api_exposes_only_public_fields(tmp_path: Path) -> None:
    app = Flask(__name__)
    database_path = tmp_path / "account-sessions.db"
    blueprint = create_account_session_blueprint(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        seed_legacy_profiles=False,
        auto_execute=False,
    )
    app.register_blueprint(blueprint)
    client = app.test_client()

    response = client.get("/api/platforms")

    assert response.status_code == 200
    platforms = response.get_json()["platforms"]
    assert [item["id"] for item in platforms] == EXPECTED_IDS
    assert all(
        set(item)
        == {
            "id",
            "display_name",
            "logo_url",
            "status",
            "delivery_enabled",
            "account_enabled",
            "sort_order",
        }
        for item in platforms
    )

    app.extensions["account_sessions"].close()


def test_unknown_platform_cannot_fall_back_to_zol() -> None:
    account = PlatformAccount(
        account_id=str(uuid.uuid4()),
        platform="xiaohongshu",
        display_name="规划中平台账号",
        profile_path="unused-profile",
        session_status="UNVERIFIED",
    )

    with pytest.raises(AccountPlatformMismatchError, match="不支持的平台"):
        _platform_instance(account)
