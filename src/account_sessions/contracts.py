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
    model_validator,
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
AccountPlatformName = Literal[
    "xiaoheihe",
    "zol",
    "zhihu",
    "xiaohongshu",
    "baijiahao",
    "smzdm",
    "weibo",
    "toutiao",
    "douyin",
]
DeliveryMode = Literal["DRAFT", "PUBLISH", "PRIVATE_PUBLISH"]
PublishOptionKind = Literal["community", "topic"]

# 选项发现只允许极小的查询/结果投影。这里的类型与投递契约分开，避免把
# platform/account_id、正文、DOM 或任意平台原始响应带进候选接口。
PUBLISH_OPTIONS_QUERY_MAX_LENGTH = 30
PUBLISH_OPTIONS_QUERY_MAX_COUNT = 4
PUBLISH_OPTIONS_KIND_MAX_COUNT = 2
PUBLISH_OPTIONS_LIMIT_MAX = 20
PUBLISH_OPTIONS_ACCOUNT_ID_MAX_LENGTH = 64
PUBLISH_OPTIONS_CANDIDATE_KEY_MAX_LENGTH = 128
PUBLISH_OPTIONS_LABEL_MAX_LENGTH = 120
PUBLISH_OPTIONS_OPTION_ID_MAX_LENGTH = 128
PUBLISH_OPTIONS_TIMESTAMP_MAX_LENGTH = 64
PUBLISH_OPTIONS_ERROR_CODE_MAX_LENGTH = 64

PublishOptionQuery = Annotated[
    StrictStr,
    Field(min_length=1, max_length=PUBLISH_OPTIONS_QUERY_MAX_LENGTH),
]
PublishOptionCandidateKey = Annotated[
    StrictStr,
    Field(min_length=1, max_length=PUBLISH_OPTIONS_CANDIDATE_KEY_MAX_LENGTH),
]
PublishOptionLabel = Annotated[
    StrictStr,
    Field(min_length=1, max_length=PUBLISH_OPTIONS_LABEL_MAX_LENGTH),
]
PublishOptionId = Annotated[
    StrictStr,
    Field(min_length=1, max_length=PUBLISH_OPTIONS_OPTION_ID_MAX_LENGTH),
]
PublishOptionTimestamp = Annotated[
    StrictStr,
    Field(min_length=1, max_length=PUBLISH_OPTIONS_TIMESTAMP_MAX_LENGTH),
]
PublishOptionErrorCode = Annotated[
    StrictStr,
    Field(min_length=1, max_length=PUBLISH_OPTIONS_ERROR_CODE_MAX_LENGTH),
]

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

    @model_validator(mode="after")
    def validate_private_platform(self) -> "DeliveryRequest":
        if self.mode == "PRIVATE_PUBLISH" and self.platform != "xiaohongshu":
            raise ValueError("目前仅小红书支持仅自己可见发布")
        return self


class PublishOptionsRequest(BaseModel):
    """账号级只读候选查询请求。

    ``platform`` 和 ``account_id`` 有意不在 body 中：平台必须从路径指定的
    账号记录派生，避免调用方伪造跨账号/跨平台查询。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    queries: list[PublishOptionQuery] = Field(
        min_length=1,
        max_length=PUBLISH_OPTIONS_QUERY_MAX_COUNT,
    )
    kinds: list[PublishOptionKind] = Field(
        default_factory=lambda: ["community", "topic"],
        min_length=1,
        max_length=PUBLISH_OPTIONS_KIND_MAX_COUNT,
    )
    limit: StrictInt = Field(default=PUBLISH_OPTIONS_LIMIT_MAX, ge=1, le=PUBLISH_OPTIONS_LIMIT_MAX)
    mode: DeliveryMode = "DRAFT"

    @field_validator("queries", mode="before")
    @classmethod
    def _require_json_array(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("字段必须是 JSON 数组")
        return value

    @field_validator("kinds", mode="before")
    @classmethod
    def _deduplicate_kinds(cls, value: object) -> object:
        # 保留调用方顺序，同时让默认/显式重复 kind 不扩大后续工作量。
        if not isinstance(value, list):
            raise ValueError("字段必须是 JSON 数组")
        result: list[object] = []
        for item in value:
            if item not in result:
                result.append(item)
        return result


class PublishOptionCandidate(BaseModel):
    """候选的最小安全投影，不携带 DOM、URL、路径或原始响应。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidate_key: PublishOptionCandidateKey
    label: PublishOptionLabel
    platform_option_id: PublishOptionId | None = None
    source_query: PublishOptionQuery | None = None


class PublishOptionGroup(BaseModel):
    """一个候选类型及其有界候选列表。"""

    model_config = ConfigDict(extra="forbid")

    kind: PublishOptionKind
    candidates: list[PublishOptionCandidate] = Field(
        default_factory=list,
        max_length=PUBLISH_OPTIONS_LIMIT_MAX,
    )


class PublishOptionsResponse(BaseModel):
    """候选发现响应；字段白名单由模型本身固定。"""

    model_config = ConfigDict(extra="forbid")

    account_id: Annotated[
        StrictStr,
        Field(min_length=1, max_length=PUBLISH_OPTIONS_ACCOUNT_ID_MAX_LENGTH),
    ]
    platform: AccountPlatformName
    supported: StrictBool
    mode: DeliveryMode
    groups: list[PublishOptionGroup] = Field(
        default_factory=list,
        max_length=PUBLISH_OPTIONS_KIND_MAX_COUNT,
    )
    observed_at: PublishOptionTimestamp
    expires_at: PublishOptionTimestamp | None = None
    error_code: PublishOptionErrorCode | None = None

    @model_validator(mode="after")
    def _supported_requires_candidates(self) -> "PublishOptionsResponse":
        if self.supported and not any(group.candidates for group in self.groups):
            raise ValueError("supported=true 时必须返回至少一个包含候选的候选组")
        return self


class SessionPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persist_login: bool


class ClearLoginStateRequest(BaseModel):
    """高风险 Profile 清理必须携带固定确认值。"""

    model_config = ConfigDict(extra="forbid")

    confirmation: Literal["CLEAR_LOGIN_STATE"]
