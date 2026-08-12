"""ArticlePublished 事件的幂等持久化边界。"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from article_mvp.contracts import ArticlePublished
from article_mvp.db.database import init_db, session_scope
from article_mvp.db.models import PlatformArticle, PlatformArticleStatus
from article_mvp.errors import (
    PublishMappingConflictError,
    PublishResultUnknownError,
)


class PublishedEventService:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url

    async def handle(self, event: ArticlePublished) -> PlatformArticle:
        await init_db(self.database_url)
        try:
            async with session_scope(self.database_url) as session:
                event_mapping = await session.scalar(
                    select(PlatformArticle).where(
                        PlatformArticle.event_id == event.event_id
                    )
                )
                if event_mapping is not None:
                    if (
                        event_mapping.task_id == event.task_id
                        and event_mapping.platform == event.platform
                        and event_mapping.external_article_id
                        == event.external_article_id
                    ):
                        return event_mapping
                    raise PublishMappingConflictError(
                        "事件 ID 已关联到另一篇平台文章，禁止覆盖"
                    )

                task_mapping = await session.scalar(
                    select(PlatformArticle).where(
                        PlatformArticle.task_id == event.task_id,
                        PlatformArticle.platform == event.platform,
                    )
                )
                if task_mapping is not None:
                    if (
                        task_mapping.external_article_id == event.external_article_id
                        and task_mapping.event_id in (None, event.event_id)
                    ):
                        if task_mapping.event_id is None:
                            task_mapping.event_id = event.event_id
                            await session.flush()
                        return task_mapping
                    raise PublishMappingConflictError(
                        "同一任务已映射到不同的平台文章 ID，禁止覆盖"
                    )

                external_mapping = await session.scalar(
                    select(PlatformArticle).where(
                        PlatformArticle.platform == event.platform,
                        PlatformArticle.external_article_id == event.external_article_id,
                    )
                )
                if external_mapping is not None:
                    if (
                        external_mapping.task_id == event.task_id
                        and external_mapping.event_id in (None, event.event_id)
                    ):
                        if external_mapping.event_id is None:
                            external_mapping.event_id = event.event_id
                            await session.flush()
                        return external_mapping
                    raise PublishMappingConflictError(
                        "该平台文章 ID 已属于另一发布任务，禁止重复关联"
                    )

                mapping = PlatformArticle(
                    task_id=event.task_id,
                    platform=event.platform,
                    external_article_id=event.external_article_id,
                    event_id=event.event_id,
                    title=event.title,
                    platform_url=event.platform_url,
                    published_at=event.published_at,
                    status=PlatformArticleStatus.MAPPED,
                    extra_data={
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "event_occurred_at": event.occurred_at.isoformat(),
                        **event.evidence,
                    },
                )
                session.add(mapping)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise PublishMappingConflictError(
                        "写入平台文章映射时发生唯一性冲突"
                    ) from exc
                return mapping
        except PublishMappingConflictError:
            raise
        except Exception as exc:
            raise PublishResultUnknownError(
                f"平台已返回文章 ID，但映射写入失败: {exc}",
                error_code="PUBLISH_MAPPING_PERSISTENCE_FAILED",
            ) from exc
