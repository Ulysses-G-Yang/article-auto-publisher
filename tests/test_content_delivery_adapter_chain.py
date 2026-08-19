"""ContentVersion 到真实平台适配器正文校验的安全链路测试。"""

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import sqlite3
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.account_service import AccountSessionService
from account_sessions.contracts import DeliveryRequest
from account_sessions.database import AccountDatabase
from account_sessions.delivery_service import DeliveryService
from account_sessions.models import PlatformAccount
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from content_studio.assets import AssetStore
from content_studio.contracts import (
    CreateDraftRequest,
    PatchDraftRequest,
    ReplaceTargetsRequest,
)
from content_studio.database import ContentDatabase, sqlite_url
from content_studio.service import ContentStudioService
from platforms.xiaoheihe import XiaoheihePlatform
from platforms.zol import ZOLPlatform
from tests.test_regression import FakePage, FakeXiaoPage

FROZEN_BODY = "第一段\n\nA &amp; B\u200b\n第三段"


class _Context:
    pages: list = []

    def __init__(self, platform_name: str) -> None:
        self.platform_name = platform_name

    async def cookies(self) -> list[dict]:
        if self.platform_name == "zol":
            return [
                {"name": "last_userid", "value": f"fixture-{self.platform_name}"},
                {"name": "userName", "value": f"{self.platform_name}链路账号"},
            ]
        return []


class _AdapterRuntimeMixin:
    """只替换浏览器外围；publish/fill_content 仍来自真实平台类。"""

    draft_calls = 0

    async def initialize(self) -> None:
        self.context = _Context(self.platform_name)
        self.page = self._fake_page
        self.simulator.random_delay = AsyncMock()
        self.simulator.simulate_scroll = AsyncMock()
        self.simulator.random_mouse_movement = AsyncMock()
        editor = (
            self._fake_page.body
            if self.platform_name == "xiaoheihe"
            else self._fake_page.frame_body
        )
        original_inner_text = editor.inner_text

        async def browser_dom_text() -> str:
            raw = await original_inner_text()
            return raw.replace("&amp;", "&").replace("\u200b", "")

        editor.inner_text = browser_dom_text

    async def check_login(self, *args, **kwargs) -> bool:
        return True

    async def fetch_identity_payload(self) -> dict:
        return {
            "ok": True,
            "user_id": f"fixture-{self.platform_name}",
            "display_name": f"{self.platform_name}链路账号",
        }

    async def navigate_to_editor(self) -> None:
        return None

    async def preflight_delivery(self, _title: str) -> None:
        # 隔离链路没有真实草稿页；平台基线行为由 ZOL 专项测试覆盖。
        return None

    async def fill_title(self, title: str) -> None:
        self.written_title = title

    async def select_topic(self, **kwargs) -> dict:
        return {"success": True, "selection_status": "not_required"}

    async def _safe_simulate_scroll(self, **kwargs) -> None:
        return None

    async def _safe_random_mouse_movement(self, **kwargs) -> None:
        return None

    async def save_draft(self, title: str = "") -> str:
        self.draft_calls += 1
        return f"https://example.invalid/{self.platform_name}/draft/1"

    async def cleanup(self) -> None:
        self.context = None


class ChainXiaoheihePlatform(_AdapterRuntimeMixin, XiaoheihePlatform):
    def __init__(self) -> None:
        super().__init__()
        self._fake_page = FakeXiaoPage()


class ChainZOLPlatform(_AdapterRuntimeMixin, ZOLPlatform):
    def __init__(self) -> None:
        super().__init__()
        self._fake_page = FakePage("iframe")


def _account_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}"


def _make_studio(tmp_path: Path, accounts: AccountSessionService) -> ContentStudioService:
    class NoLegacySource:
        def list_articles(self, *, limit=50, offset=0):
            return {"articles": [], "total": 0, "limit": limit, "offset": offset}

    class NoDocxImporter:
        async def parse(self, data: bytes, filename: str):
            raise AssertionError("本链路测试不应导入 DOCX")

    return ContentStudioService(
        ContentDatabase(sqlite_url(tmp_path / "content.db")),
        asset_store=AssetStore(tmp_path / "assets"),
        legacy_source=NoLegacySource(),
        docx_importer=NoDocxImporter(),
        account_service=accounts,
    )


