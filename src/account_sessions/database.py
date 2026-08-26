"""账号会话域独立异步数据库生命周期。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from account_sessions.models import Base
from account_sessions.runtime_paths import default_database_path


def sqlite_url(path: str | Path) -> str:
    resolved = Path(path).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{resolved.as_posix()}"


class AccountDatabase:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or sqlite_url(default_database_path())
        self.engine: AsyncEngine = create_async_engine(
            self.database_url,
            pool_pre_ping=True,
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def set_pragmas(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(self._upgrade_platform_account_schema)
            await connection.run_sync(self._upgrade_delivery_operation_schema)

    @staticmethod
    def _upgrade_platform_account_schema(connection) -> None:
        """为既有账号数据库幂等补充会话健康字段。"""

        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(platform_accounts)"
            ).fetchall()
        }
        additions = (
            (
                "heartbeat_enabled",
                "ALTER TABLE platform_accounts "
                "ADD COLUMN heartbeat_enabled BOOLEAN NOT NULL DEFAULT 1",
            ),
            (
                "next_heartbeat_at",
                "ALTER TABLE platform_accounts ADD COLUMN next_heartbeat_at DATETIME",
            ),
            (
                "last_heartbeat_at",
                "ALTER TABLE platform_accounts ADD COLUMN last_heartbeat_at DATETIME",
            ),
            (
                "heartbeat_failures",
                "ALTER TABLE platform_accounts "
                "ADD COLUMN heartbeat_failures INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "last_heartbeat_error_code",
                "ALTER TABLE platform_accounts "
                "ADD COLUMN last_heartbeat_error_code VARCHAR(64)",
            ),
            (
                "heartbeat_claim_owner",
                "ALTER TABLE platform_accounts "
                "ADD COLUMN heartbeat_claim_owner VARCHAR(64)",
            ),
            (
                "heartbeat_claimed_at",
                "ALTER TABLE platform_accounts "
                "ADD COLUMN heartbeat_claimed_at DATETIME",
            ),
            (
                "heartbeat_claim_expires_at",
                "ALTER TABLE platform_accounts "
                "ADD COLUMN heartbeat_claim_expires_at DATETIME",
            ),
        )
        for column_name, statement in additions:
            if column_name not in columns:
                connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_account_heartbeat_due "
            "ON platform_accounts(status, heartbeat_enabled, next_heartbeat_at)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_account_heartbeat_claim "
            "ON platform_accounts(heartbeat_claim_expires_at, heartbeat_claim_owner)"
        )


    @staticmethod
    def _upgrade_delivery_operation_schema(connection) -> None:
        """为既有账号数据库幂等补充内容域版本引用。"""

        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(delivery_operations)"
            ).fetchall()
        }
        if "content_reference" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_operations ADD COLUMN content_reference VARCHAR(128)"
            )
        if "request_key" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_operations ADD COLUMN request_key VARCHAR(128)"
            )
        if "persist_login_snapshot" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_operations ADD COLUMN persist_login_snapshot BOOLEAN"
            )
        if "article_mapping_status" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_operations "
                "ADD COLUMN article_mapping_status VARCHAR(16) NOT NULL DEFAULT 'NOT_PENDING'"
            )
        if "article_mapping_attempts" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_operations "
                "ADD COLUMN article_mapping_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "article_mapping_error_code" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_operations "
                "ADD COLUMN article_mapping_error_code VARCHAR(64)"
            )
        if "article_mapping_last_attempt_at" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_operations "
                "ADD COLUMN article_mapping_last_attempt_at DATETIME"
            )
        if "verification_evidence" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_operations ADD COLUMN verification_evidence TEXT"
            )
        if "degraded" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_operations ADD COLUMN degraded VARCHAR(32)"
            )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_delivery_operations_request_key "
            "ON delivery_operations(request_key) WHERE request_key IS NOT NULL"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_delivery_article_mapping_status "
            "ON delivery_operations(article_mapping_status, article_mapping_last_attempt_at)"
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                async with session.begin():
                    yield session
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()
