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
    AccountIdentityMismatchError,
    AccountNotFoundError,
    AccountPlatformMismatchError,
    AccountSessionError,
    AccountVerificationError,
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
from platforms.session_state import login_check_failure

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
        include_archived: bool = False,
    ) -> list[dict]:
        _require_platform(platform)
        async with self.database.session() as session:
            statement = select(PlatformAccount).where(
                PlatformAccount.platform == platform
            )
            if usable_only or not include_archived:
                statement = statement.where(PlatformAccount.status == "ACTIVE")
            statement = statement.order_by(
                PlatformAccount.status,
                PlatformAccount.display_name,
                PlatformAccount.account_id,
            )
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
            _require_active_account(account)
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
            _require_active_account(account)
            account.session_status = "VERIFYING"
            await session.flush()
            return public_account(account)

    async def restore_verifying_after_login_failure(
        self,
        account_id: str,
        access: AccessContext,
        *,
        error_code: str,
    ) -> None:
        """只恢复仍停留在本次登录 VERIFYING 的账号，避免悬挂状态。"""

        access.require("session.verify", account_id)
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is None or account.status != "ACTIVE":
                return
            if account.session_status != "VERIFYING":
                return
            account.session_status = "ERROR"
            account.last_verified_at = None
            session.add(
                activity_for(
                    account,
                    access,
                    action="SESSION_VERIFY_FAILED",
                    level="ERROR",
                    message=f"登录任务未能完成（{error_code}）",
                )
            )
            await session.flush()

    async def verify_account(
        self,
        account_id: str,
        access: AccessContext,
        *,
        allow_interactive_login: bool = False,
    ) -> dict:
        access.require("session.verify", account_id)
        account = await self.get_account(account_id)
        _require_active_account(account)
        lease = self._lease(account, purpose="LOGIN" if allow_interactive_login else "VERIFY")
        platform = self.platform_factory(account)
        try:
            with lease:
                try:
                    # SMZDM 的显式人工登录直接交接给同一 Profile 的原生 Chrome。
                    # 不在交接前 initialize 或 check_login，确保原生 Chrome 前
                    # 完全没有 Playwright 进程或 HOME/login 自动化导航。login()
                    # 会在原生窗口关闭且 Profile 解锁后自行 initialize，随后
                    # 这里只做一次现有登录态和身份验证。
                    if allow_interactive_login and account.platform == "smzdm":
                        await platform.login()
                        valid = await _check_login(platform, read_only=False)
                    else:
                        await platform.initialize()
                        valid = await _check_login(
                            platform,
                            read_only=not allow_interactive_login,
                        )
                        if (
                            not valid
                            and allow_interactive_login
                            and login_check_failure(platform).requires_login
                        ):
                            await platform.login()
                            valid = await _check_login(platform, read_only=False)
                    if not valid:
                        failure = login_check_failure(platform)
                        raise AccountVerificationError(
                            failure.message,
                            error_code=failure.code,
                        )
                    identity = await extract_identity(platform)
                finally:
                    # initialize/login 半失败也可能留下浏览器进程；租约只能在
                    # cleanup 完成后释放，避免下一个任务抢到仍被占用的 Profile。
                    try:
                        await platform.cleanup()
                    except Exception:
                        logging.warning("账号 %s 验证后的平台清理失败", account_id)
        except Exception as exc:
            await self._mark_verification_failure(account_id, access, exc)
            raise
        except BaseException:
            # 取消、KeyboardInterrupt 和 SystemExit 不能被当作业务失败。
            raise

        async with self.database.session() as session:
            stored = await session.get(PlatformAccount, account_id)
            if stored is None:
                raise AccountNotFoundError("平台账号不存在")
            stored_id = str(stored.platform_user_id or "").strip()
            observed_id = str(identity.platform_user_id or "").strip()
            if not observed_id:
                # 身份提取器通常已经拒绝空 ID；这里仍保留最后一道边界，
                # 防止未来新增适配器把空值当成首次绑定。
                stored.session_status = "ERROR"
                stored.last_verified_at = None
                session.add(
                    activity_for(
                        stored,
                        access,
                        action="SESSION_VERIFY_FAILED",
                        level="ERROR",
                        message="平台身份校验失败（ACCOUNT_IDENTITY_UNVERIFIED）",
                    )
                )
                identity_error: AccountIdentityError | None = AccountIdentityError(
                    "平台身份未能确认"
                )
            elif stored_id and stored_id != observed_id:
                # 已绑定账号不允许被另一次扫码/残留 Profile 静默改绑。
                # 只写稳定错误状态和错误码，绝不把两个原始 ID 放入消息或日志。
                stored.session_status = "ERROR"
                stored.last_verified_at = None
                session.add(
                    activity_for(
                        stored,
                        access,
                        action="SESSION_VERIFY_FAILED",
                        level="ERROR",
                        message="平台身份校验失败（ACCOUNT_IDENTITY_MISMATCH）",
                    )
                )
                identity_error = AccountIdentityMismatchError(
                    "平台当前身份与已绑定账号不一致"
                )
            else:
                # 首次验证允许从空绑定建立稳定身份；已绑定且相同 ID 时只
                # 更新平台展示名和验证时间，不改变账号的身份归属。
                stored.platform_user_id = observed_id
                stored.display_name = identity.display_name
                stored.session_status = "VALID"
                stored.last_verified_at = datetime.now(timezone.utc)
                identity_error = None

            if identity_error is not None:
                await session.flush()
            else:
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

        # 让上面的事务先提交 ERROR 状态后再抛出稳定异常；否则上下文
        # 管理器会回滚状态，调用方就无法观察到身份漂移已被阻断。
        raise identity_error

    async def assert_delivery_identity(
        self,
        account: PlatformAccount,
        platform: Any,
        access: AccessContext,
    ) -> None:
        """在投递前于同一 Profile 租约内确认已绑定的平台身份。

        该检查只读调用平台的登录态和身份提取，不会把昵称变化写回账号；
        投递执行单的账号快照因此不会被运行时页面内容静默改写。
        """

        # A receipt belongs only to this exact live browser and this identity check.
        from platforms.base import BasePlatform

        if isinstance(platform, BasePlatform):
            platform.invalidate_delivery_identity()
        if not await _check_login(platform, read_only=True):
            failure = login_check_failure(platform)
            await self._mark_runtime_identity_failure(
                account.account_id,
                access,
                status="LOGIN_REQUIRED" if failure.requires_login else "ERROR",
                error_code=failure.code,
            )
            raise AccountIdentityError(
                failure.message,
                error_code=failure.code,
            )

        try:
            identity = await extract_identity(platform)
        except AccountIdentityError as exc:
            await self._mark_runtime_identity_failure(
                account.account_id,
                access,
                status="ERROR",
                error_code=getattr(exc, "error_code", "ACCOUNT_IDENTITY_UNVERIFIED"),
            )
            raise
        except Exception as exc:
            await self._mark_runtime_identity_failure(
                account.account_id,
                access,
                status="ERROR",
                error_code="ACCOUNT_IDENTITY_UNVERIFIED",
            )
            raise AccountIdentityError(
                "投递前无法确认平台身份",
            ) from exc

        stored_id = str(account.platform_user_id or "").strip()
        observed_id = str(identity.platform_user_id or "").strip()
        if not stored_id or not observed_id:
            await self._mark_runtime_identity_failure(
                account.account_id,
                access,
                status="ERROR",
                error_code="ACCOUNT_IDENTITY_UNVERIFIED",
            )
            raise AccountIdentityError("投递前平台身份未能确认")
        if stored_id != observed_id:
            await self._mark_runtime_identity_failure(
                account.account_id,
                access,
                status="ERROR",
                error_code="ACCOUNT_IDENTITY_MISMATCH",
            )
            raise AccountIdentityMismatchError(
                "平台当前身份与已绑定账号不一致"
            )
        if isinstance(platform, BasePlatform):
            platform.remember_delivery_identity()

    async def _mark_runtime_identity_failure(
        self,
        account_id: str,
        access: AccessContext,
        *,
        status: str,
        error_code: str,
    ) -> None:
        """提交投递前身份失败状态；消息只保留稳定码，不含原始身份。"""

        async with self.database.session() as session:
            stored = await session.get(PlatformAccount, account_id)
            if stored is None:
                return
            stored.session_status = status
            stored.last_verified_at = None
            session.add(
                activity_for(
                    stored,
                    access,
                    action="SESSION_VERIFY_FAILED",
                    level="ERROR",
                    message=f"投递前平台身份校验失败（{error_code}）",
                )
            )

    async def logout_account(
        self,
        account_id: str,
        access: AccessContext,
    ) -> dict:
        """仅清除指定账号 Profile 的会话，不触碰同平台其他账号。"""

        access.require("session.manage", account_id)
        account = await self.get_account(account_id)
        _require_active_account(account)
        platform = self.platform_factory(account)
        try:
            with self._lease(account, purpose="LOGOUT"):
                try:
                    await platform.initialize()
                    await platform.context.clear_cookies()
                finally:
                    try:
                        await platform.cleanup()
                    except Exception:
                        logging.warning("账号 %s 退出后的平台清理失败", account_id)
        except Exception as exc:
            await self._mark_verification_failure(account_id, access, exc)
            raise

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

    async def archive_account(
        self,
        account_id: str,
        access: AccessContext,
    ) -> dict:
        """隐藏账号并禁止后续投递，保留 Profile 与全部审计历史。"""

        access.require("session.manage", account_id)
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is None:
                raise AccountNotFoundError("平台账号不存在")
            if account.status == "ARCHIVED":
                return public_account(account)
            if account.status != "ACTIVE":
                raise AccountSessionError(
                    "账号状态不允许归档",
                    error_code="ACCOUNT_STATUS_INVALID",
                )
            if await _active_delivery_count(session, account_id):
                raise AccountSessionError(
                    "账号仍有执行中的投递，暂时不能归档",
                    error_code="ACCOUNT_HAS_ACTIVE_DELIVERY",
                )
            account.status = "ARCHIVED"
            account.heartbeat_enabled = False
            account.next_heartbeat_at = None
            account.heartbeat_claim_owner = None
            account.heartbeat_claimed_at = None
            account.heartbeat_claim_expires_at = None
            session.add(
                activity_for(
                    account,
                    access,
                    action="ACCOUNT_ARCHIVED",
                    message="账号已归档；投递与心跳已停用，历史记录继续保留",
                )
            )
            await session.flush()
            return public_account(account)

    async def restore_account(
        self,
        account_id: str,
        access: AccessContext,
    ) -> dict:
        """恢复归档账号；原身份绑定和历史记录保持不变。"""

        access.require("session.manage", account_id)
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is None:
                raise AccountNotFoundError("平台账号不存在")
            if account.status == "ACTIVE":
                return public_account(account)
            if account.status != "ARCHIVED":
                raise AccountSessionError(
                    "账号状态不允许恢复",
                    error_code="ACCOUNT_STATUS_INVALID",
                )
            account.status = "ACTIVE"
            account.heartbeat_enabled = True
            account.next_heartbeat_at = None
            account.heartbeat_claim_owner = None
            account.heartbeat_claimed_at = None
            account.heartbeat_claim_expires_at = None
            session.add(
                activity_for(
                    account,
                    access,
                    action="ACCOUNT_RESTORED",
                    message="归档账号已恢复；登录态状态保持原值",
                )
            )
            await session.flush()
            return public_account(account)

    async def clear_login_state(
        self,
        account_id: str,
        access: AccessContext,
    ) -> dict:
        """清空托管 Profile/Cookie，但保留账号行、身份绑定和投递历史。

        为避免活跃账号误清，会要求先归档；legacy Profile 可能仍被旧入口
        共享，因此拒绝在这里删除。受控 Profile 清空后立即重建空目录，确保
        未来恢复账号并重新登录时仍满足严格 Profile 路径契约。
        """

        access.require("session.manage", account_id)
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is None:
                raise AccountNotFoundError("平台账号不存在")
            if account.status != "ARCHIVED":
                raise AccountSessionError(
                    "请先归档账号，再退出并清除登录态",
                    error_code="ACCOUNT_ARCHIVE_REQUIRED",
                )
            if account.is_legacy_profile:
                raise AccountSessionError(
                    "旧入口共享 Profile 不允许在账号域清除",
                    error_code="LEGACY_PROFILE_CLEAR_UNSUPPORTED",
                )
            if await _active_delivery_count(session, account_id):
                raise AccountSessionError(
                    "账号仍有执行中的投递，暂时不能清除登录态",
                    error_code="ACCOUNT_HAS_ACTIVE_DELIVERY",
                )
            profile = Path(account.profile_path)
            if not self._profile_within_roots(profile, account.platform):
                raise AccountSessionError(
                    "账号 Profile 不在允许的运行目录中",
                    error_code="ACCOUNT_PROFILE_PATH_INVALID",
                )

        try:
            with self._lease(account, purpose="CLEAR_LOGIN_STATE"):
                shutil.rmtree(profile)
                profile.mkdir(parents=True, exist_ok=False)
        except AccountSessionError:
            raise
        except Exception as exc:
            # 删除成功但建目录失败时尽量恢复空目录，避免账号永远不可登录。
            try:
                profile.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            raise AccountSessionError(
                "账号登录态清理失败",
                error_code="ACCOUNT_LOGIN_STATE_CLEAR_FAILED",
            ) from exc

        async with self.database.session() as session:
            stored = await session.get(PlatformAccount, account_id)
            if stored is None:
                raise AccountNotFoundError("平台账号不存在")
            stored.session_status = "LOGIN_REQUIRED"
            stored.persist_login = False
            stored.last_verified_at = None
            stored.heartbeat_enabled = False
            stored.next_heartbeat_at = None
            stored.heartbeat_failures = 0
            stored.last_heartbeat_error_code = None
            stored.heartbeat_claim_owner = None
            stored.heartbeat_claimed_at = None
            stored.heartbeat_claim_expires_at = None
            session.add(
                activity_for(
                    stored,
                    access,
                    action="LOGIN_STATE_CLEARED",
                    message="账号登录态和隔离 Profile 已清空；身份绑定与投递历史保留",
                )
            )
            await session.flush()
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
            identity_error_code = str(getattr(exc, "error_code", "")).upper()
            if identity_error_code in {
                "ACCOUNT_IDENTITY_UNVERIFIED",
                "ACCOUNT_IDENTITY_MISMATCH",
            }:
                # 身份缺失/漂移必须让当前账号进入 ERROR，即使它此前是
                # VALID；否则旧的 VALID 会掩盖本次验证已经失败的事实。
                account.session_status = "ERROR"
                account.last_verified_at = None
            elif classification.category == "LOGIN_REQUIRED":
                account.session_status = "LOGIN_REQUIRED"
                account.last_verified_at = None
            elif isinstance(exc, AccountVerificationError):
                # Keep the last successful timestamp as history, but do not
                # present a failed current check as a currently usable account.
                account.session_status = "ERROR"
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

        # 2026-08-19 已用真实草稿证明 ZOL 自有章节标题控件可持久化 H2，
        # 且 7 张正文图片保存后重开仍保持 29-token 图文顺序。生产内容投递
        # 必须启用同一条已验收路径，不能继续只在验收脚本中打开。
        return ZOLPlatform(enable_heading_experiment=True, **kwargs)
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


def _require_active_account(account: PlatformAccount) -> None:
    if account.status != "ACTIVE":
        raise AccountSessionError(
            "账号已归档，请先恢复后再操作",
            error_code="ACCOUNT_ARCHIVED",
        )


async def _active_delivery_count(session, account_id: str) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(DeliveryOperation)
        .where(
            DeliveryOperation.account_id == account_id,
            DeliveryOperation.status.in_({"QUEUED", "RUNNING"}),
        )
    )
    return int(value or 0)


def _looks_like_chrome_profile(path: Path) -> bool:
    return path.is_dir() and (path / "Default").is_dir() and (path / "Local State").is_file()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
