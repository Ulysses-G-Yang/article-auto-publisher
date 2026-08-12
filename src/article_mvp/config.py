"""平台 YAML 配置的强类型加载与证据等级校验。"""

from datetime import datetime
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from article_mvp.errors import ConfigurationError


class EvidenceLevel(str, Enum):
    LEGACY_UNVERIFIED = "legacy_unverified"
    ASSUMED = "assumed"
    OBSERVED = "observed"
    VERIFIED = "verified"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: EvidenceLevel
    source: str = Field(min_length=1)
    verified_at: datetime | None = None

    @model_validator(mode="after")
    def verified_requires_timestamp(self) -> "Evidence":
        if self.level is EvidenceLevel.VERIFIED and self.verified_at is None:
            raise ValueError("verified 证据必须包含 verified_at")
        return self


class EndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = ""
    url_contains: str = ""
    method: str = "GET"
    evidence: Evidence

    @model_validator(mode="after")
    def endpoint_has_matcher(self) -> "EndpointConfig":
        if not self.url and not self.url_contains:
            raise ValueError("Endpoint 必须包含 url 或 url_contains")
        self.method = self.method.upper()
        return self


class SelectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    body: str
    file_input: str
    publish_button: str
    confirm_button: str
    logged_in_probe: str
    community_button: str = ""
    topic_button: str = ""
    dialog_search: str = ""
    dialog_result: str = ""


class PublishConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login_url: HttpUrl
    editor_url: HttpUrl
    response_timeout_ms: int = Field(default=30_000, ge=1_000, le=120_000)
    submit_endpoint: EndpointConfig
    post_id_paths: list[str] = Field(min_length=1)
    platform_url_paths: list[str] = Field(default_factory=list)
    published_at_paths: list[str] = Field(default_factory=list)
    selectors: SelectorConfig


class PaginationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor_param: str = Field(min_length=1)
    next_cursor_path: str = Field(min_length=1)
    max_pages: int = Field(default=20, ge=1, le=100)


class CollectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: EndpointConfig
    required_headers: list[str] = Field(default_factory=list)
    required_cookies: list[str] = Field(default_factory=list)
    query_params: dict[str, str] = Field(default_factory=dict)
    json_paths: dict[str, str] = Field(default_factory=dict)
    response_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    max_attempts: int = Field(default=3, ge=1, le=5)
    pagination: PaginationConfig | None = None

    def ensure_verified(self) -> None:
        if self.endpoint.evidence.level is not EvidenceLevel.VERIFIED:
            raise ConfigurationError(
                "小黑盒采集 Endpoint 尚未 verified，禁止发起 HTTPX 请求"
            )
        if "read_count" not in self.json_paths:
            raise ConfigurationError("verified 采集配置必须定义 read_count JSON Path")


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    platform: str = Field(pattern=r"^[a-z0-9_]+$")
    display_name: str
    publish: PublishConfig
    collector: CollectorConfig


def default_xiaoheihe_config_path() -> Path:
    return Path(__file__).resolve().parent / "platforms" / "xiaoheihe" / "config.yaml"


def load_platform_config(path: str | Path | None = None) -> PlatformConfig:
    config_path = Path(path) if path else default_xiaoheihe_config_path()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"无法读取平台配置: {config_path}") from exc
    try:
        return PlatformConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigurationError(f"平台配置校验失败: {config_path}: {exc}") from exc