@pytest.mark.parametrize(
    ("platform_name", "adapter_type"),
    [
        ("xiaoheihe", ChainXiaoheihePlatform),
        ("zol", ChainZOLPlatform),
    ],
)
def test_frozen_content_version_reaches_real_adapter_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    adapter_type,
) -> None:
    # 租约锁也限制到 pytest 临时目录，不使用运行中的真实账号或 Profile。
    monkeypatch.setenv("ACCOUNT_SESSION_DATA_DIR", str(tmp_path / "account-runtime"))
    account_db = AccountDatabase(_account_url(tmp_path))
    profile_root = tmp_path / "profiles"
    account_id = str(uuid.uuid4())
    profile = profile_root / platform_name / account_id
    profile.mkdir(parents=True)
    adapter = adapter_type()
    accounts = AccountSessionService(
        account_db,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: adapter,
        allowed_profile_roots=(profile_root,),
    )
    studio = _make_studio(tmp_path, accounts)

    async def scenario() -> tuple[dict, str, list[dict]]:
        await accounts.initialize()
        account = PlatformAccount(
            account_id=account_id,
            platform=platform_name,
            platform_user_id=f"fixture-{platform_name}",
            display_name=f"{platform_name}链路账号",
            profile_path=str(profile.resolve()),
            status="ACTIVE",
            session_status="VALID",
            persist_login=True,
        )
        async with account_db.session() as session:
            session.add(account)

        await studio.initialize()
        draft = await studio.create_draft(
            CreateDraftRequest(
                title="冻结版本标题",
                blocks=[{"type": "text", "text": FROZEN_BODY, "position": 0}],
            )
        )
        targeted = await studio.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=draft["revision"],
                targets=[
                    {
                        "platform": platform_name,
                        "account_id": account_id,
                        "mode": "DRAFT",
                    }
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        plan = await studio.create_delivery_plan(
            draft["draft_id"], targeted["revision"], LOCAL_WEB_CONTEXT
        )
        plan_context, _targets = await studio.get_plan_execution_context(
            plan["plan_id"], LOCAL_WEB_CONTEXT
        )

        # 冻结后修改活动草稿，旧 ContentVersion 必须保持不变。
        await studio.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=targeted["revision"],
                title="活动草稿已修改",
                blocks=[{"type": "text", "text": "不应投递的新正文", "position": 0}],
            ),
        )
        frozen_title, frozen_blocks, _images = await studio.resolve_delivery_payload(
            plan_context["version_id"]
        )

        delivery = DeliveryService(
            accounts,
            platform_factory=lambda _account: adapter,
            public_publish_enabled=False,
            content_resolver=studio.resolve_delivery_payload,
        )
        queued = await delivery.request_delivery(
            DeliveryRequest.model_validate(
                {
                    "article": {
                        "title": "SHOULD_NOT_BE_USED",
                        "body": "SHOULD_NOT_BE_USED",
                    },
                    "platform": platform_name,
                    "account_id": account_id,
                    "mode": "DRAFT",
                }
            ),
            LOCAL_WEB_CONTEXT,
            frozen_content_hash=plan_context["content_hash"],
            content_reference=plan_context["version_id"],
            persist_login_snapshot=True,
        )
        completed = await delivery.execute_operation(
            queued["operation_id"], LOCAL_WEB_CONTEXT
        )
        return completed, frozen_title, frozen_blocks

    try:
        completed, frozen_title, frozen_blocks = asyncio.run(scenario())
        assert completed["status"] == "DRAFT_SAVED"
        assert frozen_title == "冻结版本标题"
        assert frozen_blocks[0]["text"] == FROZEN_BODY
        assert adapter.written_title == "冻结版本标题"
        assert adapter.draft_calls == 1
        if platform_name == "xiaoheihe":
            written = adapter._fake_page.body.text
        else:
            written = adapter._fake_page.frame_body.text
        assert "第一段" in written
        assert "A &amp; B" in written
        assert "第三段" in written
        assert "SHOULD_NOT_BE_USED" not in written

        # SQLite 审计真值仍是原始冻结块，不是校验模块的比较视图。
        with sqlite3.connect(tmp_path / "content.db") as connection:
            raw = connection.execute(
                "SELECT blocks_json FROM content_versions"
            ).fetchone()[0]
        assert "A &amp; B" in raw
        assert "\\u200b" in raw
    finally:
        asyncio.run(studio.database.dispose())
        asyncio.run(account_db.dispose())
