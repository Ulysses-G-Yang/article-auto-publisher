"""前端、CLI 与未来 MCP 共用的账号级授权上下文。"""

from dataclasses import dataclass, field


class PermissionDeniedError(RuntimeError):
    error_code = "ACCOUNT_PERMISSION_DENIED"


@dataclass(frozen=True, slots=True)
class AccessContext:
    actor_id: str
    source: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    allowed_account_ids: frozenset[str] | None = None

    def require(self, capability: str, account_id: str) -> None:
        if capability not in self.capabilities:
            raise PermissionDeniedError(f"当前调用方缺少权限: {capability}")
        if (
            self.allowed_account_ids is not None
            and account_id not in self.allowed_account_ids
        ):
            raise PermissionDeniedError("当前调用方无权使用该平台账号")


LOCAL_WEB_CONTEXT = AccessContext(
    actor_id="local-web-user",
    source="WEB",
    capabilities=frozenset(
        {
            "session.read",
            "session.verify",
            "session.manage",
            "draft.create",
            "publish.request",
            "publish.execute",
            "logs.read",
        }
    ),
)
