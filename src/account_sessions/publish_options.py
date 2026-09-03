"""账号级发布选项只读服务。

当前生产安全路径不进入平台编辑器：小黑盒和中关村在线的候选位于编辑器，
而编辑器可能触发自动保存，因此真实候选发现必须保持 fail-closed。其他平台
直接返回 ``unsupported``，不构造适配器，也不初始化浏览器。
"""

from __future__ import annotations

from datetime import datetime, timezone

from account_sessions.account_service import AccountSessionService
from account_sessions.contracts import (
    PublishOptionsRequest,
    PublishOptionsResponse,
)
from account_sessions.errors import (
    AccountPlatformMismatchError,
    AccountSessionError,
    AccountUnavailableError,
)
from account_sessions.permissions import AccessContext
from account_sessions.platform_catalog import ACCOUNT_ENABLED_PLATFORMS

READONLY_UNVERIFIED_ERROR = "PUBLISH_OPTIONS_READONLY_UNVERIFIED"
UNSUPPORTED_ERROR = "PUBLISH_OPTIONS_UNSUPPORTED"
_UNVERIFIED_EDITOR_PLATFORMS = frozenset({"xiaoheihe", "zol"})


class PublishOptionsService:
    """按账号发现发布选项，但默认不执行任何平台副作用。"""

    def __init__(
        self,
        accounts: AccountSessionService,
        *,
        platform_factory=None,
    ) -> None:
        self.accounts = accounts
        # Phase 3 没有任何已验证的 live hook。保留参数只为兼容运行时构造器，
        # 但故意不保存/调用它，确保该服务不会构造适配器或启动浏览器。

    async def discover(
        self,
        account_id: str,
        request: PublishOptionsRequest,
        access: AccessContext,
    ) -> PublishOptionsResponse:
        """返回账号对应平台的有界候选投影。"""

        # 权限检查必须早于账号查询，避免未授权调用方用错误码探测账号是否存在。
        access.require("session.read", account_id)
        account = await self.accounts.get_account(account_id)
        if account.status != "ACTIVE":
            raise AccountSessionError(
                "账号已归档，请先恢复后再操作",
                error_code="ACCOUNT_ARCHIVED",
                http_status=409,
            )
        if account.session_status != "VALID":
            raise AccountUnavailableError("所选账号登录态当前不可用")
        if not isinstance(request, PublishOptionsRequest):
            request = PublishOptionsRequest.model_validate(request)

        observed_at = _now_iso()
        platform_name = account.platform
        if platform_name not in ACCOUNT_ENABLED_PLATFORMS:
            raise AccountPlatformMismatchError("不支持的平台")

        # XHH/ZOL 候选只能从编辑器读取；编辑器可能 autosave，当前版本禁止
        # 任何进入编辑器的尝试，所以这一分支绝不构造平台对象。
        if platform_name in _UNVERIFIED_EDITOR_PLATFORMS:
            return _response(
                account_id=account.account_id,
                platform=platform_name,
                request=request,
                observed_at=observed_at,
                error_code=READONLY_UNVERIFIED_ERROR,
            )

        # 其余平台暂未有经过只读安全证明的候选 hook；直接返回稳定
        # unsupported，绝不构造适配器、租约、浏览器或 identity。
        return _response(
            account_id=account.account_id,
            platform=platform_name,
            request=request,
            observed_at=observed_at,
            error_code=UNSUPPORTED_ERROR,
        )


def _response(
    *,
    account_id: str,
    platform: str,
    request: PublishOptionsRequest,
    observed_at: str,
    error_code: str | None,
) -> PublishOptionsResponse:
    return PublishOptionsResponse(
        account_id=account_id,
        platform=platform,
        supported=False,
        mode=request.mode,
        groups=[],
        observed_at=observed_at,
        expires_at=None,
        error_code=error_code,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "PublishOptionsService",
    "READONLY_UNVERIFIED_ERROR",
    "UNSUPPORTED_ERROR",
]
