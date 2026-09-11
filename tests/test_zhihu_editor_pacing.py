"""Real browser input/selection timing on an isolated Zhihu-shaped editor."""

import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from playwright.async_api import async_playwright

from platforms.content_validation import ContentValidationError
from platforms.zhihu import ZhihuPlatform

FIXTURE = Path(__file__).parent / "fixtures" / "zhihu" / "editor.html"


@pytest.mark.parametrize("case", ["normal", "ignored_enter", "lost_heading", "lost_input"])
def test_paced_writer_waits_for_native_blocks_and_stops_before_later_upload(tmp_path, case):
    async def scenario():
        payload = io.BytesIO()
        Image.new("RGB", (20, 20), "blue").save(payload, format="PNG")
        asset = tmp_path / "body.png"
        asset.write_bytes(payload.getvalue())
        second_asset = tmp_path / "second-body.png"
        second_asset.write_bytes(payload.getvalue())
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()

                async def route(request):
                    if request.request.resource_type == "image":
                        await request.fulfill(body=payload.getvalue(), content_type="image/png")
                    else:
                        await request.fulfill(
                            body=FIXTURE.read_text(encoding="utf-8"), content_type="text/html"
                        )

                await page.route("**/*", route)
                await page.goto("https://fixture.invalid/editor")
                await page.evaluate("name => {window.caseName = name}", case)
                platform = ZhihuPlatform()
                platform.page = page
                platform.context = page.context
                platform.simulator.random_delay = AsyncMock()
                platform.TEXT_CHUNK_INTERVAL_SECONDS = 0.005
                platform.BLOCK_SETTLE_SECONDS = 0.03
                platform.EDITOR_WAIT_INTERVAL_SECONDS = 0.02
                platform.EDITOR_WAIT_ATTEMPTS = 35
                blocks = [
                    {"type": "text", "text": "第一段分多次输入验证全文清空。\n"
                     "第二行 &amp; 空白\u200b"},
                    {"type": "heading", "level": 2, "text": "不能与后文合并的小标题"},
                    {"type": "text", "text": "标题后必须是独立正文段落。"},
                    {"type": "image", "local_path": str(second_asset)},
                    {"type": "heading", "level": 2, "text": "图片之后的二级标题"},
                    {"type": "text", "text": "重建编辑器之后仍写入末尾。"},
                    {"type": "image", "local_path": str(asset)},
                    {"type": "text", "text": "最后一段不能丢失。"},
                ]
                if case == "normal":
                    result = await platform.fill_content(blocks, [])
                    assert result["media_status"] == "completed"
                    assert await platform._read_editor_dom_tokens() == (
                        platform._expected_content_tokens(blocks)
                    )
                    assert await page.evaluate("window.uploads") == 2
                    chunks = await page.evaluate("window.inputs")
                    assert max(map(len, chunks)) <= platform.TEXT_CHUNK_SIZE
                    assert "旧稿" not in await page.locator("[contenteditable]").inner_text()
                else:
                    with pytest.raises(ContentValidationError):
                        await platform.fill_content(blocks, [])
                    assert await page.evaluate("window.uploads") == 0
                    assert "最后一段" not in await page.locator("[contenteditable]").inner_text()
                    if case == "ignored_enter":
                        assert await page.evaluate("window.enters") == 1
                        assert "第二行" not in await page.locator("[contenteditable]").inner_text()
            finally:
                await browser.close()

    asyncio.run(scenario())
