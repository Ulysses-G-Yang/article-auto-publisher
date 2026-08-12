"""账号绑定的内容投递用例。

执行单在创建时固化平台、账号、操作者和内容版本；后台执行过程中不读取
“当前账号”之类的全局状态。公开发布默认关闭，并额外要求一次性确认令牌。
"""

import hashlib
import hmac
import os
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

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
        content_resolver: Callable[[str], Awaitable[tuple[list[dict], list[dict]]]] | None = None,
    ) -> None:
        self.accounts = accounts
        self.database = accounts.database
        self.platform_factory = platform_factory or accounts.platform_factory
        self.content_resolver = content_resolver
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
    ) -> dict:
        capability = "draft.create" if request.mode == "DRAFT" else "publish.request"
        account = await self.accounts.require_account(
            request.account_id,
            request.platform,
            access,
            capability,
        )
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
            account_id=account.account_id,
            platform=account.platform,
            mode=request.mode,
            source=access.source,
            actor_id=access.actor_id,
            title=request.article.title,
            body=request.article.body,
            content_reference=content_reference,
            content_version=(
                frozen_content_hash or content_version(request.article.title, request.article.body)
            ),
            account_display_name_snapshot=account.display_name,
            status="QUEUED",
            confirmation_used=request.mode == "PUBLISH",
        )
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

        await self._mark_started(operation_id, account, access)
        platform = self.platform_factory(account)
        buffered_log = BufferedPlatformLog()
        try:
            if operation.content_reference:
                if self.content_resolver is None:
                    raise AccountUnavailableError(
                        "内容版本解析器不可用",
                        error_code="CONTENT_VERSION_UNAVAILABLE",
                    )
                content_blocks, images = await self.content_resolver(operation.content_reference)
            else:
                content_blocks = [{"type": "text", "text": operation.body}]
                images = []
            with self.accounts._lease(account, purpose=operation.mode):
                await platform.initialize()
                result = await platform.publish(
                    title=operation.title,
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
            if not account.persist_login and platform.context is not None:
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
            await platform.cleanup()

    async def get_operation(
        self,
        operation_id: str,
        access: AccessContext,
    ) -> dict:
        operation, account = await self._load_operation(operation_id)
        access.require("logs.read", account.account_id)
        return operation_payload(operation, account)

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

    async def _mark_started(
        self,
        operation_id: str,
        account: PlatformAccount,
        access: AccessContext,
    ) -> None:
        async with self.database.session() as session:
            operation = await session.get(DeliveryOperation, operation_id)
            if operation is None or operation.status != "QUEUED":
                raise AccountUnavailableError("执行单状态已变化，不能重复执行")
            operation.status = "RUNNING"
            operation.started_at = datetime.now(timezone.utc)
            session.add(
                activity_for(
                    account,
                    access,
                    action="DELIVERY_STARTED",
                    message="平台自动化已开始",
                    operation_id=operation_id,
                )
            )

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
                        else f"操作已结束，但目标账号会话清理失败: {error}"
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
