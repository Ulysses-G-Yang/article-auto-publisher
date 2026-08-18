"""Content Studio 对外 HTTP 契约。

契约层只描述受支持的公开字段；数据库路径、Cookie、Token 与本机图片路径
永远不会进入这些模型。
"""

from typing import Annotated, Any, Literal
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
CoverStrategy = Literal["NONE", "FIRST_BODY_IMAGE", "EXPLICIT"]
ContentSchemaVersion = Literal[1, 2]


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


class CoverInput(StrictModel):
    """封面策略；EXPLICIT 必须引用当前草稿的受控图片资产。"""

    strategy: CoverStrategy
    asset_id: str | None = Field(default=None, min_length=36, max_length=36)

    @model_validator(mode="after")
    def validate_strategy(self) -> "CoverInput":
        if self.strategy == "EXPLICIT" and not self.asset_id:
            raise ValueError("EXPLICIT 封面必须引用 asset_id")
        if self.strategy != "EXPLICIT" and self.asset_id is not None:
            raise ValueError("只有 EXPLICIT 封面可以引用 asset_id")
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
    blocks: list[ContentBlockInput] | None = Field(default=None, max_length=2_000)
    cover: CoverInput = Field(default_factory=lambda: CoverInput(strategy="NONE"))
    content_schema_version: ContentSchemaVersion = 1
    document: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_document_version(self) -> "CreateDraftRequest":
        if self.content_schema_version == 2 and self.document is None:
            raise ValueError("content_schema_version=2 必须提供 document")
        if self.content_schema_version == 1 and self.document is not None:
            raise ValueError("v1 草稿不能提供 document")
        return self


class PatchDraftRequest(StrictModel):
    revision: Annotated[int, Field(ge=1)]
    title: str = Field(max_length=200)
    blocks: list[ContentBlockInput] | None = Field(default=None, max_length=2_000)
    cover: CoverInput | None = None
    content_schema_version: ContentSchemaVersion | None = None
    document: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_document_version(self) -> "PatchDraftRequest":
        # 省略 schema 是历史 v1 客户端语义；只有显式声明 2 才能提交 canonical
        # v2 document，避免旧页面无意间把 v2 草稿降级覆盖。
        if self.document is not None and self.content_schema_version != 2:
            raise ValueError("提供 document 时必须显式声明 content_schema_version=2")
        if self.content_schema_version == 2 and self.document is None:
            raise ValueError("content_schema_version=2 必须提供 document")
        return self


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
