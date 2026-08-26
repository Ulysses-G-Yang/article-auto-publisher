"""降级成功判定（放宽成功标准）专项测试。

覆盖：
- `publish()` 降级判定：P2 草稿箱唯一 → 降级成功带 degraded 标记；
  全部证据不足 → DELIVERY_INCOMPLETE（不伪装成功）。
- `DeliveryService` 持久化：`degraded` 列写入与 payload 返回；
  `DELIVERY_INCOMPLETE` 状态独立于 FAILED / RESULT_UNKNOWN。
全程 mock，不启动真实浏览器、不触网。
"""

from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.account_service import AccountSessionService
from account_sessions.database import AccountDatabase
from account_sessions.delivery_service import DeliveryService
from account_sessions.errors import AccountUnavailableError
from account_sessions.models import DeliveryOperation, PlatformAccount
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from platforms.base import BasePlatform, DraftResultUnknownError, DraftVerificationEvidence


def account_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}"


async def make_delivery(
    tmp_path: Path,
) -> tuple[AccountDatabase, DeliveryService, PlatformAccount]:
    database = AccountDatabase(account_url(tmp_path))
    accounts = AccountSessionService(database, seed_legacy_profiles=False)
    await accounts.initialize()
    account = PlatformAccount(
        account_id=str(uuid.uuid4()),
        platform="weibo",
        platform_user_id="degraded-user",
        display_name="降级判定测试账号",
        profile_path=str(tmp_path / "profiles" / "weibo" / "acc"),
        status="ACTIVE",
        session_status="VALID",
        persist_login=True,
    )
    async with database.session() as session:
        session.add(account)
        await session.flush()
    delivery = DeliveryService(accounts, public_publish_enabled=False)
    return database, delivery, account


