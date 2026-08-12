"""看板只读查询；不返回 raw_data、extra_data 或认证材料。"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select

from article_mvp.config import load_platform_config
from article_mvp.db.database import session_scope
from article_mvp.db.models import CollectionRun, MetricSnapshot, PlatformArticle


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


class DashboardQueryService:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url

    async def fetch(self) -> dict[str, Any]:
        config = load_platform_config()
        latest_times = (
            select(
                MetricSnapshot.article_id.label("article_id"),
                func.max(MetricSnapshot.snapshot_time).label("snapshot_time"),
            )
            .group_by(MetricSnapshot.article_id)
            .subquery()
        )
        article_statement = (
            select(PlatformArticle, MetricSnapshot)
            .outerjoin(latest_times, latest_times.c.article_id == PlatformArticle.id)
            .outerjoin(
                MetricSnapshot,
                and_(
                    MetricSnapshot.article_id == latest_times.c.article_id,
                    MetricSnapshot.snapshot_time == latest_times.c.snapshot_time,
                ),
            )
            .order_by(PlatformArticle.created_at.desc())
            .limit(100)
        )

        async with session_scope(self.database_url) as session:
            article_rows = (await session.execute(article_statement)).all()
            run_rows = (
                await session.scalars(
                    select(CollectionRun)
                    .order_by(CollectionRun.started_at.desc())
                    .limit(50)
                )
            ).all()
            total_articles = int(
                await session.scalar(select(func.count()).select_from(PlatformArticle)) or 0
            )
            total_snapshots = int(
                await session.scalar(select(func.count()).select_from(MetricSnapshot)) or 0
            )

        articles = [
            {
                "id": article.id,
                "task_id": article.task_id,
                "platform": article.platform,
                "external_article_id": article.external_article_id,
                "title": article.title,
                "platform_url": article.platform_url,
                "status": article.status.value,
                "created_at": iso_utc(article.created_at),
                "published_at": iso_utc(article.published_at),
                "latest_metric": None
                if snapshot is None
                else {
                    "read_count": snapshot.read_count,
                    "like_count": snapshot.like_count,
                    "comment_count": snapshot.comment_count,
                    "collect_count": snapshot.collect_count,
                    "exposure_count": snapshot.exposure_count,
                    "share_count": snapshot.share_count,
                    "revenue": decimal_text(snapshot.revenue),
                    "snapshot_time": iso_utc(snapshot.snapshot_time),
                },
            }
            for article, snapshot in article_rows
        ]
        runs = [
            {
                "id": run.id,
                "platform": run.platform,
                "status": run.status.value,
                "articles_processed": run.articles_processed,
                "error_type": run.error_type,
                "started_at": iso_utc(run.started_at),
                "finished_at": iso_utc(run.finished_at),
            }
            for run in run_rows
        ]
        latest_run = runs[0] if runs else None
        return {
            "summary": {
                "total_articles": total_articles,
                "mapped_articles": sum(1 for item in articles if item["status"] == "MAPPED"),
                "total_snapshots": total_snapshots,
                "latest_run_status": latest_run["status"] if latest_run else None,
                "latest_run_at": latest_run["started_at"] if latest_run else None,
            },
            "contract": {
                "publish_evidence": config.publish.submit_endpoint.evidence.level.value,
                "publish_source": config.publish.submit_endpoint.evidence.source,
                "collector_evidence": config.collector.endpoint.evidence.level.value,
                "collector_source": config.collector.endpoint.evidence.source,
                "collector_enabled": (
                    config.collector.endpoint.evidence.level.value == "verified"
                ),
            },
            "articles": articles,
            "runs": runs,
        }
