"""多平台账号和持久登录态用例。"""

import logging
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from account_sessions.database import AccountDatabase
from account_sessions.errors import (
    AccountIdentityError,
    AccountNotFoundError,
    AccountPlatformMismatchError,
    AccountSessionError,
)
from account_sessions.identity import extract_identity
from account_sessions.leases import AccountProfileLease, _is_within
from account_sessions.models import (
    AccountActivity,
    DeliveryOperation,
    PlatformAccount,
    PublishConfirmation,
)
from account_sessions.permissions import AccessContext
from account_sessions.platform_catalog import ACCOUNT_ENABLED_PLATFORMS
from account_sessions.runtime_paths import (
    default_root,
    legacy_profile_path,
    managed_profile_path,
)
from account_sessions.security import mask_platform_user_id, safe_error_message

SUPPORTED_PLATFORMS = ACCOUNT_ENABLED_PLATFORMS


class AccountSessionService:
    def __init__(
        self,
        database: AccountDatabase,
        *,
        acquire_legacy_guard: Callable[[str], bool] | None = None,
        release_legacy_guard: Callable[[str], None] | None = None,
        seed_legacy_profiles: bool = True,
        platform_factory: Callable[[PlatformAccount], Any] | None = None,
        allowed_profile_roots: tuple[str | Path, ...] | None = None,
    ) -> None:
        self.database = database
        self.acquire_legacy_guard = acquire_legacy_guard
        self.release_legacy_guard = release_legacy_guard
        self.seed_legacy_profiles = seed_legacy_profiles
        self.platform_factory = platform_factory or _platform_instance
        self.allowed_profile_roots = allowed_profile_roots

    async def initialize(self) -> None:
        await self.database.initialize()
        if self.seed_legacy_profiles:
            await self._seed_legacy_candidates()

    async def _seed_legacy_candidates(self) -> None:
        async with self.database.session() as session:
            for platform in SUPPORTED_PLATFORMS:
                profile = legacy_profile_path(platform)
                if not _looks_like_chrome_profile(profile):
                    continue
                existing = await session.scalar(
                    select(PlatformAccount).where(
                        PlatformAccount.profile_path == str(profile)
                    )
                )
                if existing is not None:
                    continue
                account_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"articleops:legacy-profile:{platform}:{profile}",
                    )
                )
                session.add(
                    PlatformAccount(
                        account_id=account_id,
                        platform=platform,
                        display_name="待验证账号",
                        profile_path=str(profile),
                        is_legacy_profile=True,
                        session_status="UNVERIFIED",
                        persist_login=True,
                    )
                )

    async def list_accounts(
        self,
        platform: str,
        access: AccessContext,
        *,
        usable_only: bool = False,
    ) -> list[dict]:
        _require_platform(platform)
        async with self.database.session() as session:
            statement = select(PlatformAccount).where(
                PlatformAccount.platform == platform,
                PlatformAccount.status == "ACTIVE",
            ).order_by(PlatformAccount.display_name, PlatformAccount.account_id)
            accounts = list((await session.scalars(statement)).all())
        visible = []
        for account in accounts:
            try:
                access.require("session.read", account.account_id)
            except Exception:
                continue
            # usable=true 表示列出可用于投递选择器的活动账号；失效账号仍需
            # 可见，以便用户理解为何不能选择，而不是伪装成账号不存在。
            if usable_only and account.status != "ACTIVE":
                continue
            visible.append(public_account(account))
        return visible

    async def get_account_summary(self, access: AccessContext) -> dict:
        """返回跨平台账号状态摘要，不暴露 Profile、Cookie 或原始平台 ID。"""

        async with self.database.session() as session:
            statement = (
                select(PlatformAccount)
                .where(PlatformAccount.status == "ACTIVE")
                .order_by(
                    PlatformAccount.platform,
                    PlatformAccount.display_name,
                    PlatformAccount.account_id,
                )
            )
            accounts = list((await session.scalars(statement)).all())

        visible: list[dict] = []
        for account in accounts:
            try:
                access.require("session.read", account.account_id)
            except Exception:
                continue
            visible.append(
                {
                    "platform": account.platform,
                    **public_account(account),
                }
            )

        valid_accounts = sum(account["session_status"] == "VALID" for account in visible)
        return {
            "summary": {
                "total_accounts": len(visible),
                "valid_accounts": valid_accounts,
                "attention_required_accounts": len(visible) - valid_accounts,
                "platforms_with_accounts": len({account["platform"] for account in visible}),
            },
            "accounts": visible,
        }

    async def get_account(self, account_id: str) -> PlatformAccount:
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is None:
                raise AccountNotFoundError("平台账号不存在")
            return account

    async def require_account(
        self,
        account_id: str,
        platform: str,
        access: AccessContext,
        capability: str,
    ) -> PlatformAccount:
        _require_platform(platform)
        access.require(capability, account_id)
        account = await self.get_account(account_id)
        if account.platform != platform:
            raise AccountPlatformMismatchError("所选账号不属于当前平台")
        return account

    async def set_session_policy(
        self,
        account_id: str,
        persist_login: bool,
        access: AccessContext,
    ) -> dict:
        access.require("session.manage", account_id)
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is None:
                raise AccountNotFoundError("平台账号不存在")
            account.persist_login = persist_login
            await session.flush()
            payload = public_account(account)
            session.add(
                activity_for(
                    account,
                    access,
                    action="SESSION_POLICY_UPDATED",
                    message="已开启登录态复用" if persist_login else "已关闭登录态复用",
                )
            )
            return payload

    async def create_login_candidate(
        self,
        platform: str,
        access: AccessContext,
    ) -> dict:
        _require_platform(platform)
        account_id = str(uuid.uuid4())
        access.require("session.manage", account_id)
        profile = managed_profile_path(platform, account_id)
        profile.mkdir(parents=True, exist_ok=False)
        async with self.database.session() as session:
            account = PlatformAccount(
                account_id=account_id,
                platform=platform,
                display_name="等待登录验证",
                profile_path=str(profile.resolve()),
                is_legacy_profile=False,
                session_status="VERIFYING",
                persist_login=True,
            )
            session.add(account)
            await session.flush()
            return public_account(account)

    async def mark_verifying(
        self,
        account_id: str,
        access: AccessContext,
    ) -> dict:
        access.require("session.verify", account_id)
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is None:
                raise AccountNotFoundError("平台账号不存在")
            account.session_status = "VERIFYING"
            await session.flush()
            return public_account(account)

    async def verify_account(
        self,
        account_id: str,
        access: AccessContext,
        *,
        allow_interactive_login: bool = False,
    ) -> dict:
        access.require("session.verify", account_id)
        account = await self.get_account(account_id)
        lease = self._lease(account, purpose="LOGIN" if allow_interactive_login else "VERIFY")
        platform = self.platform_factory(account)
        try:
            with lease:
                await platform.initialize()
                valid = await _check_login(platform, read_only=not allow_interactive_login)
                if not valid and allow_interactive_login:
                    await platform.login()
                    valid = await _check_login(platform, read_only=False)
                if not valid:
                    raise AccountIdentityError(
                        "登录态已失效，需要重新登录",
                        error_code="LOGIN_REQUIRED",
                    )
                identity = await extract_identity(platform)
        except Exception as exc:
            # 清理只负责释放浏览器资源，不能遮盖验证阶段的稳定错误。
            # 尤其是 Profile 被占用时，必须把原始 AccountBusyError 留给
            # 心跳分类器处理，而不是被 cleanup 的异常替换。
            try:
                await platform.cleanup()
            except Exception:
                logging.warning("账号 %s 验证失败后的平台清理失败", account_id)
            await self._mark_verification_failure(account_id, access, exc)
            raise
        except BaseException:
            # 取消、KeyboardInterrupt 和 SystemExit 不能被当作业务失败；仍
            # 尝试普通资源清理，但让原始控制流异常继续向上传播。
            try:
                await platform.cleanup()
            except Exception:
                logging.warning("账号 %s 取消验证后的平台清理失败", account_id)
            raise
        else:
            # 验证和身份提取已经成功；清理失败不应把一次成功验证降级为
            # 失败，也不应覆盖已写入的 VALID/last_verified_at 状态。
            try:
                await platform.cleanup()
            except Exception:
                logging.warning("账号 %s 验证成功后的平台清理失败", account_id)

        async with self.database.session() as session:
            stored = await session.get(PlatformAccount, account_id)
            if stored is None:
                raise AccountNotFoundError("平台账号不存在")
            stored.platform_user_id = identity.platform_user_id
            stored.display_name = identity.display_name
            stored.session_status = "VALID"
            stored.last_verified_at = datetime.now(timezone.utc)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise AccountIdentityError("该平台身份已登记到另一个账号") from exc
            session.add(
                activity_for(
                    stored,
                    access,
                    action="SESSION_VERIFIED",
                    message="登录态和平台身份验证成功",
                )
            )
            return public_account(stored)

    async def logout_account(
        self,
        account_id: str,
        access: AccessContext,
    ) -> dict:
        """仅清除指定账号 Profile 的会话，不触碰同平台其他账号。"""

        access.require("session.manage", account_id)
        account = await self.get_account(account_id)
        platform = self.platform_factory(account)
        try:
            with self._lease(account, purpose="LOGOUT"):
                await platform.initialize()
                await platform.context.clear_cookies()
        except Exception as exc:
            await self._mark_verification_failure(account_id, access, exc)
            raise
        finally:
            await platform.cleanup()

        async with self.database.session() as session:
            stored = await session.get(PlatformAccount, account_id)
            if stored is None:
                raise AccountNotFoundError("平台账号不存在")
            stored.session_status = "LOGIN_REQUIRED"
            stored.last_verified_at = None
            session.add(
                activity_for(
                    stored,
                    access,
                    action="SESSION_LOGGED_OUT",
                    message="已退出该账号的持久登录态",
                )
            )
            return public_account(stored)

    async def remove_account(
        self,
        account_id: str,
        access: AccessContext,
    ) -> dict:
        """永久删除一个账号及其隔离 Profile。

        安全护栏：存在投递历史（delivery_operations）的账号拒绝删除，
        投递记录需要保留审计；仅允许删除无历史、失效或占位账号。
        Profile 目录只在受控根目录内清理，避免误删任意路径。
        """

        access.require("session.manage", account_id)
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is None:
                raise AccountNotFoundError("平台账号不存在")
            history_count = await session.scalar(
                select(func.count())
                .select_from(DeliveryOperation)
                .where(DeliveryOperation.account_id == account_id)
            )
            if history_count:
                raise AccountSessionError(
                    "该账号存在投递历史，禁止删除；投递记录需要保留审计",
                    error_code="ACCOUNT_HAS_DELIVERY_HISTORY",
                )
            summary = public_account(account)
            await session.execute(
                delete(AccountActivity).where(AccountActivity.account_id == account_id)
            )
            await session.execute(
                delete(PublishConfirmation).where(
                    PublishConfirmation.account_id == account_id
                )
            )
            await session.delete(account)
            await session.flush()
            profile = Path(account.profile_path)

        if self._profile_within_roots(profile, account.platform):
            try:
                shutil.rmtree(profile)
            except OSError as exc:
                logging.warning("删除账号 %s 后 Profile 清理失败: %s", account_id, exc)
        return {"deleted": summary}

    def _profile_within_roots(self, profile: Path, platform: str) -> bool:
        """复用租约的根目录约束：仅允许受控 Profile 根内的目录。"""

        path = Path(profile).expanduser().resolve()
        roots = {
            Path(root).expanduser().resolve()
            for root in (
                self.allowed_profile_roots
                or (
                    default_root() / "profiles",
                    legacy_profile_path(platform).parent,
                )
            )
        }
        return any(_is_within(path, root) for root in roots)

    async def _mark_verification_failure(
        self,
        account_id: str,
        access: AccessContext,
        exc: BaseException,
    ) -> None:
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is None:
                return
            # 这里与心跳服务共享稳定错误码分类；不要用模糊错误文本把
            # Profile 忙、选择器缺失等可恢复错误误标为 LOGIN_REQUIRED。
            from account_sessions.session_health import classify_verification_failure

            classification = classify_verification_failure(exc)
            if classification.category == "LOGIN_REQUIRED":
                account.session_status = "LOGIN_REQUIRED"
                account.last_verified_at = None
            elif classification.preserve_session and account.session_status == "VERIFYING":
                account.session_status = "VALID" if account.last_verified_at else "UNVERIFIED"
            elif classification.category == "ERROR" and account.session_status != "VALID":
                account.session_status = "ERROR"
            message = (
                f"会话验证失败（{classification.error_code}）"
                if access.source == "SYSTEM"
                else safe_error_message(exc)
            )
            session.add(
                activity_for(
                    account,
                    access,
                    action=(
                        "SESSION_VERIFY_DEFERRED"
                        if access.source == "SYSTEM" and classification.category == "BUSY"
                        else "SESSION_VERIFY_FAILED"
                    ),
                    level=(
                        "WARNING"
                        if access.source == "SYSTEM" and classification.category == "BUSY"
                        else "ERROR"
                    ),
                    message=message,
                )
            )

    async def list_activity(
        self,
        account_id: str,
        access: AccessContext,
        *,
        limit: int = 100,
    ) -> list[dict]:
        access.require("logs.read", account_id)
        await self.get_account(account_id)
        async with self.database.session() as session:
            statement = (
                select(AccountActivity)
                .where(AccountActivity.account_id == account_id)
                .order_by(AccountActivity.created_at.desc(), AccountActivity.id.desc())
                .limit(min(max(limit, 1), 200))
            )
            rows = list((await session.scalars(statement)).all())
        return [
            {
                "id": row.id,
                "operation_id": row.operation_id,
                "platform": row.platform,
                "display_name": row.display_name_snapshot,
                "source": row.source,
                "action": row.action,
                "level": row.level,
                "message": row.message,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]

    def _lease(self, account: PlatformAccount, *, purpose: str) -> AccountProfileLease:
        return AccountProfileLease(
            account,
            purpose=purpose,
            allowed_profile_roots=self.allowed_profile_roots,
            acquire_legacy_guard=self.acquire_legacy_guard,
            release_legacy_guard=self.release_legacy_guard,
        )


def public_account(account: PlatformAccount) -> dict:
    return {
        "account_id": account.account_id,
        "display_name": account.display_name,
        "masked_platform_user_id": mask_platform_user_id(account.platform_user_id),
        "status": account.status,
        "session_status": account.session_status,
        "persist_login": account.persist_login,
        "heartbeat_enabled": account.heartbeat_enabled,
        "next_heartbeat_at": _iso(account.next_heartbeat_at),
        "last_heartbeat_at": _iso(account.last_heartbeat_at),
        "heartbeat_failures": account.heartbeat_failures,
        "last_heartbeat_error_code": account.last_heartbeat_error_code,
        "last_verified_at": _iso(account.last_verified_at),
    }


def activity_for(
    account: PlatformAccount,
    access: AccessContext,
    *,
    action: str,
    message: str,
    level: str = "INFO",
    operation_id: str | None = None,
) -> AccountActivity:
    return AccountActivity(
        account_id=account.account_id,
        operation_id=operation_id,
        platform=account.platform,
        display_name_snapshot=account.display_name,
        actor_id=access.actor_id,
        source=access.source,
        action=action,
        level=level,
        message=safe_error_message(message),
    )


def _platform_instance(account: PlatformAccount):
    kwargs = {"profile_dir": account.profile_path, "strict_profile_lock": True}
    if account.platform == "xiaoheihe":
        from platforms.xiaoheihe import XiaoheihePlatform

        return XiaoheihePlatform(**kwargs)
    if account.platform == "zol":
        from platforms.zol import ZOLPlatform

        return ZOLPlatform(**kwargs)
    if account.platform == "zhihu":
        from platforms.zhihu import ZhihuPlatform

        return ZhihuPlatform(**kwargs)
    if account.platform == "douyin":
        from platforms.douyin import DouyinPlatform

        return DouyinPlatform(**kwargs)
    if account.platform == "xiaohongshu":
        from platforms.xiaohongshu import XiaohongshuPlatform

        return XiaohongshuPlatform(**kwargs)
    if account.platform == "weibo":
        from platforms.weibo import WeiboPlatform

        return WeiboPlatform(**kwargs)
    if account.platform == "baijiahao":
        from platforms.baijiahao import BaijiahaoPlatform

        return BaijiahaoPlatform(**kwargs)
    if account.platform == "smzdm":
        from platforms.smzdm import SmzdmPlatform

        return SmzdmPlatform(**kwargs)
    if account.platform == "toutiao":
        from platforms.toutiao import ToutiaoPlatform

        return ToutiaoPlatform(**kwargs)
    raise AccountPlatformMismatchError("不支持的平台")


async def _check_login(platform, *, read_only: bool) -> bool:
    if platform.platform_name == "zol":
        return await platform.check_login(allow_cookie_bridge=not read_only)
    return await platform.check_login()


def _require_platform(platform: str) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        raise AccountPlatformMismatchError("不支持的平台")


def _looks_like_chrome_profile(path: Path) -> bool:
    return path.is_dir() and (path / "Default").is_dir() and (path / "Local State").is_file()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
