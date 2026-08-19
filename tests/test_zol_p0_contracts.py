"""ZOL P0 安全契约的离线单元测试。

本文件不启动浏览器、不访问真实平台，只锁定冻结内容进入适配器后的
确定性边界：图片位置不得回退、编辑器重建后必须重新定位，以及尚无
标题 DOM 证据时不得把 heading 静默写成普通文本。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from content_studio.platform_format_capabilities import (
    DEFAULT_PLATFORM_FORMAT_CAPABILITIES,
)
from platforms.content_validation import ContentValidationError
from platforms.zol import ZOLPlatform
from tests.test_regression import FakeFrame, FakeLocator, FakePage


def test_default_zol_format_capability_remains_closed() -> None:
    declaration = DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get("zol")

    assert declaration is not None
    assert declaration.supported == frozenset()
    assert declaration.heading_levels == frozenset()


def test_seven_image_positions_map_exactly_without_fallback_to_first_image() -> None:
    positions = [0, 2, 3, 5, 7, 8, 10]
    blocks = [{"type": "image", "position": position} for position in positions]
    images = [
        {
            "position_index": position,
            "local_path": f"D:/fixture/image-{index}.png",
        }
        for index, position in enumerate(positions)
    ]

    assert [
        ZOLPlatform._image_path_for_block(block, images) for block in blocks
    ] == [f"D:/fixture/image-{index}.png" for index in range(7)]
    assert ZOLPlatform._image_path_for_block(
        {"type": "image", "position": 999}, images
    ) is None
    assert ZOLPlatform._image_path_for_block(
        {"type": "image"}, images
    ) is None


def test_missing_image_position_is_reported_and_never_uploads_first_image() -> None:
    platform = ZOLPlatform()
    platform.page = FakePage("contenteditable")
    platform.simulator.random_delay = AsyncMock()
    platform._upload_image = AsyncMock()

    with pytest.raises(ContentValidationError, match="ZOL_IMAGE_FILE_MISSING"):
        asyncio.run(
            platform.fill_content(
                [
                    {"type": "text", "text": "正文"},
                    {"type": "image", "position": 1},
                ],
                [{"position_index": 0, "local_path": "D:/fixture/first.png"}],
            )
        )

    platform._upload_image.assert_not_awaited()


@pytest.mark.parametrize("level", [None, 1, 4])
def test_heading_unknown_or_unsupported_level_is_fail_closed(level) -> None:
    platform = ZOLPlatform()
    block = {"type": "heading", "text": "不会写入"}
    if level is not None:
        block["level"] = level

    with pytest.raises(ContentValidationError, match="ZOL_HEADING_UNSUPPORTED_LEVEL"):
        asyncio.run(platform.fill_content([block], []))


@pytest.mark.parametrize("level", [2, 3])
def test_heading_h2_h3_remain_closed_without_experimental_flag(level) -> None:
    platform = ZOLPlatform()

    with pytest.raises(ContentValidationError, match="ZOL_HEADING_UNVERIFIED"):
        asyncio.run(
            platform.fill_content(
                [{"type": "heading", "level": level, "text": "不会写入"}],
                [],
            )
        )


def test_image_order_comparison_rejects_wrong_sequence_without_self_report() -> None:
    assert ZOLPlatform._image_order_matches([1, 3, 5], [1, 3, 5]) is True
    assert ZOLPlatform._image_order_matches([1, 3, 5], [5, 3, 1]) is False
    assert ZOLPlatform._image_order_matches([1, 3, 5], None) is False


def test_multi_image_success_without_dom_fingerprint_stays_failed() -> None:
    platform = ZOLPlatform()
    platform.page = FakePage("contenteditable")
    platform.simulator.random_delay = AsyncMock()
    platform._upload_image = AsyncMock(
        side_effect=[
            {"success": True, "filename": "image-1.png"},
            {"success": True, "filename": "image-2.png"},
        ]
    )

    with pytest.raises(ContentValidationError, match="ZOL_IMAGE_ORDER_UNVERIFIED"):
        asyncio.run(
            platform.fill_content(
                [
                    {"type": "image", "position": 1},
                    {"type": "image", "position": 3},
                ],
                [
                    {"position_index": 1, "local_path": "D:/fixture/image-1.png"},
                    {"position_index": 3, "local_path": "D:/fixture/image-2.png"},
                ],
            )
        )

    assert platform._upload_image.await_count == 1


def test_dom_token_comparison_ignores_image_url_identity_but_rejects_interleaving() -> None:
    first = {"kind": "image", "fingerprint": "a"}
    second = {"kind": "image", "fingerprint": "b"}
    text = {"kind": "text", "text": "正文"}

    assert ZOLPlatform._content_tokens_match([text, first, second], [text, first, second])
    assert ZOLPlatform._content_tokens_match([text, first, second], [text, second, first])
    assert not ZOLPlatform._content_tokens_match([text, first, second], [first, text, second])


def test_editor_dom_src_is_reduced_to_non_sensitive_fingerprint() -> None:
    platform = ZOLPlatform()

    class TokenEditor(FakeLocator):
        async def evaluate(self, script, *_args):
            if "const tokens" in script:
                return [
                    {"kind": "text", "text": "前文"},
                    {"kind": "image", "src": "https://cdn.invalid/a.png"},
                    {"kind": "heading", "tag": "h2", "text": "标题"},
                ]
            return await super().evaluate(script, *_args)

    editor = TokenEditor(tag="body")
    tokens = asyncio.run(platform._read_editor_dom_tokens(editor, "iframe"))

    assert [item["kind"] for item in tokens] == ["text", "image", "heading"]
    assert "src" not in tokens[1]
    assert len(tokens[1]["fingerprint"]) == 64


def test_image_upload_re_resolves_rebuilt_iframe_before_next_text_block() -> None:
    platform = ZOLPlatform()
    page = FakePage("iframe")
    platform.page = page
    platform.simulator.random_delay = AsyncMock()
    original_body = page.frame_body
    replacement_body = FakeLocator(
        page=page,
        tag="body",
        text="前文",
        value="前文",
    )

    async def replace_iframe_after_upload(_path):
        page.frame_body = replacement_body
        page.frame = FakeFrame(page, replacement_body)
        page.iframe_handle.content_frame = AsyncMock(return_value=page.frame)
        return {
            "success": True,
            "filename": "image.png",
            "image_src_fingerprint": "image-fingerprint",
        }

    platform._upload_image = AsyncMock(side_effect=replace_iframe_after_upload)
    platform._read_editor_dom_tokens = AsyncMock(
        side_effect=[
            [
                {"kind": "text", "text": "前文"},
                {"kind": "image", "fingerprint": "image-fingerprint"},
            ],
            [
                {"kind": "text", "text": "前文"},
                {"kind": "image", "fingerprint": "image-fingerprint"},
                {"kind": "text", "text": "后文"},
            ],
        ]
    )

    result = asyncio.run(
        platform.fill_content(
            [
                {"type": "text", "text": "前文"},
                {"type": "image", "position": 1},
                {"type": "text", "text": "后文"},
            ],
            [{"position_index": 1, "local_path": "D:/fixture/image.png"}],
        )
    )

    assert result["text_ok"] is True
    assert "后文" in replacement_body.text
    assert "后文" not in original_body.text


class _ImageCountLocator(FakeLocator):
    def __init__(self, *args, image_count: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.image_count = image_count

    def locator(self, selector):
        if selector == "img":
            return FakeLocator(count=self.image_count)
        return super().locator(selector)


def test_image_count_uses_the_same_resolved_iframe_body_as_content_editor() -> None:
    platform = ZOLPlatform()
    page = FakePage("iframe")
    body = _ImageCountLocator(page=page, tag="body", image_count=7)
    page.frame_body = body
    page.frame = FakeFrame(page, body)
    page.iframe_handle.content_frame = AsyncMock(return_value=page.frame)
    platform.page = page

    assert asyncio.run(platform._editor_image_count()) == 7
