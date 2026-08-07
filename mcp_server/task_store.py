"""Durable storage for MCP-side asynchronous task mappings.

The Flask application remains the source of truth for business tasks.  This
store only keeps the public MCP task id, its kind, the mapped Flask task ids,
and a small status/result summary so polling continues after an MCP restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TaskStore:
    """Small async facade over a SQLite task-mapping table.

    SQLite operations run in worker threads so a slow filesystem operation does
    not block the MCP event loop.  Every operation opens its own connection,
    which keeps the store safe for concurrent Streamable HTTP requests.
    """

    _JSON_FIELDS = {"internal_task_ids", "metadata", "result"}
    _UPDATE_FIELDS = {
        "status",
        "message",
        "result",
        "metadata",
        "internal_task_ids",
    }

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_tasks (
                    task_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    platform TEXT,
                    status TEXT NOT NULL,
                    message TEXT,
                    internal_task_ids_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcp_tasks_updated_at "
                "ON mcp_tasks(updated_at)"
            )

    @classmethod
    def _decode(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["internal_task_ids"] = json.loads(data.pop("internal_task_ids_json") or "[]")
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        raw_result = data.pop("result_json")
        data["result"] = json.loads(raw_result) if raw_result else None
        return data

    @staticmethod
    def _encode_value(field: str, value: Any) -> Any:
        if field in {"internal_task_ids", "metadata", "result"}:
            return json.dumps(value, ensure_ascii=False) if value is not None else None
        return value

    def _create_sync(
        self,
        task_id: str,
        kind: str,
        status: str,
        platform: str | None,
        message: str,
        internal_task_ids: list[int],
        metadata: dict[str, Any] | None,
        result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = self._now()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO mcp_tasks (
                    task_id, kind, platform, status, message,
                    internal_task_ids_json, metadata_json, result_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    kind,
                    platform,
                    status,
                    message,
                    json.dumps(internal_task_ids, ensure_ascii=False),
                    json.dumps(metadata or {}, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM mcp_tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._decode(row)  # type: ignore[return-value]

    async def create(
        self,
        task_id: str,
        kind: str,
        status: str,
        *,
        platform: str | None = None,
        message: str = "",
        internal_task_ids: list[int] | None = None,
        metadata: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._create_sync,
            task_id,
            kind,
            status,
            platform,
            message,
            internal_task_ids or [],
            metadata,
            result,
        )

    def _get_sync(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM mcp_tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._decode(row)

    async def get(self, task_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, task_id)

    def _update_sync(self, task_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        if not updates:
            return self._get_sync(task_id)
        invalid = set(updates) - self._UPDATE_FIELDS
        if invalid:
            raise ValueError(f"unsupported task fields: {sorted(invalid)}")

        column_map = {
            "internal_task_ids": "internal_task_ids_json",
            "metadata": "metadata_json",
            "result": "result_json",
        }
        assignments = []
        values: list[Any] = []
        for field, value in updates.items():
            assignments.append(f"{column_map.get(field, field)}=?")
            values.append(self._encode_value(field, value))
        assignments.append("updated_at=?")
        values.extend([self._now(), task_id])

        with self._lock, self._connection() as conn:
            conn.execute(
                f"UPDATE mcp_tasks SET {', '.join(assignments)} WHERE task_id=?",
                values,
            )
            row = conn.execute("SELECT * FROM mcp_tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._decode(row)

    async def update(self, task_id: str, **updates: Any) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._update_sync, task_id, updates)
