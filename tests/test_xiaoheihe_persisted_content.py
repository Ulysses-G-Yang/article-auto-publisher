"""小黑盒图文顺序与保存后重开硬校验。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from platforms.base import DraftResultUnknownError
from platforms.xiaoheihe import XiaoheihePlatform


class _TokenEditor:
    def __init__(self, raw_tokens):
        self.raw_tokens = raw_tokens
        self.scripts: list[str] = []

    async def evaluate(self, script: str):
        self.scripts.append(script)
        return self.raw_tokens


class _TitleEditor:
    def __init__(self, title: str):
        self.title = title

    async def inner_text(self):
        return self.title


def test_image_path_mapping_never_falls_back_to_first_unmatched_image():
    images = [
        {"position_index": 3, "local_path": "D:/images/first.png"},
        {"position_index": 7, "local_path": "D:/images/second.png"},
    ]

    assert XiaoheihePlatform._image_path_for_block(
        {"type": "image", "position": 7}, images
    ) == "D:/images/second.png"
    assert XiaoheihePlatform._image_path_for_block(
        {"type": "image", "position": 99}, images
    ) is None
    assert XiaoheihePlatform._image_path_for_block(
        {"type": "image", "position": 3}, images + [images[0]]
    ) is None


def test_content_token_contract_rejects_missing_or_misplaced_images():
    blocks = [
        {"type": "text", "text": "第一段"},
        {"type": "image", "position": 1},
        {"type": "heading", "level": 2, "text": "章节标题"},
        {"type": "text", "text": "末尾段落"},
    ]
    expected = XiaoheihePlatform._expected_content_tokens(blocks)

    assert XiaoheihePlatform._content_tokens_match(expected, list(expected))
    assert not XiaoheihePlatform._content_tokens_match(
        expected, [expected[0], expected[2], expected[1], expected[3]]
    )
    assert not XiaoheihePlatform._content_tokens_match(
        expected, [expected[0], expected[1], expected[2]]
    )


@pytest.mark.asyncio
async def test_editor_dom_reader_normalizes_entities_and_preserves_order():
    editor = _TokenEditor(
        [
            {"kind": "text", "text": "A &amp; B\u200b"},
            {"kind": "image"},
            {"kind": "heading", "tag": "h2", "text": "标题&nbsp;二"},
            {"kind": "text", "text": "最后一段"},
        ]
    )
    platform = XiaoheihePlatform()

    tokens = await platform._read_editor_dom_tokens(editor)

    assert tokens == [
        {"kind": "text", "text": "A & B"},
        {"kind": "image"},
        {"kind": "heading", "tag": "h2", "text": "标题 二"},
        {"kind": "text", "text": "最后一段"},
    ]
    assert "document.body" not in editor.scripts[0]


@pytest.mark.asyncio
async def test_persisted_reopen_missing_tail_is_result_unknown(monkeypatch):
    platform = XiaoheihePlatform()
    platform.page = SimpleNamespace(
        url="https://www.xiaoheihe.cn/creator/editor/draft/article/test",
        is_closed=lambda: False,
    )
    platform.DRAFT_CONTENT_POLL_DELAYS = (0,)
    platform._expected_persisted_blocks = [
        {"type": "text", "text": "第一段"},
        {"type": "image", "position": 1},
        {"type": "text", "text": "必须存在的末尾段落"},
    ]

    async def open_unique(_title):
        return None

    async def first_visible(_selector):
        return _TitleEditor("唯一草稿标题")

    async def current_editor():
        return _TokenEditor([])

    async def read_tokens(_editor):
        return [
            {"kind": "text", "text": "第一段"},
            {"kind": "image"},
        ]

    monkeypatch.setattr(platform, "_open_unique_matching_draft", open_unique)
    monkeypatch.setattr(platform, "_first_visible", first_visible)
    monkeypatch.setattr(platform, "_current_body_editor", current_editor)
    monkeypatch.setattr(platform, "_read_editor_dom_tokens", read_tokens)

    with pytest.raises(DraftResultUnknownError, match="图文结构不完整") as exc_info:
        await platform._verify_persisted_draft_content("唯一草稿标题")

    message = str(exc_info.value)
    assert "必须存在的末尾段落" not in message
    assert "expected=T:3,I,T:9" in message


@pytest.mark.asyncio
async def test_persisted_reopen_accepts_exact_full_structure(monkeypatch):
    blocks = [
        {"type": "text", "text": "第一段"},
        {"type": "image", "position": 1},
        {"type": "heading", "level": 2, "text": "第二节"},
        {"type": "text", "text": "末尾段落"},
    ]
    platform = XiaoheihePlatform()
    platform.page = SimpleNamespace(
        url="https://www.xiaoheihe.cn/creator/editor/draft/article/test",
        is_closed=lambda: False,
    )
    platform.DRAFT_CONTENT_POLL_DELAYS = (0,)
    platform._expected_persisted_blocks = blocks

    async def open_unique(_title):
        return None

    async def first_visible(_selector):
        return _TitleEditor("唯一草稿标题")

    async def current_editor():
        return _TokenEditor([])

    async def read_tokens(_editor):
        return XiaoheihePlatform._expected_content_tokens(blocks)

    monkeypatch.setattr(platform, "_open_unique_matching_draft", open_unique)
    monkeypatch.setattr(platform, "_first_visible", first_visible)
    monkeypatch.setattr(platform, "_current_body_editor", current_editor)
    monkeypatch.setattr(platform, "_read_editor_dom_tokens", read_tokens)

    await platform._verify_persisted_draft_content("唯一草稿标题")
