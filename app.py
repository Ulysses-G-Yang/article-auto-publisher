"""自动化文章发布工具 - Flask 入口"""
import sys
import os
import asyncio
import threading
import signal
import shutil

# 确保模块路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from flask import Flask

from article_mvp.web import create_dashboard_blueprint
from config import get_config
from core.logging_setup import configure_logging
from loguru import logger
from models.database import Database
from web.article_mvp_bridge import build_current_workflow_snapshot
from web.routes import register_routes

_QUEUE_START_LOCK = threading.Lock()
_QUEUE_STARTED = False


def clear_platform_cookies(platform: str) -> bool:
    """清除指定平台的 Chrome Cookie/站点会话数据，实现账号切换。

    只接受项目支持的平台，并且所有删除目标必须位于该平台自己的
    ``data/chrome_profiles/{platform}`` 目录内；不会触碰另一个平台的 Profile。
    """
    if platform not in ("zol", "xiaoheihe"):
        return False

    profile_root = os.path.realpath(os.path.join(BASE_DIR, "data", "chrome_profiles"))
    profile_base = os.path.realpath(os.path.join(profile_root, platform))
    try:
        if os.path.commonpath([profile_root, profile_base]) != profile_root:
            logger.error("拒绝清理越界的 Chrome Profile: platform={}", platform)
            return False
    except ValueError:
        return False

    if not os.path.isdir(profile_base):
        return False

    cookie_paths = [
        os.path.join(profile_base, "Default", "Network", "Cookies"),
        os.path.join(profile_base, "Default", "Network", "Cookies-wal"),
        os.path.join(profile_base, "Default", "Network", "Cookies-shm"),
        os.path.join(profile_base, "Default", "Cookies"),
        os.path.join(profile_base, "Default", "Local Storage"),
        os.path.join(profile_base, "Default", "Session Storage"),
        os.path.join(profile_base, "Default", "Web Data"),
        os.path.join(profile_base, "Default", "Login Data"),
    ]

    cleared = False
    for path in cookie_paths:
        try:
            target = os.path.realpath(path)
            if os.path.commonpath([profile_base, target]) != profile_base:
                logger.error("拒绝清理越界的 Cookie 路径: platform={}, path={}", platform, path)
                continue
            if os.path.isfile(path):
                os.remove(path)
                cleared = True
            elif os.path.isdir(path):
                shutil.rmtree(path)
                cleared = True
        except Exception as exc:
            # Chrome 正在占用文件时可能清理失败；记录原因但继续尝试其他文件。
            logger.warning("清理 {} Cookie 文件失败: path={}, error={}", platform, path, exc)

    return cleared


def kill_zombie_chrome():
    """只清理 Chrome Profile 的锁文件，保留 Cookie 会话"""
    profile_base = os.path.join(BASE_DIR, "data", "chrome_profiles")
    cleaned = 0

    # 只清理 Singleton 锁文件，不触碰 Chrome 进程和 Profile 数据。
    for platform in ["zol", "xiaoheihe"]:
        profile_dir = os.path.join(profile_base, platform)
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_path = os.path.join(profile_dir, lock_file)
            try:
                if os.path.exists(lock_path):
                    os.remove(lock_path)
                    cleaned += 1
            except Exception as exc:
                logger.warning("清理 Chrome Profile 锁文件失败: path={}, error={}", lock_path, exc)

    return cleaned


def create_app() -> Flask:
    cfg = get_config()
    app_cfg = cfg["app"]
    configure_logging(cfg["paths"]["logs"])

    app = Flask(
        __name__,
        template_folder="web/templates",
        static_folder="web/static",
    )
    app.secret_key = app_cfg["secret_key"]
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB 上传限制

    # 注册路由
    register_routes(app)

    # 新数据层保持独立包边界，但通过 Blueprint 接入现役 5000 端口。
    # Provider 仅返回脱敏任务摘要，新包不会反向导入现役发布模块。
    database_path = os.path.abspath(cfg["paths"]["database"]).replace("\\", "/")
    dashboard_database_url = f"sqlite+aiosqlite:///{database_path}"
    current_db = Database.get_instance()
    app.register_blueprint(
        create_dashboard_blueprint(
            database_url=dashboard_database_url,
            current_workflow_provider=lambda: build_current_workflow_snapshot(current_db),
        ),
        url_prefix="/data-center",
    )

    # 添加清理 Profile 锁文件的 API；保留原有 /api/cleanup 端点兼容性。
    @app.route("/api/cleanup", methods=["POST"])
    def api_cleanup():
        killed = kill_zombie_chrome()
        return {"status": "ok", "killed": killed}

    return app


def start_queue_worker():
    """在后台线程中启动队列 worker（只启动一次）"""
    global _QUEUE_STARTED
    with _QUEUE_START_LOCK:
        if _QUEUE_STARTED:
            logger.debug("队列 worker 已启动，跳过重复启动")
            return
        _QUEUE_STARTED = True

    # 在创建 worker 前完成启动迁移，确保历史 queued/retrying/processing 任务
    # 不会在新任务上传前后被误当成自动恢复对象。
    from models.database import Database
    paused = Database.get_instance().pause_unfinished_tasks()
    if paused:
        logger.info("启动时暂停 {} 个历史未完成任务", paused)

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        from core.queue_manager import get_queue_manager
        qm = get_queue_manager()
        loop.run_until_complete(qm.start())
        loop.run_forever()

    try:
        t = threading.Thread(target=_run, daemon=True, name="article-publisher-queue")
        t.start()
        logger.info("队列 worker 已启动；历史未完成任务不会自动恢复")
    except Exception:
        with _QUEUE_START_LOCK:
            _QUEUE_STARTED = False
        raise


def on_shutdown(signum, frame):
    """服务器关闭时清理 Chrome Profile 锁文件"""
    logger.info("正在清理 Chrome Profile 锁文件...")
    cleaned = kill_zombie_chrome()
    logger.info("已清理 {} 个 Chrome Profile 锁文件", cleaned)
    sys.exit(0)


if __name__ == "__main__":
    cfg = get_config()
    app_cfg = cfg["app"]
    if app_cfg.get("environment") == "production":
        raise RuntimeError("生产环境请使用 run_flask_production.py，不要使用 Flask 开发服务器")
    configure_logging(cfg["paths"]["logs"])

    # 启动前先清理上次残留的僵尸 Chrome
    cleaned = kill_zombie_chrome()
    if cleaned > 0:
        logger.info("启动前清理了 {} 个 Chrome Profile 锁文件", cleaned)

    app = create_app()

    # 注册信号处理（Ctrl+C 或 kill 时自动清理）
    signal.signal(signal.SIGINT, on_shutdown)
    signal.signal(signal.SIGTERM, on_shutdown)

    # 启动队列 worker
    start_queue_worker()

    logger.info("自动化文章发布工具启动中，访问地址: http://{}:{}", app_cfg["host"], app_cfg["port"])

    app.run(
        host=app_cfg["host"],
        port=app_cfg["port"],
        debug=bool(app_cfg.get("debug", False)),
        use_reloader=False,
    )
