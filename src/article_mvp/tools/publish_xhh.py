"""从 JSON 文件执行一次带双重确认的小黑盒公开发布。"""

import argparse
import asyncio
import json
from pathlib import Path

from article_mvp.contracts import PublishRequest
from article_mvp.db.database import dispose_db
from article_mvp.platforms.xiaoheihe.publisher import (
    PUBLIC_CONFIRMATION,
    XiaoheihePublisher,
)
from article_mvp.services.publish_service import PublishService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="小黑盒阶段一发布与 post_id 捕获")
    parser.add_argument("request_json", type=Path, help="PublishRequest JSON 文件")
    parser.add_argument(
        "--confirm-publication",
        action="store_true",
        help="确认本次操作会公开发布文章",
    )
    return parser.parse_args()


async def _main() -> None:
    args = parse_args()
    request = PublishRequest.model_validate_json(
        args.request_json.read_text(encoding="utf-8")
    )
    confirmation = PUBLIC_CONFIRMATION if args.confirm_publication else ""
    publisher = XiaoheihePublisher(public_confirmation=confirmation)
    try:
        mapping = await PublishService(publisher).publish(request)
        print(
            json.dumps(
                {
                    "mapping_id": mapping.id,
                    "task_id": mapping.task_id,
                    "platform": mapping.platform,
                    "external_article_id": mapping.external_article_id,
                    "platform_url": mapping.platform_url,
                    "status": mapping.status.value,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await dispose_db()


if __name__ == "__main__":
    asyncio.run(_main())
