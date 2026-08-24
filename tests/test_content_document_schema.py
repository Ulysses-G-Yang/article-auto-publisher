"""Content Studio v2 文档列迁移和 SQLite 生命周期验收。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from content_studio.content_document import ContentDocument
from content_studio.database import ContentDatabase, sqlite_url
from content_studio.models import ContentDraft, ContentVersion


def run(coroutine):
    return asyncio.run(coroutine)


def valid_document() -> dict:
    return ContentDocument(
        schema_version=2,
        title="schema test",
        blocks=[
            {
                "kind": "paragraph",
                "children": [
                    {
                        "kind": "image",
                        "asset_id": "00000000-0000-4000-8000-000000000001",
                    }
                ],
            }
        ],
    ).model_dump(mode="json", exclude_none=True)


def make_old_cover_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE content_drafts (
                draft_id TEXT PRIMARY KEY,
                seed_key TEXT UNIQUE,
                source_type TEXT NOT NULL,
                source_ref TEXT,
                title TEXT NOT NULL,
                blocks_json JSON NOT NULL,
                cover_strategy VARCHAR(32) NOT NULL DEFAULT 'NONE',
                cover_asset_id VARCHAR(36),
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE content_versions (
                version_id TEXT PRIMARY KEY,
                draft_id TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                title TEXT NOT NULL,
                blocks_json JSON NOT NULL,
                cover_strategy VARCHAR(32) NOT NULL DEFAULT 'NONE',
                cover_asset_id VARCHAR(36),
                created_at DATETIME NOT NULL
            );
            INSERT INTO content_drafts VALUES
                ('draft-old', NULL, 'DOCX', 'old.docx', '旧标题', '[]',
                 'EXPLICIT', '00000000-0000-4000-8000-000000000001',
                 'ACTIVE', 2, '2026-08-01', '2026-08-01');
            INSERT INTO content_versions VALUES
                ('version-old', 'draft-old', 2, 'old-hash', '旧标题', '[]',
                 'EXPLICIT', '00000000-0000-4000-8000-000000000001', '2026-08-01');
            """
        )


def test_old_cover_schema_migrates_document_columns_idempotently_and_preserves_values(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-content.db"
    make_old_cover_database(database_path)
    database = ContentDatabase(sqlite_url(database_path))

    async def scenario():
        await database.initialize()
        await database.initialize()
        await database.dispose()
        restarted = ContentDatabase(sqlite_url(database_path))
        try:
            await restarted.initialize()
            async with restarted.session() as session:
                return await session.get(ContentDraft, "draft-old"), await session.get(
                    ContentVersion, "version-old"
                )
        finally:
            await restarted.dispose()

    try:
        draft, version = run(scenario())
        assert draft.content_schema_version == 1
        assert draft.document_json is None
        assert draft.cover_strategy == "EXPLICIT"
        assert draft.cover_asset_id == "00000000-0000-4000-8000-000000000001"
        assert version.content_schema_version == 1
        assert version.document_json is None
        assert version.delivery_document_json is None
        assert version.delivery_policy_version is None
        assert version.delivery_loss_report_json is None
        assert version.cover_strategy == "EXPLICIT"
        assert version.cover_asset_id == "00000000-0000-4000-8000-000000000001"

        with sqlite3.connect(database_path) as connection:
            for table in ("content_drafts", "content_versions"):
                columns = {
                    row[1]: {"type": row[2], "default": row[4]}
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                assert columns["content_schema_version"]["default"] == "1"
                assert columns["document_json"]["type"] == "JSON"
            version_columns = {
                row[1]: row[2]
                for row in connection.execute("PRAGMA table_info(content_versions)")
            }
            assert version_columns["delivery_document_json"] == "JSON"
            assert version_columns["delivery_policy_version"] == "VARCHAR(32)"
            assert version_columns["delivery_loss_report_json"] == "JSON"
            draft_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(content_drafts)")
            }
            assert "delivery_document_json" not in draft_columns
    finally:
        run(database.dispose())


def test_new_schema_defaults_and_document_json_round_trip_without_automatic_write(
    tmp_path: Path,
) -> None:
    database = ContentDatabase(sqlite_url(tmp_path / "new-content.db"))
    document = valid_document()

    async def scenario():
        await database.initialize()
        async with database.session() as session:
            draft = ContentDraft(
                draft_id="draft-new",
                source_type="BLANK",
                title="新标题",
                blocks_json=[],
                status="ACTIVE",
                revision=1,
            )
            session.add(draft)
            await session.flush()
            assert draft.content_schema_version == 1
            assert draft.document_json is None
            draft.content_schema_version = 2
            draft.document_json = document
            version = ContentVersion(
                version_id="version-new",
                draft_id=draft.draft_id,
                source_revision=1,
                content_hash="new-hash",
                title="新标题",
                blocks_json=[],
                content_schema_version=2,
                document_json=document,
                delivery_document_json=document,
                delivery_policy_version="stable_v1",
                delivery_loss_report_json={
                    "policy_version": "stable_v1",
                    "removed_marks": [],
                },
            )
            session.add(version)
            await session.flush()
        async with database.session() as session:
            return await session.get(ContentDraft, "draft-new"), await session.get(
                ContentVersion, "version-new"
            )

    try:
        draft, version = run(scenario())
        assert draft.content_schema_version == 2
        assert draft.document_json == document
        assert version.content_schema_version == 2
        assert version.document_json == document
        assert version.delivery_document_json == document
        assert version.delivery_policy_version == "stable_v1"
        assert version.delivery_loss_report_json == {
            "policy_version": "stable_v1",
            "removed_marks": [],
        }
    finally:
        run(database.dispose())


def test_sqlite_pragmas_remain_enabled(tmp_path: Path) -> None:
    database = ContentDatabase(sqlite_url(tmp_path / "pragmas.db"))

    async def read_pragmas():
        await database.initialize()
        async with database.engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: (
                    sync_connection.exec_driver_sql("PRAGMA journal_mode").scalar(),
                    sync_connection.exec_driver_sql("PRAGMA busy_timeout").scalar(),
                    sync_connection.exec_driver_sql("PRAGMA foreign_keys").scalar(),
                )
            )

    try:
        journal_mode, busy_timeout, foreign_keys = run(read_pragmas())
        assert str(journal_mode).lower() == "wal"
        assert busy_timeout == 5000
        assert foreign_keys == 1
    finally:
        run(database.dispose())
