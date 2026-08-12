"""Content Studio Flask Blueprint。"""

import atexit
import logging
from collections.abc import Coroutine
from pathlib import Path
from threading import Lock
from typing import Any

from flask import Blueprint, jsonify, request, send_file
from pydantic import ValidationError

from account_sessions.contracts import ArticleInput, DeliveryRequest
from account_sessions.errors import AccountSessionError, ConfirmationRequiredError
from account_sessions.permissions import LOCAL_WEB_CONTEXT, PermissionDeniedError
from account_sessions.runtime import AccountRuntime
from account_sessions.security import safe_error_message
from content_studio.assets import AssetStore
from content_studio.contracts import (
    CreateDeliveryPlanRequest,
    CreateDraftRequest,
    DraftListQuery,
    ExecuteDeliveryPlanRequest,
    LegacyArticleListQuery,
    PatchDraftRequest,
    ReplaceTargetsRequest,
)
from content_studio.database import ContentDatabase
from content_studio.errors import (
    ContentStudioError,
    DraftBatchConfirmationRequiredError,
    DraftRevisionConflictError,
)
from content_studio.importers import DocxImportAdapter, LegacyDatabaseSource
from content_studio.service import ContentStudioService

LOGGER = logging.getLogger(__name__)


class ContentStudioRuntimeState:
    def __init__(
        self,
        *,
        account_state,
        database_url: str | None = None,
        asset_root: str | Path | None = None,
        work_root: str | Path | None = None,
        legacy_source=None,
        runtime: AccountRuntime | None = None,
    ) -> None:
        self.account_state = account_state
        self.database = ContentDatabase(database_url)
        self.asset_store = AssetStore(asset_root)
        self.service = ContentStudioService(
            self.database,
            asset_store=self.asset_store,
            legacy_source=legacy_source or LegacyDatabaseSource(),
            docx_importer=DocxImportAdapter(self.asset_store, work_root=work_root),
            account_service=account_state.accounts,
        )
        self.account_state.delivery.content_resolver = self.service.resolve_delivery_payload
        self._runtime = runtime
        self._owns_runtime = False
        self._initialized = False
        self._lock = Lock()

    def run(self, coroutine: Coroutine[Any, Any, Any], *, timeout: float = 60) -> Any:
        runtime = self._ensure_runtime()
        return runtime.run(coroutine, timeout=timeout)

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        runtime = self._ensure_runtime()
        future = runtime.submit(coroutine)

        def consume_result(done) -> None:
            try:
                done.result()
            except Exception:
                LOGGER.exception("投递计划后台执行失败")

        future.add_done_callback(consume_result)

    def _ensure_runtime(self) -> AccountRuntime:
        with self._lock:
            if self._runtime is None:
                # 与账号域共用同一事件循环，避免同一个账号 AsyncEngine 被两个
                # event loop 交叉使用；account_state 同时负责先初始化账号表。
                self._runtime = self.account_state._ensure_runtime()
            if not self._initialized:
                self._runtime.run(self.service.initialize())
                self._initialized = True
            return self._runtime

    def close(self) -> None:
        with self._lock:
            runtime = self._runtime
            owns_runtime = self._owns_runtime
            self._runtime = None
            self._initialized = False
        if runtime is not None:
            if owns_runtime:
                runtime.close(self.database.dispose())
            else:
                try:
                    runtime.run(self.database.dispose())
                except Exception:
                    LOGGER.exception("Content Studio 数据库关闭失败")

    async def execute_plan(
        self,
        plan_id: str,
        payload: ExecuteDeliveryPlanRequest,
    ) -> dict:
        context, targets = await self.service.get_plan_execution_context(plan_id)
        requested_ids = set(payload.target_ids or [target["target_id"] for target in targets])
        known_ids = {target["target_id"] for target in targets}
        unknown = requested_ids - known_ids
        if unknown:
            raise ContentStudioError("投递计划包含未知目标", error_code="PLAN_TARGET_UNKNOWN")

        selected = [target for target in targets if target["target_id"] in requested_ids]
        draft_targets = [target for target in selected if target["mode"] == "DRAFT"]
        if draft_targets and not payload.draft_batch_confirmed:
            raise DraftBatchConfirmationRequiredError(
                f"请确认将同一内容保存到 {len(draft_targets)} 个平台草稿"
            )

        for target in selected:
            if target["operation_id"] or target["status"] in {
                "QUEUED",
                "RUNNING",
                "DRAFT_SAVED",
                "PUBLISHED",
            }:
                continue
            confirmation = payload.confirmations.get(target["target_id"])
            request_payload = DeliveryRequest(
                article=ArticleInput(
                    title=context["title"],
                    body=f"[Content Studio version {context['content_hash']}]",
                ),
                platform=target["platform"],
                account_id=target["account_id"],
                mode=target["mode"],
                confirmation_token=confirmation,
            )
            try:
                operation = await self.account_state.delivery.request_delivery(
                    request_payload,
                    LOCAL_WEB_CONTEXT,
                    frozen_content_hash=context["content_hash"],
                    content_reference=context["content_hash"],
                    confirmation_scope=target["target_id"],
                )
            except ConfirmationRequiredError as exc:
                await self.service.set_plan_target_result(
                    plan_id,
                    target["target_id"],
                    status="CONFIRMATION_REQUIRED",
                    error_code=exc.error_code,
                    error_message=str(exc),
                )
                target["status"] = "CONFIRMATION_REQUIRED"
                target["confirmation_required"] = True
                target["confirmation_token"] = exc.token
                target["expires_at"] = exc.expires_at
                target["error_code"] = exc.error_code
                continue
            except (AccountSessionError, PermissionDeniedError) as exc:
                await self.service.set_plan_target_result(
                    plan_id,
                    target["target_id"],
                    status="BLOCKED",
                    error_code=getattr(exc, "error_code", "DELIVERY_BLOCKED"),
                    error_message=str(exc),
                )
                target["status"] = "BLOCKED"
                target["error_code"] = getattr(exc, "error_code", "DELIVERY_BLOCKED")
                target["error_message"] = str(exc)
                continue
            except Exception as exc:
                message = safe_error_message(exc)
                await self.service.set_plan_target_result(
                    plan_id,
                    target["target_id"],
                    status="FAILED",
                    error_code=getattr(exc, "error_code", "DELIVERY_FAILED"),
                    error_message=message,
                )
                target["status"] = "FAILED"
                target["error_code"] = getattr(exc, "error_code", "DELIVERY_FAILED")
                target["error_message"] = message
                continue

            await self.service.set_plan_target_result(
                plan_id,
                target["target_id"],
                status="QUEUED",
                operation_id=operation["operation_id"],
            )
            target["status"] = "QUEUED"
            target["operation_id"] = operation["operation_id"]
            if self.account_state.auto_execute:
                self.account_state.submit(
                    self._execute_operation_and_sync(
                        plan_id,
                        target["target_id"],
                        operation["operation_id"],
                    )
                )

        plan = await self.service.get_delivery_plan(plan_id)
        response_targets = {target["target_id"]: target for target in plan["targets"]}
        for target in selected:
            response_targets[target["target_id"]].update(
                {
                    key: value
                    for key, value in target.items()
                    if key in {"confirmation_token", "expires_at"}
                }
            )
        plan["targets"] = list(response_targets.values())
        return plan

    async def _execute_operation_and_sync(
        self,
        plan_id: str,
        target_id: str,
        operation_id: str,
    ) -> None:
        try:
            operation = await self.account_state.delivery.execute_operation(
                operation_id,
                LOCAL_WEB_CONTEXT,
            )
            await self.service.set_plan_target_result(
                plan_id,
                target_id,
                status=operation["status"],
                operation_id=operation_id,
            )
        except Exception as exc:
            await self.service.set_plan_target_result(
                plan_id,
                target_id,
                status="FAILED",
                operation_id=operation_id,
                error_code=getattr(exc, "error_code", "DELIVERY_FAILED"),
                error_message=str(exc)[:1000],
            )
            return


