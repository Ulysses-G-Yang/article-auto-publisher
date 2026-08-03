"""SQLite 数据库模型"""
import sqlite3
import json
import os
from datetime import datetime
from config import get_config


class Database:
    _instance = None

    def __init__(self):
        cfg = get_config()
        self.db_path = cfg["paths"]["database"]
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_tables()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_tables(self):
        with self._get_conn() as conn:
            conn.executescript("""
                -- 平台账号表
                CREATE TABLE IF NOT EXISTS platform_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL UNIQUE,
                    username TEXT,
                    cookie_file TEXT,
                    status TEXT DEFAULT 'logged_out',
                    last_login_time TEXT,
                    created_at TEXT DEFAULT (datetime('now', 'localtime')),
                    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
                );

                -- 文章表
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    title TEXT,
                    content_text TEXT,
                    content_json TEXT,
                    keywords TEXT,
                    topic_zol TEXT,
                    topic_xiaoheihe TEXT,
                    image_count INTEGER DEFAULT 0,
                    char_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'parsed',
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );

                -- 文章图片表
                CREATE TABLE IF NOT EXISTS article_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    position_index INTEGER NOT NULL,
                    width INTEGER,
                    height INTEGER,
                    file_size INTEGER,
                    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
                );

                -- 发布任务表
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    status TEXT DEFAULT 'queued',
                    title_used TEXT,
                    topic_used TEXT,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    draft_url TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    created_at TEXT DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
                    UNIQUE(article_id, platform)
                );

                -- 任务执行日志
                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    level TEXT DEFAULT 'INFO',
                    message TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                -- 平台话题缓存
                CREATE TABLE IF NOT EXISTS topic_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    category_name TEXT NOT NULL,
                    category_id TEXT,
                    keywords TEXT,
                    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                    UNIQUE(platform, category_name)
                );
            """)

    # ==================== 账号操作 ====================
    def upsert_account(self, platform: str, **kwargs):
        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM platform_accounts WHERE platform=?", (platform,)
            ).fetchone()
            if existing:
                if kwargs:
                    fields = ", ".join(f"{k}=?" for k in kwargs)
                    values = list(kwargs.values()) + [platform]
                    conn.execute(
                        f"UPDATE platform_accounts SET {fields}, updated_at=datetime('now','localtime') WHERE platform=?",
                        values,
                    )
            else:
                # INSERT 路径：包含所有传入的 kwargs
                cols = list(kwargs.keys())
                col_names = ", ".join(["platform"] + cols)
                placeholders = ", ".join(["?"] * (len(cols) + 1) + ["datetime('now','localtime')"])
                conn.execute(
                    f"INSERT INTO platform_accounts ({col_names}, created_at) VALUES ({placeholders})",
                    [platform] + list(kwargs.values()),
                )

    def get_account(self, platform: str) -> dict:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM platform_accounts WHERE platform=?", (platform,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_accounts(self) -> list:
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM platform_accounts").fetchall()]

    # ==================== 文章操作 ====================
    def insert_article(self, filename: str, original_path: str, content_text: str,
                       content_json: str, keywords: str, image_count: int,
                       char_count: int) -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO articles (filename, original_path, content_text, content_json,
                   keywords, image_count, char_count)
                   VALUES (?,?,?,?,?,?,?)""",
                (filename, original_path, content_text, content_json, keywords, image_count, char_count),
            )
            return cur.lastrowid

    def update_article_topics(self, article_id: int, topic_zol: str, topic_xiaoheihe: str, title: str = None):
        with self._get_conn() as conn:
            params = [topic_zol, topic_xiaoheihe, article_id]
            extra = ""
            if title:
                extra = ", title=?"
                params.insert(0, title)
            conn.execute(
                f"UPDATE articles SET topic_zol=?, topic_xiaoheihe=?{extra} WHERE id=?",
                params,
            )

    def get_article(self, article_id: int) -> dict:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
            return dict(row) if row else None

    def get_all_articles(self) -> list:
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM articles ORDER BY created_at DESC"
            ).fetchall()]

    def insert_image(self, article_id: int, filename: str, local_path: str,
                     position_index: int, width: int, height: int, file_size: int):
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO article_images (article_id, filename, local_path, position_index, width, height, file_size)
                   VALUES (?,?,?,?,?,?,?)""",
                (article_id, filename, local_path, position_index, width, height, file_size),
            )

    def get_article_images(self, article_id: int) -> list:
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM article_images WHERE article_id=? ORDER BY position_index",
                (article_id,),
            ).fetchall()]

    # ==================== 任务操作 ====================
    def create_task(self, article_id: int, platform: str) -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (article_id, platform) VALUES (?,?)",
                (article_id, platform),
            )
            return cur.lastrowid

    def update_task(self, task_id: int, **kwargs):
        if not kwargs:
            return
        with self._get_conn() as conn:
            fields = ", ".join(f"{k}=?" for k in kwargs)
            conn.execute(f"UPDATE tasks SET {fields} WHERE id=?", list(kwargs.values()) + [task_id])

    def get_task(self, task_id: int) -> dict:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def get_tasks(self, article_id: int = None) -> list:
        with self._get_conn() as conn:
            if article_id:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE article_id=? ORDER BY created_at DESC",
                    (article_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_pending_tasks(self, limit: int = 10) -> list:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('queued','retrying') ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def add_task_log(self, task_id: int, level: str, message: str):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO task_logs (task_id, level, message) VALUES (?,?,?)",
                (task_id, level, message),
            )

    def get_task_logs(self, task_id: int) -> list:
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM task_logs WHERE task_id=? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()]

    # ==================== 话题缓存 ====================
    def cache_topics(self, platform: str, topics: list):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM topic_cache WHERE platform=?", (platform,))
            for t in topics:
                conn.execute(
                    "INSERT OR REPLACE INTO topic_cache (platform, category_name, category_id, keywords, updated_at) VALUES (?,?,?,?,datetime('now','localtime'))",
                    (platform, t.get("name"), t.get("id"), json.dumps(t.get("keywords", []), ensure_ascii=False)),
                )

    def get_cached_topics(self, platform: str) -> list:
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM topic_cache WHERE platform=?", (platform,)
            ).fetchall()]
