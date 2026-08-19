"""小黑盒格式能力必须以编辑器 DOM 证据为准。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from content_studio.platform_format_capabilities import (
    DEFAULT_PLATFORM_FORMAT_CAPABILITIES,
)
from platforms.xiaoheihe import XiaoheihePlatform
from tests.test_regression import FakeXiaoPage


def test_xiaoheihe_fake_heading_write_is_not_a_real_h2_and_is_not_declared():
    """普通文字回读不能冒充 H2；未有 DOM 证据时能力表必须保持关闭。"""

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
    # Fake editor 没有生成 h2/heading node；因此不能仅凭文本把 heading
    # 送入平台默认能力声明。
    assert "heading" not in DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get(
        "xiaoheihe"
    ).supported
