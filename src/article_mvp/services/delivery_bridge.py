"""把统一工作台投递完成结果幂等写入 ``PlatformArticle`` 的桥接边界。

账号域的投递成功与文章映射使用两个独立数据库。调用方必须先提交账号域
``DeliveryOperation``，再调用本模块；本模块失败不会回写或降级投递结果。
所有外部文章 ID 都必须来自显式结果字段或可证明的数字路径，无法证明时
使用 ``draft:``/``unmapped:`` 内部键并保持 ``UNMAPPED``。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from article_mvp.db.database import init_db, session_scope
from article_mvp.db.models import PlatformArticle, PlatformArticleStatus
from article_mvp.errors import MvpError, PublishMappingConflictError

_NUMERIC_PATH_ID = re.compile(r"^[0-9]{1,64}$")


def synthesize_task_id(operation_id: str) -> int:
    """从投递执行单 UUID 派生稳定负 63-bit task_id，隔离旧非负任务空间。"""

    value = int.from_bytes(uuid.UUID(operation_id).bytes[:8], "big")
    return -(value % (2**63 - 1) + 1)


def extract_article_id(platform_url: str | None) -> str:
    """仅从 HTTP(S) URL 的最后一个纯数字路径段提取文章 ID。"""

    if not isinstance(platform_url, str) or not platform_url.strip():
        return ""
    try:
        parsed = urlsplit(platform_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        last = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        return last if _NUMERIC_PATH_ID.fullmatch(last) else ""
    except (TypeError, ValueError):
        return ""


def _normalize_platform_article_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MvpError("平台文章 ID 类型不受支持", error_code="ARTICLE_MAPPING_INVALID_ID")
    if isinstance(value, int) and value < 0:
        raise MvpError("平台文章 ID 不能为负数", error_code="ARTICLE_MAPPING_INVALID_ID")
    normalized = str(value).strip()
    if not normalized or normalized.startswith("-") or len(normalized) > 255:
        raise MvpError("平台文章 ID 长度不符合要求", error_code="ARTICLE_MAPPING_INVALID_ID")
    return normalized


async def _find_existing(
    session,
    *,
    task_id: int,
    platform: str,
    event_id: str | None,
    external_article_id: str,
) -> PlatformArticle | None:
    """兼容旧正 task_id：先稳定事件/外部键，再回退新负 task_id。"""

    if event_id:
        mapping = await session.scalar(
            select(PlatformArticle).where(PlatformArticle.event_id == event_id)
        )
        if mapping is not None:
            return mapping
    mapping = await session.scalar(
        select(PlatformArticle).where(
            PlatformArticle.platform == platform,
            PlatformArticle.external_article_id == external_article_id,
        )
    )
    if mapping is not None:
        return mapping
    return await session.scalar(
        select(PlatformArticle).where(
            PlatformArticle.task_id == task_id,
            PlatformArticle.platform == platform,
        )
    )


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
        platform_article_id: str | int | None = None,
        completed_at: datetime | None = None,
        content_reference: str | None = None,
    ) -> PlatformArticle:
        if mode not in {"DRAFT", "PUBLISH"}:
            raise MvpError("投递模式不受支持", error_code="ARTICLE_MAPPING_INVALID_MODE")
        await init_db(self.database_url)
        task_id = synthesize_task_id(operation_id)
        normalized_explicit_id = (
            _normalize_platform_article_id(platform_article_id)
            if platform_article_id is not None
            else ""
        )
        url_id = extract_article_id(platform_url)
        mapped = mode == "PUBLISH" and bool(normalized_explicit_id or url_id)
        external_article_id = (
            (normalized_explicit_id or url_id)
            if mapped
            else f"draft:{operation_id}" if mode == "DRAFT" else f"unmapped:{operation_id}"
        )
        event_id = f"delivery:{operation_id}" if mode == "PUBLISH" else None
        timestamp = completed_at or datetime.now(timezone.utc)
        extra: dict[str, Any] = {
            "source": "delivery",
            "mode": mode,
            "operation_id": operation_id,
            "content_reference": content_reference,
            "draft_url": draft_url,
        }
        if mode == "DRAFT":
            extra["delivery_completed_at"] = timestamp.isoformat()
        if normalized_explicit_id:
            extra["platform_article_id"] = normalized_explicit_id

        async with session_scope(self.database_url) as session:
            existing = await _find_existing(
                session,
                task_id=task_id,
                platform=platform,
                event_id=event_id,
                external_article_id=external_article_id,
            )
            if existing is not None:
                return await self._reuse_or_upgrade(
                    session,
                    existing,
                    task_id=task_id,
                    platform=platform,
                    mode=mode,
                    event_id=event_id,
                    external_article_id=external_article_id,
                    mapped=mapped,
                    title=title,
                    platform_url=platform_url,
                    timestamp=timestamp,
                    extra=extra,
                )

            mapping = PlatformArticle(
                task_id=task_id,
                platform=platform,
                external_article_id=external_article_id,
                event_id=event_id,
                title=title or None,
                platform_url=platform_url if mode == "PUBLISH" else draft_url,
                published_at=timestamp if mode == "PUBLISH" else None,
                status=(
                    PlatformArticleStatus.MAPPED
                    if mapped
                    else PlatformArticleStatus.UNMAPPED
                ),
                extra_data=extra,
            )
            try:
                async with session.begin_nested():
                    session.add(mapping)
                    await session.flush()
            except IntegrityError as exc:
                existing = await _find_existing(
                    session,
                    task_id=task_id,
                    platform=platform,
                    event_id=event_id,
                    external_article_id=external_article_id,
                )
                if existing is None:
                    raise PublishMappingConflictError(
                        "写入平台文章映射时发生唯一性冲突"
                    ) from exc
                return await self._reuse_or_upgrade(
                    session,
                    existing,
                    task_id=task_id,
                    platform=platform,
                    mode=mode,
                    event_id=event_id,
                    external_article_id=external_article_id,
                    mapped=mapped,
                    title=title,
                    platform_url=platform_url,
                    timestamp=timestamp,
                    extra=extra,
                )
            return mapping

    async def _reuse_or_upgrade(
        self,
        session,
        existing: PlatformArticle,
        *,
        task_id: int,
        platform: str,
        mode: str,
        event_id: str | None,
        external_article_id: str,
        mapped: bool,
        title: str,
        platform_url: str | None,
        timestamp: datetime,
        extra: dict[str, Any],
    ) -> PlatformArticle:
        same_event = bool(event_id and existing.event_id == event_id)
        same_external = existing.external_article_id == external_article_id
        same_task = existing.task_id == task_id and existing.platform == platform
        if same_event:
            if existing.platform != platform:
                raise PublishMappingConflictError("事件 ID 已关联到另一平台")
            if same_external:
                if mode == "DRAFT":
                    # 历史桥接曾把草稿完成时间写入 published_at；同一执行单
                    # 再次幂等回放时也要纠正这一语义，不能继续冒充发布时间。
                    existing.published_at = None
                    existing.extra_data = {
                        **(existing.extra_data or {}),
                        **extra,
                    }
                    await session.flush()
                return existing
            if not (
                mapped
                and existing.status == PlatformArticleStatus.UNMAPPED
                and event_id is not None
                and existing.external_article_id == (
                    "unmapped:" + event_id.removeprefix("delivery:")
                )
            ):
                raise PublishMappingConflictError("同一事件已关联到另一平台文章 ID")
        elif same_external:
            # DRAFT 的内部外部键包含 operation_id，可以兼容旧正 task_id；
            # PUBLISH 的真实外部 ID 则不能被另一 event 静默复用。
            if event_id is not None or existing.event_id is not None:
                raise PublishMappingConflictError("该平台文章 ID 已属于另一发布任务")
            if existing.platform != platform:
                raise PublishMappingConflictError("平台文章映射已属于另一平台")
            if mode == "DRAFT":
                existing.published_at = None
                existing.extra_data = {**(existing.extra_data or {}), **extra}
                await session.flush()
            return existing
        elif same_task:
            raise PublishMappingConflictError("同一任务已映射到另一篇平台文章")
        else:
            raise PublishMappingConflictError("平台文章映射已属于另一执行单")
        if mapped and existing.status == PlatformArticleStatus.UNMAPPED and same_event:
            conflict = await session.scalar(
                select(PlatformArticle).where(
                    PlatformArticle.platform == platform,
                    PlatformArticle.external_article_id == external_article_id,
                    PlatformArticle.id != existing.id,
                )
            )
            if conflict is not None:
                raise PublishMappingConflictError("该平台文章 ID 已属于另一发布任务")
            existing.external_article_id = external_article_id
            existing.status = PlatformArticleStatus.MAPPED
            existing.title = title or existing.title
            existing.platform_url = platform_url or existing.platform_url
            existing.published_at = timestamp
            existing.extra_data = {**(existing.extra_data or {}), **extra}
            await session.flush()
        return existing
