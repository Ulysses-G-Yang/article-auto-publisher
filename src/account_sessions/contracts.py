"""账号会话与内容投递 HTTP 契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PlatformName = Literal["xiaoheihe", "zol", "zhihu", "xiaohongshu"]
DeliveryMode = Literal["DRAFT", "PUBLISH"]


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


class SessionPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persist_login: bool
