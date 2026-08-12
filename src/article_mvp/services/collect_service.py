"""单文章采集、快照幂等写入与运行记录编排。"""

from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from article_mvp.contracts import CollectorAuth, MetricValues
from article_mvp.db.database import init_db, session_scope
from article_mvp.db.models import (
    CollectionRun,
    CollectionRunStatus,
    MetricSnapshot,
    PlatformArticle,
    PlatformArticleStatus,
)
from article_mvp.errors import CollectorError
from article_mvp.platforms.xiaoheihe.collector import XiaoheiheCollector


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CollectService:
    def __init__(self, collector: XiaoheiheCollector) -> None:
        self.collector = collector

    async def collect(
        self,
        article_id: int,
        auth: CollectorAuth,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> MetricSnapshot:
        await init_db()
        started_at = utc_now()
        try:
            async with session_scope() as session:
                article = await session.get(PlatformArticle, article_id)
                if article is None:
                    raise CollectorError(
                        f"平台文章不存在: {article_id}",
                        error_code="ARTICLE_NOT_FOUND",
                    )
                if article.status is not PlatformArticleStatus.MAPPED:
                    raise CollectorError(
                        f"平台文章状态不可采集: {article.status.value}",
                        error_code="ARTICLE_NOT_MAPPED",
                    )

            if client is None:
                async with httpx.AsyncClient(follow_redirects=False) as owned_client:
                    raw = await self.collector.fetch_raw(article, owned_client, auth)
            else:
                raw = await self.collector.fetch_raw(article, client, auth)
            values = self.collector.normalize(raw)
            return await self._persist_success(article_id, values, started_at)
        except Exception as exc:
            await self._persist_failure(started_at, exc)
            raise

    async def _persist_success(
        self,
        article_id: int,
        values: MetricValues,
        started_at: datetime,
    ) -> MetricSnapshot:
        snapshot_time = values.snapshot_time or utc_now()
        if snapshot_time.tzinfo is None:
            snapshot_time = snapshot_time.replace(tzinfo=timezone.utc)
        snapshot_time = snapshot_time.astimezone(timezone.utc).replace(second=0, microsecond=0)

        async with session_scope() as session:
            existing = await session.scalar(
                select(MetricSnapshot).where(
                    MetricSnapshot.article_id == article_id,
                    MetricSnapshot.snapshot_time == snapshot_time,
                )
            )
            if existing is None:
                existing = MetricSnapshot(
                    article_id=article_id,
                    read_count=values.read_count,
                    like_count=values.like_count,
                    comment_count=values.comment_count,
                    collect_count=values.collect_count,
                    exposure_count=values.exposure_count,
                    share_count=values.share_count,
                    revenue=values.revenue,
                    snapshot_time=snapshot_time,
                    raw_data=values.raw_data,
                )
                session.add(existing)
                await session.flush()
            session.add(
                CollectionRun(
                    platform="xiaoheihe",
                    status=CollectionRunStatus.SUCCESS,
                    articles_processed=1,
                    started_at=started_at,
                    finished_at=utc_now(),
                )
            )
            await session.flush()
            return existing

    async def _persist_failure(self, started_at: datetime, exc: Exception) -> None:
        error_code = getattr(exc, "error_code", "FATAL")
        safe_message = str(exc)[:1000]
        try:
            async with session_scope() as session:
                session.add(
                    CollectionRun(
                        platform="xiaoheihe",
                        status=CollectionRunStatus.FATAL,
                        articles_processed=0,
                        error_type=error_code,
                        error_message=safe_message,
                        started_at=started_at,
                        finished_at=utc_now(),
                    )
                )
        except Exception:
            # 不使用采集记录写入失败覆盖原始平台异常。
            return
