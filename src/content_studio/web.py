"""Content Studio Flask Blueprint。"""

import asyncio
import atexit
import hmac
import json
import logging
import random
from collections.abc import Coroutine
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from flask import Blueprint, jsonify, request, send_file, session
from pydantic import ValidationError

from account_sessions.contracts import ArticleInput, DeliveryRequest
from account_sessions.errors import AccountSessionError, ConfirmationRequiredError
from account_sessions.mcp_access import (
    MCPInternalAccessResolver,
    MCPRequestValidationError,
)
from account_sessions.permissions import LOCAL_WEB_CONTEXT, PermissionDeniedError
from account_sessions.platform_catalog import DELIVERY_ENABLED_PLATFORMS
from account_sessions.runtime import AccountRuntime
from account_sessions.security import safe_error_message
from content_studio.assets import AssetStore
from content_studio.contracts import (
    CreateDeliveryPlanRequest,
    CreateDraftRequest,
    DraftListQuery,
    ExecuteDeliveryPlanRequest,
    LegacyArticleListQuery,
    MCPDraftDeliveryRequest,
    PatchDraftRequest,
    ReplaceTargetsRequest,
)
from content_studio.database import ContentDatabase
from content_studio.errors import ContentStudioError, DraftRevisionConflictError
from content_studio.importers import DocxImportAdapter, LegacyDatabaseSource
from content_studio.platform_format_capabilities import PlatformFormatCapabilities
from content_studio.service import (
    PLAN_OPERATION_SYNC_STATUSES,
    ContentStudioService,
)
from publication_ai.contracts import (
    PublicationAdviceRequest,
    PublicationAIModelListRequest,
    PublicationAISettingsUpdate,
    PublicationGuidanceResponse,
    validate_for_platforms,
)
from publication_ai.deepseek import DeepSeekPublicationAdvisor
from publication_ai.errors import PublicationAIError
from publication_ai.settings_store import PublicationAISettingsStore

