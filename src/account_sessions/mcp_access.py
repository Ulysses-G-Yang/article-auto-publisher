"""受控的内部 MCP 账号级访问边界。

该模块故意独立于 ``LOCAL_WEB_CONTEXT``：MCP 只能使用启动时显式配置的
Bearer token 和账号白名单读取公开投影。只有另一个显式开关开启时，才额外
授予这些白名单账号的平台草稿能力；公开发布权限永远不在此边界授予。
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from account_sessions.errors import AccountSessionError
from account_sessions.permissions import AccessContext

MCP_INTERNAL_TOKEN_ENV = "ARTICLEOPS_MCP_INTERNAL_TOKEN"
MCP_ALLOWED_ACCOUNT_IDS_ENV = "ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS"
MCP_DRAFT_DELIVERY_ENABLED_ENV = "ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED"
MIN_INTERNAL_TOKEN_LENGTH = 32
MAX_INTERNAL_TOKEN_LENGTH = 512
MAX_ACCOUNT_ID_LENGTH = 128
MAX_ALLOWED_ACCOUNT_IDS = 1000


class MCPAccessError(AccountSessionError):
    """内部 MCP 访问边界的稳定错误。"""


class MCPAccessNotConfiguredError(MCPAccessError):
    error_code = "MCP_ACCESS_NOT_CONFIGURED"
    http_status = 503

    def __init__(self) -> None:
        super().__init__("MCP 账号只读访问尚未配置")


class MCPAccessDeniedError(MCPAccessError):
    error_code = "MCP_ACCESS_DENIED"
    http_status = 401

    def __init__(self) -> None:
        super().__init__("MCP 账号只读访问未获授权")


class MCPRequestValidationError(MCPAccessError):
    error_code = "MCP_INVALID_ARGUMENT"
    http_status = 400

    def __init__(self, message: str = "MCP 请求参数无效") -> None:
        # The caller supplies only stable, non-sensitive validation messages.
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class MCPInternalAccessSettings:
    """内部 MCP 认证配置；token 字段禁止出现在 repr 中。"""

    internal_token: str = field(repr=False)
    allowed_account_ids: frozenset[str] = field(default_factory=frozenset)
    draft_delivery_enabled: bool = False

    @property
    def configured(self) -> bool:
        return bool(
            self.internal_token
            and len(self.internal_token) >= MIN_INTERNAL_TOKEN_LENGTH
            and len(self.internal_token) <= MAX_INTERNAL_TOKEN_LENGTH
            and not any(
                char.isspace() or ord(char) < 32 or ord(char) == 127
                for char in self.internal_token
            )
            and self.allowed_account_ids
        )


def _parse_allowed_account_ids(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values or len(values) > MAX_ALLOWED_ACCOUNT_IDS or any(
        len(item) > MAX_ACCOUNT_ID_LENGTH
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in item)
        for item in values
    ):
        return frozenset()
    return frozenset(values)


def load_internal_access_settings(
    environ: Mapping[str, str] | None = None,
) -> MCPInternalAccessSettings:
    """从环境读取配置；缺失或空白账号集合保持 fail-closed。"""

    values = os.environ if environ is None else environ
    draft_delivery_enabled = str(
        values.get(MCP_DRAFT_DELIVERY_ENABLED_ENV, "")
    ).strip().lower() in {"1", "true", "yes", "on"}
    return MCPInternalAccessSettings(
        internal_token=str(values.get(MCP_INTERNAL_TOKEN_ENV, "")).strip(),
        allowed_account_ids=_parse_allowed_account_ids(
            values.get(MCP_ALLOWED_ACCOUNT_IDS_ENV)
        ),
        draft_delivery_enabled=draft_delivery_enabled,
    )


class MCPInternalAccessResolver:
    """将一次 Flask 请求解析为受限的账号级 ``AccessContext``。"""

    def __init__(
        self,
        settings: MCPInternalAccessSettings | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._environ = environ

    @property
    def settings(self) -> MCPInternalAccessSettings:
        # 默认按请求读取环境，便于测试和配置管理；生产进程通常不会修改环境。
        return self._settings or load_internal_access_settings(self._environ)

    def resolve(self, headers: Mapping[str, str]) -> AccessContext:
        settings = self.settings
        if not settings.configured:
            raise MCPAccessNotConfiguredError()

        authorization = next(
            (value for key, value in headers.items() if key.lower() == "authorization"),
            "",
        )
        scheme, separator, supplied_token = authorization.partition(" ")
        supplied_token = supplied_token.strip() if separator else ""
        if (
            scheme.lower() != "bearer"
            or not supplied_token
            or not hmac.compare_digest(supplied_token, settings.internal_token)
        ):
            raise MCPAccessDeniedError()

        capabilities = {"session.read", "logs.read"}
        if settings.draft_delivery_enabled:
            capabilities.add("draft.create")
        return AccessContext(
            actor_id="articleops-mcp",
            source="MCP",
            capabilities=frozenset(capabilities),
            allowed_account_ids=settings.allowed_account_ids,
        )
