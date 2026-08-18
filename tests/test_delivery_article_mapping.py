from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.account_service import AccountSessionService
from account_sessions.contracts import ArticleInput, DeliveryRequest
from account_sessions.database import AccountDatabase
from account_sessions.delivery_service import DeliveryService
from account_sessions.models import DeliveryOperation, PlatformAccount
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from article_mvp.db.database import dispose_db, init_db, session_scope
from article_mvp.db.models import PlatformArticle, PlatformArticleStatus
from article_mvp.errors import MvpError, PublishMappingConflictError
from article_mvp.services import delivery_bridge as delivery_bridge_module
from article_mvp.services.delivery_bridge import (
    DeliveryBridge,
    extract_article_id,
    synthesize_task_id,
)


def account_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}"


def article_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'articles.db').as_posix()}"


async def make_delivery(
    tmp_path: Path,
    *,
    sink=None,
) -> tuple[AccountDatabase, DeliveryService, PlatformAccount, dict]:
    database = AccountDatabase(account_url(tmp_path))
    accounts = AccountSessionService(database, seed_legacy_profiles=False)
    await accounts.initialize()
    account = PlatformAccount(
        account_id=str(uuid.uuid4()),
        platform="xiaoheihe",
        platform_user_id="mapping-user",
        display_name="映射测试账号",
        profile_path=str(tmp_path / "profile"),
        status="ACTIVE",
        session_status="VALID",
        persist_login=True,
    )
    async with database.session() as session:
        session.add(account)
    delivery = DeliveryService(
        accounts,
        public_publish_enabled=False,
        delivery_event_sink=sink,
    )
    request = DeliveryRequest(
        article=ArticleInput(title="桥接测试标题", body="桥接测试正文"),
        platform="xiaoheihe",
        account_id=account.account_id,
        mode="DRAFT",
    )
    queued = await delivery.request_delivery(request, LOCAL_WEB_CONTEXT)
    return database, delivery, account, queued


async def complete_direct(
    delivery: DeliveryService,
    account: PlatformAccount,
    queued: dict,
    result: dict,
) -> dict:
    return await delivery._mark_completed(
        queued["operation_id"],
        account,
        LOCAL_WEB_CONTEXT,
        result,
        [],
    )


async def read_operation(database: AccountDatabase, operation_id: str) -> DeliveryOperation:
    async with database.session() as session:
        operation = await session.get(DeliveryOperation, operation_id)
        assert operation is not None
        return operation


