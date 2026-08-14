"""把统一工作台投递完成结果幂等写入 PlatformArticle 的桥接边界。

旧链路（article_mvp）的 ``task_id`` 空间来自旧发布任务；统一工作台的
投递执行单是 UUID，两者不可混用。桥接从投递执行单 ID 派生一个稳定的
非负整数作为 ``task_id``（与旧空间隔离），并把投递结果幂等映射为
``PlatformArticle``：

- PUBLISH：走 ``PublishedEventService``（ArticlePublished 事件语义）。
- DRAFT：记录为 ``UNMAPPED``（草稿尚无平台文章 ID），``platform_url``
  保存平台草稿箱 URL，``extra_data`` 记录执行单与内容引用。

桥接失败不改变投递执行单本身的成功状态（best-effort），但会如实记录日志。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from article_mvp.contracts import ArticlePublished
from article_mvp.db.database import init_db, session_scope
from article_mvp.db.models import PlatformArticle, PlatformArticleStatus
from article_mvp.services.published_event_service import PublishedEventService


def synthesize_task_id(operation_id: str) -> int:
    """从投递执行单 UUID 派生稳定非负 task_id（与旧系统任务空间隔离）。"""

    return int.from_bytes(uuid.UUID(operation_id).bytes[:8], "big") % (2**31)


def extract_article_id(platform_url: str) -> str:
    """尽力从平台文章 URL 提取文章 ID（最后一段数字路径）；失败返回空串。"""

    try:
        path = urlsplit(platform_url).path.rstrip("/")
        last = path.rsplit("/", 1)[-1] if path else ""
        if last.isdigit():
            return last
    except Exception:  # noqa: BLE001
        pass
    return ""


class DeliveryBridge:
    """将 delivery_service 的完成结果映射到 PlatformArticle（幂等）。"""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url

    async def record(
        self,
        *,
        operation_id: str,
        platform: str,
        mode: str,
        title: str,
        draft_url: str | None = None,
        platform_url: str | None = None,
        completed_at: datetime | None = None,
        content_reference: str | None = None,
    ) -> PlatformArticle:
        await init_db(self.database_url)
        task_id = synthesize_task_id(operation_id)
        extra = {
            "source": "delivery",
            "mode": mode,
            "operation_id": operation_id,
            "content_reference": content_reference,
            "draft_url": draft_url,
        }
        if mode == "PUBLISH" and platform_url:
            article_id = extract_article_id(platform_url) or f"publish:{operation_id}"
            event = ArticlePublished(
                event_id=f"delivery:{operation_id}",
                task_id=task_id,
                platform=platform,
                external_article_id=article_id,
                title=title or None,
                platform_url=platform_url,
                published_at=completed_at or datetime.now(timezone.utc),
                evidence=extra,
            )
            return await PublishedEventService(self.database_url).handle(event)

        # DRAFT：草稿尚无平台文章 ID，记录为 UNMAPPED + 草稿箱 URL
        async with session_scope(self.database_url) as session:
            existing = await session.scalar(
                select(PlatformArticle).where(
                    PlatformArticle.task_id == task_id,
                    PlatformArticle.platform == platform,
                )
            )
            if existing is not None:
                return existing
            mapping = PlatformArticle(
                task_id=task_id,
                platform=platform,
                external_article_id=f"draft:{operation_id}",
                event_id=None,
                title=title or None,
                platform_url=draft_url,
                published_at=completed_at or datetime.now(timezone.utc),
                status=PlatformArticleStatus.UNMAPPED,
                extra_data=extra,
            )
            session.add(mapping)
            try:
                await session.flush()
            except IntegrityError:
                existing = await session.scalar(
                    select(PlatformArticle).where(
                        PlatformArticle.task_id == task_id,
                        PlatformArticle.platform == platform,
                    )
                )
                if existing is not None:
                    return existing
                raise
            return mapping
