"""账号会话域 SQLAlchemy 2.0 模型。"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "platform_user_id",
            name="uq_account_platform_user",
        ),
        UniqueConstraint("profile_path", name="uq_account_profile_path"),
        Index("ix_account_platform_status", "platform", "status", "session_status"),
        Index("ix_account_heartbeat_due", "status", "heartbeat_enabled", "next_heartbeat_at"),
        Index(
            "ix_account_heartbeat_claim",
            "heartbeat_claim_expires_at",
            "heartbeat_claim_owner",
        ),
    )

    account_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    is_legacy_profile: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    session_status: Mapped[str] = mapped_column(String(24), default="UNVERIFIED", nullable=False)
    persist_login: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    heartbeat_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    next_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    last_heartbeat_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    heartbeat_claim_owner: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    heartbeat_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    operations: Mapped[list["DeliveryOperation"]] = relationship(back_populates="account")


class DeliveryOperation(Base):
    __tablename__ = "delivery_operations"
    __table_args__ = (
        Index("ix_delivery_account_created", "account_id", "created_at"),
        Index("ix_delivery_status_created", "status", "created_at"),
        Index(
            "ix_delivery_article_mapping_status",
            "article_mapping_status",
            "article_mapping_last_attempt_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("platform_accounts.account_id"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    persist_login_snapshot: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    content_version: Mapped[str] = mapped_column(String(64), nullable=False)
    account_display_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", nullable=False)
    draft_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    platform_article_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_evidence: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    degraded: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default=None
    )
    article_mapping_status: Mapped[str] = mapped_column(
        String(16),
        default="NOT_PENDING",
        server_default="NOT_PENDING",
        nullable=False,
    )
    article_mapping_attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    article_mapping_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    article_mapping_last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmation_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[PlatformAccount] = relationship(back_populates="operations")


class AccountActivity(Base):
    __tablename__ = "account_activity"
    __table_args__ = (
        Index("ix_activity_account_created", "account_id", "created_at"),
        Index("ix_activity_operation_created", "operation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("platform_accounts.account_id"), nullable=False
    )
    operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="INFO", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class PublishConfirmation(Base):
    __tablename__ = "publish_confirmations"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("platform_accounts.account_id"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
