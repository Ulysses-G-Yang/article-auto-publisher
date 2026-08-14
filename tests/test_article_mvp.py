from __future__ import annotations

# unittest discover 不读取 pytest.ini 的 pythonpath；以下引导代码必须先于
# article_mvp 导入执行，因此本文件有意忽略 E402。
# ruff: noqa: E402, I001

import ast
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import httpx
import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy import inspect, select

from article_mvp.config import EvidenceLevel, PaginationConfig, load_platform_config
from article_mvp.contracts import CollectorAuth
from article_mvp.db.database import (
    dispose_db,
    get_engine,
    init_db,
    session_scope,
)
from article_mvp.db.models import (
    CollectionRun,
    CollectionRunStatus,
    MetricSnapshot,
    PlatformArticle,
    PlatformArticleStatus,
)
from article_mvp.errors import (
    ConfigurationError,
    PublishMappingConflictError,
    PublishResultUnknownError,
    RateLimitedError,
    SessionExpiredError,
)
from article_mvp.platforms.xiaoheihe.collector import XiaoheiheCollector
from article_mvp.platforms.xiaoheihe.publisher import XiaoheihePublisher
from article_mvp.runtime_paths import database_path as default_database_path
from article_mvp.runtime_paths import runtime_data_dir
from article_mvp.security import extract_json_path, redact_sensitive
from article_mvp.services.collect_service import CollectService
from article_mvp.services.published_event_service import PublishedEventService
from article_mvp.tools.migrate_legacy_tables import migrate
from article_mvp.tools.probe_xhh import sanitized_url
from article_mvp.web import create_dashboard_app
from article_mvp.web.runtime import AsyncRuntime


def database_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'mvp.db').as_posix()}"


class FakeResponse:
    def __init__(self, payload, *, status: int = 200, json_error: Exception | None = None):
        self._payload = payload
        self._json_error = json_error
        self.status = status
        self.url = "https://www.xiaoheihe.cn/api/post/submit"
        self.request = SimpleNamespace(method="POST")

    async def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


class FakeResponseInfo:
    def __init__(self, response: FakeResponse):
        self.response = response

    @property
    def value(self):
        async def resolve():
            return self.response

        return resolve()


class FakeExpectResponse:
    def __init__(self, response: FakeResponse, *, timeout_error: bool = False):
        self.response = response
        self.timeout_error = timeout_error

    async def __aenter__(self):
        return FakeResponseInfo(self.response)

    async def __aexit__(self, exc_type, exc, traceback):
        if self.timeout_error:
            raise PlaywrightTimeoutError("timed out")
        return False


class FakePage:
    def __init__(self, response: FakeResponse, *, timeout_error: bool = False):
        self.response = response
        self.timeout_error = timeout_error
        self.url = "https://www.xiaoheihe.cn/creator/editor"
        self.predicate = None
        self.timeout = None

    def expect_response(self, predicate, *, timeout):
        self.predicate = predicate
        self.timeout = timeout
        return FakeExpectResponse(self.response, timeout_error=self.timeout_error)


async def no_op_trigger(_page):
    return None


def verified_config():
    config = load_platform_config().model_copy(deep=True)
    config.collector.endpoint.url = "https://metrics.example.test/article"
    config.collector.endpoint.evidence.level = EvidenceLevel.VERIFIED
    config.collector.endpoint.evidence.verified_at = datetime.now(timezone.utc)
    config.collector.json_paths = {
        "read_count": "data.stats.read",
        "like_count": "data.stats.like",
        "comment_count": "data.stats.comment",
        "collect_count": "data.stats.collect",
        "exposure_count": "data.stats.exposure",
        "share_count": "data.stats.share",
        "revenue": "data.stats.revenue",
        "snapshot_time": "data.snapshot_time",
    }
    return config


