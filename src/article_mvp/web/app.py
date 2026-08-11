"""只读看板 Flask 应用工厂。"""

import atexit
from pathlib import Path

from flask import Flask, jsonify, render_template

from article_mvp.db.database import init_db
from article_mvp.web.query import DashboardQueryService
from article_mvp.web.runtime import AsyncRuntime


def create_dashboard_app(
    *,
    database_url: str | None = None,
    runtime: AsyncRuntime | None = None,
) -> Flask:
    package_root = Path(__file__).resolve().parent
    app = Flask(
        "article_mvp_dashboard",
        template_folder=str(package_root / "templates"),
        static_folder=str(package_root / "static"),
        static_url_path="/static",
    )
    owns_runtime = runtime is None
    async_runtime = runtime or AsyncRuntime()
    async_runtime.run(init_db(database_url))
    query_service = DashboardQueryService(database_url)
    app.extensions["article_mvp_async_runtime"] = async_runtime

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "article-mvp-dashboard"}

    @app.get("/api/dashboard")
    def api_dashboard():
        try:
            return jsonify(async_runtime.run(query_service.fetch()))
        except Exception:
            app.logger.exception("加载 MVP 看板失败")
            return jsonify(
                {
                    "error": "DASHBOARD_QUERY_FAILED",
                    "message": "看板查询失败，请检查服务日志",
                }
            ), 500

    if owns_runtime:
        atexit.register(async_runtime.close)
    return app