async def insert_operation(
    database: AccountDatabase,
    account: PlatformAccount,
    operation_id: str,
) -> None:
    async with database.session() as session:
        session.add(
            DeliveryOperation(
                operation_id=operation_id,
                account_id=account.account_id,
                platform=account.platform,
                mode="DRAFT",
                source="TEST",
                actor_id="tester",
                title="降级判定测试",
                body="正文",
                content_version="v1",
                account_display_name_snapshot=account.display_name,
                status="QUEUED",
                created_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()


async def read_operation(
    database: AccountDatabase,
    operation_id: str,
) -> DeliveryOperation:
    async with database.session() as session:
        operation = await session.get(DeliveryOperation, operation_id)
        assert operation is not None
        return operation


# ---------------------------------------------------------------------------
# base.publish() 降级判定
# ---------------------------------------------------------------------------


class _FakeDb:
    def add_task_log(self, *_args: object) -> None:
        return None


class _FakePlatform(BasePlatform):
    """只 stub 必需抽象方法；publish() 走基类流水线。"""

    platform_name = "weibo"

    async def check_login(self) -> bool:
        return True

    async def login(self) -> None:
        return None

    async def navigate_to_editor(self) -> None:
        return None

    async def fill_title(self, _title: str) -> None:
        return None

    async def fill_content(self, _content_blocks: list, _images: list) -> dict:
        return {
            "text_ok": True,
            "media_status": "completed",
            "expected_images": 0,
            "uploaded_images": 0,
            "failed_images": [],
        }

    async def select_topic(self, **_kwargs: object) -> dict:
        return {"success": True}

    async def apply_cover(self, _cover: dict | None = None) -> dict:
        return {"success": True, "cover_status": "not_required"}

    async def save_draft(self, _title: str = "") -> str:
        return ""


def _run_publish(platform: _FakePlatform) -> dict:
    platform.simulator.random_delay = AsyncMock()
    with (
        patch.object(platform, "_safe_simulate_scroll", new=AsyncMock()),
        patch.object(platform, "_safe_random_mouse_movement", new=AsyncMock()),
    ):
        return asyncio.run(
            platform.publish(
                title="降级判定测试",
                content_blocks=[{"type": "text", "text": "正文"}],
                images=[],
                cover={"strategy": "NONE"},
                delivery_mode="DRAFT",
                auto_login=False,
                task_id=0,
                db=_FakeDb(),
            )
        )


def _platform_with_save_raising(evidence: DraftVerificationEvidence) -> _FakePlatform:
    platform = _FakePlatform()
    platform._last_draft_evidence = evidence

    async def save_draft(_title: str = "") -> str:
        raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 测试", evidence=evidence)

    platform.save_draft = save_draft  # type: ignore[method-assign]
    return platform


def test_publish_degraded_success_when_draft_list_unique() -> None:
    """保存响应未确认但草稿箱标题唯一 → 降级成功，带 draft_list_confirmed。"""
    evidence = DraftVerificationEvidence()
    evidence.mark_save_response(status=None)
    evidence.mark_draft_list(match_count=1)
    evidence.set_draft_url("https://weibo.com/draft/42")
    evidence.finalize()

    result = _run_publish(_platform_with_save_raising(evidence))

    assert result["success"] is True
    assert result["degraded"] == "draft_list_confirmed"
    assert result["draft_url"] == "https://weibo.com/draft/42"
    assert result["verification_evidence"]["draft_list_title_unique"] is True


def test_publish_degraded_success_allows_empty_draft_url() -> None:
    """降级成功时 draft_url 可能为空：仍算成功，前端走草稿箱链接兜底。"""
    evidence = DraftVerificationEvidence()
    evidence.mark_save_response(status=None)
    evidence.mark_draft_list(match_count=1)
    evidence.finalize()

    result = _run_publish(_platform_with_save_raising(evidence))

    assert result["success"] is True
    assert result["degraded"] == "draft_list_confirmed"
    assert result["draft_url"] == ""


def test_publish_delivery_incomplete_when_no_evidence_confirmed() -> None:
    """全部证据不足（草稿箱也无唯一草稿）→ 投递未完成，不伪装成功。"""
    evidence = DraftVerificationEvidence()
    evidence.mark_save_response(status=None)
    evidence.mark_draft_list(match_count=0)
    evidence.finalize()

    result = _run_publish(_platform_with_save_raising(evidence))

    assert result["success"] is False
    assert result["error_code"] == "DELIVERY_INCOMPLETE"
    assert "投递未完成" in result["error"]
    assert result["verification_evidence"]["draft_list_title_unique"] is False


def test_publish_delivery_incomplete_when_empty_draft_url_without_exception() -> None:
    """save_draft 返回空串且无降级 → 投递未完成。"""
    result = _run_publish(_FakePlatform())

    assert result["success"] is False
    assert result["error_code"] == "DELIVERY_INCOMPLETE"
    assert "投递未完成" in result["error"]


def test_publish_full_success_is_not_degraded() -> None:
    """完整证据链走通 → 正常成功，degraded 为 None。"""
    platform = _FakePlatform()

    async def save_draft(_title: str = "") -> str:
        evidence = DraftVerificationEvidence()
        evidence.mark_save_response(status=200)
        evidence.mark_draft_list(match_count=1)
        evidence.mark_reopen(title_match=True, dom_blocks_match=True)
        evidence.set_draft_url("https://weibo.com/draft/1")
        evidence.finalize()
        platform._last_draft_evidence = evidence
        return "https://weibo.com/draft/1"

    platform.save_draft = save_draft  # type: ignore[method-assign]

    result = _run_publish(platform)

    assert result["success"] is True
    assert result["degraded"] is None
    assert result["draft_url"] == "https://weibo.com/draft/1"
    assert result["verification_evidence"]["save_response_2xx"] is True


# ---------------------------------------------------------------------------
# DeliveryService 持久化
# ---------------------------------------------------------------------------


class TestDeliveryServiceDegradedPersistence:
    @pytest.mark.asyncio
    async def test_mark_failed_delivery_incomplete_sets_distinct_status(
        self, tmp_path: Path
    ) -> None:
        database, delivery, account = await make_delivery(tmp_path)
        try:
            operation_id = str(uuid.uuid4())
            await insert_operation(database, account, operation_id)
            exc = AccountUnavailableError(
                "投递未完成：未能在平台确认草稿保存结果",
                error_code="DELIVERY_INCOMPLETE",
                evidence={
                    "save_response_2xx": None,
                    "draft_list_title_unique": False,
                    "unknown": True,
                },
            )
            await delivery._mark_failed(
                operation_id,
                account,
                LOCAL_WEB_CONTEXT,
                exc,
                [],
            )
            operation = await read_operation(database, operation_id)
            assert operation.status == "DELIVERY_INCOMPLETE"
            assert operation.error_code == "DELIVERY_INCOMPLETE"
            assert operation.verification_evidence is not None
        finally:
            await database.dispose()

    @pytest.mark.asyncio
    async def test_mark_failed_unknown_stays_result_unknown(
        self, tmp_path: Path
    ) -> None:
        database, delivery, account = await make_delivery(tmp_path)
        try:
            operation_id = str(uuid.uuid4())
            await insert_operation(database, account, operation_id)
            exc = AccountUnavailableError(
                "保存结果未知",
                error_code="DRAFT_RESULT_UNKNOWN",
                evidence={"save_response_2xx": None, "unknown": True},
            )
            await delivery._mark_failed(
                operation_id,
                account,
                LOCAL_WEB_CONTEXT,
                exc,
                [],
            )
            operation = await read_operation(database, operation_id)
            assert operation.status == "RESULT_UNKNOWN"
            assert operation.error_code == "DRAFT_RESULT_UNKNOWN"
        finally:
            await database.dispose()

    @pytest.mark.asyncio
    async def test_mark_failed_ordinary_error_stays_failed(
        self, tmp_path: Path
    ) -> None:
        database, delivery, account = await make_delivery(tmp_path)
        try:
            operation_id = str(uuid.uuid4())
            await insert_operation(database, account, operation_id)
            await delivery._mark_failed(
                operation_id,
                account,
                LOCAL_WEB_CONTEXT,
                AccountUnavailableError("平台拒绝请求", error_code="PLATFORM_REJECTED"),
                [],
            )
            operation = await read_operation(database, operation_id)
            assert operation.status == "FAILED"
            assert operation.error_code == "PLATFORM_REJECTED"
        finally:
            await database.dispose()

    @pytest.mark.asyncio
    async def test_mark_completed_persists_degraded_and_evidence(
        self, tmp_path: Path
    ) -> None:
        database, delivery, account = await make_delivery(tmp_path)
        try:
            operation_id = str(uuid.uuid4())
            await insert_operation(database, account, operation_id)
            result = {
                "success": True,
                "draft_url": "",
                "post_url": None,
                "degraded": "draft_list_confirmed",
                "verification_evidence": {
                    "save_response_2xx": None,
                    "draft_list_title_unique": True,
                    "draft_list_match_count": 1,
                    "unknown": True,
                    "summary": "草稿箱存在标题唯一匹配的草稿",
                },
            }
            payload = await delivery._mark_completed(
                operation_id,
                account,
                LOCAL_WEB_CONTEXT,
                result,
                [],
            )
            operation = await read_operation(database, operation_id)
            assert operation.status == "DRAFT_SAVED"
            assert operation.degraded == "draft_list_confirmed"
            assert payload["degraded"] == "draft_list_confirmed"
            assert payload["verification_evidence"]["draft_list_title_unique"] is True
        finally:
            await database.dispose()

    @pytest.mark.asyncio
    async def test_mark_completed_without_degraded_is_none(
        self, tmp_path: Path
    ) -> None:
        database, delivery, account = await make_delivery(tmp_path)
        try:
            operation_id = str(uuid.uuid4())
            await insert_operation(database, account, operation_id)
            result = {
                "success": True,
                "draft_url": "https://weibo.com/draft/1",
                "post_url": None,
            }
            payload = await delivery._mark_completed(
                operation_id,
                account,
                LOCAL_WEB_CONTEXT,
                result,
                [],
            )
            assert payload["degraded"] is None
            operation = await read_operation(database, operation_id)
            assert operation.degraded is None
        finally:
            await database.dispose()
