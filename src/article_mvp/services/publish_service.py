"""小黑盒发布用例编排。"""

from article_mvp.contracts import PublishRequest
from article_mvp.db.database import init_db, session_scope
from article_mvp.db.models import PlatformArticle
from article_mvp.platforms.xiaoheihe.publisher import XiaoheihePublisher


class PublishService:
    def __init__(self, publisher: XiaoheihePublisher) -> None:
        self.publisher = publisher

    async def publish(self, request: PublishRequest) -> PlatformArticle:
        await init_db()
        async with session_scope() as session:
            return await self.publisher.publish(request, session)
