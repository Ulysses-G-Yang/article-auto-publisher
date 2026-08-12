"""账号会话与内容投递 Flask Blueprint。"""

import atexit
import logging
from collections.abc import Coroutine
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlencode

from flask import Blueprint, jsonify, redirect, request
from pydantic import ValidationError

from account_sessions.account_service import AccountSessionService
from account_sessions.contracts import DeliveryRequest, SessionPolicyRequest
from account_sessions.database import AccountDatabase
from account_sessions.delivery_service import DeliveryService
from account_sessions.errors import AccountSessionError, ConfirmationRequiredError
from account_sessions.permissions import (
    LOCAL_WEB_CONTEXT,
    PermissionDeniedError,
)
from account_sessions.runtime import AccountRuntime

LOGGER = logging.getLogger(__name__)


class AccountSessionRuntimeState:
    """为同步 Flask 请求提供独立的异步账号域运行时。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        runtime: AccountRuntime | None = None,
        seed_legacy_profiles: bool = True,
        auto_execute: bool = True,
        platform_factory=None,
        public_publish_enabled: bool | None = None,
        acquire_legacy_guard=None,
        release_legacy_guard=None,
        allowed_profile_roots=None,
    ) -> None:
        self.database = AccountDatabase(database_url)
        self.accounts = AccountSessionService(
            self.database,
            acquire_legacy_guard=acquire_legacy_guard,
            release_legacy_guard=release_legacy_guard,
            seed_legacy_profiles=seed_legacy_profiles,
            platform_factory=platform_factory,
            allowed_profile_roots=allowed_profile_roots,
        )
        self.delivery = DeliveryService(
            self.accounts,
            platform_factory=platform_factory,
            public_publish_enabled=public_publish_enabled,
        )
        self.auto_execute = auto_execute
        self._runtime = runtime
        self._owns_runtime = runtime is None
        self._initialized = False
        self._lock = Lock()

    def run(self, coroutine: Coroutine[Any, Any, Any], *, timeout: float = 30) -> Any:
        runtime = self._ensure_runtime()
        return runtime.run(coroutine, timeout=timeout)

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        runtime = self._ensure_runtime()
        future = runtime.submit(coroutine)

        def consume_result(done) -> None:
            try:
                done.result()
            except Exception:
                LOGGER.exception("账号会话后台操作失败")

        future.add_done_callback(consume_result)

    def _ensure_runtime(self) -> AccountRuntime:
        with self._lock:
            if self._runtime is None:
                self._runtime = AccountRuntime()
            if not self._initialized:
                self._runtime.run(self.accounts.initialize())
                self._initialized = True
            return self._runtime

    def close(self) -> None:
        with self._lock:
            runtime = self._runtime
            owns_runtime = self._owns_runtime
            self._runtime = None
            self._initialized = False
        if runtime is not None and owns_runtime:
            runtime.close(self.database.dispose())


def create_account_session_blueprint(
    *,
    database_url: str | None = None,
    runtime: AccountRuntime | None = None,
    seed_legacy_profiles: bool = True,
    auto_execute: bool = True,
    platform_factory=None,
    public_publish_enabled: bool | None = None,
    allowed_profile_roots=None,
) -> Blueprint:
    """创建可挂载到现役 5000 端口的账号会话 Blueprint。"""

    from core.platform_guard import release as release_platform
    from core.platform_guard import try_acquire as try_acquire_platform

    project_root = Path(__file__).resolve().parents[2]
    blueprint = Blueprint(
        "account_sessions",
        __name__,
        template_folder=str(project_root / "web" / "templates"),
    )
    state = AccountSessionRuntimeState(
        database_url=database_url,
        runtime=runtime,
        seed_legacy_profiles=seed_legacy_profiles,
        auto_execute=auto_execute,
        platform_factory=platform_factory,
        public_publish_enabled=public_publish_enabled,
        acquire_legacy_guard=try_acquire_platform,
        release_legacy_guard=release_platform,
        allowed_profile_roots=allowed_profile_roots,
    )

    @blueprint.record_once
    def register_state(setup_state) -> None:
        setup_state.app.extensions["account_sessions"] = state
        if runtime is None:
            atexit.register(state.close)

    @blueprint.get("/delivery/new")
    def new_delivery():
        draft_id = (request.args.get("draft_id") or "").strip()
        if draft_id:
            return redirect(f"/upload?{urlencode({'draft_id': draft_id})}", code=302)
        return redirect("/upload", code=302)

    @blueprint.get("/api/platforms")
    def list_platforms():
        return jsonify(
            {
                "platforms": [
                    {"id": "xiaoheihe", "display_name": "小黑盒"},
                    {"id": "zol", "display_name": "中关村在线"},
                ]
            }
        )

    @blueprint.get("/api/platforms/<platform>/accounts")
    def list_accounts(platform: str):
        usable = request.args.get("usable", "false").lower() == "true"
        accounts = state.run(
            state.accounts.list_accounts(
                platform,
                LOCAL_WEB_CONTEXT,
                usable_only=usable,
            )
        )
        return jsonify({"platform": platform, "accounts": accounts})

    @blueprint.post("/api/platforms/<platform>/accounts/login")
    def create_account_login(platform: str):
        account = state.run(state.accounts.create_login_candidate(platform, LOCAL_WEB_CONTEXT))
        state.submit(
            state.accounts.verify_account(
                account["account_id"],
                LOCAL_WEB_CONTEXT,
                allow_interactive_login=True,
            )
        )
        return jsonify(account), 202

    @blueprint.post("/api/accounts/<account_id>/verify")
    def verify_account(account_id: str):
        account = state.run(state.accounts.mark_verifying(account_id, LOCAL_WEB_CONTEXT))
        state.submit(
            state.accounts.verify_account(
                account_id,
                LOCAL_WEB_CONTEXT,
                allow_interactive_login=False,
            )
        )
        return jsonify(account), 202

    @blueprint.post("/api/accounts/<account_id>/session-policy")
    def update_session_policy(account_id: str):
        payload = SessionPolicyRequest.model_validate(request.get_json(silent=True) or {})
        account = state.run(
            state.accounts.set_session_policy(
                account_id,
                payload.persist_login,
                LOCAL_WEB_CONTEXT,
            )
        )
        return jsonify(account)

    @blueprint.post("/api/account-sessions/<account_id>/logout")
    def logout_account(account_id: str):
        account = state.run(
            state.accounts.logout_account(account_id, LOCAL_WEB_CONTEXT),
            timeout=60,
        )
        return jsonify(account)

    @blueprint.get("/api/account-sessions/<account_id>/activity")
    def account_activity(account_id: str):
        rows = state.run(state.accounts.list_activity(account_id, LOCAL_WEB_CONTEXT))
        return jsonify({"account_id": account_id, "activities": rows})

    @blueprint.post("/api/delivery-operations")
    def create_delivery_operation():
        payload = DeliveryRequest.model_validate(request.get_json(silent=True) or {})
        operation = state.run(state.delivery.request_delivery(payload, LOCAL_WEB_CONTEXT))
        if state.auto_execute:
            state.submit(
                state.delivery.execute_operation(
                    operation["operation_id"],
                    LOCAL_WEB_CONTEXT,
                )
            )
        return jsonify(operation), 202

    @blueprint.get("/api/delivery-operations/<operation_id>")
    def get_delivery_operation(operation_id: str):
        operation = state.run(state.delivery.get_operation(operation_id, LOCAL_WEB_CONTEXT))
        return jsonify(operation)

    @blueprint.errorhandler(ConfirmationRequiredError)
    def confirmation_required(exc: ConfirmationRequiredError):
        return jsonify(
            {
                "error": exc.error_code,
                "message": str(exc),
                "confirmation_token": exc.token,
                "expires_at": exc.expires_at,
                "summary": exc.summary,
            }
        ), exc.http_status

    @blueprint.errorhandler(AccountSessionError)
    def account_error(exc: AccountSessionError):
        return jsonify({"error": exc.error_code, "message": str(exc)}), exc.http_status

    @blueprint.errorhandler(PermissionDeniedError)
    def permission_error(exc: PermissionDeniedError):
        return jsonify({"error": exc.error_code, "message": str(exc)}), 403

    @blueprint.errorhandler(ValidationError)
    def validation_error(exc: ValidationError):
        return jsonify(
            {
                "error": "REQUEST_VALIDATION_FAILED",
                "message": "请求字段不符合投递契约",
                "details": exc.errors(include_url=False, include_input=False),
            }
        ), 422

    return blueprint
