"""账号绑定的内容投递用例。

执行单在创建时固化平台、账号、操作者和内容版本；后台执行过程中不读取
“当前账号”之类的全局状态。公开发布默认关闭，并额外要求一次性确认令牌。
"""

import hashlib
import hmac
import logging
import os
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

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
    content_version,
    delivery_fingerprint,
    safe_error_message,
)

SESSION_INVALIDATING_ERROR_CODES = frozenset({"LOGIN_REQUIRED", "SESSION_EXPIRED"})


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
        content_resolver: Callable[[str], Awaitable[tuple[str, list[dict], list[dict]]]]
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
                )
                return operation_payload(operation, existing_account)
        if account.status != "ACTIVE" or account.session_status != "VALID":
            raise AccountUnavailableError("所选账号登录态当前不可用")

        if request.mode == "PUBLISH":
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
            if not self.public_publish_enabled:
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
            content_version=(
                frozen_content_hash or content_version(request.article.title, request.article.body)
            ),
            account_display_name_snapshot=account.display_name,
            status="QUEUED",
            confirmation_used=request.mode == "PUBLISH",
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
        try:
            platform = self.platform_factory(account)
            if operation.content_reference:
                if self.content_resolver is None:
                    raise AccountUnavailableError(
                        "内容版本解析器不可用",
                        error_code="CONTENT_VERSION_UNAVAILABLE",
                    )
                resolved_title, content_blocks, images = await self.content_resolver(
                    operation.content_reference
                )
            else:
                resolved_title = operation.title
                content_blocks = [{"type": "text", "text": operation.body}]
                images = []
            with self.accounts._lease(account, purpose=operation.mode):
                await platform.initialize()
                result = await platform.publish(
                    title=resolved_title,
                    content_blocks=content_blocks,
                    images=images,
                    task_id=0,
                    db=buffered_log,
                    auto_login=False,
                    delivery_mode=operation.mode,
                )
            if not result.get("success"):
                raise AccountUnavailableError(
                    result.get("error") or "平台未确认投递成功",
                    error_code=result.get("error_code") or "DELIVERY_FAILED",
                )
            media_incomplete = result.get("media_status") in {"partial", "failed"}
            if media_incomplete and not result.get("draft_url"):
                # 草稿都没保存下来，才整体判失败
                raise AccountUnavailableError(
                    "图片未完整写入平台且草稿未保存",
                    error_code=(result.get("media_error_code") or "PLATFORM_MEDIA_INCOMPLETE"),
                )
            if media_incomplete:
                # 草稿已保存但图片未完整：如实标记「已保存（图片未完整）」，
                # 绝不伪装成完整成功，也绝不把已保存的草稿抹成失败。
                return await self._mark_completed_with_warnings(
                    operation_id,
                    account,
                    access,
                    result,
                    buffered_log.entries,
                )
            if operation.mode == "PUBLISH" and not result.get("post_url"):
                raise AccountUnavailableError(
                    "平台未返回公开文章地址，发布结果未知",
                    error_code="PUBLISH_RESULT_UNKNOWN",
                )
            return await self._mark_completed(
                operation_id,
                account,
                access,
                result,
                buffered_log.entries,
            )
        except BaseException as exc:
            await self._mark_failed(
                operation_id,
                account,
                access,
                exc,
                buffered_log.entries,
            )
            raise
        finally:
            if (
                platform is not None
                and not (
                    account.persist_login
                    if operation.persist_login_snapshot is None
                    else operation.persist_login_snapshot
                )
                and platform.context is not None
            ):
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
            if platform is not None:
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
                "error_code": operation.error_code,
                "error_message": operation.error_message,
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
                    message="已生成一次性公开发布确认令牌",
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
    ) -> None:
        if (
            operation.account_id != request.account_id
            or operation.platform != request.platform
            or operation.mode != request.mode
            or operation.actor_id != access.actor_id
            or operation.source != access.source
            or operation.content_reference != content_reference
            or operation.persist_login_snapshot != persist_login_snapshot
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
    ) -> dict:
        async with self.database.session() as session:
            operation = await session.get(DeliveryOperation, operation_id)
            if operation is None:
                raise AccountNotFoundError("投递执行单不存在")
            operation.status = "DRAFT_SAVED" if operation.mode == "DRAFT" else "PUBLISHED"
            operation.draft_url = result.get("draft_url")
            operation.platform_url = result.get("post_url") or None
            operation.completed_at = datetime.now(timezone.utc)
            _append_buffered_logs(session, operation, account, access, logs)
            session.add(
                activity_for(
                    account,
                    access,
                    action=operation.status,
                    message=(
                        "平台已确认草稿保存成功"
                        if operation.mode == "DRAFT"
                        else "平台已确认公开发布成功"
                    ),
                    operation_id=operation_id,
                )
            )
            await session.flush()
            # best-effort 桥接：投递结果映射到 PlatformArticle（失败不影响执行单）
            if self.delivery_event_sink is not None:
                try:
                    await self.delivery_event_sink(
                        operation_id=operation_id,
                        platform=account.platform,
                        mode=operation.mode,
                        title=operation.title,
                        draft_url=operation.draft_url,
                        platform_url=operation.platform_url,
                        completed_at=operation.completed_at,
                        content_reference=operation.content_reference,
                    )
                except BaseException as exc:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        "投递结果桥接失败（执行单仍为成功）: %s",
                        safe_error_message(exc),
                    )
            return operation_payload(operation, account)

    async def _mark_completed_with_warnings(
        self,
        operation_id: str,
        account: PlatformAccount,
        access: AccessContext,
        result: dict,
        logs: list[tuple[str, str]],
    ) -> dict:
        """草稿已保存但图片未完整写入：状态置为 *_WITH_WARNINGS，保留草稿链接。

        图片失败信息写入 error_message（业务可读，不伪装成功）；PlatformArticle
        桥接照常执行（草稿确实存在），status 由桥接侧记录为 UNMAPPED 草稿。
        """
        async with self.database.session() as session:
            operation = await session.get(DeliveryOperation, operation_id)
            if operation is None:
                raise AccountNotFoundError("投递执行单不存在")
            base = "DRAFT_SAVED" if operation.mode == "DRAFT" else "PUBLISHED"
            operation.status = f"{base}_WITH_WARNINGS"
            operation.draft_url = result.get("draft_url")
            operation.platform_url = result.get("post_url") or None
            operation.error_code = result.get("media_error_code") or "PLATFORM_MEDIA_INCOMPLETE"
            operation.error_message = (
                result.get("media_error")
                or (
                    "图片未完整写入平台"
                    f"（{result.get('uploaded_images', 0)}"
                    f"/{result.get('expected_images', 0)}）"
                )
            )
            operation.completed_at = datetime.now(timezone.utc)
            _append_buffered_logs(session, operation, account, access, logs)
            session.add(
                activity_for(
                    account,
                    access,
                    action="DELIVERY_COMPLETED_WITH_WARNINGS",
                    level="WARN",
                    message=(
                        f"草稿已保存，但图片未完整写入: {operation.error_message}"
                    ),
                    operation_id=operation_id,
                )
            )
            await session.flush()
            # 桥接照常执行：草稿真实存在，映射为 UNMAPPED 草稿记录
            if self.delivery_event_sink is not None:
                try:
                    await self.delivery_event_sink(
                        operation_id=operation_id,
                        platform=account.platform,
                        mode=operation.mode,
                        title=operation.title,
                        draft_url=operation.draft_url,
                        platform_url=operation.platform_url,
                        completed_at=operation.completed_at,
                        content_reference=operation.content_reference,
                    )
                except BaseException as exc:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        "投递结果桥接失败（执行单仍为成功）: %s",
                        safe_error_message(exc),
                    )
            return operation_payload(operation, account)

    async def _mark_failed(
        self,
        operation_id: str,
        account: PlatformAccount,
        access: AccessContext,
        exc: BaseException,
        logs: list[tuple[str, str]],
    ) -> None:
        async with self.database.session() as session:
            operation = await session.get(DeliveryOperation, operation_id)
            if operation is None:
                return
            operation.status = "FAILED"
            operation.error_code = getattr(exc, "error_code", None) or "DELIVERY_FAILED"
            operation.error_message = safe_error_message(exc)
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
                    action="DELIVERY_FAILED",
                    level="ERROR",
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
        "error_code": operation.error_code,
        "error_message": operation.error_message,
        "created_at": _iso(operation.created_at),
        "started_at": _iso(operation.started_at),
        "completed_at": _iso(operation.completed_at),
    }


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
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
