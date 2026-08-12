"""SQLAlchemy 2.0 异步 ORM 使用的数据模型。"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class PlatformArticleStatus(str, Enum):
    MAPPED = "MAPPED"
    UNMAPPED = "UNMAPPED"
    DELETED = "DELETED"


class CollectionRunStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL_FAIL = "PARTIAL_FAIL"
    FATAL = "FATAL"


def string_enum(enum_type: type[Enum], *, name: str, length: int) -> SqlEnum:
    return SqlEnum(
        enum_type,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
        length=length,
    )


class PlatformArticle(Base):
    __tablename__ = "platform_articles"
    __table_args__ = (
        Index(
            "ux_platform_articles_platform_external_id",
            "platform",
            "external_article_id",
            unique=True,
        ),
        Index(
            "ux_platform_articles_task_platform",
            "task_id",
            "platform",
            unique=True,
        ),
        Index("ux_platform_articles_event_id", "event_id", unique=True),
        Index("ix_platform_articles_platform_status", "platform", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_article_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status: Mapped[PlatformArticleStatus] = mapped_column(
        string_enum(
            PlatformArticleStatus,
            name="platform_article_status",
            length=16,
        ),
        default=PlatformArticleStatus.MAPPED,
        nullable=False,
    )
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    snapshots: Mapped[list["MetricSnapshot"]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "article_id",
            "snapshot_time",
            name="uq_metric_snapshots_article_time",
        ),
        CheckConstraint("read_count >= 0", name="ck_metric_snapshots_read_nonnegative"),
        CheckConstraint(
            "like_count IS NULL OR like_count >= 0",
            name="ck_metric_snapshots_like_nonnegative",
        ),
        CheckConstraint(
            "comment_count IS NULL OR comment_count >= 0",
            name="ck_metric_snapshots_comment_nonnegative",
        ),
        CheckConstraint(
            "collect_count IS NULL OR collect_count >= 0",
            name="ck_metric_snapshots_collect_nonnegative",
        ),
        CheckConstraint(
            "exposure_count IS NULL OR exposure_count >= 0",
            name="ck_metric_snapshots_exposure_nonnegative",
        ),
        CheckConstraint(
            "share_count IS NULL OR share_count >= 0",
            name="ck_metric_snapshots_share_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("platform_articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    read_count: Mapped[int] = mapped_column(Integer, nullable=False)
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collect_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exposure_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    share_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    snapshot_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    article: Mapped[PlatformArticle] = relationship(back_populates="snapshots")


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    __table_args__ = (
        Index("ix_collection_runs_platform_started", "platform", "started_at"),
        CheckConstraint(
            "articles_processed >= 0",
            name="ck_collection_runs_processed_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[CollectionRunStatus] = mapped_column(
        string_enum(CollectionRunStatus, name="collection_run_status", length=16),
        nullable=False,
    )
    articles_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
