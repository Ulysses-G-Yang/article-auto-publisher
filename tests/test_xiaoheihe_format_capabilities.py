"""小黑盒格式能力必须以编辑器 DOM 证据为准。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from content_studio.platform_format_capabilities import (
    DEFAULT_PLATFORM_FORMAT_CAPABILITIES,
)
from platforms.content_validation import ContentValidationError
from platforms.xiaoheihe import XiaoheihePlatform
from tests.test_regression import FakeKeyboard, FakeLocator, FakeXiaoPage


class _HeadingKeyboard(FakeKeyboard):
    """把已验证的 Markdown 输入规则投影为最小 DOM fake。"""

    async def type(self, value, delay=0):
        if value in ("# ", "## "):
            self.page.pending_heading_level = 2 if value == "# " else 3
            return
        await self.insert_text(value)

    async def insert_text(self, value):
        level = getattr(self.page, "pending_heading_level", None)
        await super().insert_text(value)
        if level is not None:
            self.page.body.heading_nodes.append(
                {"tag": f"h{level}", "text": value}
            )
            self.page.pending_heading_level = None
            self.page.active_heading = self.page.body.heading_nodes[-1]
        elif getattr(self.page, "active_heading", None) is not None:
            self.page.active_heading["text"] += value

    async def press(self, key):
        if key == "Enter":
            self.page.active_heading = None
        await super().press(key)


class _HeadingEditor(FakeLocator):
    def __init__(self, page):
        super().__init__(page=page, tag="div")
        self.heading_nodes = []

    async def fill(self, value):
        await super().fill(value)
        self.heading_nodes.clear()

    async def evaluate(self, script, *_args):
        if "querySelectorAll('h1,h2,h3,h4,h5,h6')" in script:
            return list(self.heading_nodes)
        return None


class _HeadingPage(FakeXiaoPage):
    def __init__(self):
        super().__init__()
        self.body = _HeadingEditor(self)
        self.keyboard = _HeadingKeyboard(self)
        self.pending_heading_level = None


class _PlainHeadingPage(_HeadingPage):
    """模拟平台把 Markdown 当普通文字，必须触发 fail-closed。"""

    def __init__(self):
        super().__init__()
        self.keyboard = FakeKeyboard(self)


def test_xiaoheihe_legacy_heading_without_level_keeps_text_compatibility():
    """旧 v1 heading 没有层级，继续按普通文字兼容，不伪造结构证据。"""

    platform = XiaoheihePlatform()
    platform.page = FakeXiaoPage()
    platform.simulator.random_delay = AsyncMock()

    result = asyncio.run(
        platform.fill_content(
            [{"type": "heading", "text": "二级标题的普通文字"}],
            [],
        )
    )

    assert result["text_ok"] is True
    assert platform.page.body.tag.lower() == "div"
    assert platform.page.body.text == "二级标题的普通文字"
    assert DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get("xiaoheihe").heading_levels == {
        2,
        3,
    }


def test_xiaoheihe_heading_levels_are_written_as_real_nodes_in_order():
    platform = XiaoheihePlatform()
    platform.page = _HeadingPage()
    platform.simulator.random_delay = AsyncMock()
    platform._upload_image = AsyncMock(
        return_value={"success": True, "filename": "fixture.png"}
    )

    result = asyncio.run(
        platform.fill_content(
            [
                {"type": "heading", "level": 2, "text": "H2 一"},
                {"type": "text", "text": "正文一"},
                {"type": "image", "position": 2, "local_path": "D:/one.png"},
                {"type": "heading", "level": 3, "text": "H3 二"},
                {"type": "heading", "level": 2, "text": "H2 三"},
            ],
            [],
        )
    )

    assert result["text_ok"] is True
    assert [node["tag"] for node in platform.page.body.heading_nodes] == [
        "h2",
        "h3",
        "h2",
    ]
    assert [node["text"] for node in platform.page.body.heading_nodes] == [
        "H2 一",
        "H3 二",
        "H2 三",
    ]


def test_xiaoheihe_heading_fails_closed_when_dom_is_plain_text():
    platform = XiaoheihePlatform()
    platform.page = _PlainHeadingPage()
    platform.simulator.random_delay = AsyncMock()

    with pytest.raises(ContentValidationError, match="标题层级回读失败"):
        asyncio.run(
            platform.fill_content(
                [{"type": "heading", "level": 2, "text": "没有 H2"}],
                [],
            )
        )


def test_xiaoheihe_heading_validation_normalizes_entities_and_zero_width_chars():
    platform = XiaoheihePlatform()
    platform.page = _HeadingPage()
    platform.simulator.random_delay = AsyncMock()

    result = asyncio.run(
        platform.fill_content(
            [{"type": "heading", "level": 2, "text": "A &amp; B\u200b"}],
            [],
        )
    )

    assert result["text_ok"] is True
    assert platform.page.body.heading_nodes == [
        {"tag": "h2", "text": "A &amp; B\u200b"}
    ]


def test_xiaoheihe_heading_rejects_unverified_level_before_input():
    platform = XiaoheihePlatform()
    platform.page = _HeadingPage()
    platform.simulator.random_delay = AsyncMock()

    with pytest.raises(ContentValidationError, match="仅验证了 H2/H3"):
        asyncio.run(
            platform.fill_content(
                [{"type": "heading", "level": 4, "text": "未验证"}],
                [],
            )
        )
