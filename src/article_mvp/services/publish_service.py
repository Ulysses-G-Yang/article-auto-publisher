"""小黑盒发布用例编排。"""

from article_mvp.contracts import PublishRequest
from article_mvp.db.models import PlatformArticle
from article_mvp.platforms.xiaoheihe.publisher import XiaoheihePublisher
from article_mvp.services.published_event_service import PublishedEventService


class PublishService:
    def __init__(
        self,
        publisher: XiaoheihePublisher,
        event_service: PublishedEventService | None = None,
    ) -> None:
        self.publisher = publisher
        self.event_service = event_service or PublishedEventService()

    async def publish(self, request: PublishRequest) -> PlatformArticle:
        event = await self.publisher.publish(request)
        return await self.event_service.handle(event)
