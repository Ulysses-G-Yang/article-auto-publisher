"""初始化异步 ORM 表。"""

import asyncio

from article_mvp.db.database import dispose_db, init_db


async def _main() -> None:
    await init_db()
    await dispose_db()


if __name__ == "__main__":
    asyncio.run(_main())