LOGGER = logging.getLogger(__name__)
AI_SETTINGS_CSRF_SESSION_KEY = "articleops_ai_settings_csrf"
AI_SETTINGS_CSRF_HEADER = "X-ArticleOps-AI-Settings"


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _visible_text_for_publication_advice(blocks: object) -> str:
    """从服务端公开投影按 position 提取正文文字，不携带图片或资产标识。"""

    if not isinstance(blocks, list):
        raise PublicationAIError("AI_RESPONSE_INVALID")
    ordered: list[tuple[int, int, str]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            raise PublicationAIError("AI_RESPONSE_INVALID")
        position = block.get("position", index)
        if not isinstance(position, int) or isinstance(position, bool):
            raise PublicationAIError("AI_RESPONSE_INVALID")
        ordered.append((position, index, text))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return "\n".join(item[2] for item in ordered)


class PublicationAdviceRevisionConflictError(ContentStudioError):
    """建议接口的版本冲突响应，不回显草稿正文。"""

    error_code = "DRAFT_REVISION_CONFLICT"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("草稿已更新，请刷新后重新请求 AI 建议")


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
        platform_format_capabilities: PlatformFormatCapabilities | None = None,
        operation_delay_range: tuple[float, float] = (8.0, 20.0),
        publication_advisor=None,
        publication_settings_store: PublicationAISettingsStore | None = None,
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
            platform_format_capabilities=platform_format_capabilities,
        )
        self.account_state.delivery.content_resolver = self.service.resolve_delivery_payload
        self.publication_advisor = publication_advisor or (
            None
            if publication_settings_store is not None
            else DeepSeekPublicationAdvisor()
        )
        self.publication_settings_store = publication_settings_store
        self._runtime = runtime
        self._owns_runtime = False
        self._initialized = False
        self._lock = Lock()
        self._operation_delay_range = operation_delay_range
        self._operation_lock: asyncio.Lock | None = None
        self._has_executed_operation = False

    async def get_publication_advice(
        self,
        draft_id: str,
        payload: PublicationAdviceRequest,
    ) -> dict[str, Any]:
        """读取当前草稿并请求只读建议；本方法不写数据库或平台。"""

        draft = await self.service.get_draft(draft_id)
        current_revision = draft.get("revision")
        if current_revision != payload.revision:
            raise PublicationAdviceRevisionConflictError()

        title = draft.get("title")
        if not isinstance(title, str) or not title:
            raise PublicationAIError("AI_RESPONSE_INVALID")
        body = _visible_text_for_publication_advice(draft.get("blocks"))
        if len(body) > 200_000:
            raise PublicationAIError("AI_INPUT_TOO_LARGE")

        advisor = (
            self.publication_settings_store.create_advisor()
            if self.publication_settings_store is not None
            else self.publication_advisor
        )
        if advisor is None:
            raise PublicationAIError("AI_CONFIGURATION_ERROR")
        recommendations = await advisor.advise(
            title,
            body,
            payload.platforms,
        )
        if isinstance(recommendations, PublicationGuidanceResponse):
            model = getattr(advisor, "model", "")
            result = recommendations
        else:
            try:
                result = PublicationGuidanceResponse.model_validate(recommendations)
            except (TypeError, ValueError):
                raise PublicationAIError("AI_RESPONSE_INVALID") from None
            model = getattr(advisor, "model", "")
        try:
            result = validate_for_platforms(result, payload.platforms)
        except (TypeError, ValueError):
            raise PublicationAIError("AI_RESPONSE_INVALID") from None
        if not isinstance(model, str) or not model:
            model = "unknown"
        return {
            "draft_id": draft_id,
            "revision": payload.revision,
            "model": model,
            "recommendations": [
                item.model_dump(mode="json") for item in result.recommendations
            ],
            "generated_at": _utc_iso_now(),
        }

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
                if self.account_state.auto_execute:
                    recoverable = self._runtime.run(
                        self.service.list_recoverable_plan_operations(
                            self.account_state.delivery,
                            LOCAL_WEB_CONTEXT,
                        )
                    )
                    if recoverable:
                        self.account_state.submit(
                            self._execute_operations_serially(
                                [
                                    (
                                        item["plan_id"],
                                        item["target_id"],
                                        item["operation_id"],
                                        LOCAL_WEB_CONTEXT,
                                    )
                                    for item in recoverable
                                ]
                            )
                        )
            return self._runtime

    def close(self) -> None:
        with self._lock:
            runtime = self._runtime
            owns_runtime = self._owns_runtime
            self._runtime = None
            self._initialized = False
            self._operation_lock = None
            self._has_executed_operation = False
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
        access,
    ) -> dict:
        context, targets = await self.service.get_plan_execution_context(plan_id, access)
        requested_ids = set(payload.target_ids or [target["target_id"] for target in targets])
        known_ids = {target["target_id"] for target in targets}
        unknown = requested_ids - known_ids
        if unknown:
            raise ContentStudioError("投递计划包含未知目标", error_code="PLAN_TARGET_UNKNOWN")

        selected = [target for target in targets if target["target_id"] in requested_ids]
        queued_operations: list[tuple[str, str, str, Any]] = []

        for target in selected:
            if target["status"] == "FORMAT_REVIEW_REQUIRED":
                continue
            if target["operation_id"] or target["status"] in {
                "QUEUED",
                "RUNNING",
                "DRAFT_SAVED",
                "PUBLISHED",
            }:
                continue
            claim_id = await self.service.claim_plan_target(
                plan_id,
                target["target_id"],
            )
            if claim_id is None:
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
                platform_selection=target.get("platform_selection"),
            )
            try:
                operation = await self.account_state.delivery.request_delivery(
                    request_payload,
                    access,
                    frozen_content_hash=context["content_hash"],
                    content_reference=context["version_id"],
                    confirmation_scope=target["target_id"],
                    request_key=f"content-plan-target:{target['target_id']}",
                    persist_login_snapshot=target["persist_login"],
                )
            except ConfirmationRequiredError as exc:
                await self.service.set_plan_target_result(
                    plan_id,
                    target["target_id"],
                    status="CONFIRMATION_REQUIRED",
                    error_code=exc.error_code,
                    error_message=safe_error_message(exc),
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
                    error_message=safe_error_message(exc),
                )
                target["status"] = "BLOCKED"
                target["error_code"] = getattr(exc, "error_code", "DELIVERY_BLOCKED")
                target["error_message"] = safe_error_message(exc)
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

            operation_status = operation.get("status") or "QUEUED"
            await self.service.set_plan_target_result(
                plan_id,
                target["target_id"],
                status=operation_status,
                operation_id=operation["operation_id"],
                error_code=operation.get("error_code"),
                error_message=operation.get("error_message"),
                degraded=operation.get("degraded"),
                verification_evidence=operation.get("verification_evidence"),
            )
            target["status"] = operation_status
            target["operation_id"] = operation["operation_id"]
            if self.account_state.auto_execute and operation_status == "QUEUED":
                queued_operations.append(
                    (
                        plan_id,
                        target["target_id"],
                        operation["operation_id"],
                        access,
                    )
                )

        if queued_operations:
            self.account_state.submit(
                self._execute_operations_serially(queued_operations)
            )

        plan = await self.reconcile_plan_operations(plan_id, access)
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

    async def reconcile_plan_operations(self, plan_id: str, access) -> dict:
        """用账号域执行单真值修正 Content Studio 计划目标。

        两个数据库采用最终一致性。后台协程可能在写回计划前中断，所以每次
        查询计划都对仍处于 CREATING/QUEUED/RUNNING 的目标做一次只读对账。
        只读草稿核验还允许把结果未知终态单向升级为草稿成功；其他终态不会
        被旧状态倒退覆盖。
        """

        plan = await self.service.get_delivery_plan(plan_id, access)
        for target in plan["targets"]:
            operation_id = target.get("operation_id")
            target_status = str(target.get("status") or "")
            active_status = target_status in {
                "CREATING",
                "QUEUED",
                "RUNNING",
            }
            readonly_upgrade_status = target_status in {
                "RESULT_UNKNOWN",
                "DELIVERY_INCOMPLETE",
                "DRAFT_SAVED_WITH_WARNINGS",
            }
            if not operation_id or not (active_status or readonly_upgrade_status):
                continue
            try:
                operation = await self.account_state.delivery.get_operation(
                    operation_id,
                    access,
                )
            except Exception:
                if not active_status:
                    # 已进入终态时读取失败不能覆盖既有真值。
                    continue
                await self.service.set_plan_target_result(
                    plan_id,
                    target["target_id"],
                    status="RESULT_UNKNOWN",
                    expected_status=target_status,
                    operation_id=operation_id,
                    error_code="DELIVERY_OPERATION_UNAVAILABLE",
                    error_message="执行单状态不可读取，请人工核对平台结果",
                )
                continue
            operation_status = str(operation.get("status") or "")
            if operation_status not in PLAN_OPERATION_SYNC_STATUSES:
                continue
            if not active_status:
                allowed_upgrades = {
                    "RESULT_UNKNOWN": {
                        "DRAFT_SAVED_WITH_WARNINGS",
                        "DRAFT_SAVED",
                    },
                    "DELIVERY_INCOMPLETE": {
                        "DRAFT_SAVED_WITH_WARNINGS",
                        "DRAFT_SAVED",
                    },
                    "DRAFT_SAVED_WITH_WARNINGS": {"DRAFT_SAVED"},
                }
                if operation_status not in allowed_upgrades.get(target_status, set()):
                    continue
            if (
                operation_status == target.get("status")
                and operation.get("error_code") == target.get("error_code")
                and operation.get("error_message") == target.get("error_message")
                and operation.get("degraded") == target.get("degraded")
                and operation.get("verification_evidence")
                == target.get("verification_evidence")
            ):
                continue
            await self.service.set_plan_target_result(
                plan_id,
                target["target_id"],
                status=operation_status,
                expected_status=target_status,
                operation_id=operation_id,
                error_code=(
                    operation.get("error_code")
                    or (
                        "DELIVERY_RESULT_UNKNOWN"
                        if operation_status == "RESULT_UNKNOWN"
                        else None
                    )
                ),
                error_message=operation.get("error_message"),
                degraded=operation.get("degraded"),
                verification_evidence=operation.get("verification_evidence"),
            )
        return await self.service.get_delivery_plan(plan_id, access)

    async def _execute_operations_serially(
        self,
        operations: list[tuple[str, str, str, Any]],
    ) -> None:
        operation_lock = getattr(self, "_operation_lock", None)
        if operation_lock is None:
            operation_lock = asyncio.Lock()
            self._operation_lock = operation_lock

        async with operation_lock:
            for plan_id, target_id, operation_id, access in operations:
                if getattr(self, "_has_executed_operation", False):
                    delay = random.uniform(*self._operation_delay_range)
                    await asyncio.sleep(delay)
                try:
                    await self._execute_operation_and_sync(
                        plan_id,
                        target_id,
                        operation_id,
                        access,
                    )
                except Exception:
                    LOGGER.exception(
                        "投递计划串行执行失败，继续处理下一目标",
                        extra={"plan_id": plan_id, "target_id": target_id},
                    )
                finally:
                    self._has_executed_operation = True

    async def _execute_operation_and_sync(
        self,
        plan_id: str,
        target_id: str,
        operation_id: str,
        access,
    ) -> None:
        try:
            operation = await self.account_state.delivery.execute_operation(
                operation_id,
                access,
            )
            operation_status = operation.get("status") or "QUEUED"
            await self.service.set_plan_target_result(
                plan_id,
                target_id,
                status=operation_status,
                operation_id=operation_id,
                error_code=operation.get("error_code"),
                error_message=operation.get("error_message"),
                degraded=operation.get("degraded"),
                verification_evidence=operation.get("verification_evidence"),
            )

        except Exception as exc:
            try:
                operation = await self.account_state.delivery.get_operation(
                    operation_id,
                    access,
                )
            except Exception:
                operation = None

            if (
                operation
                and operation.get("status") in PLAN_OPERATION_SYNC_STATUSES
                and operation.get("status") not in {"QUEUED", "RUNNING"}
            ):
                await self.service.set_plan_target_result(
                    plan_id,
                    target_id,
                    status=operation["status"],
                    operation_id=operation_id,
                    error_code=operation.get("error_code"),
                    error_message=operation.get("error_message"),
                    degraded=operation.get("degraded"),
                    verification_evidence=operation.get("verification_evidence"),
                )
                return

            await self.service.set_plan_target_result(
                plan_id,
                target_id,
                status="FAILED",
                operation_id=operation_id,
                error_code=getattr(exc, "error_code", "DELIVERY_FAILED"),
                error_message=safe_error_message(exc),
            )