@pytest.mark.asyncio
async def test_init_db_sets_pragmas_and_indexes(tmp_path):
    url = database_url(tmp_path)
    await dispose_db()
    await init_db(url)
    await init_db(url)
    engine = get_engine(url)

    async with engine.connect() as connection:
        journal_mode = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar()
        busy_timeout = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar()
        foreign_keys = (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar()

        def schema_details(sync_connection):
            inspector = inspect(sync_connection)
            return (
                set(inspector.get_table_names()),
                {item["name"] for item in inspector.get_indexes("platform_articles")},
                inspector.get_foreign_keys("metric_snapshots"),
            )

        tables, indexes, foreign_key_rows = await connection.run_sync(schema_details)

    assert journal_mode.casefold() == "wal"
    assert busy_timeout == 5000
    assert foreign_keys == 1
    assert {"platform_articles", "metric_snapshots", "collection_runs"} <= tables
    assert "ux_platform_articles_platform_external_id" in indexes
    assert "ux_platform_articles_task_platform" in indexes
    assert "ux_platform_articles_event_id" in indexes
    assert foreign_key_rows[0]["referred_table"] == "platform_articles"
    await dispose_db()


@pytest.mark.asyncio
async def test_publish_capture_maps_nested_post_id_and_redacts(tmp_path):
    url = database_url(tmp_path)
    await dispose_db()
    await init_db(url)
    response = FakeResponse(
        {
            "data": {
                "post_id": 7788,
                "post_url": "https://www.xiaoheihe.cn/app/bbs/link/7788",
                "publish_time": "2026-08-12T09:42:00+08:00",
                "token": "must-not-persist",
            }
        }
    )
    page = FakePage(response)
    publisher = XiaoheihePublisher(page=page)

    event = await publisher.publish_and_capture(
        page,
        task_id=21,
        title="正式发布标题",
        trigger=no_op_trigger,
    )
    assert event.external_article_id == "7788"
    assert event.evidence["publish_response"]["data"]["token"] == "[REDACTED]"

    mapping = await PublishedEventService(url).handle(event)
    assert mapping.id is not None

    async with session_scope(url) as session:
        saved = await session.scalar(select(PlatformArticle))
        assert saved.external_article_id == "7788"
        assert saved.title == "正式发布标题"
        assert saved.published_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 8, 12, 1, 42, tzinfo=timezone.utc
        )
        assert saved.status is PlatformArticleStatus.MAPPED
        assert saved.event_id == event.event_id
        assert saved.extra_data["publish_response"]["data"]["token"] == "[REDACTED]"
        assert page.predicate(response)
    await dispose_db()


@pytest.mark.asyncio
async def test_init_db_upgrades_existing_sqlite_without_losing_data(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE platform_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                platform VARCHAR(32) NOT NULL,
                external_article_id VARCHAR(128) NOT NULL,
                platform_url VARCHAR(1024),
                status VARCHAR(16) NOT NULL,
                extra_data JSON NOT NULL,
                created_at DATETIME NOT NULL
            );
            CREATE TABLE metric_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL,
                read_count INTEGER NOT NULL,
                like_count INTEGER,
                comment_count INTEGER,
                collect_count INTEGER,
                revenue NUMERIC(18, 4),
                snapshot_time DATETIME NOT NULL,
                raw_data JSON NOT NULL,
                FOREIGN KEY(article_id) REFERENCES platform_articles(id)
            );
            INSERT INTO platform_articles (
                task_id, platform, external_article_id, platform_url,
                status, extra_data, created_at
            ) VALUES (
                88, 'xiaoheihe', 'legacy-post', NULL,
                'MAPPED', '{}', '2026-08-11 00:00:00'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    await dispose_db()
    await init_db(url)
    await init_db(url)

    engine = get_engine(url)
    async with engine.connect() as async_connection:
        platform_columns = {
            row[1]
            for row in (
                await async_connection.exec_driver_sql(
                    'PRAGMA table_info("platform_articles")'
                )
            ).all()
        }
        metric_columns = {
            row[1]
            for row in (
                await async_connection.exec_driver_sql(
                    'PRAGMA table_info("metric_snapshots")'
                )
            ).all()
        }
    assert {"event_id", "title", "published_at"} <= platform_columns
    assert {"exposure_count", "share_count"} <= metric_columns

    async with session_scope(url) as session:
        article = await session.scalar(select(PlatformArticle))
        assert article.task_id == 88
        assert article.external_article_id == "legacy-post"
        assert article.title is None
        assert article.published_at is None
    await dispose_db()


@pytest.mark.asyncio
async def test_publish_capture_is_idempotent_and_detects_conflict(tmp_path):
    url = database_url(tmp_path)
    await dispose_db()
    await init_db(url)
    page = FakePage(FakeResponse({"post_id": "same-id"}))
    publisher = XiaoheihePublisher(page=page)

    first_event = await publisher.publish_and_capture(
        page, task_id=1, trigger=no_op_trigger
    )
    second_event = await publisher.publish_and_capture(
        page, task_id=1, trigger=no_op_trigger
    )
    assert first_event.event_id == second_event.event_id

    service = PublishedEventService(url)
    first = await service.handle(first_event)
    second = await service.handle(second_event)
    assert first.id == second.id

    conflict = first_event.model_copy(
        update={"event_id": "another-event", "task_id": 2}
    )
    with pytest.raises(PublishMappingConflictError):
        await service.handle(conflict)

    reused_event_id = first_event.model_copy(
        update={"task_id": 3, "external_article_id": "other-post"}
    )
    with pytest.raises(PublishMappingConflictError):
        await service.handle(reused_event_id)
    await dispose_db()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "timeout_error", "error_code"),
    [
        (FakeResponse({}, status=503), False, "PUBLISH_RESPONSE_REJECTED"),
        (
            FakeResponse({}, json_error=ValueError("bad json")),
            False,
            "PUBLISH_RESPONSE_INVALID_JSON",
        ),
        (FakeResponse({"data": {}}), False, "PUBLISH_POST_ID_MISSING"),
        (FakeResponse({"post_id": "late"}), True, "PUBLISH_RESPONSE_TIMEOUT"),
    ],
)
async def test_publish_capture_errors_are_result_unknown(
    tmp_path,
    response,
    timeout_error,
    error_code,
):
    page = FakePage(response, timeout_error=timeout_error)
    publisher = XiaoheihePublisher(page=page)
    with pytest.raises(PublishResultUnknownError) as captured:
        await publisher.publish_and_capture(
            page, task_id=10, trigger=no_op_trigger
        )
    assert captured.value.error_code == error_code


