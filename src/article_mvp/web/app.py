"""可独立测试、也可挂载到现役 Flask 服务的只读数据看板。"""

import atexit
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Lock
from typing import Any

from flask import Blueprint, Flask, current_app, jsonify, render_template

from article_mvp.db.database import init_db
from article_mvp.web.query import DashboardQueryService
from article_mvp.web.runtime import AsyncRuntime

CurrentWorkflowProvider = Callable[[], Mapping[str, Any]]


def empty_current_workflow() -> dict[str, Any]:
    """独立测试模式没有现役发布任务 Provider 时使用的安全空结构。"""

    return {
        "available": False,
        "summary": {
            "total_articles": 0,
            "total_tasks": 0,
            "xiaoheihe_tasks": 0,
            "saved_drafts": 0,
        },
        "tasks": [],
    }


class DashboardRuntimeState:
    """按需启动异步运行时，避免仅创建现役 Flask App 就产生后台线程。"""

    def __init__(
        self,
        database_url: str | None,
        runtime: AsyncRuntime | None,
    ) -> None:
        self.database_url = database_url
        self.query_service = DashboardQueryService(database_url)
        self._runtime = runtime
        self._owns_runtime = runtime is None
        self._initialized = False
        self._lock = Lock()
        if runtime is not None:
            runtime.run(init_db(database_url))
            self._initialized = True

    def fetch(self) -> dict[str, Any]:
        runtime = self._ensure_runtime()
        return runtime.run(self.query_service.fetch())

    def _ensure_runtime(self) -> AsyncRuntime:
        with self._lock:
            if self._runtime is None:
                self._runtime = AsyncRuntime()
            if not self._initialized:
                self._runtime.run(init_db(self.database_url))
                self._initialized = True
            return self._runtime

    def close(self) -> None:
        with self._lock:
            runtime = self._runtime
            owns_runtime = self._owns_runtime
            self._runtime = None
            self._initialized = False
        if runtime is not None and owns_runtime:
            runtime.close()


def create_dashboard_blueprint(
    *,
    database_url: str | None = None,
    runtime: AsyncRuntime | None = None,
    current_workflow_provider: CurrentWorkflowProvider | None = None,
) -> Blueprint:
    """创建可挂载 Blueprint；新包通过 Provider 单向读取现役任务摘要。"""

    package_root = Path(__file__).resolve().parent
    blueprint = Blueprint(
        "article_mvp_dashboard",
        __name__,
        template_folder=str(package_root / "templates"),
        static_folder=str(package_root / "static"),
        static_url_path="/assets",
    )
    state = DashboardRuntimeState(database_url, runtime)

    @blueprint.record_once
    def register_state(setup_state) -> None:
        setup_state.app.extensions["article_mvp_dashboard"] = state
        if runtime is None:
            atexit.register(state.close)

    @blueprint.get("/")
    def dashboard():
        return render_template("dashboard.html")

    @blueprint.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "article-mvp-dashboard"}

    @blueprint.get("/api/dashboard")
    def api_dashboard():
        try:
            payload = state.fetch()
        except Exception:
            current_app.logger.exception("加载 MVP 看板失败")
            return jsonify(
                {
                    "error": "DASHBOARD_QUERY_FAILED",
                    "message": "看板查询失败，请检查服务日志",
                }
            ), 500

        payload["current_workflow"] = empty_current_workflow()
        if current_workflow_provider is not None:
            try:
                payload["current_workflow"] = dict(current_workflow_provider())
            except Exception:
                current_app.logger.exception("加载当前发布任务摘要失败")
        return jsonify(payload)

    return blueprint


def create_dashboard_app(
    *,
    database_url: str | None = None,
    runtime: AsyncRuntime | None = None,
    current_workflow_provider: CurrentWorkflowProvider | None = None,
) -> Flask:
    """仅供单元测试使用；正式页面由现役 5000 端口挂载 Blueprint。"""

    app = Flask("article_mvp_dashboard", static_folder=None)
    app.register_blueprint(
        create_dashboard_blueprint(
            database_url=database_url,
            runtime=runtime,
            current_workflow_provider=current_workflow_provider,
        )
    )
    return app
