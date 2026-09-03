"""AI 发布建议的线程安全运行期设置。

API Key 只保存在当前 Python 进程内；不会写入配置文件、数据库或日志。
服务重启后自动回退到环境变量配置，避免在尚无管理员鉴权的局域网页面
持久化第三方付费凭据。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from threading import RLock
from typing import Any

import httpx

from publication_ai.contracts import (
    PublicationAIModelListRequest,
    PublicationAISettingsUpdate,
)
from publication_ai.deepseek import DeepSeekPublicationAdvisor, PublicationAISettings
from publication_ai.errors import PublicationAIError


class PublicationAISettingsStore:
    """保存当前进程的非持久 AI 设置，并兼容既有环境变量。"""

    def __init__(
        self,
        fallback_settings: Mapping[str, Any],
        *,
        environ: MutableMapping[str, str] | None = None,
    ) -> None:
        self._fallback = PublicationAISettings.from_mapping(fallback_settings)
        self._environ = environ if environ is not None else os.environ
        self._runtime_settings: PublicationAISettings | None = None
        self._runtime_api_key: str | None = None
        self._lock = RLock()

    def effective_settings(self) -> PublicationAISettings:
        with self._lock:
            return self._runtime_settings or self._fallback

    def _effective_api_key_unlocked(self) -> tuple[str, str]:
        if self._runtime_api_key:
            return self._runtime_api_key, "runtime"
        environment_key = self._environ.get("DEEPSEEK_API_KEY", "").strip()
        if environment_key:
            return environment_key, "environment"
        return "", "missing"

    def public_view(self) -> dict[str, Any]:
        with self._lock:
            settings = self._runtime_settings or self._fallback
            api_key, source = self._effective_api_key_unlocked()
        return {
            "enabled": settings.enabled,
            "base_url": settings.base_url,
            "model": settings.model,
            "api_key_configured": bool(api_key),
            "api_key_source": source,
            "persistence": "process",
        }

    def update(self, payload: PublicationAISettingsUpdate) -> dict[str, Any]:
        current = self.effective_settings()
        new_api_key: str | None = None
        if payload.api_key is not None:
            new_api_key = payload.api_key.get_secret_value().strip()
            if not new_api_key:
                raise PublicationAIError("AI_CONFIGURATION_ERROR")
        candidate = PublicationAISettings.from_mapping(
            {
                "enabled": payload.enabled,
                "base_url": payload.base_url,
                "model": payload.model,
                "connect_timeout_seconds": current.connect_timeout_seconds,
                "read_timeout_seconds": current.read_timeout_seconds,
                "max_output_tokens": current.max_output_tokens,
            }
        )
        with self._lock:
            self._runtime_settings = candidate
            if payload.clear_api_key:
                self._runtime_api_key = None
            elif new_api_key is not None:
                self._runtime_api_key = new_api_key
        return self.public_view()

    def create_advisor(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> DeepSeekPublicationAdvisor:
        with self._lock:
            settings = self._runtime_settings or self._fallback
            api_key, _source = self._effective_api_key_unlocked()
        return DeepSeekPublicationAdvisor(
            settings,
            http_client=http_client,
            api_key=api_key,
            api_key_env=None,
        )

    def create_model_list_advisor(
        self,
        payload: PublicationAIModelListRequest,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> DeepSeekPublicationAdvisor:
        """构造一次性模型列表客户端，不把地址或新密钥写入运行期设置。"""

        with self._lock:
            current = self._runtime_settings or self._fallback
            effective_api_key, _source = self._effective_api_key_unlocked()
        api_key = (
            payload.api_key.get_secret_value().strip()
            if payload.api_key is not None
            else effective_api_key
        )
        settings = PublicationAISettings.from_mapping(
            {
                "enabled": current.enabled,
                "base_url": payload.base_url,
                "model": current.model,
                "connect_timeout_seconds": current.connect_timeout_seconds,
                "read_timeout_seconds": current.read_timeout_seconds,
                "max_output_tokens": current.max_output_tokens,
            }
        )
        return DeepSeekPublicationAdvisor(
            settings,
            http_client=http_client,
            api_key=api_key,
            api_key_env=None,
        )


__all__ = ["PublicationAISettingsStore"]