@pytest.mark.asyncio
async def test_unverified_collector_is_blocked_before_network():
    collector = XiaoheiheCollector()
    article = PlatformArticle(
        task_id=1,
        platform="xiaoheihe",
        external_article_id="post-1",
        status=PlatformArticleStatus.MAPPED,
    )
    auth = CollectorAuth(cookies={"heybox_id": "a", "pkey": "b"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(ConfigurationError):
            await collector.fetch_raw(article, client, auth)


@pytest.mark.asyncio
async def test_collector_fetches_and_normalizes_verified_contract():
    config = verified_config()
    payload = {
        "data": {
            "stats": {
                "read": 123,
                "like": 4,
                "comment": 5,
                "collect": 6,
                "exposure": 456,
                "share": 8,
                "revenue": "7.2500",
            },
            "snapshot_time": "2026-08-11T08:30:45Z",
            "user_token": "sensitive",
        }
    }

    async def handler(request: httpx.Request):
        assert request.url.params["post_id"] == "post-1"
        return httpx.Response(200, json=payload)

    collector = XiaoheiheCollector(config=config)
    article = PlatformArticle(
        task_id=1,
        platform="xiaoheihe",
        external_article_id="post-1",
        status=PlatformArticleStatus.MAPPED,
    )
    auth = CollectorAuth(cookies={"heybox_id": "a", "pkey": "b"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        raw = await collector.fetch_raw(article, client, auth)
    metrics = collector.normalize(raw)
    assert metrics.read_count == 123
    assert metrics.exposure_count == 456
    assert metrics.share_count == 8
    assert str(metrics.revenue) == "7.2500"
    assert metrics.raw_data["data"]["user_token"] == "[REDACTED]"


def test_collector_keeps_missing_optional_metrics_as_none():
    config = verified_config()
    collector = XiaoheiheCollector(config=config)
    metrics = collector.normalize({"data": {"stats": {"read": 12}}})
    assert metrics.read_count == 12
    assert metrics.like_count is None
    assert metrics.comment_count is None
    assert metrics.collect_count is None
    assert metrics.exposure_count is None
    assert metrics.share_count is None
    assert metrics.revenue is None


def test_publisher_rejects_ambiguous_naive_platform_time():
    assert XiaoheihePublisher._parse_platform_datetime("2026-08-12 09:42:00") is None


@pytest.mark.asyncio
async def test_collector_pagination_and_rate_limit_classification():
    config = verified_config()
    config.collector.pagination = PaginationConfig(
        cursor_param="cursor",
        next_cursor_path="data.next_cursor",
        max_pages=3,
    )
    seen: list[str] = []

    def handler(request: httpx.Request):
        cursor = request.url.params.get("cursor", "")
        seen.append(cursor)
        if not cursor:
            return httpx.Response(200, json={"data": {"next_cursor": "next"}})
        return httpx.Response(200, json={"data": {"next_cursor": ""}})

    collector = XiaoheiheCollector(config=config)
    article = PlatformArticle(
        task_id=1,
        platform="xiaoheihe",
        external_article_id="post-1",
        status=PlatformArticleStatus.MAPPED,
    )
    auth = CollectorAuth(cookies={"heybox_id": "a", "pkey": "b"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload = await collector.fetch_raw(article, client, auth)
    assert seen == ["", "next"]
    assert len(payload["pages"]) == 2

    config.collector.pagination = None
    config.collector.max_attempts = 1
    collector = XiaoheiheCollector(config=config)
    transport = httpx.MockTransport(lambda _request: httpx.Response(429))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RateLimitedError):
            await collector.fetch_raw(article, client, auth)


@pytest.mark.asyncio
async def test_collect_service_writes_idempotent_snapshot_and_runs(tmp_path, monkeypatch):
    url = database_url(tmp_path)
    monkeypatch.setenv("ARTICLE_MVP_DATABASE_URL", url)
    await dispose_db()
    await init_db(url)
    async with session_scope(url) as session:
        article = PlatformArticle(
            task_id=1,
            platform="xiaoheihe",
            external_article_id="post-1",
            status=PlatformArticleStatus.MAPPED,
        )
        session.add(article)
        await session.flush()
        article_id = article.id

    payload = {
        "data": {
            "stats": {
                "read": 9,
                "like": 1,
                "comment": 2,
                "collect": 3,
                "exposure": 90,
                "share": 4,
                "revenue": None,
            },
            "snapshot_time": "2026-08-11T08:30:45Z",
        }
    }
    collector = XiaoheiheCollector(config=verified_config())
    service = CollectService(collector)
    auth = CollectorAuth(cookies={"heybox_id": "a", "pkey": "b"})
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        first = await service.collect(article_id, auth, client=client)
        second = await service.collect(article_id, auth, client=client)
    assert first.id == second.id

    async with session_scope(url) as session:
        snapshots = (await session.scalars(select(MetricSnapshot))).all()
        runs = (await session.scalars(select(CollectionRun))).all()
        assert len(snapshots) == 1
        assert snapshots[0].read_count == 9
        assert snapshots[0].exposure_count == 90
        assert snapshots[0].share_count == 4
        assert len(runs) == 2
        assert {run.status for run in runs} == {CollectionRunStatus.SUCCESS}
    await dispose_db()


@pytest.mark.asyncio
async def test_collect_service_records_session_failure(tmp_path, monkeypatch):
    url = database_url(tmp_path)
    monkeypatch.setenv("ARTICLE_MVP_DATABASE_URL", url)
    await dispose_db()
    await init_db(url)
    async with session_scope(url) as session:
        article = PlatformArticle(
            task_id=1,
            platform="xiaoheihe",
            external_article_id="post-1",
            status=PlatformArticleStatus.MAPPED,
        )
        session.add(article)
        await session.flush()
        article_id = article.id

    service = CollectService(XiaoheiheCollector(config=verified_config()))
    auth = CollectorAuth(cookies={"heybox_id": "a", "pkey": "b"})
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SessionExpiredError):
            await service.collect(article_id, auth, client=client)

    async with session_scope(url) as session:
        run = await session.scalar(select(CollectionRun))
        assert run.status is CollectionRunStatus.FATAL
        assert run.error_type == "SESSION_EXPIRED"
    await dispose_db()


def test_security_and_probe_helpers_do_not_leak_values():
    payload = {
        "data": {"stats": {"read": 10}},
        "access_token": "secret",
        "profile": {"email": "person@example.test"},
    }
    redacted = redact_sensitive(payload)
    assert extract_json_path(payload, "data.stats.read") == 10
    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["profile"]["email"] == "[REDACTED]"
    assert sanitized_url("https://example.test/a?post_id=123&token=secret") == (
        "https://example.test/a?post_id=&token="
    )


def test_new_package_has_no_legacy_runtime_imports():
    package_root = Path(__file__).resolve().parents[1] / "src" / "article_mvp"
    forbidden_roots = {"core", "models", "mcp_server", "web", "human"}
    for source_path in package_root.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        assert "text-restored" not in source
        tree = ast.parse(source, filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = {node.module.split(".", 1)[0]}
            else:
                continue
            assert not imported.intersection(forbidden_roots), (
                source_path,
                imported.intersection(forbidden_roots),
            )


def test_default_runtime_paths_are_isolated_from_legacy_data(monkeypatch):
    monkeypatch.delenv("ARTICLE_MVP_DATA_DIR", raising=False)
    data_dir = runtime_data_dir()
    assert data_dir.name == "article_mvp"
    assert data_dir.parent.name == "data"
    assert default_database_path() == data_dir / "article_mvp.db"
    assert default_database_path().name != "app.db"

    publisher = XiaoheihePublisher()
    assert publisher.profile_dir == data_dir / "profiles" / "xiaoheihe"
    assert publisher.lock_path == data_dir / "locks" / "xiaoheihe.lock"


@pytest.mark.asyncio
async def test_legacy_table_migration_is_idempotent_and_keeps_source(tmp_path):
    source = tmp_path / "legacy.db"
    destination = tmp_path / "article_mvp.db"
    source_url = f"sqlite+aiosqlite:///{source.as_posix()}"
    destination_url = f"sqlite+aiosqlite:///{destination.as_posix()}"

    await dispose_db()
    await init_db(source_url)
    async with session_scope(source_url) as session:
        article = PlatformArticle(
            task_id=101,
            platform="xiaoheihe",
            external_article_id="migrated-post",
            title="迁移文章",
            status=PlatformArticleStatus.MAPPED,
        )
        session.add(article)
        await session.flush()
        session.add(
            MetricSnapshot(
                article_id=article.id,
                read_count=88,
                snapshot_time=datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc),
            )
        )
        session.add(
            CollectionRun(
                platform="xiaoheihe",
                status=CollectionRunStatus.SUCCESS,
                articles_processed=1,
            )
        )
    await dispose_db()

    await init_db(destination_url)
    await dispose_db()
    first = migrate(source, destination)
    second = migrate(source, destination)

    assert first == {
        "platform_articles": 1,
        "metric_snapshots": 1,
        "collection_runs": 1,
    }
    assert second == {
        "platform_articles": 0,
        "metric_snapshots": 0,
        "collection_runs": 0,
    }

    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        assert source_connection.execute(
            "SELECT COUNT(*) FROM platform_articles"
        ).fetchone()[0] == 1
        assert destination_connection.execute(
            "SELECT title FROM platform_articles"
        ).fetchone()[0] == "迁移文章"
        assert destination_connection.execute(
            "SELECT read_count FROM metric_snapshots"
        ).fetchone()[0] == 88
    finally:
        source_connection.close()
        destination_connection.close()


@pytest.mark.asyncio
async def test_legacy_table_migration_rejects_different_existing_row(tmp_path):
    source = tmp_path / "legacy.db"
    destination = tmp_path / "article_mvp.db"
    for path, title in ((source, "源文章"), (destination, "目标文章")):
        url = f"sqlite+aiosqlite:///{path.as_posix()}"
        await dispose_db()
        await init_db(url)
        async with session_scope(url) as session:
            session.add(
                PlatformArticle(
                    id=1,
                    task_id=1,
                    platform="xiaoheihe",
                    external_article_id="same-id",
                    title=title,
                    status=PlatformArticleStatus.MAPPED,
                )
            )
        await dispose_db()

    with pytest.raises(RuntimeError, match="内容不同"):
        migrate(source, destination)

    source_connection = sqlite3.connect(source)
    try:
        assert source_connection.execute(
            "SELECT title FROM platform_articles WHERE id=1"
        ).fetchone()[0] == "源文章"
    finally:
        source_connection.close()


def test_dashboard_shows_safe_summary_without_raw_payloads(tmp_path):
    url = database_url(tmp_path)
    runtime = AsyncRuntime()

    app = create_dashboard_app(
        database_url=url,
        runtime=runtime,
    )

    async def seed_dashboard() -> None:
        async with session_scope(url) as session:
            article = PlatformArticle(
                task_id=91,
                platform="xiaoheihe",
                external_article_id="post-dashboard",
                title="正式文章标题",
                platform_url="https://www.xiaoheihe.cn/app/bbs/link/post-dashboard",
                status=PlatformArticleStatus.MAPPED,
                published_at=datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc),
                extra_data={"token": "must-not-be-visible"},
            )
            session.add(article)
            await session.flush()
            session.add(
                MetricSnapshot(
                    article_id=article.id,
                    read_count=320,
                    like_count=12,
                    comment_count=3,
                    collect_count=7,
                    exposure_count=640,
                    share_count=5,
                    revenue=Decimal("1.2500"),
                    snapshot_time=datetime(2026, 8, 11, 9, 30, tzinfo=timezone.utc),
                    raw_data={"cookie": "must-not-be-visible"},
                )
            )
            session.add(
                CollectionRun(
                    platform="xiaoheihe",
                    status=CollectionRunStatus.SUCCESS,
                    articles_processed=1,
                    error_message="must-not-be-visible",
                    started_at=datetime(2026, 8, 11, 9, 30, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 8, 11, 9, 31, tzinfo=timezone.utc),
                )
            )

    try:
        runtime.run(seed_dashboard())
        client = app.test_client()
        page_response = client.get("/")
        assert page_response.status_code == 200
        page_html = page_response.get_data(as_text=True)
        assert "发布与采集数据中心" in page_html
        assert "vendor/coreui/coreui.min.css" in page_html
        assert "vendor/gridstack/gridstack-all.js" in page_html
        assert 'data-module-toggle="summary"' in page_html
        assert client.get("/assets/vendor/coreui/coreui.min.css").status_code == 200
        assert client.get("/assets/vendor/gridstack/gridstack-all.js").status_code == 200
        assert client.get("/healthz").status_code == 200

        api_response = client.get("/api/dashboard")
        assert api_response.status_code == 200
        payload = api_response.get_json()
        assert payload["summary"]["total_articles"] == 1
        assert payload["summary"]["total_snapshots"] == 1
        assert payload["articles"][0]["latest_metric"]["read_count"] == 320
        assert payload["articles"][0]["title"] == "正式文章标题"
        assert payload["articles"][0]["published_at"] == "2026-08-11T08:00:00+00:00"
        assert payload["articles"][0]["latest_metric"]["exposure_count"] == 640
        assert payload["articles"][0]["latest_metric"]["share_count"] == 5
        assert payload["articles"][0]["latest_metric"]["revenue"] == "1.2500"
        assert payload["contract"]["collector_enabled"] is False
        assert "current_workflow" not in payload
        serialized = api_response.get_data(as_text=True)
        assert "must-not-be-visible" not in serialized
        assert "raw_data" not in serialized
        assert "extra_data" not in serialized
        assert "error_message" not in serialized
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_delivery_bridge_records_draft_mapping_idempotently(tmp_path: Path) -> None:
    from article_mvp.services.delivery_bridge import (
        DeliveryBridge,
        synthesize_task_id,
    )

    url = database_url(tmp_path)
    bridge = DeliveryBridge(url)
    try:
        first = await bridge.record(
            operation_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
            platform="xiaoheihe",
            mode="DRAFT",
            title="凌晨三点，公司的智能马桶开始给我做绩效面谈",
            draft_url="https://www.xiaoheihe.cn/creator/draft",
            content_reference="draft:8b2a96e6-7faa-501e-9430-1a1b3256202c",
        )
        assert first.status == PlatformArticleStatus.UNMAPPED
        assert first.platform_url == "https://www.xiaoheihe.cn/creator/draft"
        assert first.task_id == synthesize_task_id("3fa85f64-5717-4562-b3fc-2c963f66afa6")
        assert first.extra_data["mode"] == "DRAFT"
        assert first.extra_data["operation_id"] == "3fa85f64-5717-4562-b3fc-2c963f66afa6"

        second = await bridge.record(
            operation_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
            platform="xiaoheihe",
            mode="DRAFT",
            title="凌晨三点，公司的智能马桶开始给我做绩效面谈",
            draft_url="https://www.xiaoheihe.cn/creator/draft",
        )
        assert second.id == first.id
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_delivery_bridge_records_publish_mapping(tmp_path: Path) -> None:
    from article_mvp.services.delivery_bridge import (
        DeliveryBridge,
        extract_article_id,
    )

    url = database_url(tmp_path)
    bridge = DeliveryBridge(url)
    try:
        record = await bridge.record(
            operation_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
            platform="zhihu",
            mode="PUBLISH",
            title="知乎已发布文章",
            platform_url="https://zhuanlan.zhihu.com/p/123456789",
        )
        assert record.status == PlatformArticleStatus.MAPPED
        assert record.external_article_id == "123456789"
        assert record.platform_url == "https://zhuanlan.zhihu.com/p/123456789"
        assert record.event_id == "delivery:7c9e6679-7425-40de-944b-e07fc1f90ae7"
    finally:
        await dispose_db()

    assert extract_article_id("https://www.xiaoheihe.cn/bbs/post/987654") == "987654"
    assert extract_article_id("https://weibo.com/ttarticle/p/show?id=230940123") == ""
    assert extract_article_id("") == ""
