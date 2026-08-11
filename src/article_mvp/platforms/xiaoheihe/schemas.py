"""小黑盒插件内部响应模型。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProbeRecord(BaseModel):
    """不包含请求值、Cookie 或 Token 的安全探测记录。"""

    model_config = ConfigDict(extra="forbid")

    url: str
    method: str
    status: int
    request_header_names: list[str] = Field(default_factory=list)
    request_body_keys: list[str] = Field(default_factory=list)
    response_shape: Any = None
    content_type: str = ""
