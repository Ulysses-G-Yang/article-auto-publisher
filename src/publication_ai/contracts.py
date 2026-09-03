"""DeepSeek 建议请求和严格 JSON 输出契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

PublicationPlatform = Literal[
    "xiaoheihe",
    "zol",
    "zhihu",
    "xiaohongshu",
    "baijiahao",
    "smzdm",
    "weibo",
    "toutiao",
]

_MAX_PLATFORMS = 8
_MAX_QUERIES = 4
_MAX_TOPICS = 5
_MAX_KEYWORDS = 10

ShortText = Annotated[StrictStr, Field(min_length=1, max_length=120)]
TopicQuery = Annotated[StrictStr, Field(min_length=1, max_length=30)]
Keyword = Annotated[StrictStr, Field(min_length=1, max_length=40)]
ReasonText = Annotated[StrictStr, Field(min_length=1, max_length=300)]


class PublicationAdviceRequest(BaseModel):
    """内容工作台的只读建议请求；平台名只接受既有八个平台。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    revision: StrictInt = Field(ge=1)
    platforms: list[PublicationPlatform] = Field(min_length=1, max_length=_MAX_PLATFORMS)

    @field_validator("platforms", mode="before")
    @classmethod
    def _deduplicate_platforms(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("platforms 必须是 JSON 数组")
        result: list[object] = []
        for platform in value:
            if platform not in result:
                result.append(platform)
        return result


class PublicationRecommendation(BaseModel):
    """单个平台恰好一条的最小建议结果。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    platform: PublicationPlatform
    topic_queries: list[TopicQuery] = Field(min_length=1, max_length=_MAX_QUERIES)
    suggested_topics: list[ShortText] = Field(default_factory=list, max_length=_MAX_TOPICS)
    suggested_community: ShortText | None = None
    keywords: list[Keyword] = Field(default_factory=list, max_length=_MAX_KEYWORDS)
    reason: ReasonText


class PublicationGuidanceResponse(BaseModel):
    """模型输出的严格 JSON 对象；未知字段全部拒绝。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    recommendations: list[PublicationRecommendation] = Field(
        min_length=1,
        max_length=_MAX_PLATFORMS,
    )

    @field_validator("recommendations")
    @classmethod
    def _reject_duplicate_platforms(
        cls,
        value: list[PublicationRecommendation],
    ) -> list[PublicationRecommendation]:
        platforms = [item.platform for item in value]
        if len(platforms) != len(set(platforms)):
            raise ValueError("recommendations 中 platform 不能重复")
        return value


class PublicationAISettingsUpdate(BaseModel):
    """网页端运行期 AI 设置；密钥用 SecretStr 防止意外出现在 repr。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: StrictBool
    base_url: StrictStr = Field(min_length=1, max_length=500)
    model: StrictStr = Field(min_length=1, max_length=128)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    clear_api_key: StrictBool = False

    @model_validator(mode="after")
    def _reject_key_and_clear_together(self) -> PublicationAISettingsUpdate:
        if self.api_key is not None and self.clear_api_key:
            raise ValueError("api_key 与 clear_api_key 不能同时提交")
        return self

    @field_validator("api_key")
    @classmethod
    def _reject_whitespace_in_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if not raw or any(character.isspace() for character in raw):
            raise ValueError("API Key 格式无效")
        return value


def validate_for_platforms(
    response: PublicationGuidanceResponse,
    platforms: list[str] | tuple[str, ...],
) -> PublicationGuidanceResponse:
    """要求输出平台集合与请求完全一致，并按请求顺序返回。"""

    expected = list(platforms)
    actual = [item.platform for item in response.recommendations]
    if len(actual) != len(expected) or set(actual) != set(expected):
        raise ValueError("recommendations 必须为每个请求平台恰好一条")
    by_platform = {item.platform: item for item in response.recommendations}
    return response.model_copy(
        update={"recommendations": [by_platform[platform] for platform in expected]}
    )


__all__ = [
    "PublicationAdviceRequest",
    "PublicationAISettingsUpdate",
    "PublicationGuidanceResponse",
    "PublicationPlatform",
    "PublicationRecommendation",
    "validate_for_platforms",
]
