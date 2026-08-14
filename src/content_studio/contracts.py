"""Content Studio 对外 HTTP 契约。

契约层只描述受支持的公开字段；数据库路径、Cookie、Token 与本机图片路径
永远不会进入这些模型。
"""

from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

DraftSourceType = Literal["BLANK", "DOCX", "LEGACY_ARTICLE", "SYSTEM_SEED"]
DraftStatus = Literal["ACTIVE", "ARCHIVED"]
BlockType = Literal["text", "image"]
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


class StrictModel(BaseModel):
    """拒绝静默吞掉拼错或过期的请求字段。"""

    model_config = ConfigDict(extra="forbid")


class ContentBlockInput(StrictModel):
    """有序图文块；图片只引用受控 asset_id，不暴露文件路径。"""

    block_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    type: BlockType
    text: str | None = Field(default=None, max_length=200_000)
    asset_id: str | None = Field(default=None, min_length=36, max_length=36)
    alt: str | None = Field(default=None, max_length=500)
    position: Annotated[int, Field(ge=0, le=100_000)]

    @model_validator(mode="after")
    def validate_payload_for_type(self) -> "ContentBlockInput":
        if self.type == "text" and not (self.text or "").strip():
            raise ValueError("文本块的 text 不能为空")
        if self.type == "image" and not self.asset_id:
            raise ValueError("图片块必须引用 asset_id")
        if self.type == "text" and self.asset_id is not None:
            raise ValueError("文本块不能引用 asset_id")
        return self


class DraftTargetInput(StrictModel):
    """一个平台账号对应一个投递目标。"""

    target_id: str | None = Field(default=None, min_length=36, max_length=36)
    platform: PlatformName
    account_id: str = Field(min_length=36, max_length=36)
    mode: DeliveryMode = "DRAFT"
    persist_login: bool | None = None


class CreateDraftRequest(StrictModel):
    title: str = Field(default="", max_length=200)
    blocks: list[ContentBlockInput] = Field(default_factory=list, max_length=2_000)


class PatchDraftRequest(StrictModel):
    revision: Annotated[int, Field(ge=1)]
    title: str = Field(max_length=200)
    blocks: list[ContentBlockInput] = Field(max_length=2_000)


class ReplaceTargetsRequest(StrictModel):
    revision: Annotated[int, Field(ge=1)]
    targets: list[DraftTargetInput] = Field(max_length=50)


class CreateDeliveryPlanRequest(StrictModel):
    """revision 必填，避免冻结到用户未看见的并发版本。"""

    revision: Annotated[int, Field(ge=1)]


class ExecuteDeliveryPlanRequest(StrictModel):
    target_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)
    draft_batch_confirmed: bool = False
    confirmations: dict[str, str] = Field(default_factory=dict)


class DraftListQuery(StrictModel):
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    offset: Annotated[int, Field(ge=0)] = 0


class LegacyArticleListQuery(StrictModel):
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    offset: Annotated[int, Field(ge=0)] = 0
