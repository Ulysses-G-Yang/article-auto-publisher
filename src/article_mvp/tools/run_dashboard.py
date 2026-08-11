"""使用 Waitress 启动独立只读看板。"""

import os

from waitress import serve

from article_mvp.web import create_dashboard_app


def dashboard_port() -> int:
    raw = os.getenv("ARTICLE_MVP_DASHBOARD_PORT", "5100")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("ARTICLE_MVP_DASHBOARD_PORT 必须是整数") from exc
    if not 1 <= value <= 65535:
        raise ValueError("ARTICLE_MVP_DASHBOARD_PORT 必须在 1 到 65535 之间")
    return value


def main() -> None:
    host = os.getenv("ARTICLE_MVP_DASHBOARD_HOST", "127.0.0.1").strip()
    if not host:
        raise ValueError("ARTICLE_MVP_DASHBOARD_HOST 不能为空")
    app = create_dashboard_app()
    runtime = app.extensions["article_mvp_async_runtime"]
    try:
        serve(app, host=host, port=dashboard_port(), threads=4)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
