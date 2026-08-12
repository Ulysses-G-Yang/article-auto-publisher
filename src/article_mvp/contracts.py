"""跨层传递的最小、稳定数据契约。"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PublishRequest(BaseModel):
    """阶段一发布入口，只接受标准化内容，不耦合 DOCX 或 Web 表单。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=30)
    body: str = Field(min_length=1)
    image_paths: list[str] = Field(default_factory=list)
    community: str | None = None
    topic: str | None = None

    @field_validator("image_paths")
    @classmethod
    def reject_empty_image_paths(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("image_paths 不能包含空路径")
        return values


class CollectorAuth(BaseModel):
    """只存在于运行时内存中的认证材料。"""

    model_config = ConfigDict(extra="forbid")

    cookies: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


class MetricValues(BaseModel):
    """平台原始指标归一化后的内部表示。"""

    model_config = ConfigDict(extra="forbid")

    read_count: int = Field(ge=0)
    like_count: int | None = Field(default=None, ge=0)
    comment_count: int | None = Field(default=None, ge=0)
    collect_count: int | None = Field(default=None, ge=0)
    exposure_count: int | None = Field(default=None, ge=0)
    share_count: int | None = Field(default=None, ge=0)
    revenue: Decimal | None = Field(default=None, ge=0)
    snapshot_time: datetime | None = None
    raw_data: dict = Field(default_factory=dict)