def create_pre_mapping_account_database(path: Path) -> None:
    """创建 C6b 之前的最小账号库，验证升级不会重建或丢失执行单。"""

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE platform_accounts (
                account_id VARCHAR(36) PRIMARY KEY,
                platform VARCHAR(32) NOT NULL,
                platform_user_id VARCHAR(255),
                display_name VARCHAR(255) NOT NULL,
                profile_path VARCHAR(2048) NOT NULL,
                is_legacy_profile BOOLEAN NOT NULL DEFAULT 0,
                status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
                session_status VARCHAR(24) NOT NULL DEFAULT 'UNVERIFIED',
                persist_login BOOLEAN NOT NULL DEFAULT 1,
                last_verified_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE delivery_operations (
                operation_id VARCHAR(36) PRIMARY KEY,
                account_id VARCHAR(36) NOT NULL,
                platform VARCHAR(32) NOT NULL,
                mode VARCHAR(16) NOT NULL,
                source VARCHAR(16) NOT NULL,
                actor_id VARCHAR(128) NOT NULL,
                title VARCHAR(255) NOT NULL,
                body TEXT NOT NULL,
                content_version VARCHAR(64) NOT NULL,
                account_display_name_snapshot VARCHAR(255) NOT NULL,
                status VARCHAR(24) NOT NULL DEFAULT 'QUEUED',
                draft_url VARCHAR(2048),
                platform_article_id VARCHAR(255),
                platform_url VARCHAR(2048),
                error_code VARCHAR(64),
                error_message TEXT,
                confirmation_used BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                started_at DATETIME,
                completed_at DATETIME,
                FOREIGN KEY(account_id) REFERENCES platform_accounts(account_id)
            );
            INSERT INTO platform_accounts (
                account_id, platform, platform_user_id, display_name,
                profile_path, status, session_status, created_at, updated_at
            ) VALUES (
                'legacy-account', 'xiaoheihe', 'legacy-user', '旧账号',
                'C:/profiles/legacy', 'ACTIVE', 'VALID',
                '2026-08-18T00:00:00+00:00', '2026-08-18T00:00:00+00:00'
            );
            INSERT INTO delivery_operations (
                operation_id, account_id, platform, mode, source, actor_id,
                title, body, content_version, account_display_name_snapshot,
                status, created_at
            ) VALUES (
                'legacy-operation', 'legacy-account', 'xiaoheihe', 'DRAFT',
                'LOCAL_WEB', 'legacy-actor', '旧执行单', '旧正文', 'legacy-hash',
                '旧账号', 'DRAFT_SAVED', '2026-08-18T00:00:01+00:00'
            );
            """
        )


@pytest.mark.asyncio
async def test_article_mapping_columns_migrate_idempotently_and_preserve_old_row(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-accounts.db"
    create_pre_mapping_account_database(database_path)
    database = AccountDatabase(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    try:
        await database.initialize()
        async with database.session() as session:
            operation = await session.get(DeliveryOperation, "legacy-operation")
            assert operation is not None
            assert operation.status == "DRAFT_SAVED"
            assert operation.title == "旧执行单"
            assert operation.article_mapping_status == "NOT_PENDING"
            assert operation.article_mapping_attempts == 0
            assert operation.article_mapping_error_code is None
            assert operation.article_mapping_last_attempt_at is None

        # 第二次启动不得重复 ALTER、覆盖旧执行单或破坏索引。
        await database.initialize()
        async with database.engine.connect() as connection:
            columns = {
                row[1]
                for row in (
                    await connection.exec_driver_sql(
                        "PRAGMA table_info(delivery_operations)"
                    )
                ).fetchall()
            }
            indexes = {
                row[1]
                for row in (
                    await connection.exec_driver_sql(
                        "PRAGMA index_list(delivery_operations)"
                    )
                ).fetchall()
            }
        assert {
            "article_mapping_status",
            "article_mapping_attempts",
            "article_mapping_error_code",
            "article_mapping_last_attempt_at",
        } <= columns
        assert "ix_delivery_article_mapping_status" in indexes
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_content_studio_execution_has_no_second_published_event_bridge() -> None:
    """成功操作只回写计划目标，不在 Content Studio 再写一次文章映射。"""

    from content_studio.web import ContentStudioRuntimeState

    class FakeDelivery:
        async def execute_operation(self, _operation_id, _access):
            return {
                "operation_id": "operation-bridge-once",
                "status": "DRAFT_SAVED",
                "platform": "xiaoheihe",
                "platform_article_id": "123",
            }

    class FakeService:
        def __init__(self) -> None:
            self.results = []

        async def set_plan_target_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    class ExplodingSecondBridge:
        async def handle(self, _event):
            raise AssertionError("Content Studio 不得再次调用 PublishedEventService")

    service = FakeService()
    state = ContentStudioRuntimeState.__new__(ContentStudioRuntimeState)
    state.account_state = SimpleNamespace(delivery=FakeDelivery())
    state.service = service
    # 旧实现即使被外部注入该属性，也不应再触发它。
    state.published_service = ExplodingSecondBridge()

    await state._execute_operation_and_sync(
        "plan-once",
        "target-once",
        "operation-bridge-once",
        LOCAL_WEB_CONTEXT,
    )

    assert len(service.results) == 1
    assert service.results[0][1]["status"] == "DRAFT_SAVED"


def test_account_runtime_reconciles_article_mapping_after_interrupted_operations() -> None:
    """启动顺序先恢复投递，再补偿映射，最后才启动心跳。"""

    from account_sessions.web import AccountSessionRuntimeState

    events: list[str] = []

    class FakeRuntime:
        def run(self, coroutine, *, timeout=30):
            del timeout
            return asyncio.run(coroutine)

    class FakeAccounts:
        async def initialize(self):
            events.append("accounts.initialize")

    class FakeDelivery:
        async def reconcile_interrupted_operations(self):
            events.append("delivery.interrupted")

        async def reconcile_pending_article_mappings(self):
            events.append("delivery.article_mapping")

    class FakeHeartbeatScheduler:
        async def start(self):
            events.append("heartbeat.start")

    state = AccountSessionRuntimeState.__new__(AccountSessionRuntimeState)
    state._runtime = FakeRuntime()
    state._initialized = False
    state._lock = Lock()
    state.accounts = FakeAccounts()
    state.delivery = FakeDelivery()
    state.heartbeat_scheduler = FakeHeartbeatScheduler()

    state._ensure_runtime()

    assert events == [
        "accounts.initialize",
        "delivery.interrupted",
        "delivery.article_mapping",
        "heartbeat.start",
    ]


@pytest.mark.asyncio
async def test_mapping_without_sink_stays_not_pending_and_is_not_faked_success(
    tmp_path: Path,
) -> None:
    database, delivery, account, queued = await make_delivery(tmp_path)
    try:
        result = await complete_direct(
            delivery,
            account,
            queued,
            {"success": True, "draft_url": "https://example.invalid/drafts/no-sink"},
        )
        assert result["status"] == "DRAFT_SAVED"
        assert await delivery.reconcile_pending_article_mappings() == {
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
        }
        stored = await read_operation(database, queued["operation_id"])
        assert stored.article_mapping_status == "NOT_PENDING"
        assert stored.article_mapping_attempts == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_completion_commits_account_success_before_sink_reads_it(tmp_path: Path) -> None:
    database_ref: AccountDatabase | None = None
    seen: list[tuple[str, str]] = []

    async def sink(**payload):
        assert payload["platform_article_id"] is None
        async with database_ref.session() as session:  # type: ignore[union-attr]
            operation = await session.get(DeliveryOperation, payload["operation_id"])
            assert operation is not None
            seen.append((operation.status, operation.article_mapping_status))

    database, delivery, account, queued = await make_delivery(tmp_path, sink=sink)
    database_ref = database
    try:
        result = await complete_direct(
            delivery,
            account,
            queued,
            {"success": True, "draft_url": "https://example.invalid/drafts/1"},
        )
        assert result["status"] == "DRAFT_SAVED"
        assert seen == [("DRAFT_SAVED", "PENDING")]
        stored = await read_operation(database, queued["operation_id"])
        assert stored.article_mapping_status == "SUCCEEDED"
        assert stored.article_mapping_attempts == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_bridge_failure_keeps_delivery_success_and_stores_stable_code(
    tmp_path: Path,
) -> None:
    async def sink(**_payload):
        raise MvpError("secret profile detail", error_code="BRIDGE_DOWN")

    database, delivery, account, queued = await make_delivery(tmp_path, sink=sink)
    try:
        result = await complete_direct(
            delivery,
            account,
            queued,
            {"success": True, "draft_url": "https://example.invalid/drafts/2"},
        )
        stored = await read_operation(database, queued["operation_id"])
        assert result["status"] == "DRAFT_SAVED"
        assert stored.status == "DRAFT_SAVED"
        assert stored.article_mapping_status == "FAILED"
        assert stored.article_mapping_error_code == "BRIDGE_DOWN"
        assert "secret" not in str(stored.article_mapping_error_code)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cancelled_bridge_preserves_success_and_pending_mapping(tmp_path: Path) -> None:
    async def sink(**_payload):
        raise asyncio.CancelledError()

    database, delivery, account, queued = await make_delivery(tmp_path, sink=sink)
    try:
        with pytest.raises(asyncio.CancelledError):
            await complete_direct(
                delivery,
                account,
                queued,
                {"success": True, "draft_url": "https://example.invalid/drafts/3"},
            )
        stored = await read_operation(database, queued["operation_id"])
        assert stored.status == "DRAFT_SAVED"
        assert stored.article_mapping_status == "PENDING"
        assert stored.article_mapping_attempts == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stale_slow_mapping_attempt_cannot_overwrite_new_success(
    tmp_path: Path,
) -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def sink(**_payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
            raise MvpError("old attempt", error_code="OLD_MAPPING_FAILURE")

    database, delivery, account, queued = await make_delivery(tmp_path, sink=sink)
    try:
        first_task = asyncio.create_task(
            complete_direct(
                delivery,
                account,
                queued,
                {"success": True, "draft_url": "https://example.invalid/drafts/slow"},
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second_outcome = await delivery._dispatch_article_mapping(queued["operation_id"])
        assert second_outcome == "SUCCEEDED"
        release_first.set()
        first_result = await first_task
        assert first_result["status"] == "DRAFT_SAVED"
        stored = await read_operation(database, queued["operation_id"])
        assert stored.article_mapping_status == "SUCCEEDED"
        assert stored.article_mapping_error_code is None
        assert stored.article_mapping_attempts == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_mapping_reconcile_is_idempotent_and_does_not_duplicate_article(
    tmp_path: Path,
) -> None:
    state = {"fail": True}
    bridge = DeliveryBridge(article_url(tmp_path))

    async def sink(**payload):
        if state["fail"]:
            raise MvpError("temporary", error_code="BRIDGE_TEMPORARY")
        return await bridge.record(**payload)

    database, delivery, account, queued = await make_delivery(tmp_path, sink=sink)
    try:
        await complete_direct(
            delivery,
            account,
            queued,
            {"success": True, "draft_url": "https://example.invalid/drafts/4"},
        )
        failed = await read_operation(database, queued["operation_id"])
        assert failed.article_mapping_status == "FAILED"
        state["fail"] = False
        first = await delivery.reconcile_pending_article_mappings()
        second = await delivery.reconcile_pending_article_mappings()
        assert first["succeeded"] == 1
        assert second["processed"] == 0
        stored = await read_operation(database, queued["operation_id"])
        assert stored.article_mapping_status == "SUCCEEDED"
        async with session_scope(article_url(tmp_path)) as session:
            count = await session.scalar(select(func.count()).select_from(PlatformArticle))
        assert count == 1
    finally:
        await database.dispose()
        await dispose_db()


@pytest.mark.asyncio
async def test_bridge_publish_id_rules_and_draft_does_not_set_published_at(
    tmp_path: Path,
) -> None:
    url = article_url(tmp_path)
    bridge = DeliveryBridge(url)
    completed = datetime(2026, 8, 18, 10, tzinfo=timezone.utc)
    try:
        unmapped = await bridge.record(
            operation_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
            platform="zhihu",
            mode="PUBLISH",
            title="无可靠 ID",
            platform_url="https://example.invalid/article?id=123",
            completed_at=completed,
        )
        assert unmapped.status == PlatformArticleStatus.UNMAPPED
        assert unmapped.external_article_id.startswith("unmapped:")
        assert unmapped.published_at == completed

        draft = await bridge.record(
            operation_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
            platform="zhihu",
            mode="DRAFT",
            title="草稿不冒充发布时间",
            draft_url="https://example.invalid/drafts/5",
            completed_at=completed,
        )
        assert draft.status == PlatformArticleStatus.UNMAPPED
        assert draft.published_at is None
        assert draft.extra_data["delivery_completed_at"] == completed.isoformat()

        mapped = await bridge.record(
            operation_id="1f1f1f1f-1111-4111-8111-111111111111",
            platform="zhihu",
            mode="PUBLISH",
            title="显式 ID",
            platform_article_id=123456,
            platform_url="https://example.invalid/article/no-slug",
            completed_at=completed,
        )
        assert mapped.status == PlatformArticleStatus.MAPPED
        assert mapped.external_article_id == "123456"
    finally:
        await dispose_db()

    assert extract_article_id("https://example.invalid/p/123456?from=feed") == "123456"
    assert extract_article_id("https://example.invalid/article?id=123456") == ""
    assert synthesize_task_id("1f1f1f1f-1111-4111-8111-111111111111") < 0


@pytest.mark.asyncio
async def test_bridge_compatibility_and_concurrent_duplicate_calls(tmp_path: Path) -> None:
    url = article_url(tmp_path)
    operation_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    bridge = DeliveryBridge(url)
    await init_db(url)
    try:
        old_task_id = 42
        async with session_scope(url) as session:
            old = PlatformArticle(
                task_id=old_task_id,
                platform="xiaoheihe",
            external_article_id=f"draft:{operation_id}",
            status=PlatformArticleStatus.UNMAPPED,
            title="旧草稿",
            published_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
            extra_data={"legacy": True},
            )
            session.add(old)
            await session.flush()
            old_id = old.id
        reused = await bridge.record(
            operation_id=operation_id,
            platform="xiaoheihe",
            mode="DRAFT",
            title="旧草稿",
            draft_url="https://example.invalid/drafts/old",
        )
        assert reused.id == old_id
        assert reused.task_id == old_task_id
        assert reused.published_at is None

        results = await asyncio.gather(
            *[
                bridge.record(
                    operation_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
                    platform="xiaoheihe",
                    mode="DRAFT",
                    title="并发草稿",
                    draft_url="https://example.invalid/drafts/concurrent",
                )
                for _ in range(3)
            ]
        )
        assert len({mapping.id for mapping in results}) == 1
        async with session_scope(url) as session:
            count = await session.scalar(
                select(func.count()).where(
                    PlatformArticle.external_article_id
                    == "draft:7c9e6679-7425-40de-944b-e07fc1f90ae7"
                )
            )
        assert count == 1
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_bridge_conflicts_and_invalid_inputs_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = article_url(tmp_path)
    bridge = DeliveryBridge(url)
    try:
        await bridge.record(
            operation_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
            platform="zhihu",
            mode="PUBLISH",
            title="原始文章",
            platform_article_id="9001",
        )
        with pytest.raises(PublishMappingConflictError):
            await bridge.record(
                operation_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
                platform="zol",
                mode="PUBLISH",
                title="跨平台冲突",
                platform_article_id="9001",
            )
        with pytest.raises(PublishMappingConflictError):
            await bridge.record(
                operation_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
                platform="zhihu",
                mode="PUBLISH",
                title="跨执行单冲突",
                platform_article_id="9001",
            )
        await bridge.record(
            operation_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            platform="zhihu",
            mode="PUBLISH",
            title="同事件原始 ID",
            platform_article_id="9002",
        )
        with pytest.raises(PublishMappingConflictError):
            await bridge.record(
                operation_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                platform="zhihu",
                mode="PUBLISH",
                title="同事件另一 ID",
                platform_article_id="9003",
            )
        monkeypatch.setattr(delivery_bridge_module, "synthesize_task_id", lambda _: -77)
        await bridge.record(
            operation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            platform="zol",
            mode="DRAFT",
            title="任务碰撞原始",
        )
        with pytest.raises(PublishMappingConflictError):
            await bridge.record(
                operation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                platform="zol",
                mode="DRAFT",
                title="任务碰撞另一执行单",
            )
        with pytest.raises(MvpError):
            await bridge.record(
                operation_id="1f1f1f1f-1111-4111-8111-111111111111",
                platform="zhihu",
                mode="UNKNOWN",
                title="未知模式",
            )
        with pytest.raises(MvpError):
            await bridge.record(
                operation_id="1f1f1f1f-1111-4111-8111-111111111111",
                platform="zhihu",
                mode="PUBLISH",
                title="负 ID",
                platform_article_id=-1,
            )
        with pytest.raises(MvpError):
            await bridge.record(
                operation_id="1f1f1f1f-1111-4111-8111-111111111111",
                platform="zhihu",
                mode="PUBLISH",
                title="布尔 ID",
                platform_article_id=True,
            )
    finally:
        await dispose_db()