def create_content_studio_blueprint(
    *,
    account_state,
    database_url: str | None = None,
    asset_root: str | Path | None = None,
    work_root: str | Path | None = None,
    legacy_source=None,
    runtime: AccountRuntime | None = None,
) -> Blueprint:
    blueprint = Blueprint("content_studio", __name__)
    state = ContentStudioRuntimeState(
        account_state=account_state,
        database_url=database_url,
        asset_root=asset_root,
        work_root=work_root,
        legacy_source=legacy_source,
        runtime=runtime,
    )

    @blueprint.record_once
    def register_state(setup_state) -> None:
        setup_state.app.extensions["content_studio"] = state
        if runtime is None:
            atexit.register(state.close)

    @blueprint.get("/api/content-drafts")
    def list_drafts():
        query = DraftListQuery.model_validate(request.args.to_dict())
        return jsonify(state.run(state.service.list_drafts(**query.model_dump())))

    @blueprint.post("/api/content-drafts")
    def create_draft():
        payload = CreateDraftRequest.model_validate(request.get_json(silent=True) or {})
        return jsonify(state.run(state.service.create_draft(payload))), 201

    @blueprint.get("/api/content-drafts/<draft_id>")
    def get_draft(draft_id: str):
        return jsonify(state.run(state.service.get_draft(draft_id)))

    @blueprint.patch("/api/content-drafts/<draft_id>")
    def patch_draft(draft_id: str):
        payload = PatchDraftRequest.model_validate(request.get_json(silent=True) or {})
        return jsonify(state.run(state.service.patch_draft(draft_id, payload)))

    @blueprint.post("/api/content-drafts/import-docx")
    def import_docx():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "DOCX_FILE_REQUIRED", "message": "请选择 DOCX 文件"}), 400
        data = upload.read()
        return jsonify(
            state.run(state.service.import_docx(data, upload.filename), timeout=120)
        ), 201

    @blueprint.get("/api/content-sources/legacy-articles")
    def legacy_articles():
        query = LegacyArticleListQuery.model_validate(request.args.to_dict())
        return jsonify(state.run(state.service.list_legacy_articles(**query.model_dump())))

    @blueprint.post("/api/content-drafts/from-legacy/<int:article_id>")
    def from_legacy(article_id: int):
        return jsonify(state.run(state.service.create_from_legacy(article_id), timeout=120)), 201

    @blueprint.post("/api/content-drafts/<draft_id>/assets")
    def add_asset(draft_id: str):
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "ASSET_FILE_REQUIRED", "message": "请选择图片"}), 400
        return jsonify(
            state.run(state.service.add_asset(draft_id, upload.read(), upload.filename))
        ), 201

    @blueprint.get("/api/content-assets/<asset_id>")
    def get_asset(asset_id: str):
        path, media_type = state.run(state.service.get_asset(asset_id))
        response = send_file(path, mimetype=media_type, conditional=True, max_age=3600)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Disposition"] = "inline"
        return response

    @blueprint.put("/api/content-drafts/<draft_id>/targets")
    def replace_targets(draft_id: str):
        payload = ReplaceTargetsRequest.model_validate(request.get_json(silent=True) or {})
        return jsonify(
            state.run(state.service.replace_targets(draft_id, payload, LOCAL_WEB_CONTEXT))
        )

    @blueprint.post("/api/content-drafts/<draft_id>/delivery-plans")
    def create_delivery_plan(draft_id: str):
        payload = CreateDeliveryPlanRequest.model_validate(request.get_json(silent=True) or {})
        return jsonify(
            state.run(
                state.service.create_delivery_plan(
                    draft_id,
                    payload.revision,
                    LOCAL_WEB_CONTEXT,
                )
            )
        ), 201

    @blueprint.get("/api/delivery-plans/<plan_id>")
    def get_delivery_plan(plan_id: str):
        return jsonify(state.run(state.service.get_delivery_plan(plan_id)))

    @blueprint.post("/api/delivery-plans/<plan_id>/execute")
    def execute_delivery_plan(plan_id: str):
        payload = ExecuteDeliveryPlanRequest.model_validate(request.get_json(silent=True) or {})
        result = state.run(state.execute_plan(plan_id, payload), timeout=120)
        has_confirmation = any(
            target.get("confirmation_required") and target.get("confirmation_token")
            for target in result["targets"]
        )
        return jsonify(result), 428 if has_confirmation else 202

    @blueprint.errorhandler(DraftRevisionConflictError)
    def revision_conflict(exc: DraftRevisionConflictError):
        return jsonify(
            {
                "error": exc.error_code,
                "message": str(exc),
                "server_draft": exc.server_draft,
            }
        ), exc.http_status

    @blueprint.errorhandler(ContentStudioError)
    def content_error(exc: ContentStudioError):
        return jsonify({"error": exc.error_code, "message": str(exc)}), exc.http_status

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
                "message": "请求字段不符合 Content Studio 契约",
                "details": exc.errors(include_url=False, include_input=False),
            }
        ), 422

    return blueprint
