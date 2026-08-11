"""SQLite + aiosqlite 的惰性异步 Engine 与 Session 管理。"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from article_mvp.db.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_database_url: str | None = None
_runtime_lock = Lock()


def default_database_url() -> str:
    configured = os.getenv("ARTICLE_MVP_DATABASE_URL")
    if configured:
        return configured.strip()
    project_root = Path(__file__).resolve().parents[3]
    data_dir = Path(os.getenv("ARTICLE_MVP_DATA_DIR", project_root / "data"))
    database_path = (data_dir / "app.db").resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{database_path.as_posix()}"


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def get_engine(database_url: str | None = None) -> AsyncEngine:
    """首次调用时创建 Engine；运行中禁止悄然切换数据库。"""

    global _engine, _session_factory, _database_url
    requested_url = database_url or default_database_url()
    with _runtime_lock:
        if _engine is not None:
            if requested_url != _database_url:
                raise RuntimeError("数据库已初始化；切换 URL 前必须先 await dispose_db()")
            return _engine
        _engine = create_async_engine(requested_url, pool_pre_ping=True)
        _install_sqlite_pragmas(_engine)
        _session_factory = async_sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        _database_url = requested_url
        return _engine


def get_session_factory(
    database_url: str | None = None,
) -> async_sessionmaker[AsyncSession]:
    get_engine(database_url)
    if _session_factory is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("异步 Session Factory 初始化失败")
    return _session_factory


async def init_db(database_url: str | None = None) -> None:
    engine = get_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope(
    database_url: str | None = None,
) -> AsyncIterator[AsyncSession]:
    factory = get_session_factory(database_url)
    async with factory() as session:
        try:
            async with session.begin():
                yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_db() -> None:
    global _engine, _session_factory, _database_url
    with _runtime_lock:
        engine = _engine
        _engine = None
        _session_factory = None
        _database_url = None
    if engine is not None:
        await engine.dispose()
