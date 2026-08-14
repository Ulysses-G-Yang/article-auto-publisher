"""自动化文章发布工具 - Flask 入口"""

# 运行期先注入项目 src 路径，后续项目包导入必须位于该引导之后。
# ruff: noqa: E402

import asyncio
import os
import shutil
import signal
import sys
import threading

# 确保模块路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from flask import Flask
from loguru import logger

from account_sessions import create_account_session_blueprint
from article_mvp.services.delivery_bridge import DeliveryBridge
from article_mvp.web import create_dashboard_blueprint
from config import get_config
from content_studio import create_content_studio_blueprint
from core.logging_setup import configure_logging
from web.routes import register_routes

_QUEUE_START_LOCK = threading.Lock()
_QUEUE_STARTED = False


def _make_delivery_event_sink():
    """构造投递结果 → PlatformArticle 的幂等桥接回调（best-effort）。"""

    bridge = DeliveryBridge()
    return bridge.record


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

    # 旧账号按钮也必须与登录、发布和新账号域共享同一租约，不能在另一个
    # 流程正使用 Profile 时删除 Cookie 数据库。
    from core.platform_guard import release as release_platform
    from core.platform_guard import try_acquire as try_acquire_platform

    if not try_acquire_platform(platform):
        logger.warning("平台账号正在使用，拒绝清理 Cookie: platform={}", platform)
        return False

    singleton_names = ("SingletonLock", "SingletonCookie", "SingletonSocket")
    if any(os.path.exists(os.path.join(profile_base, name)) for name in singleton_names):
        logger.warning("Chrome 正在占用 Profile，拒绝清理 Cookie: platform={}", platform)
        release_platform(platform)
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

    try:
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
    finally:
        release_platform(platform)


def kill_zombie_chrome():
    """兼容旧调用，但不再擅自删除 Chrome 的 Singleton 占用凭据。

    Singleton 文件既可能是异常残留，也可能代表另一个仍在工作的 Chrome。
    仅凭文件存在无法安全区分两者；账号会话域会在取得跨进程租约后返回
    ``PROFILE_IN_USE``，由用户关闭占用进程后再重试。
    """

    logger.debug("安全模式不自动删除 Chrome Profile Singleton 文件")
    return 0


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

    # 只组合 HTTP 路由；article_mvp 自行管理独立数据库和运行目录。
    app.register_blueprint(
        create_dashboard_blueprint(),
        url_prefix="/data-center",
    )
    account_blueprint = create_account_session_blueprint(
        delivery_event_sink=_make_delivery_event_sink(),
    )
    app.register_blueprint(account_blueprint)
    app.register_blueprint(
        create_content_studio_blueprint(
            account_state=app.extensions["account_sessions"],
        )
    )

    # 保留原有 /api/cleanup 端点兼容性；安全模式不再删除不明占用锁。
    @app.route("/api/cleanup", methods=["POST"])
    def api_cleanup():
        killed = kill_zombie_chrome()
        return {"status": "ok", "killed": killed}

    return app


def start_queue_worker():
    """在后台线程中启动队列 worker（只启动一次）"""
    if not get_config()["app"].get("legacy_upload_queue_enabled", False):
        logger.info("旧上传队列已关闭；跳过旧 worker，保留历史任务和日志不变")
        return False
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
        return True
    except Exception:
        with _QUEUE_START_LOCK:
            _QUEUE_STARTED = False
        raise


def on_shutdown(signum, frame):
    """服务器关闭时由浏览器上下文和操作系统释放各自租约。"""
    logger.info("服务正在关闭；不会擅自删除 Chrome Profile 占用凭据")
    sys.exit(0)


if __name__ == "__main__":
    cfg = get_config()
    app_cfg = cfg["app"]
    if app_cfg.get("environment") == "production":
        raise RuntimeError("生产环境请使用 run_flask_production.py，不要使用 Flask 开发服务器")
    configure_logging(cfg["paths"]["logs"])

    app = create_app()

    # 注册信号处理（Ctrl+C 或 kill 时自动清理）
    signal.signal(signal.SIGINT, on_shutdown)
    signal.signal(signal.SIGTERM, on_shutdown)

    # 启动队列 worker
    start_queue_worker()

    logger.info(
        "自动化文章发布工具启动中，访问地址: http://{}:{}", app_cfg["host"], app_cfg["port"]
    )

    app.run(
        host=app_cfg["host"],
        port=app_cfg["port"],
        debug=bool(app_cfg.get("debug", False)),
        use_reloader=False,
    )
