"""刻意保持很小的平台契约，避免演变为万能适配器。"""

from abc import ABC, abstractmethod
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from article_mvp.contracts import CollectorAuth, MetricValues, PublishRequest
from article_mvp.db.models import PlatformArticle


class BasePublisher(ABC):
    @abstractmethod
    async def publish(
        self,
        request: PublishRequest,
        session: AsyncSession,
    ) -> PlatformArticle:
        """发布内容并返回平台文章映射。"""


class BaseCollector(ABC):
    @abstractmethod
    async def fetch_raw(
        self,
        article: PlatformArticle,
        client: httpx.AsyncClient,
        auth: CollectorAuth,
    ) -> dict[str, Any]:
        """从平台获取原始指标响应。"""

    @abstractmethod
    def normalize(self, payload: dict[str, Any]) -> MetricValues:
        """将平台字段转换为内部指标契约。"""
