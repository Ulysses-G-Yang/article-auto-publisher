from __future__ import annotations

# unittest discover 不读取 pytest.ini 的 pythonpath；以下引导代码必须先于
# article_mvp 导入执行，因此本文件有意忽略 E402。
# ruff: noqa: E402, I001

import ast
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
from article_mvp.security import extract_json_path, redact_sensitive
from article_mvp.services.collect_service import CollectService
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
                "token": "must-not-persist",
            }
        }
    )
    page = FakePage(response)
    publisher = XiaoheihePublisher(page=page)

    async with session_scope(url) as session:
        mapping = await publisher.publish_and_capture(
            page,
            session,
            task_id=21,
            trigger=no_op_trigger,
        )
        assert mapping.id is not None

    async with session_scope(url) as session:
        saved = await session.scalar(select(PlatformArticle))
        assert saved.external_article_id == "7788"
        assert saved.status is PlatformArticleStatus.MAPPED
        assert saved.extra_data["publish_response"]["data"]["token"] == "[REDACTED]"
        assert page.predicate(response)
    await dispose_db()


@pytest.mark.asyncio
async def test_publish_capture_is_idempotent_and_detects_conflict(tmp_path):
    url = database_url(tmp_path)
    await dispose_db()
    await init_db(url)
    page = FakePage(FakeResponse({"post_id": "same-id"}))
    publisher = XiaoheihePublisher(page=page)

    async with session_scope(url) as session:
        first = await publisher.publish_and_capture(
            page, session, task_id=1, trigger=no_op_trigger
        )
        second = await publisher.publish_and_capture(
            page, session, task_id=1, trigger=no_op_trigger
        )
        assert first is second

    async with session_scope(url) as session:
        with pytest.raises(PublishMappingConflictError):
            await publisher.publish_and_capture(
                page, session, task_id=2, trigger=no_op_trigger
            )
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
    url = database_url(tmp_path)
    await dispose_db()
    await init_db(url)
    page = FakePage(response, timeout_error=timeout_error)
    publisher = XiaoheihePublisher(page=page)
    async with session_scope(url) as session:
        with pytest.raises(PublishResultUnknownError) as captured:
            await publisher.publish_and_capture(
                page, session, task_id=10, trigger=no_op_trigger
            )
        assert captured.value.error_code == error_code
    await dispose_db()


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
    assert str(metrics.revenue) == "7.2500"
    assert metrics.raw_data["data"]["user_token"] == "[REDACTED]"


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
            "stats": {"read": 9, "like": 1, "comment": 2, "collect": 3, "revenue": None},
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


def test_dashboard_shows_safe_summary_without_raw_payloads(tmp_path):
    url = database_url(tmp_path)
    runtime = AsyncRuntime()

    def current_workflow_provider():
        return {
            "available": True,
            "summary": {
                "total_articles": 19,
                "total_tasks": 24,
                "xiaoheihe_tasks": 10,
                "saved_drafts": 17,
            },
            "tasks": [
                {
                    "id": 7,
                    "platform": "xiaoheihe",
                    "status": "completed",
                    "article_title": "现役任务",
                    "title_used": "安全标题",
                    "created_at": "2026-08-11T09:00:00",
                }
            ],
        }

    app = create_dashboard_app(
        database_url=url,
        runtime=runtime,
        current_workflow_provider=current_workflow_provider,
    )

    async def seed_dashboard() -> None:
        async with session_scope(url) as session:
            article = PlatformArticle(
                task_id=91,
                platform="xiaoheihe",
                external_article_id="post-dashboard",
                platform_url="https://www.xiaoheihe.cn/app/bbs/link/post-dashboard",
                status=PlatformArticleStatus.MAPPED,
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
        assert payload["articles"][0]["latest_metric"]["revenue"] == "1.2500"
        assert payload["contract"]["collector_enabled"] is False
        assert payload["current_workflow"]["summary"]["total_tasks"] == 24
        assert payload["current_workflow"]["tasks"][0]["article_title"] == "现役任务"
        serialized = api_response.get_data(as_text=True)
        assert "must-not-be-visible" not in serialized
        assert "raw_data" not in serialized
        assert "extra_data" not in serialized
        assert "error_message" not in serialized
    finally:
        runtime.close()
