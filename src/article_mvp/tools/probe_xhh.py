"""小黑盒安全 Network 探测；不保存请求值、Cookie 或 Token。"""

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import Response, async_playwright

from article_mvp.config import load_platform_config
from article_mvp.contracts import CollectorAuth
from article_mvp.db.database import dispose_db
from article_mvp.platforms.locks import PlatformFileLock
from article_mvp.platforms.xiaoheihe.collector import XiaoheiheCollector
from article_mvp.platforms.xiaoheihe.schemas import ProbeRecord
from article_mvp.security import payload_shape, sanitize_url
from article_mvp.services.collect_service import CollectService


def sanitized_url(raw_url: str) -> str:
    """兼容工具调用方的公开名称，实际规则统一由 security 模块维护。"""

    return sanitize_url(raw_url)


def request_body_keys(request) -> list[str]:
    try:
        body = request.post_data_json
    except Exception:
        return []
    return sorted(str(key) for key in body) if isinstance(body, dict) else []


async def record_response(response: Response) -> ProbeRecord:
    content_type = response.headers.get("content-type", "")
    shape: Any = None
    if "json" in content_type.casefold():
        try:
            shape = payload_shape(await response.json())
        except Exception:
            shape = "invalid_json"
    return ProbeRecord(
        url=sanitized_url(response.url),
        method=response.request.method.upper(),
        status=response.status,
        request_header_names=sorted(
            name.casefold() for name in response.request.headers.keys()
        ),
        request_body_keys=request_body_keys(response.request),
        response_shape=shape,
        content_type=content_type.split(";", 1)[0],
    )


def write_contract(records: list[ProbeRecord], path: Path) -> None:
    lines = [
        "# 小黑盒 Network 探测候选",
        "",
        "> 本文件只包含脱敏结构；所有候选在人工核验前均不得标记为 verified。",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## {record.method} `{record.url}`",
                "",
                f"- HTTP 状态：{record.status}",
                f"- Content-Type：`{record.content_type or '-'}`",
                f"- Header 名称：{', '.join(record.request_header_names) or '-'}",
                f"- 请求体字段：{', '.join(record.request_body_keys) or '-'}",
                "- 响应结构：",
                "",
                "```json",
                json.dumps(record.response_shape, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="小黑盒创作者后台 Network 探测")
    parser.add_argument(
        "--collect-article-id",
        type=int,
        default=None,
        help="配置 verified 后，用当前内存登录态立即采集该映射 ID",
    )
    return parser.parse_args()


async def _main() -> None:
    args = parse_args()
    config = load_platform_config()
    project_root = Path(__file__).resolve().parents[4]
    data_dir = Path(os.getenv("ARTICLE_MVP_DATA_DIR", project_root / "data"))
    profile_dir = data_dir / "profiles" / "xiaoheihe"
    output_dir = data_dir / "probes"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"xiaoheihe-{timestamp}.json"
    markdown_path = output_dir / f"xiaoheihe-{timestamp}.md"
    records: list[ProbeRecord] = []
    pending: set[asyncio.Task] = set()

    proxy_server = os.getenv("PLAYWRIGHT_PROXY_SERVER", "").strip()
    proxy = None
    if proxy_server:
        proxy = {"server": proxy_server}
        if os.getenv("PLAYWRIGHT_PROXY_USERNAME"):
            proxy["username"] = os.environ["PLAYWRIGHT_PROXY_USERNAME"]
        if os.getenv("PLAYWRIGHT_PROXY_PASSWORD"):
            proxy["password"] = os.environ["PLAYWRIGHT_PROXY_PASSWORD"]

    with PlatformFileLock(data_dir / "locks" / "xiaoheihe.lock"):
        playwright = await async_playwright().start()
        context = None
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=False,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                proxy=proxy,
            )
            page = context.pages[0] if context.pages else await context.new_page()

            def on_response(response: Response) -> None:
                task = asyncio.create_task(record_response(response))
                pending.add(task)

                def done(completed: asyncio.Task) -> None:
                    pending.discard(completed)
                    if not completed.cancelled() and completed.exception() is None:
                        records.append(completed.result())

                task.add_done_callback(done)

            page.on("response", on_response)
            await page.goto(str(config.publish.login_url), wait_until="domcontentloaded")
            await asyncio.to_thread(
                input,
                "请完成登录，并手动访问‘我的文章’和‘数据中心’；操作完成后按 Enter：",
            )
            if pending:
                await asyncio.gather(*tuple(pending), return_exceptions=True)

            unique = {
                (record.method, record.url, record.status): record
                for record in records
            }
            ordered = sorted(unique.values(), key=lambda item: (item.url, item.method, item.status))
            json_path.write_text(
                json.dumps(
                    [record.model_dump(mode="json") for record in ordered],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            write_contract(ordered, markdown_path)
            print(f"已输出脱敏探测记录: {json_path}")
            print(f"已输出候选契约文档: {markdown_path}")

            if args.collect_article_id is not None:
                cookies = {
                    item["name"]: item["value"]
                    for item in await context.cookies()
                    if item.get("name") and item.get("value")
                }
                user_agent = await page.evaluate("() => navigator.userAgent")
                auth = CollectorAuth(
                    cookies=cookies,
                    headers={"User-Agent": user_agent},
                )
                snapshot = await CollectService(XiaoheiheCollector()).collect(
                    args.collect_article_id,
                    auth,
                )
                print(
                    json.dumps(
                        {
                            "snapshot_id": snapshot.id,
                            "article_id": snapshot.article_id,
                            "read_count": snapshot.read_count,
                            "snapshot_time": snapshot.snapshot_time.isoformat(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        finally:
            if context is not None:
                await context.close()
            await playwright.stop()
            await dispose_db()


if __name__ == "__main__":
    asyncio.run(_main())
