"""Content Studio SQLAlchemy 2.0 模型。"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ContentDraft(Base):
    __tablename__ = "content_drafts"
    __table_args__ = (Index("ix_content_draft_status_updated", "status", "updated_at"),)

    draft_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    seed_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    blocks_json: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    content_schema_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    document_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cover_strategy: Mapped[str] = mapped_column(
        String(32), default="NONE", server_default="NONE", nullable=False
    )
    cover_asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    assets: Mapped[list["ContentAsset"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )
    targets: Mapped[list["DraftTarget"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )
    versions: Mapped[list["ContentVersion"]] = relationship(back_populates="draft")


class ContentAsset(Base):
    __tablename__ = "content_assets"
    __table_args__ = (
        Index("ix_content_asset_draft_created", "draft_id", "created_at"),
        Index("ix_content_asset_sha256", "sha256"),
    )

    asset_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.draft_id", ondelete="CASCADE"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    draft: Mapped[ContentDraft] = relationship(back_populates="assets")


class DraftTarget(Base):
    __tablename__ = "draft_targets"
    __table_args__ = (
        UniqueConstraint("draft_id", "account_id", name="uq_draft_target_account"),
        Index("ix_draft_target_draft_position", "draft_id", "position"),
    )

    target_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.draft_id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    persist_login: Mapped[bool] = mapped_column(default=True, nullable=False)
    # 平台选择属于目标而非内容版本；这些字段保持 nullable 以兼容旧草稿。
    platform_selection: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    platform_selection_options: Mapped[dict | list | None] = mapped_column(
        JSON, nullable=True
    )
    platform_selection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform_selection_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    draft: Mapped[ContentDraft] = relationship(back_populates="targets")


class ContentVersion(Base):
    __tablename__ = "content_versions"
    __table_args__ = (
        UniqueConstraint("draft_id", "content_hash", name="uq_draft_content_hash"),
        Index("ix_content_version_draft_created", "draft_id", "created_at"),
    )

    version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.draft_id", ondelete="RESTRICT"), nullable=False
    )
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    blocks_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    content_schema_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    document_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    delivery_document_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    delivery_policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delivery_loss_report_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cover_strategy: Mapped[str] = mapped_column(
        String(32), default="NONE", server_default="NONE", nullable=False
    )
    cover_asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    draft: Mapped[ContentDraft] = relationship(back_populates="versions")
    plans: Mapped[list["DeliveryPlan"]] = relationship(back_populates="version")


class DeliveryPlan(Base):
    __tablename__ = "delivery_plans"
    __table_args__ = (
        Index("ix_delivery_plan_draft_created", "draft_id", "created_at"),
        Index("ix_delivery_plan_status_updated", "status", "updated_at"),
    )

    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.draft_id", ondelete="RESTRICT"), nullable=False
    )
    version_id: Mapped[str] = mapped_column(
        ForeignKey("content_versions.version_id", ondelete="RESTRICT"), nullable=False
    )
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="READY", nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    version: Mapped[ContentVersion] = relationship(back_populates="plans")
    targets: Mapped[list["DeliveryPlanTarget"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class DeliveryPlanTarget(Base):
    __tablename__ = "delivery_plan_targets"
    __table_args__ = (
        UniqueConstraint("plan_id", "source_target_id", name="uq_plan_source_target"),
        Index("ix_plan_target_plan_position", "plan_id", "position"),
        Index("ix_plan_target_operation", "operation_id"),
    )

    target_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("delivery_plans.plan_id", ondelete="CASCADE"), nullable=False
    )
    source_target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    persist_login: Mapped[bool] = mapped_column(nullable=False)
    # 计划创建时复制的不可变目标选择；不写入 ContentVersion。
    platform_selection: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    platform_selection_options: Mapped[dict | list | None] = mapped_column(
        JSON, nullable=True
    )
    platform_selection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform_selection_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="READY", nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(36))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    degraded: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    verification_evidence: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    execution_claim_id: Mapped[str | None] = mapped_column(String(36))
    execution_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    plan: Mapped[DeliveryPlan] = relationship(back_populates="targets")
