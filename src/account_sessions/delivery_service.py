"""账号绑定的内容投递用例。

执行单在创建时固化平台、账号、操作者和内容版本；后台执行过程中不读取
“当前账号”之类的全局状态。公开发布默认关闭，并额外要求一次性确认令牌。
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from account_sessions.account_service import (
    AccountSessionService,
    activity_for,
    public_account,
)
from account_sessions.contracts import DeliveryRequest
from account_sessions.errors import (
    AccountNotFoundError,
    AccountUnavailableError,
    ConfirmationInvalidError,
    ConfirmationRequiredError,
    PublicPublishDisabledError,
)
from account_sessions.models import (
    AccountActivity,
    DeliveryOperation,
    PlatformAccount,
    PublishConfirmation,
)
from account_sessions.permissions import AccessContext
from account_sessions.security import (
    canonical_platform_selection,
    content_version,
    delivery_fingerprint,
    platform_selection_hash,
    safe_error_message,
)
from platforms.content_validation import safe_media_error
from platforms.media_progress import safe_media_progress

SESSION_INVALIDATING_ERROR_CODES = frozenset({"LOGIN_REQUIRED", "SESSION_EXPIRED"})
RESULT_UNKNOWN_ERROR_CODES = frozenset(
    {"DRAFT_RESULT_UNKNOWN", "PUBLISH_RESULT_UNKNOWN", "DELIVERY_RESULT_UNKNOWN"}
)
ARTICLE_MAPPING_SUCCESS_STATUSES = frozenset(
    {"DRAFT_SAVED", "DRAFT_SAVED_WITH_WARNINGS", "PUBLISHED", "PUBLISHED_WITH_WARNINGS"}
)
ARTICLE_MAPPING_PENDING_STATUSES = frozenset({"PENDING", "FAILED"})
ARTICLE_MAPPING_NOT_PENDING = "NOT_PENDING"
ARTICLE_MAPPING_PENDING = "PENDING"
ARTICLE_MAPPING_SUCCEEDED = "SUCCEEDED"
ARTICLE_MAPPING_FAILED = "FAILED"
ARTICLE_MAPPING_FAILED_CODE = "ARTICLE_MAPPING_FAILED"
_STABLE_MAPPING_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class BufferedPlatformLog:
    """适配旧发布流水线的同步日志接口，但不写旧任务数据库。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def add_task_log(self, _task_id: int, level: str, message: str) -> None:
        self.entries.append((str(level or "INFO").upper(), safe_error_message(message)))


