from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from platforms.base import DraftResultUnknownError
from platforms.content_validation import ContentValidationError
from platforms.smzdm import DRAFT_LIST_URL, SmzdmPlatform


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
    assert keyboard.insert_text.await_args_list == [call(char) for char in "开头章节结尾"]
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
        call(char) for char in "开头连续图片后的正文第二组图片后的正文"
    ]
    platform._validate_dom_exact.assert_awaited_once_with(blocks, phase="正文最终")
    assert result["media_status"] == "completed"
    assert result["uploaded_images"] == 7


def test_smzdm_creates_real_paragraph_with_public_tiptap_commands() -> None:
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
    assert "root.editor" in transaction_script
    assert "commands.insertContentAt" in transaction_script
    assert "commands.focus('end')" in transaction_script
    assert "modelTailHasImage" in transaction_script
    assert "root.pmViewDesc" not in transaction_script
    assert "view.state.tr.insert" not in transaction_script
    assert "ArrowDown" not in transaction_script
    assert "keyboard" not in transaction_script
    assert "tail.tagName.toLowerCase() === 'p'" in editor.evaluate.await_args_list[1].args[0]


def test_smzdm_waits_for_async_paragraph_after_image_atom() -> None:
    editor = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                {"ok": True, "action": "inserted"},
                False,
                {"ok": True, "action": "existing-model"},
                False,
                {"ok": True, "action": "existing"},
                True,
            ]
        ),
    )
    platform = SmzdmPlatform()
    platform.page = SimpleNamespace()
    platform._current_body_editor = AsyncMock(return_value=editor)

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()) as sleep:
        asyncio.run(platform._create_paragraph_after_image())

    assert editor.evaluate.await_count == 6
    assert sleep.await_count == 2


def test_smzdm_fails_closed_when_tiptap_editor_api_is_unavailable() -> None:
    editor = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={"ok": False, "reason": "editor-api-unavailable"}
        )
    )
    platform = SmzdmPlatform()
    platform.POST_IMAGE_PARAGRAPH_POLL_ATTEMPTS = 2
    platform.page = SimpleNamespace()
    platform._current_body_editor = AsyncMock(return_value=editor)

    with pytest.raises(
        ContentValidationError,
        match="reason=editor-api-unavailable",
    ):
        asyncio.run(platform._create_paragraph_after_image())

    assert editor.evaluate.await_count == 2


def test_smzdm_fails_closed_when_post_image_paragraph_never_appears() -> None:
    editor = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                {"ok": True, "action": "inserted"},
                False,
                {"ok": True, "action": "inserted"},
                False,
                {"ok": True, "action": "inserted"},
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
            match="reason=dom-tail-not-ready",
        ):
            asyncio.run(platform._create_paragraph_after_image())

    assert editor.evaluate.await_count == 6


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
    def __init__(self, entities: list[str]) -> None:
        self.entities = entities
        self.goto = AsyncMock()

    async def evaluate(self, script: str) -> list[dict[str, str]]:
        assert ".pandect-content-common" in script
        return [
            {
                "title": "重复标题",
                "title_href": "",
                "status": "草稿",
                "edit_href": edit_url,
                "delete_id": edit_url.rsplit("/", 1)[-1],
            }
            for edit_url in self.entities
        ]


def test_smzdm_same_title_drafts_use_unique_new_entity_id() -> None:
    platform = SmzdmPlatform()
    platform.page = DraftListPage(
        [
            "https://post.smzdm.com/edit/first",
            "https://post.smzdm.com/edit/second",
        ]
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        asyncio.run(platform.preflight_delivery("重复标题"))
        platform.page.entities.append("https://post.smzdm.com/edit/new-draft")
        result = asyncio.run(platform._find_unique_new_draft())

    assert result == "https://post.smzdm.com/edit/new-draft"
    assert platform._preflight_draft_ids == frozenset({"first", "second"})
    assert platform.page.goto.await_count == 2
    assert all(
        call.args[0] == DRAFT_LIST_URL
        for call in platform.page.goto.await_args_list
    )


def test_smzdm_multiple_new_entity_ids_are_result_unknown() -> None:
    platform = SmzdmPlatform()
    platform.page = DraftListPage(["https://post.smzdm.com/edit/existing"])
    platform._preflight_draft_ids = frozenset({"existing"})
    platform.page.entities.extend(
        [
            "https://post.smzdm.com/edit/new-one",
            "https://post.smzdm.com/edit/new-two",
        ]
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError, match="唯一确认"):
            asyncio.run(platform._find_unique_new_draft())


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://evil.example/edit/id",
        "https://post.smzdm.com/publish/id",
        "javascript:alert(1)",
    ],
)
def test_smzdm_rejects_unsafe_draft_edit_url(unsafe_url: str) -> None:
    with pytest.raises(DraftResultUnknownError, match="编辑地址无效"):
        SmzdmPlatform._validated_draft_entity(unsafe_url)


def test_smzdm_real_cloud_list_keeps_same_title_drafts_by_stable_id() -> None:
    entities = SmzdmPlatform._normalize_draft_list_rows(
        [
            {
                "title": "同名文章",
                "title_href": "https://post.smzdm.com/edit/ak8xo458",
                "status": "草稿",
                "edit_href": "https://post.smzdm.com/edit/ak8xo458",
                "delete_id": "ak8xo458",
            },
            {
                "title": "同名文章",
                "title_href": "https://post.smzdm.com/edit/ad72o4dz",
                "status": "草稿",
                "edit_href": "https://post.smzdm.com/edit/ad72o4dz",
                "delete_id": "ad72o4dz",
            },
            {
                "title": "已发布文章",
                "title_href": "https://post.smzdm.com/p/123/",
                "status": "已发布",
                "edit_href": "https://post.smzdm.com/edit/published-id",
                "delete_id": "published-id",
            },
        ]
    )

    assert entities == {
        "ak8xo458": ("https://post.smzdm.com/edit/ak8xo458", "同名文章"),
        "ad72o4dz": ("https://post.smzdm.com/edit/ad72o4dz", "同名文章"),
    }


def test_smzdm_real_cloud_list_rejects_conflicting_card_ids() -> None:
    with pytest.raises(DraftResultUnknownError, match="删除标识"):
        SmzdmPlatform._normalize_draft_list_rows(
            [
                {
                    "title": "目标文章",
                    "title_href": "",
                    "status": "草稿",
                    "edit_href": "https://post.smzdm.com/edit/edit-id",
                    "delete_id": "other-id",
                }
            ]
        )
