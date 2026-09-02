"""账号会话与内容投递 HTTP 契约。"""

import math
import re
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)

PlatformName = Literal[
    "xiaoheihe",
    "zol",
    "zhihu",
    "xiaohongshu",
    "baijiahao",
    "smzdm",
    "weibo",
    "toutiao",
]
DeliveryMode = Literal["DRAFT", "PUBLISH"]

# 平台选择是平台适配器之间唯一的可扩展边界；仅接受有限数量的 JSON 标量，
# 避免把任意请求对象、路径或凭据带入投递执行单。
PlatformSelectionValue: TypeAlias = StrictStr | StrictInt | StrictFloat | StrictBool | None
PlatformSelection: TypeAlias = Annotated[
    dict[StrictStr, PlatformSelectionValue],
    Field(max_length=32),
]
PLATFORM_SELECTION_KEY_MAX_LENGTH = 64
PLATFORM_SELECTION_STRING_MAX_LENGTH = 512
_PLATFORM_SELECTION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_PLATFORM_SELECTION_BLOCKED_KEY_FRAGMENTS = frozenset(
    {
        "cookie",
        "token",
        "profile",
        "path",
        "html",
        "selector",
        "authorization",
        "secret",
    }
)


def validate_platform_selection(value: PlatformSelection | None) -> PlatformSelection | None:
    """校验平台选择的键和值边界；``None`` 保持旧请求语义。"""

    if value is None:
        return None
    for key, item in value.items():
        if (
            not key.strip()
            or len(key) > PLATFORM_SELECTION_KEY_MAX_LENGTH
            or _PLATFORM_SELECTION_KEY_PATTERN.fullmatch(key) is None
        ):
            raise ValueError(
                "platform_selection 的 key 必须为 ASCII snake_case 且首字母小写"
            )
        if any(fragment in key for fragment in _PLATFORM_SELECTION_BLOCKED_KEY_FRAGMENTS):
            raise ValueError("platform_selection 的 key 包含受保护字段")
        if isinstance(item, str) and len(item) > PLATFORM_SELECTION_STRING_MAX_LENGTH:
            raise ValueError("platform_selection 的字符串值不能超过 512 个字符")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("platform_selection 的数值必须为有限值")
    return value


class ArticleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=200_000)


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    article: ArticleInput
    platform: PlatformName
    account_id: str = Field(min_length=36, max_length=36)
    mode: DeliveryMode = "DRAFT"
    confirmation_token: str | None = Field(default=None, max_length=512)
    platform_selection: PlatformSelection | None = None

    _validate_platform_selection = field_validator("platform_selection")(
        validate_platform_selection
    )


class SessionPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persist_login: bool


class ClearLoginStateRequest(BaseModel):
    """高风险 Profile 清理必须携带固定确认值。"""

    model_config = ConfigDict(extra="forbid")

    confirmation: Literal["CLEAR_LOGIN_STATE"]