class DeliveryService:
    def __init__(
        self,
        accounts: AccountSessionService,
        *,
        platform_factory: Callable[[PlatformAccount], Any] | None = None,
        public_publish_enabled: bool | None = None,
        content_resolver: Callable[
            [str],
            Awaitable[
                tuple[str, list[dict], list[dict]]
                | tuple[str, list[dict], list[dict], dict]
            ],
        ]
        | None = None,
        delivery_event_sink: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.accounts = accounts
        self.database = accounts.database
        self.platform_factory = platform_factory or accounts.platform_factory
        self.content_resolver = content_resolver
        self.delivery_event_sink = delivery_event_sink
        self.public_publish_enabled = (
            _env_flag("ACCOUNT_SESSIONS_ALLOW_PUBLIC_PUBLISH")
            if public_publish_enabled is None
            else public_publish_enabled
        )

    async def request_delivery(
        self,
        request: DeliveryRequest,
        access: AccessContext,
        *,
        frozen_content_hash: str | None = None,
        content_reference: str | None = None,
        confirmation_scope: str | None = None,
        request_key: str | None = None,
        persist_login_snapshot: bool | None = None,
    ) -> dict:
        capability = "draft.create" if request.mode == "DRAFT" else "publish.request"
        account = await self.accounts.require_account(
            request.account_id,
            request.platform,
            access,
            capability,
        )
        if request_key:
            existing = await self._load_operation_by_request_key(request_key)
            if existing is not None:
                operation, existing_account = existing
                self._assert_idempotent_match(
                    operation,
                    request=request,
                    access=access,
                    content_reference=content_reference,
                    persist_login_snapshot=persist_login_snapshot,
                    platform_selection=request.platform_selection,
                )
                return operation_payload(operation, existing_account)
        if account.status != "ACTIVE" or account.session_status != "VALID":
            raise AccountUnavailableError("所选账号登录态当前不可用")

        if request.mode in {"PUBLISH", "PRIVATE_PUBLISH"}:
            if not request.confirmation_token:
                raise await self._new_confirmation(
                    request,
                    account,
                    access,
                    frozen_content_hash=frozen_content_hash,
                    confirmation_scope=confirmation_scope,
                )
            await self._consume_confirmation(
                request,
                account,
                access,
                frozen_content_hash=frozen_content_hash,
                confirmation_scope=confirmation_scope,
            )
            access.require("publish.execute", account.account_id)
            if request.mode == "PUBLISH" and not self.public_publish_enabled:
                raise PublicPublishDisabledError("公开发布总开关保持关闭；本阶段只允许保存草稿")

        operation_id = str(uuid.uuid4())
        operation = DeliveryOperation(
            operation_id=operation_id,
            request_key=request_key,
            account_id=account.account_id,
            platform=account.platform,
            mode=request.mode,
            source=access.source,
            actor_id=access.actor_id,
            title=request.article.title,
            body=(
                "[Content Studio content reference]" if content_reference else request.article.body
            ),
            content_reference=content_reference,
            persist_login_snapshot=persist_login_snapshot,
            platform_selection_snapshot=(
                dict(request.platform_selection)
                if request.platform_selection is not None
                else None
            ),
            content_version=(
                frozen_content_hash or content_version(request.article.title, request.article.body)
            ),
            account_display_name_snapshot=account.display_name,
            status="QUEUED",
            article_mapping_status=ARTICLE_MAPPING_NOT_PENDING,
            confirmation_used=request.mode in {"PUBLISH", "PRIVATE_PUBLISH"},
        )
        try:
            async with self.database.session() as session:
                session.add(operation)
                session.add(
                    activity_for(
                        account,
                        access,
                        action="DELIVERY_QUEUED",
                        message=(
                            "草稿保存执行单已创建"
                            if request.mode == "DRAFT"
                            else "仅自己可见发布执行单已创建"
                            if request.mode == "PRIVATE_PUBLISH"
                            else "公开发布执行单已创建"
                        ),
                        operation_id=operation_id,
                    )
                )
        except IntegrityError:
            if not request_key:
                raise
            existing = await self._load_operation_by_request_key(request_key)
            if existing is None:
                raise
            operation, account = existing
            self._assert_idempotent_match(
                operation,
                request=request,
                access=access,
                content_reference=content_reference,
                persist_login_snapshot=persist_login_snapshot,
                platform_selection=request.platform_selection,
            )
        return operation_payload(operation, account)

    async def execute_operation(
        self,
        operation_id: str,
        access: AccessContext,
    ) -> dict:
        operation, account = await self._load_operation(operation_id)
        capability = "draft.create" if operation.mode == "DRAFT" else "publish.execute"
        access.require(capability, account.account_id)
        if operation.status != "QUEUED":
            return operation_payload(operation, account)

        if not await self._mark_started(operation_id, account, access):
            current, current_account = await self._load_operation(operation_id)
            return operation_payload(current, current_account)
        buffered_log = BufferedPlatformLog()
        platform = None
        selection_outcome: dict | None = None
        try:
            platform = self.platform_factory(account)
            if operation.content_reference:
                if self.content_resolver is None:
                    raise AccountUnavailableError(
                        "内容版本解析器不可用",
                        error_code="CONTENT_VERSION_UNAVAILABLE",
                    )
                resolved = await self.content_resolver(operation.content_reference)
                if not isinstance(resolved, tuple) or len(resolved) not in {3, 4}:
                    raise AccountUnavailableError(
                        "内容版本解析结果不符合投递契约",
                        error_code="CONTENT_VERSION_PAYLOAD_INVALID",
                    )
                if len(resolved) == 4:
                    resolved_title, content_blocks, images, cover = resolved
                else:
                    resolved_title, content_blocks, images = resolved
                    cover = {"strategy": "NONE", "asset_id": None}
                if not isinstance(cover, dict):
                    raise AccountUnavailableError(
                        "内容版本封面载荷不符合投递契约",
                        error_code="CONTENT_VERSION_COVER_INVALID",
                    )
                cover_strategy = str(cover.get("strategy") or "NONE").upper()
                if cover_strategy not in {"NONE", "FIRST_BODY_IMAGE", "EXPLICIT"}:
                    raise AccountUnavailableError(
                        "内容版本封面策略不受支持",
                        error_code="CONTENT_VERSION_COVER_INVALID",
                    )
                if cover_strategy != "NONE" and not cover.get("local_path"):
                    raise AccountUnavailableError(
                        "内容版本封面素材不可用",
                        error_code="CONTENT_VERSION_COVER_UNAVAILABLE",
                    )
                cover = {**cover, "strategy": cover_strategy}
            else:
                resolved_title = operation.title
                content_blocks = [{"type": "text", "text": operation.body}]
                images = []
                cover = {"strategy": "NONE", "asset_id": None}
            with self.accounts._lease(account, purpose=operation.mode):
                try:
                    await platform.initialize()
                    # 即使账号页曾显示 VALID，也必须在同一 Profile 租约内
                    # 再次做只读登录态/身份确认；任何失败都在 publish 前终止，
                    # 避免把残留或错绑 Profile 的内容投递出去。
                    await self.accounts.assert_delivery_identity(
                        account,
                        platform,
                        access,
                    )
                    publish_method = platform.publish
                    extra = {}
                    if operation.mode == "PRIVATE_PUBLISH":
                        if account.platform != "xiaohongshu" or not operation.confirmation_used:
                            raise AccountUnavailableError(
                                "仅自己可见发布未确认或平台不支持",
                                error_code="PRIVATE_PUBLISH_NOT_CONFIRMED",
                            )
                        publish_method = platform.publish_private_article
                        extra["confirmed"] = True
                    result = await publish_method(
                        title=resolved_title,
                        content_blocks=content_blocks,
                        images=images,
                        cover=cover,
                        task_id=0,
                        db=buffered_log,
                        auto_login=False,
                        delivery_mode=operation.mode,
                        selection_override=getattr(
                            operation, "platform_selection_snapshot", None
                        ),
                        **extra,
                    )
                    selection_outcome = _selection_outcome(result)
                finally:
                    # CDP/持久 Profile 必须先关闭浏览器资源，再释放跨进程租约。
                    # initialize() 即使只完成了一半也必须走同一清理路径。
                    await self._cleanup_platform_after_operation(
                        platform,
                        account,
                        operation,
                        access,
                        operation_id,
                    )
            if not result.get("success"):
                media_progress = safe_media_progress(result.get("media_progress"))
                if media_progress is not None:
                    buffered_log.add_task_log(
                        0,
                        "WARN",
                        _format_media_progress_log(media_progress),
                    )
                raise AccountUnavailableError(
                    result.get("error") or "平台未确认投递成功",
                    error_code=result.get("error_code") or "DELIVERY_FAILED",
                    evidence=result.get("verification_evidence"),
                )
            media_incomplete = result.get("media_status") in {"partial", "failed"}
            verification_evidence = result.get("verification_evidence")
            if operation.mode == "PRIVATE_PUBLISH":
                if (
                    result.get("status") != "SUBMITTED"
                    or result.get("visibility") != "SELF_ONLY"
                    or not isinstance(verification_evidence, dict)
                    or verification_evidence.get("submit_acknowledged") is not True
                ):
                    raise AccountUnavailableError(
                        "发布已尝试，但平台未明确确认接收；不会自动重发",
                        error_code="PUBLISH_RESULT_UNKNOWN",
                    )
                return await self._mark_completed(
                    operation_id, account, access, result, buffered_log.entries,
                    selection_outcome=selection_outcome,
                )
            entity_confirmed = _evidence_confirms_entity(verification_evidence)
            content_confirmed = _evidence_confirms_complete_draft(
                verification_evidence
            )
            if operation.mode == "DRAFT" and not entity_confirmed:
                # 适配器返回 success/draft_url 也不能代替本次副作用证明。
                # 未绑定稳定草稿 ID 时，可能已经发生保存，必须停为结果未知，
                # 不能用同名标题、当前页 URL 或普通 2xx 伪报成功。
                raise AccountUnavailableError(
                    "平台可能已执行保存，但未能绑定本次云端草稿实体",
                    error_code="DRAFT_RESULT_UNKNOWN",
                    evidence=verification_evidence,
                )
            verification_warning = bool(
                result.get("draft_verification_warning")
            ) or (
                operation.mode == "DRAFT"
                and entity_confirmed
                and not content_confirmed
            )
            if verification_warning:
                result.setdefault("draft_verification_warning", True)
                result.setdefault(
                    "draft_verification_warning_message",
                    "云端草稿实体已确认，但重开后的完整图文仍需核对",
                )
            cover_strategy = str(cover.get("strategy") or "NONE").upper()
            cover_status = result.get("cover_status")
            cover_incomplete = (
                cover_strategy != "NONE" and cover_status != "completed"
            )
            if cover_incomplete:
                result.setdefault("cover_status", "unverified")
                result.setdefault("cover_error_code", "PLATFORM_COVER_UNVERIFIED")
                result.setdefault("cover_error", "平台未确认封面设置成功")
            if media_incomplete and not result.get("draft_url") and not entity_confirmed:
                # 草稿都没保存下来，才整体判失败
                raise AccountUnavailableError(
                    "图片未完整写入平台且草稿未保存",
                    error_code=(result.get("media_error_code") or "PLATFORM_MEDIA_INCOMPLETE"),
                )
            if operation.mode == "PUBLISH" and _is_zol_public_submission(account.platform, result):
                if media_incomplete or cover_incomplete or verification_warning:
                    raise AccountUnavailableError(
                        "平台接收回执与内容完整性结果冲突，需人工核对",
                        error_code="PUBLISH_RESULT_UNKNOWN",
                    )
                return await self._mark_completed(
                    operation_id, account, access, result, buffered_log.entries,
                    selection_outcome=selection_outcome,
                )
            if operation.mode == "PUBLISH" and not result.get("post_url"):
                raise AccountUnavailableError(
                    "平台未返回公开文章地址，发布结果未知",
                    error_code="PUBLISH_RESULT_UNKNOWN",
                )
            if media_incomplete or cover_incomplete or verification_warning:
                # 草稿已保存但正文图片或封面未完整：如实标记 WITH_WARNINGS，
                # 绝不伪装成完整成功，也绝不把已保存的草稿抹成失败。
                return await self._mark_completed_with_warnings(
                    operation_id,
                    account,
                    access,
                    result,
                    buffered_log.entries,
                    selection_outcome=selection_outcome,
                )
            return await self._mark_completed(
                operation_id,
                account,
                access,
                result,
                buffered_log.entries,
                selection_outcome=selection_outcome,
            )
        except BaseException as exc:
            # 完成事务已经提交后，桥接取消/异常不能把真实成功降级为 FAILED；
            # 启动 recovery 会重试仍为 PENDING/FAILED 的映射。
            try:
                current_operation, _ = await self._load_operation(operation_id)
            except Exception:
                current_operation = None
            if (
                current_operation is None
                or current_operation.status not in ARTICLE_MAPPING_SUCCESS_STATUSES | {"SUBMITTED"}
            ):
                await self._mark_failed(
                    operation_id,
                    account,
                    access,
                    exc,
                    buffered_log.entries,
                    selection_outcome=selection_outcome,
                )
            raise

    async def _cleanup_platform_after_operation(
        self,
        platform: Any,
        account: PlatformAccount,
        operation: DeliveryOperation,
        access: AccessContext,
        operation_id: str,
    ) -> None:
        """在 Profile 租约内清理会话和浏览器资源。"""

        persist_login = (
            account.persist_login
            if operation.persist_login_snapshot is None
            else operation.persist_login_snapshot
        )
        try:
            if not persist_login and getattr(platform, "context", None) is not None:
                try:
                    await platform.context.clear_cookies()
                    await self._record_session_cleanup(
                        account,
                        access,
                        operation_id,
                        success=True,
                    )
                except Exception as exc:
                    await self._record_session_cleanup(
                        account,
                        access,
                        operation_id,
                        success=False,
                        error=exc,
                    )
                finally:
                    await self._mark_session_login_required(account.account_id)
        finally:
            try:
                await platform.cleanup()
            except Exception as exc:
                await self._record_session_cleanup(
                    account,
                    access,
                    operation_id,
                    success=False,
                    error=exc,
                )

    async def get_operation(
        self,
        operation_id: str,
        access: AccessContext,
    ) -> dict:
        operation, account = await self._load_operation(operation_id)
        access.require("logs.read", account.account_id)
        return operation_payload(operation, account)

    async def reconcile_readonly_draft_verification(
        self,
        operation_id: str,
        access: AccessContext,
        probe: dict,
    ) -> dict:
        """用只读草稿证据调和结果未知执行单，绝不重新执行平台操作。

        只读探针只能证明云端草稿实体存在，不能证明冻结图文完整，因此只会
        升级为 ``DRAFT_SAVED_WITH_WARNINGS``。能与原执行单 URL/ID 对上时记录
        为精确绑定；只有唯一同标题卡片时也允许解除“失败”展示，但明确记录
        为“未归因到本次执行”，不创建文章映射。
        """

        operation, account = await self._load_operation(operation_id)
        access.require("draft.create", account.account_id)
        access.require("logs.read", account.account_id)

        previous_status = str(operation.status or "")
        current_payload = operation_payload(operation, account)
        response = {
            "status_updated": False,
            "previous_status": previous_status,
            "operation_status": previous_status,
            "operation": current_payload,
        }
        if operation.mode != "DRAFT":
            response["reconciliation_reason"] = "NOT_DRAFT_OPERATION"
            return response
        if previous_status not in {"RESULT_UNKNOWN", "DELIVERY_INCOMPLETE"}:
            response["reconciliation_reason"] = "STATUS_NOT_RECONCILABLE"
            return response

        existing_evidence = _decode_evidence(operation.verification_evidence) or {}
        binding_method = _readonly_probe_binding_method(
            operation,
            existing_evidence,
            probe,
        )
        if binding_method is None:
            response["reconciliation_reason"] = "DRAFT_ENTITY_NOT_BOUND"
            return response

        now = datetime.now(timezone.utc)
        draft_url = str(probe.get("draft_url") or "").strip()
        candidate_id = _readonly_probe_entity_id(
            operation.platform,
            draft_url,
            probe.get("structure"),
        )
        previous_error_code = operation.error_code
        exact_binding = binding_method in {"draft_url", "platform_article_id"}
        merged_evidence = dict(existing_evidence)
        merged_evidence.update(
            {
                "draft_url": draft_url,
                "draft_list_title_unique": int(probe.get("match_count") or 0) == 1,
                "draft_list_match_count": int(probe.get("match_count") or 0),
            }
        )
        if exact_binding:
            merged_evidence["draft_entity_bound"] = True
            merged_evidence["draft_entity_source"] = (
                existing_evidence.get("draft_entity_source")
                or "existing_draft_id"
            )
        else:
            merged_evidence["draft_entity_bound"] = False
            merged_evidence["draft_entity_source"] = "title_match_without_baseline"
            merged_evidence["draft_entity_id_match"] = False
        if binding_method in {"draft_url", "platform_article_id"}:
            merged_evidence["draft_entity_id_match"] = True
        merged_evidence["readonly_probe"] = {
            "confirmed_at": now.isoformat(),
            "draft_url": draft_url,
            "entity_id": candidate_id,
            "binding_method": binding_method,
            "attributed_to_operation": exact_binding,
            "match_count": int(probe.get("match_count") or 0),
            "previous_status": previous_status,
            "previous_error_code": previous_error_code,
            "previous_entity_bound": existing_evidence.get("draft_entity_bound"),
        }
        evidence_json = json.dumps(merged_evidence, ensure_ascii=False)
        warning_code = (
            "DRAFT_CONTENT_UNVERIFIED"
            if exact_binding
            else "DRAFT_ENTITY_UNATTRIBUTED"
        )
        warning_message = (
            "只读核验已确认云端草稿存在，完整图文仍需人工核对"
            if exact_binding
            else "平台存在唯一同标题草稿，但未能证明由本次执行新建；请人工核对"
        )

        previous_evidence_raw = operation.verification_evidence
        evidence_condition = (
            DeliveryOperation.verification_evidence.is_(None)
            if previous_evidence_raw is None
            else DeliveryOperation.verification_evidence == previous_evidence_raw
        )
        values: dict[str, Any] = {
            "status": "DRAFT_SAVED_WITH_WARNINGS",
            "draft_url": draft_url,
            "error_code": warning_code,
            "error_message": warning_message,
            "verification_evidence": evidence_json,
        }
        if exact_binding:
            values.update(
                {
                    "article_mapping_status": (
                        ARTICLE_MAPPING_PENDING
                        if self.delivery_event_sink is not None
                        else ARTICLE_MAPPING_NOT_PENDING
                    ),
                    "article_mapping_error_code": None,
                    "article_mapping_last_attempt_at": None,
                }
            )
        if exact_binding and candidate_id:
            values["platform_article_id"] = candidate_id

        updated = False
        async with self.database.session() as session:
            result = await session.execute(
                update(DeliveryOperation)
                .where(
                    DeliveryOperation.operation_id == operation_id,
                    DeliveryOperation.mode == "DRAFT",
                    DeliveryOperation.status == previous_status,
                    evidence_condition,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            updated = result.rowcount == 1
            if updated:
                session.add(
                    activity_for(
                        account,
                        access,
                        action="DRAFT_PROBE_STATUS_RECONCILED",
                        level="WARN",
                        message=(
                            "只读核验确认云端草稿实体，执行单由"
                            f" {previous_status} 调和为草稿已保存（需核对）"
                        ),
                        operation_id=operation_id,
                    )
                )

        if updated and exact_binding:
            await self._dispatch_article_mapping(operation_id)
        operation, current_account = await self._load_operation(operation_id)
        response.update(
            {
                "status_updated": updated,
                "operation_status": operation.status,
                "operation": operation_payload(operation, current_account),
                "reconciliation_reason": (
                    (
                        "DRAFT_ENTITY_CONFIRMED"
                        if exact_binding
                        else "DRAFT_CARD_CONFIRMED_UNATTRIBUTED"
                    )
                    if updated
                    else "CONCURRENT_STATE_CHANGED"
                ),
            }
        )
        return response

    async def list_recent_operations(
        self,
        access: AccessContext,
        *,
        limit: int = 20,
    ) -> list[dict]:
        """返回最近投递执行记录（脱敏：仅公开快照字段，不暴露账号细节）。"""

        async with self.database.session() as session:
            statement = (
                select(DeliveryOperation)
                .order_by(
                    DeliveryOperation.created_at.desc(),
                    DeliveryOperation.operation_id.desc(),
                )
                .limit(min(max(limit, 1), 50))
            )
            rows = list((await session.scalars(statement)).all())
        return [
            {
                "operation_id": operation.operation_id,
                "platform": operation.platform,
                "account_display_name": operation.account_display_name_snapshot,
                "mode": operation.mode,
                "status": operation.status,
                "draft_url": operation.draft_url,
                "platform_article_id": operation.platform_article_id,
                "article_mapping_status": operation.article_mapping_status,
                "article_mapping_attempts": operation.article_mapping_attempts,
                "article_mapping_error_code": operation.article_mapping_error_code,
                "article_mapping_last_attempt_at": _iso(
                    operation.article_mapping_last_attempt_at
                ),
                "error_code": operation.error_code,
                "error_message": operation.error_message,
                "verification_evidence": _decode_evidence(
                    operation.verification_evidence
                ),
                "degraded": operation.degraded,
                "created_at": _iso(operation.created_at),
                "completed_at": _iso(operation.completed_at),
            }
            for operation in rows
        ]

    async def reconcile_interrupted_operations(self) -> None:
        """启动恢复：QUEUED 可续跑，RUNNING 标为结果未知，绝不自动重放。"""

        now = datetime.now(timezone.utc)
        async with self.database.session() as session:
            interrupted = list(
                (
                    await session.scalars(
                        select(DeliveryOperation).where(DeliveryOperation.status == "RUNNING")
                    )
                ).all()
            )
            for operation in interrupted:
                operation.status = "RESULT_UNKNOWN"
                operation.error_code = "DELIVERY_RESULT_UNKNOWN"
                operation.error_message = "服务中断时平台操作正在执行，请人工核对平台结果"
                operation.completed_at = now

    async def _new_confirmation(
        self,
        request: DeliveryRequest,
        account: PlatformAccount,
        access: AccessContext,
        *,
        frozen_content_hash: str | None = None,
        confirmation_scope: str | None = None,
    ) -> ConfirmationRequiredError:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        fingerprint = _fingerprint(
            request,
            frozen_content_hash=frozen_content_hash,
            confirmation_scope=confirmation_scope,
        )
        async with self.database.session() as session:
            session.add(
                PublishConfirmation(
                    token_hash=_token_hash(token),
                    fingerprint=fingerprint,
                    account_id=account.account_id,
                    actor_id=access.actor_id,
                    expires_at=expires,
                )
            )
            session.add(
                activity_for(
                    account,
                    access,
                    action="PUBLISH_CONFIRMATION_REQUESTED",
                    message="已生成一次性发布确认令牌",
                )
            )
        return ConfirmationRequiredError(
            token,
            expires.isoformat(),
            {
                "platform": account.platform,
                "account": public_account(account),
                "title": request.article.title,
                "mode": request.mode,
            },
        )

    async def _consume_confirmation(
        self,
        request: DeliveryRequest,
        account: PlatformAccount,
        access: AccessContext,
        *,
        frozen_content_hash: str | None = None,
        confirmation_scope: str | None = None,
    ) -> None:
        token_hash = _token_hash(request.confirmation_token or "")
        now = datetime.now(timezone.utc)
        async with self.database.session() as session:
            confirmation = await session.get(PublishConfirmation, token_hash)
            if confirmation is None:
                raise ConfirmationInvalidError("公开发布确认令牌无效")
            expires_at = confirmation.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            valid = (
                confirmation.used_at is None
                and expires_at > now
                and confirmation.account_id == account.account_id
                and confirmation.actor_id == access.actor_id
                and hmac.compare_digest(
                    confirmation.fingerprint,
                    _fingerprint(
                        request,
                        frozen_content_hash=frozen_content_hash,
                        confirmation_scope=confirmation_scope,
                    ),
                )
            )
            if not valid:
                raise ConfirmationInvalidError("公开发布确认令牌已失效或与当前内容不匹配")
            confirmation.used_at = now

    async def _load_operation(
        self,
        operation_id: str,
    ) -> tuple[DeliveryOperation, PlatformAccount]:
        async with self.database.session() as session:
            operation = await session.get(DeliveryOperation, operation_id)
            if operation is None:
                raise AccountNotFoundError("投递执行单不存在")
            account = await session.get(PlatformAccount, operation.account_id)
            if account is None:
                raise AccountNotFoundError("投递账号不存在")
            return operation, account

    async def _load_operation_by_request_key(
        self,
        request_key: str,
    ) -> tuple[DeliveryOperation, PlatformAccount] | None:
        async with self.database.session() as session:
            operation = await session.scalar(
                select(DeliveryOperation).where(DeliveryOperation.request_key == request_key)
            )
            if operation is None:
                return None
            account = await session.get(PlatformAccount, operation.account_id)
            if account is None:
                raise AccountNotFoundError("投递账号不存在")
            return operation, account

    @staticmethod
    def _assert_idempotent_match(
        operation: DeliveryOperation,
        *,
        request: DeliveryRequest,
        access: AccessContext,
        content_reference: str | None,
        persist_login_snapshot: bool | None,
        platform_selection: dict | None,
    ) -> None:
        stored_platform_selection = getattr(
            operation, "platform_selection_snapshot", None
        )
        if (
            operation.account_id != request.account_id
            or operation.platform != request.platform
            or operation.mode != request.mode
            or operation.actor_id != access.actor_id
            or operation.source != access.source
            or operation.content_reference != content_reference
            or operation.persist_login_snapshot != persist_login_snapshot
            or platform_selection_hash(stored_platform_selection)
            != platform_selection_hash(platform_selection)
            or canonical_platform_selection(stored_platform_selection)
            != canonical_platform_selection(platform_selection)
        ):
            raise AccountUnavailableError(
                "投递幂等键与既有执行单不匹配",
                error_code="DELIVERY_IDEMPOTENCY_CONFLICT",
            )

    async def _mark_started(
        self,
        operation_id: str,
        account: PlatformAccount,
        access: AccessContext,
    ) -> bool:
        async with self.database.session() as session:
            claimed = await session.execute(
                update(DeliveryOperation)
                .where(
                    DeliveryOperation.operation_id == operation_id,
                    DeliveryOperation.status == "QUEUED",
                )
                .values(status="RUNNING", started_at=datetime.now(timezone.utc))
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                return False
            session.add(
                activity_for(
                    account,
                    access,
                    action="DELIVERY_STARTED",
                    message="平台自动化已开始",
                    operation_id=operation_id,
                )
            )
            return True

    async def _mark_completed(
        self,
        operation_id: str,
        account: PlatformAccount,
        access: AccessContext,
        result: dict,
        logs: list[tuple[str, str]],
        *,
        selection_outcome: dict | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        platform_article_id = _extract_platform_article_id(result)
        async with self.database.session() as session:
            operation = await session.get(DeliveryOperation, operation_id)
            if operation is None:
                raise AccountNotFoundError("投递执行单不存在")
            operation.status = (
                "SUBMITTED" if operation.mode == "PRIVATE_PUBLISH" or (
                    operation.mode == "PUBLISH"
                    and _is_zol_public_submission(account.platform, result)
                )
                else "DRAFT_SAVED" if operation.mode == "DRAFT" else "PUBLISHED"
            )
            operation.draft_url = result.get("draft_url")
            operation.platform_url = result.get("post_url") or None
            operation.platform_article_id = platform_article_id
            operation.degraded = result.get("degraded")
            _apply_selection_outcome(operation, selection_outcome)
            evidence = result.get("verification_evidence")
            if evidence is not None:
                operation.verification_evidence = json.dumps(
                    evidence, ensure_ascii=False
                )
            operation.article_mapping_status = (
                ARTICLE_MAPPING_PENDING
                if self.delivery_event_sink is not None and operation.status != "SUBMITTED"
                else ARTICLE_MAPPING_NOT_PENDING
            )
            operation.article_mapping_error_code = None
            operation.article_mapping_last_attempt_at = None
            operation.completed_at = now
            _append_buffered_logs(session, operation, account, access, logs)
            session.add(
                activity_for(
                    account,
                    access,
                    action=operation.status,
                    message=(
                        "平台已确认草稿保存成功"
                        if operation.mode == "DRAFT"
                        else "平台已接收仅自己可见发布，不等待审核结果"
                        if operation.mode == "PRIVATE_PUBLISH"
                        else "平台已接收公开投稿，不等待审核结果"
                        if operation.status == "SUBMITTED"
                        else "平台已确认公开发布成功"
                    ),
                    operation_id=operation_id,
                )
            )
            await session.flush()
        # 事务已退出并提交；跨库 sink 绝不能持有账号库写事务。
        await self._dispatch_article_mapping(operation_id)
        operation, current_account = await self._load_operation(operation_id)
        return operation_payload(operation, current_account)

    async def _mark_completed_with_warnings(
        self,
        operation_id: str,
        account: PlatformAccount,
        access: AccessContext,
        result: dict,
        logs: list[tuple[str, str]],
        *,
        selection_outcome: dict | None = None,
    ) -> dict:
        """草稿已保存但正文媒体或封面未完整：保留链接并标记 WITH_WARNINGS。

        图片失败信息写入 error_message（业务可读，不伪装成功）；PlatformArticle
        桥接照常执行（草稿确实存在），status 由桥接侧记录为 UNMAPPED 草稿。
        """
        now = datetime.now(timezone.utc)
        platform_article_id = _extract_platform_article_id(result)
        async with self.database.session() as session:
            operation = await session.get(DeliveryOperation, operation_id)
            if operation is None:
                raise AccountNotFoundError("投递执行单不存在")
            base = "DRAFT_SAVED" if operation.mode == "DRAFT" else "PUBLISHED"
            operation.status = f"{base}_WITH_WARNINGS"
            operation.draft_url = result.get("draft_url")
            operation.platform_url = result.get("post_url") or None
            operation.platform_article_id = platform_article_id
            operation.degraded = result.get("degraded")
            _apply_selection_outcome(operation, selection_outcome)
            evidence = result.get("verification_evidence")
            if evidence is not None:
                operation.verification_evidence = json.dumps(
                    evidence, ensure_ascii=False
                )
            operation.article_mapping_status = (
                ARTICLE_MAPPING_PENDING
                if self.delivery_event_sink is not None
                else ARTICLE_MAPPING_NOT_PENDING
            )
            operation.article_mapping_error_code = None
            operation.article_mapping_last_attempt_at = None
            operation.error_code = (
                result.get("media_error_code")
                or result.get("cover_error_code")
                or "PLATFORM_MEDIA_INCOMPLETE"
            )
            warning_parts: list[str] = []
            if result.get("draft_verification_warning"):
                warning_parts.append(
                    result.get("draft_verification_warning_message")
                    or "本次草稿实体已保存，但正文或图片完整性待核对"
                )
            if result.get("media_status") in {"partial", "failed"}:
                warning_parts.append(
                    result.get("media_error")
                    or (
                        "图片未完整写入平台"
                        f"（{result.get('uploaded_images', 0)}"
                        f"/{result.get('expected_images', 0)}）"
                    )
                )
            if result.get("cover_status") not in {None, "completed", "not_required"}:
                warning_parts.append(
                    result.get("cover_error") or "平台未确认封面设置成功"
                )
            operation.error_message = safe_media_error(
                "；".join(str(part) for part in warning_parts),
                fallback="平台媒体或封面未完整写入",
            )
            operation.completed_at = now
            _append_buffered_logs(session, operation, account, access, logs)
            session.add(
                activity_for(
                    account,
                    access,
                    action="DELIVERY_COMPLETED_WITH_WARNINGS",
                    level="WARN",
                    message=(
                        f"草稿已保存，但媒体或封面未完整: {operation.error_message}"
                    ),
                    operation_id=operation_id,
                )
            )
            await session.flush()
        await self._dispatch_article_mapping(operation_id)
        operation, current_account = await self._load_operation(operation_id)
        return operation_payload(operation, current_account)

    async def _dispatch_article_mapping(self, operation_id: str) -> str:
        """提交后的单次桥接尝试；跨库 sink 始终在事务外调用。"""

        if self.delivery_event_sink is None:
            return "NOT_CONFIGURED"
        async with self.database.session() as session:
            operation = await session.get(DeliveryOperation, operation_id)
            if operation is None:
                return "NOT_FOUND"
            if operation.article_mapping_status not in ARTICLE_MAPPING_PENDING_STATUSES:
                return operation.article_mapping_status
            details = {
                "operation_id": operation.operation_id,
                "platform": operation.platform,
                "mode": operation.mode,
                "title": operation.title,
                "draft_url": operation.draft_url,
                "platform_url": operation.platform_url,
                "platform_article_id": operation.platform_article_id,
                "completed_at": operation.completed_at,
                "content_reference": operation.content_reference,
            }
            attempts = operation.article_mapping_attempts or 0

        if details["completed_at"] is None:
            return "SKIPPED"
        expected_attempt = await self._claim_article_mapping_attempt(
            operation_id,
            attempts=attempts,
        )
        if expected_attempt is None:
            return "SKIPPED"

        # 不捕获 CancelledError/SystemExit/KeyboardInterrupt：账号库中的成功状态
        # 与 PENDING mapping 已经提交，启动 reconcile 会负责补偿。
        try:
            result = self.delivery_event_sink(**details)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001
            code = _stable_article_mapping_error_code(exc)
            updated = await self._set_article_mapping_state(
                operation_id,
                status=ARTICLE_MAPPING_FAILED,
                error_code=code,
                expected_attempt=expected_attempt,
            )
            if not updated:
                return "STALE"
            logging.getLogger(__name__).warning(
                "投递结果桥接失败（执行单仍为成功，稳定码=%s）",
                code,
            )
            return ARTICLE_MAPPING_FAILED
        try:
            updated = await self._set_article_mapping_state(
                operation_id,
                status=ARTICLE_MAPPING_SUCCEEDED,
                error_code=None,
                expected_attempt=expected_attempt,
            )
            if not updated:
                return "STALE"
        except Exception:
            logging.getLogger(__name__).warning(
                "投递结果桥接状态写回失败（执行单仍为成功）"
            )
            return ARTICLE_MAPPING_PENDING
        return ARTICLE_MAPPING_SUCCEEDED

    async def _claim_article_mapping_attempt(
        self,
        operation_id: str,
        *,
        attempts: int,
    ) -> int | None:
        now = datetime.now(timezone.utc)
        async with self.database.session() as session:
            result = await session.execute(
                update(DeliveryOperation)
                .where(
                    DeliveryOperation.operation_id == operation_id,
                    DeliveryOperation.status.in_(ARTICLE_MAPPING_SUCCESS_STATUSES),
                    DeliveryOperation.article_mapping_status.in_(
                        ARTICLE_MAPPING_PENDING_STATUSES
                    ),
                    DeliveryOperation.article_mapping_attempts == attempts,
                )
                .values(
                    article_mapping_attempts=attempts + 1,
                    article_mapping_last_attempt_at=now,
                    article_mapping_error_code=None,
                )
                .execution_options(synchronize_session=False)
            )
            return attempts + 1 if result.rowcount == 1 else None

    async def _set_article_mapping_state(
        self,
        operation_id: str,
        *,
        status: str,
        error_code: str | None,
        expected_attempt: int,
    ) -> bool:
        async with self.database.session() as session:
            result = await session.execute(
                update(DeliveryOperation)
                .where(
                    DeliveryOperation.operation_id == operation_id,
                    DeliveryOperation.status.in_(ARTICLE_MAPPING_SUCCESS_STATUSES),
                    DeliveryOperation.article_mapping_attempts == expected_attempt,
                    DeliveryOperation.article_mapping_status.in_(
                        ARTICLE_MAPPING_PENDING_STATUSES
                    ),
                )
                .values(
                    article_mapping_status=status,
                    article_mapping_error_code=error_code,
                )
                .execution_options(synchronize_session=False)
            )
            return result.rowcount == 1

    async def reconcile_pending_article_mappings(
        self,
        *,
        limit: int = 20,
    ) -> dict[str, int]:
        """在启动时有限重试已成功投递但未完成文章映射的执行单。"""

        if self.delivery_event_sink is None:
            return {"processed": 0, "succeeded": 0, "failed": 0, "skipped": 0}
        bounded = min(max(limit, 1), 50)
        async with self.database.session() as session:
            operation_ids = list(
                (
                    await session.scalars(
                        select(DeliveryOperation.operation_id)
                        .where(
                            DeliveryOperation.status.in_(ARTICLE_MAPPING_SUCCESS_STATUSES),
                            DeliveryOperation.article_mapping_status.in_(
                                ARTICLE_MAPPING_PENDING_STATUSES
                            ),
                        )
                        .order_by(DeliveryOperation.created_at, DeliveryOperation.operation_id)
                        .limit(bounded)
                    )
                ).all()
            )
        counts = {"processed": 0, "succeeded": 0, "failed": 0, "skipped": 0}
        for operation_id in operation_ids:
            outcome = await self._dispatch_article_mapping(operation_id)
            if outcome in {"STALE", "SKIPPED"}:
                counts["skipped"] += 1
                continue
            counts["processed"] += 1
            if outcome == ARTICLE_MAPPING_SUCCEEDED:
                counts["succeeded"] += 1
            elif outcome == ARTICLE_MAPPING_FAILED:
                counts["failed"] += 1
        return counts

    async def _mark_failed(
        self,
        operation_id: str,
        account: PlatformAccount,
        access: AccessContext,
        exc: BaseException,
        logs: list[tuple[str, str]],
        *,
        selection_outcome: dict | None = None,
    ) -> None:
        async with self.database.session() as session:
            operation = await session.get(DeliveryOperation, operation_id)
            if operation is None:
                return
            error_code = getattr(exc, "error_code", None) or "DELIVERY_FAILED"
            result_unknown = error_code in RESULT_UNKNOWN_ERROR_CODES
            if error_code == "DELIVERY_INCOMPLETE":
                # 投递未完成：保存动作已触发但平台侧无任何可确认证据。
                # 这是独立于 FAILED/RESULT_UNKNOWN 的新状态，不伪装成功也不判死失败。
                operation.status = "DELIVERY_INCOMPLETE"
            else:
                operation.status = "RESULT_UNKNOWN" if result_unknown else "FAILED"
            operation.error_code = error_code
            operation.error_message = safe_error_message(exc)
            _apply_selection_outcome(operation, selection_outcome)
            evidence = getattr(exc, "evidence", None)
            if evidence is not None:
                operation.verification_evidence = json.dumps(
                    evidence, ensure_ascii=False
                )
            operation.completed_at = datetime.now(timezone.utc)
            stored_account = await session.get(PlatformAccount, operation.account_id)
            if (
                stored_account is not None
                and operation.error_code in SESSION_INVALIDATING_ERROR_CODES
            ):
                stored_account.session_status = "LOGIN_REQUIRED"
                stored_account.last_verified_at = None
            _append_buffered_logs(session, operation, account, access, logs)
            session.add(
                activity_for(
                    account,
                    access,
                    action=(
                        "DELIVERY_INCOMPLETE"
                        if error_code == "DELIVERY_INCOMPLETE"
                        else (
                            "DELIVERY_RESULT_UNKNOWN"
                            if result_unknown
                            else "DELIVERY_FAILED"
                        )
                    ),
                    level=(
                        "WARN"
                        if error_code == "DELIVERY_INCOMPLETE" or result_unknown
                        else "ERROR"
                    ),
                    message=operation.error_message,
                    operation_id=operation_id,
                )
            )

    async def _record_session_cleanup(
        self,
        account: PlatformAccount,
        access: AccessContext,
        operation_id: str,
        *,
        success: bool,
        error: Exception | None = None,
    ) -> None:
        async with self.database.session() as session:
            session.add(
                activity_for(
                    account,
                    access,
                    action=(
                        "SESSION_CLEARED_AFTER_OPERATION" if success else "SESSION_CLEANUP_FAILED"
                    ),
                    level="INFO" if success else "WARN",
                    message=(
                        "保持登录态已关闭，本次操作结束后已清除目标账号会话"
                        if success
                        else f"操作已结束，但目标账号会话清理失败: {safe_error_message(error)}"
                    ),
                    operation_id=operation_id,
                )
            )

    async def _mark_session_login_required(self, account_id: str) -> None:
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is not None:
                account.session_status = "LOGIN_REQUIRED"
                account.last_verified_at = None


def operation_payload(
    operation: DeliveryOperation,
    account: PlatformAccount,
) -> dict:
    return {
        "operation_id": operation.operation_id,
        "platform": operation.platform,
        "account": public_account(account),
        "mode": operation.mode,
        "status": operation.status,
        "draft_url": operation.draft_url,
        "platform_url": operation.platform_url,
        "platform_article_id": operation.platform_article_id,
        "platform_selection_snapshot": getattr(
            operation, "platform_selection_snapshot", None
        ),
        "platform_selection_result": getattr(
            operation, "platform_selection_result", None
        ),
        "platform_selection_status": getattr(
            operation, "platform_selection_status", None
        ),
        "platform_selection_error": getattr(
            operation, "platform_selection_error", None
        ),
        "platform_selection_error_code": getattr(
            operation, "platform_selection_error_code", None
        ),
        "article_mapping_status": operation.article_mapping_status,
        "article_mapping_attempts": operation.article_mapping_attempts,
        "article_mapping_error_code": operation.article_mapping_error_code,
        "article_mapping_last_attempt_at": _iso(
            operation.article_mapping_last_attempt_at
        ),
        "error_code": operation.error_code,
        "error_message": operation.error_message,
        "verification_evidence": _decode_evidence(
            operation.verification_evidence
        ),
        "degraded": operation.degraded,
        "created_at": _iso(operation.created_at),
        "started_at": _iso(operation.started_at),
        "completed_at": _iso(operation.completed_at),
    }


def _selection_outcome(result: object) -> dict | None:
    """Extract the bounded selection result returned by a platform adapter."""

    if not isinstance(result, Mapping):
        return None
    if not any(
        key in result
        for key in (
            "selection",
            "selection_status",
            "selection_error",
            "selection_error_code",
        )
    ):
        return None

    selection = result.get("selection")
    bounded_selection: dict[str, str | int | float | bool | None] | None = None
    if isinstance(selection, Mapping):
        bounded_selection = {}
        for key, value in selection.items():
            if not isinstance(key, str) or not key.strip() or len(key) > 64:
                continue
            if value is None or isinstance(value, (str, int, float, bool)):
                bounded_selection[key[:64]] = (
                    value[:512] if isinstance(value, str) else value
                )
        if not bounded_selection:
            bounded_selection = None

    outcome: dict[str, object] = {"selection": bounded_selection}
    for result_key, outcome_key, limit in (
        ("selection_status", "status", 32),
        ("selection_error", "error", 1000),
        ("selection_error_code", "error_code", 64),
    ):
        value = result.get(result_key)
        if value is None:
            outcome[outcome_key] = None
            continue
        text = str(value).strip()[:limit]
        outcome[outcome_key] = text or None
    return outcome


def _apply_selection_outcome(
    operation: DeliveryOperation,
    outcome: Mapping[str, object] | None,
) -> None:
    """Persist adapter selection application fields without changing delivery status."""

    if outcome is None:
        return
    selection = outcome.get("selection")
    operation.platform_selection_result = (
        dict(selection) if isinstance(selection, Mapping) else None
    )
    operation.platform_selection_status = _bounded_text(outcome.get("status"), 32)
    operation.platform_selection_error = _bounded_text(outcome.get("error"), 1000)
    operation.platform_selection_error_code = _bounded_error_code(
        outcome.get("error_code")
    )


def _bounded_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()[:limit]
    return text or None


def _bounded_error_code(value: object) -> str | None:
    text = _bounded_text(value, 64)
    if text is None:
        return None
    return text.upper() if _STABLE_MAPPING_CODE.fullmatch(text.upper()) else None


def _decode_evidence(raw: str | None) -> dict | None:
    """把数据库中的证据 JSON 解码为字典；无效或缺失返回 None。"""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _canonical_draft_url(value: object) -> str | None:
    """规范化受控草稿 URL，供同一实体比较；不发起网络请求。"""

    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    path = parsed.path.rstrip("/") or "/"
    fragment = parsed.fragment.rstrip("/")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            query,
            fragment,
        )
    )


def _readonly_probe_entity_id(
    platform: str,
    draft_url: object,
    structure: object,
) -> str | None:
    """从平台白名单 URL/结构字段提取稳定草稿 ID。"""

    raw = str(draft_url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    host = parsed.netloc.lower().split(":", 1)[0]
    allowed_hosts = {
        "xiaoheihe": {"www.xiaoheihe.cn", "xiaoheihe.cn"},
        "zol": {"post.zol.com.cn"},
        "zhihu": {"www.zhihu.com", "zhihu.com", "zhuanlan.zhihu.com"},
        "weibo": {"card.weibo.com", "me.weibo.com", "weibo.com"},
        "smzdm": {"zhiyou.smzdm.com"},
        "toutiao": {"mp.toutiao.com"},
        "baijiahao": {"baijiahao.baidu.com"},
    }
    if host not in allowed_hosts.get(str(platform or ""), set()):
        return None

    structure_id: str | None = None
    if isinstance(structure, dict):
        for key in ("draft_id", "article_id", "pgc_id"):
            value = structure.get(key)
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                continue
            normalized = str(value).strip()
            if normalized and len(normalized) <= 255:
                structure_id = normalized
                break

    query_pairs = [(key.lower(), value) for key, value in parse_qsl(parsed.query)]
    if str(platform or "") == "toutiao":
        pgc_values = [value for key, value in query_pairs if key == "pgc_id"]
        if (
            parsed.scheme.lower() != "https"
            or parsed.path != "/profile_v4/graphic/publish"
            or parsed.fragment
            or len(pgc_values) != 1
            or re.fullmatch(r"\d{6,32}", pgc_values[0]) is None
        ):
            return None
    query = {key: value for key, value in query_pairs}
    url_id: str | None = None
    for key in ("draftid", "draft_id", "article_id", "pgc_id"):
        value = str(query.get(key) or "").strip()
        if value and len(value) <= 255:
            url_id = value
            break

    if url_id is None:
        route = f"{parsed.path}/{parsed.fragment}"
        patterns = {
            "xiaoheihe": r"/article/(\d+)(?:/|$)",
            "zhihu": r"/p/(\d+)(?:/|$)",
            "weibo": r"/draft/(\d+)(?:/|$)",
            "smzdm": r"/edit/(\d+)(?:/|$)",
        }
        pattern = patterns.get(str(platform or ""))
        if pattern:
            match = re.search(pattern, route, flags=re.IGNORECASE)
            if match:
                url_id = match.group(1)

    if structure_id and url_id and structure_id != url_id:
        return None
    return structure_id or url_id


def _readonly_probe_binding_method(
    operation: DeliveryOperation,
    evidence: dict,
    probe: dict,
) -> str | None:
    """确认只读探针命中的是原执行单已知实体，而非仅标题相同。"""

    if probe.get("title_matched") is not True:
        return None
    match_count = int(probe.get("match_count") or 0)
    draft_url = str(probe.get("draft_url") or "").strip()
    candidate_url = _canonical_draft_url(draft_url)
    candidate_id = _readonly_probe_entity_id(
        operation.platform,
        draft_url,
        probe.get("structure"),
    )
    if match_count < 1 or candidate_url is None or candidate_id is None:
        return None

    platform_article_id = str(operation.platform_article_id or "").strip()
    if platform_article_id and platform_article_id != candidate_id:
        return None

    known_urls = {
        value
        for value in (
            _canonical_draft_url(operation.draft_url),
            _canonical_draft_url(evidence.get("draft_url")),
        )
        if value
    }
    if candidate_url in known_urls:
        return "draft_url"

    if platform_article_id and platform_article_id == candidate_id:
        return "platform_article_id"

    # 最低门槛只用于避免“平台明明有草稿，页面却一直失败”：该状态仍是
    # WITH_WARNINGS，且不会创建文章映射或声称属于本次执行。
    if match_count == 1:
        return "unique_title_stable_entity"
    return None


def _evidence_confirms_entity(evidence: object) -> bool:
    """仅把本次实体绑定证据视为已保存；错误 ID 不能靠标题唯一兜底。"""

    if not isinstance(evidence, dict):
        return False
    bound = evidence.get("draft_entity_bound")
    return bound is True


def _evidence_confirms_complete_draft(evidence: object) -> bool:
    """完整成功还必须证明精确实体重开后的标题与图文均一致。"""

    if not _evidence_confirms_entity(evidence):
        return False
    assert isinstance(evidence, dict)
    return (
        evidence.get("reopen_title_match") is True
        and evidence.get("reopen_dom_blocks_match") is True
    )


def _append_buffered_logs(
    session,
    operation: DeliveryOperation,
    account: PlatformAccount,
    access: AccessContext,
    logs: list[tuple[str, str]],
) -> None:
    for level, message in logs:
        session.add(
            AccountActivity(
                account_id=account.account_id,
                operation_id=operation.operation_id,
                platform=account.platform,
                display_name_snapshot=operation.account_display_name_snapshot,
                actor_id=access.actor_id,
                source=access.source,
                action="PLATFORM_LOG",
                level=level,
                message=message,
            )
        )


def _fingerprint(
    request: DeliveryRequest,
    *,
    frozen_content_hash: str | None = None,
    confirmation_scope: str | None = None,
) -> str:
    return delivery_fingerprint(
        platform=request.platform,
        account_id=request.account_id,
        title=request.article.title,
        body=(
            f"content-version:{frozen_content_hash};target:{confirmation_scope or '-'}"
            if frozen_content_hash
            else request.article.body
        ),
        mode=request.mode,
        platform_selection=request.platform_selection,
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _format_media_progress_log(progress: dict[str, int | str]) -> str:
    """生成只包含安全计数和状态的固定媒体进度审计消息。"""

    return (
        "MEDIA_PROGRESS 媒体进度："
        f"expected_images={progress['expected_images']}，"
        f"uploaded_images={progress['uploaded_images']}，"
        f"failed_image_count={progress['failed_image_count']}，"
        f"media_status={progress['media_status']}"
    )


def _is_zol_public_submission(platform: str, result: dict) -> bool:
    evidence = result.get("verification_evidence")
    return (
        platform == "zol" and result.get("status") == "SUBMITTED"
        and isinstance(evidence, dict)
        and evidence.get("submit_acknowledged") is True
        and evidence.get("submission_source") == "zol_publish_response"
        and evidence.get("submission_scope") == "PUBLIC"
    )


def _extract_platform_article_id(result: Any) -> str | None:
    """仅从平台结果的显式白名单字段提取文章 ID，不序列化任意对象。"""

    if not isinstance(result, dict):
        return None
    for key in ("platform_article_id", "post_id", "article_id"):
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        normalized = str(value).strip()
        if normalized and len(normalized) <= 255:
            return normalized
    return None


def _stable_article_mapping_error_code(exc: BaseException) -> str:
    raw = getattr(exc, "error_code", None)
    if isinstance(raw, str):
        code = raw.strip().upper()
        if _STABLE_MAPPING_CODE.fullmatch(code):
            return code
    return ARTICLE_MAPPING_FAILED_CODE


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
