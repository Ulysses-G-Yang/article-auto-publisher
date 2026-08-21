from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from platforms.base import DraftResultUnknownError
from platforms.content_validation import ContentValidationError
from platforms.smzdm import SmzdmPlatform


def test_smzdm_image_mapping_is_exact_and_never_falls_back() -> None:
    images = [
        {"position_index": 2, "local_path": "first.png"},
        {"position_index": 9, "local_path": "target.png"},
    ]

    assert (
        SmzdmPlatform._image_path_for_block(
            {"type": "image", "position": 9},
            images,
        )
        == "target.png"
    )
    assert (
        SmzdmPlatform._image_path_for_block(
            {"type": "image", "position": 7},
            images,
        )
        is None
    )
    assert (
        SmzdmPlatform._image_path_for_block(
            {"type": "image", "position": 9},
            [*images, {"position_index": 9, "local_path": "duplicate.png"}],
        )
        is None
    )


def test_smzdm_expected_tokens_preserve_word_heading_and_image_order() -> None:
    blocks = [
        {"type": "text", "text": "第一段\n第二段"},
        {"type": "image", "position": 2},
        {"type": "heading", "level": 2, "text": "章节"},
        {"type": "text", "text": "尾段"},
    ]

    assert SmzdmPlatform._expected_content_tokens(blocks) == [
        {"kind": "text", "text": "第一段"},
        {"kind": "text", "text": "第二段"},
        {"kind": "image"},
        {"kind": "heading", "level": 2, "text": "章节"},
        {"kind": "text", "text": "尾段"},
    ]


def test_smzdm_real_h3_dom_is_read_back_as_word_h2() -> None:
    editor = SimpleNamespace(
        evaluate=AsyncMock(
            return_value=[
                {"kind": "text", "text": "正文"},
                {"kind": "image"},
                {"kind": "heading", "level": 2, "text": "章节"},
            ]
        )
    )
    platform = SmzdmPlatform()
    platform.page = SimpleNamespace()
    platform._current_body_editor = AsyncMock(return_value=editor)

    tokens = asyncio.run(platform._read_editor_dom_tokens())

    assert tokens == [
        {"kind": "text", "text": "正文"},
        {"kind": "image"},
        {"kind": "heading", "level": 2, "text": "章节"},
    ]
    assert "tag === 'h3'" in editor.evaluate.await_args.args[0]


def test_smzdm_fill_content_processes_blocks_in_source_order() -> None:
    editor = SimpleNamespace(click=AsyncMock())
    keyboard = SimpleNamespace(press=AsyncMock(), insert_text=AsyncMock())
    platform = SmzdmPlatform()
    platform.page = SimpleNamespace(keyboard=keyboard)
    platform.simulator.random_delay = AsyncMock()
    platform._current_body_editor = AsyncMock(return_value=editor)
    platform._place_body_caret_at_end = AsyncMock()
    platform._apply_h2_to_current_block = AsyncMock()
    platform._upload_image = AsyncMock(return_value={"success": True})
    platform._create_paragraph_after_image = AsyncMock()
    platform._validate_dom_prefix = AsyncMock()
    platform._validate_dom_exact = AsyncMock()
    blocks = [
        {"type": "text", "text": "开头"},
        {"type": "image", "position": 1},
        {"type": "heading", "level": 2, "text": "章节"},
        {"type": "text", "text": "结尾"},
    ]

    result = asyncio.run(
        platform.fill_content(
            blocks,
            [{"position_index": 1, "local_path": "middle.png"}],
        )
    )

    platform._upload_image.assert_awaited_once_with("middle.png")
    platform._create_paragraph_after_image.assert_awaited_once()
    platform._validate_dom_prefix.assert_awaited_once_with(
        blocks[:2],
        phase="图片处理后第2块",
    )
    platform._apply_h2_to_current_block.assert_awaited_once()
    platform._validate_dom_exact.assert_awaited_once_with(blocks, phase="正文最终")
    assert keyboard.insert_text.await_args_list == [call("开头"), call("章节"), call("结尾")]
    assert result["media_status"] == "completed"
    assert result["uploaded_images"] == 1


def test_smzdm_full_multi_image_contract_handles_consecutive_and_final_images() -> None:
    editor = SimpleNamespace(click=AsyncMock())
    keyboard = SimpleNamespace(press=AsyncMock(), insert_text=AsyncMock())
    platform = SmzdmPlatform()
    platform.page = SimpleNamespace(keyboard=keyboard)
    platform.simulator.random_delay = AsyncMock()
    platform._current_body_editor = AsyncMock(return_value=editor)
    platform._place_body_caret_at_end = AsyncMock()
    platform._upload_image = AsyncMock(return_value={"success": True})
    platform._create_paragraph_after_image = AsyncMock()
    platform._validate_dom_prefix = AsyncMock()
    platform._validate_dom_exact = AsyncMock()
    blocks = [
        {"type": "text", "text": "开头"},
        {"type": "image", "position": 1},
        {"type": "image", "position": 2},
        {"type": "text", "text": "连续图片后的正文"},
        {"type": "image", "position": 3},
        {"type": "image", "position": 4},
        {"type": "image", "position": 5},
        {"type": "text", "text": "第二组图片后的正文"},
        {"type": "image", "position": 6},
        {"type": "image", "position": 7},
    ]
    images = [
        {"position_index": index, "local_path": f"image-{index}.png"}
        for index in range(1, 8)
    ]

    result = asyncio.run(platform.fill_content(blocks, images))

    assert platform._upload_image.await_args_list == [
        call(f"image-{index}.png") for index in range(1, 8)
    ]
    assert platform._create_paragraph_after_image.await_count == 7
    assert keyboard.insert_text.await_args_list == [
        call("开头"),
        call("连续图片后的正文"),
        call("第二组图片后的正文"),
    ]
    platform._validate_dom_exact.assert_awaited_once_with(blocks, phase="正文最终")
    assert result["media_status"] == "completed"
    assert result["uploaded_images"] == 7


