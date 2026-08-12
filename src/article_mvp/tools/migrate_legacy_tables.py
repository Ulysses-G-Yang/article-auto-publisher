"""把旧 app.db 中的 article_mvp 三张表幂等复制到独立数据库。"""

import argparse
import asyncio
import sqlite3
from pathlib import Path

from article_mvp.db.database import dispose_db, init_db
from article_mvp.runtime_paths import database_path, legacy_database_path

TABLES = ("platform_articles", "metric_snapshots", "collection_runs")


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [str(row[1]) for row in rows]


def migrate(source: Path, destination: Path) -> dict[str, int]:
    """只读源库并复制到目标库；主键冲突且内容不同会中止整个事务。"""

    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ValueError("源数据库和目标数据库不能相同")
    if not source.is_file():
        raise FileNotFoundError(f"旧数据库不存在: {source}")
    if not destination.is_file():
        raise FileNotFoundError(f"目标数据库尚未初始化: {destination}")

    source_connection = sqlite3.connect(
        f"{source.as_uri()}?mode=ro",
        uri=True,
    )
    destination_connection = sqlite3.connect(destination)
    source_connection.row_factory = sqlite3.Row
    destination_connection.row_factory = sqlite3.Row
    try:
        destination_connection.execute("PRAGMA foreign_keys=ON")
        copied: dict[str, int] = {}
        with destination_connection:
            for table in TABLES:
                source_columns = _columns(source_connection, table)
                destination_columns = _columns(destination_connection, table)
                if not source_columns:
                    copied[table] = 0
                    continue
                common = [name for name in destination_columns if name in source_columns]
                if "id" not in common:
                    raise RuntimeError(f"{table} 缺少可迁移的主键")

                quoted = ", ".join(f'"{name}"' for name in common)
                placeholders = ", ".join("?" for _ in common)
                rows = source_connection.execute(
                    f'SELECT {quoted} FROM "{table}" ORDER BY id'
                ).fetchall()
                inserted = 0
                for row in rows:
                    values = tuple(row[name] for name in common)
                    existing = destination_connection.execute(
                        f'SELECT {quoted} FROM "{table}" WHERE id=?',
                        (row["id"],),
                    ).fetchone()
                    if existing is not None:
                        if tuple(existing[name] for name in common) != values:
                            raise RuntimeError(
                                f"{table} id={row['id']} 在目标库中内容不同"
                            )
                        continue
                    destination_connection.execute(
                        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
                        values,
                    )
                    inserted += 1
                copied[table] = inserted
        return copied
    finally:
        source_connection.close()
        destination_connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="迁移 article_mvp 阶段一数据")
    parser.add_argument("--source", type=Path, default=legacy_database_path())
    parser.add_argument("--destination", type=Path, default=database_path())
    return parser.parse_args()


async def _main() -> None:
    args = parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    if source == destination:
        raise ValueError("源数据库和目标数据库不能相同")
    if not source.is_file():
        raise FileNotFoundError(f"旧数据库不存在: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_url = f"sqlite+aiosqlite:///{destination.as_posix()}"
    await init_db(destination_url)
    await dispose_db()
    result = migrate(source, destination)
    for table, count in result.items():
        print(f"{table}: copied={count}")


if __name__ == "__main__":
    asyncio.run(_main())
