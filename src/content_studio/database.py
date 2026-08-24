"""Content Studio 独立异步数据库生命周期。"""

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

from content_studio.models import Base
from content_studio.runtime_paths import default_database_path


def sqlite_url(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{resolved.as_posix()}"


class ContentDatabase:
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
            await connection.run_sync(self._upgrade_cover_schema)
            await connection.run_sync(self._upgrade_document_schema)
            await connection.run_sync(self._upgrade_delivery_document_schema)
            await connection.run_sync(self._upgrade_plan_target_schema)

    @staticmethod
    def _upgrade_cover_schema(connection) -> None:
        """幂等补充草稿/冻结版本的封面策略与解析资产。"""

        for table in ("content_drafts", "content_versions"):
            columns = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
            if "cover_strategy" not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN cover_strategy VARCHAR(32) "
                    "NOT NULL DEFAULT 'NONE'"
                )
            if "cover_asset_id" not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN cover_asset_id VARCHAR(36)"
                )

    @staticmethod
    def _upgrade_document_schema(connection) -> None:
        """幂等补充版本化文档列；旧行保留为 v1 投影。"""

        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for table in ("content_drafts", "content_versions"):
            if table not in tables:
                continue
            columns = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
            if "content_schema_version" not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN content_schema_version INTEGER "
                    "NOT NULL DEFAULT 1"
                )
            if "document_json" not in columns:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN document_json JSON")

    @staticmethod
    def _upgrade_plan_target_schema(connection) -> None:
        """幂等补充目标级执行租约，支持已有 Content Studio 数据库。"""

        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(delivery_plan_targets)"
            ).fetchall()
        }
        if "execution_claim_id" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_plan_targets ADD COLUMN execution_claim_id VARCHAR(36)"
            )
        if "execution_claim_expires_at" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE delivery_plan_targets ADD COLUMN execution_claim_expires_at DATETIME"
            )

    @staticmethod
    def _upgrade_delivery_document_schema(connection) -> None:
        """幂等补充冻结版本的投递归一化副本。"""

        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(content_versions)"
            ).fetchall()
        }
        if "delivery_document_json" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE content_versions ADD COLUMN delivery_document_json JSON"
            )
        if "delivery_policy_version" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE content_versions ADD COLUMN delivery_policy_version VARCHAR(32)"
            )
        if "delivery_loss_report_json" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE content_versions ADD COLUMN delivery_loss_report_json JSON"
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
