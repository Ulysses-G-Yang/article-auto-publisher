"""生产环境 Flask 启动入口。

生产机器必须保持 Windows 用户桌面会话可用，因为 Playwright 需要启动有头
Chrome 并等待人工扫码。该入口使用 Waitress 提供 WSGI 服务，队列 worker
仍由项目进程启动一次；不要用 Flask 的开发服务器上线。
"""

from __future__ import annotations

import signal

from loguru import logger
from waitress import serve

from app import create_app, on_shutdown, start_queue_worker
from config import get_config


def main() -> None:
    cfg = get_config()
    app_cfg = cfg["app"]
    if app_cfg.get("environment") != "production":
        raise RuntimeError("run_flask_production.py 只允许在 APP_ENV=production 时启动")

    signal.signal(signal.SIGINT, on_shutdown)
    signal.signal(signal.SIGTERM, on_shutdown)

    application = create_app()
    # 生产启动钩子：在对外接收请求前初始化账号会话域并启动心跳调度器。
    # 即使心跳开关关闭，也会执行一次纯数据库 recovery（不打开浏览器）。
    account_state = application.extensions.get("account_sessions")
    if account_state is not None:
        try:
            account_state.start()
        except Exception:
            logger.exception("账号会话域启动初始化失败")
            raise
    start_queue_worker()
    logger.info(
        "生产 Flask 服务启动: host={}, port={}, publish_after_draft={}",
        app_cfg["host"],
        app_cfg["port"],
        app_cfg.get("publish_after_draft", False),
    )
    serve(
        application,
        host=app_cfg["host"],
        port=app_cfg["port"],
        threads=4,
        ident="article-publisher-flask",
    )


if __name__ == "__main__":
    main()