def test_smzdm_creates_real_paragraph_with_prosemirror_transaction() -> None:
    editor = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                {"ok": True, "action": "inserted"},
                True,
            ]
        )
    )
    platform = SmzdmPlatform()
    platform.page = SimpleNamespace()
    platform._current_body_editor = AsyncMock(return_value=editor)

    asyncio.run(platform._create_paragraph_after_image())

    transaction_script = editor.evaluate.await_args_list[0].args[0]
    assert "root.pmViewDesc" in transaction_script
    assert "view.state.tr.insert" in transaction_script
    assert "view.dispatch(transaction.scrollIntoView())" in transaction_script
    assert "ArrowDown" not in transaction_script
    assert "keyboard" not in transaction_script
    assert "tail.tagName.toLowerCase() === 'p'" in editor.evaluate.await_args_list[1].args[0]


def test_smzdm_waits_for_async_paragraph_after_image_atom() -> None:
    editor = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                {"ok": True, "action": "inserted"},
                False,
                False,
                True,
            ]
        ),
    )
    platform = SmzdmPlatform()
    platform.page = SimpleNamespace()
    platform._current_body_editor = AsyncMock(return_value=editor)

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()) as sleep:
        asyncio.run(platform._create_paragraph_after_image())

    assert editor.evaluate.await_count == 4
    assert sleep.await_count == 2


def test_smzdm_fails_closed_when_prosemirror_view_is_unavailable() -> None:
    editor = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={"ok": False, "reason": "editor-view-unavailable"}
        )
    )
    platform = SmzdmPlatform()
    platform.page = SimpleNamespace()
    platform._current_body_editor = AsyncMock(return_value=editor)

    with pytest.raises(
        ContentValidationError,
        match="SMZDM_POST_IMAGE_PARAGRAPH_FAILED",
    ):
        asyncio.run(platform._create_paragraph_after_image())

    assert editor.evaluate.await_count == 1


def test_smzdm_fails_closed_when_post_image_paragraph_never_appears() -> None:
    editor = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                {"ok": True, "action": "inserted"},
                False,
                False,
                False,
            ]
        ),
    )
    platform = SmzdmPlatform()
    platform.POST_IMAGE_PARAGRAPH_POLL_ATTEMPTS = 3
    platform.page = SimpleNamespace()
    platform._current_body_editor = AsyncMock(return_value=editor)

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(
            ContentValidationError,
            match="SMZDM_POST_IMAGE_PARAGRAPH_FAILED",
        ):
            asyncio.run(platform._create_paragraph_after_image())

    assert editor.evaluate.await_count == 4


class PersistedTitle:
    def __init__(self, value: str) -> None:
        self.value = value

    async def input_value(self) -> str:
        return self.value


class PersistedLocator:
    def __init__(self, title: PersistedTitle) -> None:
        self.first = title


class PersistedPage:
    def __init__(self, title: str) -> None:
        self.goto = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.title = PersistedTitle(title)

    def locator(self, selector: str) -> PersistedLocator:
        assert selector == "textarea.article-title"
        return PersistedLocator(self.title)


def test_smzdm_reopen_rejects_missing_persisted_tail() -> None:
    platform = SmzdmPlatform()
    platform.page = PersistedPage("唯一标题")
    platform._expected_persisted_blocks = [
        {"type": "text", "text": "第一段"},
        {"type": "image", "position": 1},
        {"type": "text", "text": "最后一段"},
    ]
    platform._read_editor_dom_tokens = AsyncMock(
        return_value=[
            {"kind": "text", "text": "第一段"},
            {"kind": "image"},
        ]
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError, match="图文结构不完整"):
            asyncio.run(
                platform._verify_persisted_draft(
                    "唯一标题",
                    "https://post.smzdm.com/edit/safe-id",
                )
            )


class DraftListPage:
    def __init__(self, matches: list[str]) -> None:
        self.matches = matches
        self.goto = AsyncMock()
        self.reload = AsyncMock()

    async def evaluate(self, _script: str, _title: str) -> list[str]:
        return list(self.matches)


def test_smzdm_duplicate_exact_titles_are_result_unknown() -> None:
    platform = SmzdmPlatform()
    platform.page = DraftListPage(
        [
            "https://post.smzdm.com/edit/first",
            "https://post.smzdm.com/edit/second",
        ]
    )
    platform.simulator.random_delay = AsyncMock()

    with pytest.raises(DraftResultUnknownError, match="唯一草稿"):
        asyncio.run(platform._find_unique_exact_draft("重复标题"))


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://evil.example/edit/id",
        "https://post.smzdm.com/publish/id",
        "javascript:alert(1)",
    ],
)
def test_smzdm_rejects_unsafe_draft_edit_url(unsafe_url: str) -> None:
    platform = SmzdmPlatform()
    platform.page = DraftListPage([unsafe_url])
    platform.simulator.random_delay = AsyncMock()

    with pytest.raises(DraftResultUnknownError, match="编辑地址无效"):
        asyncio.run(platform._find_unique_exact_draft("唯一标题"))