def create_content_studio_blueprint(
    *,
    account_state,
    database_url: str | None = None,
    asset_root: str | Path | None = None,
    work_root: str | Path | None = None,
    legacy_source=None,
    runtime: AccountRuntime | None = None,
    platform_format_capabilities: PlatformFormatCapabilities | None = None,
    mcp_access_resolver: MCPInternalAccessResolver | None = None,
    publication_advisor=None,
    publication_settings_store: PublicationAISettingsStore | None = None,
) -> Blueprint:
    blueprint = Blueprint("content_studio", __name__)
    state = ContentStudioRuntimeState(
        account_state=account_state,
        database_url=database_url,
        asset_root=asset_root,
        work_root=work_root,
        legacy_source=legacy_source,
        runtime=runtime,
        platform_format_capabilities=platform_format_capabilities,
        publication_advisor=publication_advisor,
        publication_settings_store=publication_settings_store,
    )
    mcp_resolver = mcp_access_resolver or MCPInternalAccessResolver()

    def require_ai_settings_csrf() -> None:
        expected = session.get(AI_SETTINGS_CSRF_SESSION_KEY, "")
        supplied = request.headers.get(AI_SETTINGS_CSRF_HEADER, "")
        if (
            not isinstance(expected, str)
            or not expected
            or not supplied
            or not hmac.compare_digest(expected, supplied)
        ):
            raise PublicationAIError("AI_SETTINGS_UNAUTHORIZED")

    def require_publication_settings_store() -> PublicationAISettingsStore:
        if state.publication_settings_store is None:
            raise PublicationAIError("AI_CONFIGURATION_ERROR")
        return state.publication_settings_store

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

    @blueprint.post("/api/content-drafts/<draft_id>/publication-advice")
    def publication_advice(draft_id: str):
        """为当前草稿生成只读平台建议；不保存、不投递、不回显文章。"""

        payload = PublicationAdviceRequest.model_validate(request.get_json(silent=True) or {})
        result = state.run(
            state.get_publication_advice(draft_id, payload),
            # AI 读取超时上限为 300 秒，给事件循环和草稿读取留出余量。
            timeout=360,
        )
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/api/settings/publication-ai")
    def get_publication_ai_settings():
        response = jsonify(require_publication_settings_store().public_view())
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post("/api/settings/publication-ai/models")
    def list_publication_ai_models():
        """读取自定义 OpenAI 兼容地址的模型列表，不保存地址或密钥。"""

        require_ai_settings_csrf()
        payload = PublicationAIModelListRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        advisor = require_publication_settings_store().create_model_list_advisor(payload)
        try:
            model_ids = state.run(advisor.list_models(), timeout=130)
        except TimeoutError:
            raise PublicationAIError("AI_TIMEOUT") from None
        response = jsonify(
            {
                "models": model_ids,
                "available_model_count": len(model_ids),
                "checked_at": _utc_iso_now(),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.put("/api/settings/publication-ai")
    def update_publication_ai_settings():
        require_ai_settings_csrf()
        payload = PublicationAISettingsUpdate.model_validate(
            request.get_json(silent=True) or {}
        )
        response = jsonify(require_publication_settings_store().update(payload))
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post("/api/settings/publication-ai/test")
    def test_publication_ai_connection():
        require_ai_settings_csrf()
        advisor = require_publication_settings_store().create_advisor()
        try:
            result = state.run(advisor.check_connection(), timeout=130)
        except TimeoutError:
            raise PublicationAIError("AI_TIMEOUT") from None
        response = jsonify(
            {
                "ok": True,
                "model": result["model"],
                "available_model_count": result["available_model_count"],
                "checked_at": _utc_iso_now(),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.after_request
    def no_store_publication_advice(response):
        """建议接口的成功与错误响应都不允许被代理或浏览器缓存。"""

        if (
            request.path.endswith("/publication-advice")
            or request.path.startswith("/api/settings/publication-ai")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

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

    @blueprint.post("/api/internal/mcp/draft-deliveries")
    def create_internal_mcp_draft_delivery():
        """从受控 DOCX 创建并执行只含 DRAFT 目标的 Content Studio 计划。"""

        access = mcp_resolver.resolve(request.headers)
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            raise MCPRequestValidationError("必须提供 DOCX 文件")
        if not upload.filename.lower().endswith(".docx"):
            raise MCPRequestValidationError("只允许 DOCX 文件")
        raw_targets = request.form.get("targets", "")
        try:
            decoded_targets = json.loads(raw_targets)
        except (TypeError, ValueError) as exc:
            raise MCPRequestValidationError("targets 必须是 JSON 对象") from exc
        payload = MCPDraftDeliveryRequest.model_validate(decoded_targets)
        for target in payload.targets:
            if target.platform not in DELIVERY_ENABLED_PLATFORMS:
                raise MCPRequestValidationError(
                    f"平台 {target.platform} 尚未开放草稿投递"
                )
            # 同时执行显式草稿能力门和账号白名单门；永远不授予 publish.execute。
            access.require("draft.create", target.account_id)

        draft = state.run(
            state.service.import_docx(upload.read(), upload.filename),
            timeout=120,
        )
        target_request = ReplaceTargetsRequest(
            revision=draft["revision"],
            targets=[
                {
                    "platform": target.platform,
                    "account_id": target.account_id,
                    "mode": "DRAFT",
                    "persist_login": target.persist_login,
                }
                for target in payload.targets
            ],
        )
        targeted = state.run(
            state.service.replace_targets(draft["draft_id"], target_request, access)
        )
        plan = state.run(
            state.service.create_delivery_plan(
                draft["draft_id"],
                targeted["revision"],
                access,
            )
        )
        result = state.run(
            state.execute_plan(
                plan["plan_id"],
                ExecuteDeliveryPlanRequest(draft_batch_confirmed=True),
                access,
            ),
            timeout=120,
        )
        return jsonify(
            {
                "draft_id": draft["draft_id"],
                "plan": result,
            }
        ), 202

    @blueprint.get("/api/internal/mcp/delivery-plans/<plan_id>")
    def get_internal_mcp_delivery_plan(plan_id: str):
        """按 MCP actor 和账号白名单读取、对账其自己的投递计划。"""

        access = mcp_resolver.resolve(request.headers)

        async def payload_with_operation_results():
            plan = await state.reconcile_plan_operations(plan_id, access)
            for target in plan.get("targets", []):
                operation_id = target.get("operation_id")
                if not operation_id:
                    continue
                operation = await state.account_state.delivery.get_operation(
                    operation_id,
                    access,
                )
                target.update(
                    {
                        "draft_url": operation.get("draft_url"),
                        "article_mapping_status": operation.get(
                            "article_mapping_status"
                        ),
                        "error_code": operation.get("error_code")
                        or target.get("error_code"),
                        "error_message": operation.get("error_message")
                        or target.get("error_message"),
                    }
                )
            return plan

        return jsonify(state.run(payload_with_operation_results()))

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
        return jsonify(state.run(state.reconcile_plan_operations(plan_id, LOCAL_WEB_CONTEXT)))

    @blueprint.post("/api/delivery-plans/<plan_id>/execute")
    def execute_delivery_plan(plan_id: str):
        payload = ExecuteDeliveryPlanRequest.model_validate(request.get_json(silent=True) or {})
        result = state.run(
            state.execute_plan(plan_id, payload, LOCAL_WEB_CONTEXT),
            timeout=120,
        )
        has_confirmation = any(
            target.get("confirmation_required") and target.get("confirmation_token")
            for target in result["targets"]
        )
        if has_confirmation:
            result["error"] = "PUBLISH_CONFIRMATION_REQUIRED"
            result["message"] = "一个或多个公开发布目标需要逐条确认"
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

    @blueprint.errorhandler(PublicationAIError)
    def publication_ai_error(exc: PublicationAIError):
        return jsonify({"error": exc.error_code, "message": exc.safe_message}), exc.http_status

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

    @blueprint.errorhandler(Exception)
    def unexpected_error(exc: Exception):
        """API 永远返回脱敏 JSON；详细堆栈只进入服务端日志。"""

        LOGGER.exception("Content Studio 未处理异常")
        return jsonify(
            {
                "error": "CONTENT_STUDIO_INTERNAL_ERROR",
                "message": "内容工作台暂时不可用，请查看服务端日志",
            }
        ), 500

    return blueprint
