from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.content_validation import ContentValidationError
from platforms.xiaohongshu import XiaohongshuPlatform


def run(coroutine):
    return asyncio.run(coroutine)


def _platform() -> XiaohongshuPlatform:
    platform = XiaohongshuPlatform()
    platform.page = SimpleNamespace(is_closed=lambda: False)
    platform.simulator.random_delay = AsyncMock()
    return platform


def test_none_cover_still_finalizes_images_before_draft_save() -> None:
    platform = _platform()
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    platform._layout_expected_image_count = 0
    platform._finalize_long_text_layout = AsyncMock(return_value={"success": True})

    result = run(platform.apply_cover({"strategy": "NONE"}))

    assert result["cover_status"] == "not_required"
    assert platform._layout_finalized is True
    assert platform._expected_persisted_cover is False
    platform._finalize_long_text_layout.assert_awaited_once_with(
        expected_images=0,
        require_first_page_image=False,
    )


def test_first_body_image_cover_requires_exact_frozen_first_asset(tmp_path) -> None:
    first = tmp_path / "first.png"
    other = tmp_path / "other.png"
    first.write_bytes(b"first")
    other.write_bytes(b"other")
    platform = _platform()
    platform._expected_persisted_blocks = [{"type": "image", "local_path": str(first)}]
    platform._layout_expected_image_count = 1
    platform._finalize_long_text_layout = AsyncMock()

    result = run(platform.apply_cover({"strategy": "EXPLICIT", "local_path": str(other)}))

    assert result["success"] is False
    assert result["safe_to_continue"] is False
    assert result["error_code"] == "XHS_COVER_MUST_BE_FIRST_BODY_IMAGE"
    platform._finalize_long_text_layout.assert_not_awaited()


def test_first_body_image_cover_is_reported_unsupported_after_layout(tmp_path) -> None:
    first = tmp_path / "first.png"
    first.write_bytes(b"first")
    platform = _platform()
    platform._expected_persisted_blocks = [{"type": "image", "local_path": str(first)}]
    platform._layout_expected_image_count = 1
    platform._finalize_long_text_layout = AsyncMock(return_value={"success": True})

    result = run(platform.apply_cover({"strategy": "FIRST_BODY_IMAGE", "local_path": str(first)}))

    assert result["success"] is False
    assert result["cover_status"] == "failed"
    assert result["safe_to_continue"] is True
    assert result["error_code"] == "XHS_COVER_ASSET_SELECTION_UNSUPPORTED"
    assert platform._layout_finalized is True
    assert platform._expected_persisted_cover is False
    platform._finalize_long_text_layout.assert_awaited_once_with(
        expected_images=1,
        require_first_page_image=False,
    )


def test_resumed_verified_layout_is_not_formatted_again(tmp_path) -> None:
    first = tmp_path / "first.png"
    first.write_bytes(b"first")
    platform = _platform()
    platform._editing_existing_draft = True
    platform._layout_finalized = True
    platform._expected_persisted_blocks = [{"type": "image", "local_path": str(first)}]
    platform._layout_expected_image_count = 1
    platform._layout_snapshot = AsyncMock(
        return_value={
            "root_visible": True,
            "card_count": 2,
            "save_visible": True,
            "next_visible": True,
            "image_count": 1,
            "loaded_image_count": 1,
            "first_card_loaded_images": 1,
        }
    )
    platform._assert_layout_body_tokens = AsyncMock()
    platform._finalize_long_text_layout = AsyncMock()

    result = run(platform.apply_cover({"strategy": "FIRST_BODY_IMAGE", "local_path": str(first)}))

    assert result["success"] is False
    assert result["cover_status"] == "failed"
    assert result["safe_to_continue"] is True
    assert result["error_code"] == "XHS_COVER_ASSET_SELECTION_UNSUPPORTED"
    platform._finalize_long_text_layout.assert_not_awaited()


def test_layout_wait_requires_loaded_remote_images_not_only_img_nodes() -> None:
    platform = _platform()
    platform._expected_persisted_blocks = [
        {"type": "text", "text": "前文"},
        {"type": "image"},
    ]
    platform._layout_snapshot = AsyncMock(
        return_value={
            "root_visible": True,
            "card_count": 1,
            "save_visible": True,
            "next_visible": True,
            "image_count": 1,
            "loaded_image_count": 0,
            "first_card_loaded_images": 0,
            "text": "前文",
        }
    )
    platform._assert_layout_body_tokens = AsyncMock()

    with patch("platforms.xiaohongshu.asyncio.sleep", new=AsyncMock()):
        result = run(
            platform._wait_for_layout_ready(
                expected_images=1,
                require_first_page_image=True,
            )
        )

    assert result is False
    platform._assert_layout_body_tokens.assert_not_awaited()


def test_layout_snapshot_emits_valid_javascript_newline_escape() -> None:
    captured = {}

    async def evaluate(script: str):
        captured["script"] = script
        return {}

    platform = _platform()
    platform.page = SimpleNamespace(is_closed=lambda: False, evaluate=evaluate)

    run(platform._layout_snapshot())

    assert "join('\\n')" in captured["script"]
    assert "join('\n')" not in captured["script"]


def test_layout_wait_requires_two_stable_complete_reads() -> None:
    platform = _platform()
    platform._expected_persisted_blocks = [
        {"type": "text", "text": "前文"},
        {"type": "image"},
        {"type": "text", "text": "后文"},
    ]
    complete = {
        "root_visible": True,
        "card_count": 1,
        "save_visible": True,
        "next_visible": True,
        "image_count": 1,
        "loaded_image_count": 1,
        "first_card_loaded_images": 1,
        "text": "前文\n后文",
    }
    platform._layout_snapshot = AsyncMock(
        side_effect=[{**complete, "loaded_image_count": 0}, complete, complete]
    )
    platform._assert_layout_body_tokens = AsyncMock()

    with patch("platforms.xiaohongshu.asyncio.sleep", new=AsyncMock()):
        result = run(
            platform._wait_for_layout_ready(
                expected_images=1,
                require_first_page_image=True,
            )
        )

    assert result is True
    assert platform._layout_snapshot.await_count == 3
    assert platform._assert_layout_body_tokens.await_count == 2


def test_layout_tokens_require_original_text_image_order() -> None:
    blocks = [
        {"type": "text", "text": "前文"},
        {"type": "image"},
        {"type": "text", "text": "后文"},
    ]
    platform = _platform()
    platform._layout_body_tokens = AsyncMock(
        return_value=[
            {"kind": "text", "text": "额外标题", "tag": "h1"},
            {"kind": "text", "text": "前文", "tag": "p"},
            {"kind": "image", "text": "", "tag": "img"},
            {"kind": "text", "text": "后文", "tag": "p"},
        ]
    )
    run(platform._assert_layout_body_tokens(blocks))

    platform._layout_body_tokens = AsyncMock(
        return_value=[
            {"kind": "text", "text": "前文", "tag": "p"},
            {"kind": "text", "text": "后文", "tag": "p"},
            {"kind": "image", "text": "", "tag": "img"},
        ]
    )
    with pytest.raises(ContentValidationError, match="XHS_LAYOUT_ORDER_INVALID"):
        run(platform._assert_layout_body_tokens(blocks))


def test_layout_tokens_join_page_wrapped_h2_and_require_heading_class() -> None:
    blocks = [
        {
            "type": "heading",
            "level": 2,
            "text": "KVM别当万能按钮，输入源和USB链路要分开看",
        },
        {"type": "text", "text": "正文"},
    ]
    platform = _platform()
    platform._layout_body_tokens = AsyncMock(
        return_value=[
            {"kind": "text", "text": "KVM别当万能按钮，输入源", "tag": "h2"},
            {"kind": "text", "text": "和USB链路要分开看", "tag": "h2"},
            {"kind": "text", "text": "正文", "tag": "div"},
        ]
    )

    run(platform._assert_layout_body_tokens(blocks))

    platform._layout_body_tokens = AsyncMock(
        return_value=[
            {"kind": "text", "text": "KVM别当万能按钮，输入源", "tag": "div"},
            {"kind": "text", "text": "和USB链路要分开看", "tag": "div"},
            {"kind": "text", "text": "正文", "tag": "div"},
        ]
    )
    with pytest.raises(ContentValidationError, match="XHS_LAYOUT_ORDER_INVALID"):
        run(platform._assert_layout_body_tokens(blocks))


def test_save_draft_refuses_raw_editor_without_layout() -> None:
    platform = _platform()
    platform._draft_box_count_before = 10
    platform._unique_visible_button = AsyncMock()

    assert run(platform.save_draft("标题")) == ""
    platform._unique_visible_button.assert_not_awaited()


def test_save_draft_clicks_exact_leave_once_and_reopens_unique_entity() -> None:
    action = AsyncMock()
    page = SimpleNamespace(is_closed=lambda: False, goto=AsyncMock())
    platform = XiaohongshuPlatform()
    platform.page = page
    platform.simulator.random_delay = AsyncMock()
    platform._layout_finalized = True
    platform._draft_box_count_before = 10
    platform._unique_visible_button = AsyncMock(return_value=action)
    platform._draft_box_count = AsyncMock(return_value=11)
    platform._verify_saved_long_draft = AsyncMock()

    result = run(platform.save_draft("唯一标题"))

    assert result.endswith("/publish/publish")
    action.click.assert_awaited_once_with(timeout=10000)
    platform._verify_saved_long_draft.assert_awaited_once_with("唯一标题")


def test_layout_title_key_accepts_platform_inserted_wrap_space_only() -> None:
    assert XiaohongshuPlatform._draft_title_key("排版探测 -20260820") == (
        XiaohongshuPlatform._draft_title_key("排版探测-20260820")
    )


def test_matching_cards_remains_fail_closed_when_space_normalization_collides() -> None:
    cards = []
    for title in ("同 名", "同名"):
        card = MagicMock()
        card.inner_text = AsyncMock(return_value=title)
        cards.append(card)
    locator = MagicMock()
    locator.count = AsyncMock(return_value=2)
    locator.nth.side_effect = cards
    platform = _platform()
    platform.page = MagicMock()
    platform.page.is_closed.return_value = False
    platform.page.locator.return_value = locator

    matches = run(platform._matching_long_draft_cards("同名"))

    assert len(matches) == 2


def test_resume_exact_title_does_not_rewrite_title() -> None:
    title_field = MagicMock()
    title_field.count = AsyncMock(return_value=1)
    title_field.is_visible = AsyncMock(return_value=True)
    title_field.input_value = AsyncMock(return_value="冻结标题")
    platform = _platform()
    platform._editing_existing_draft = True
    locator = MagicMock()
    locator.first = title_field
    platform.page.locator = MagicMock(return_value=locator)

    run(platform.fill_title("冻结标题"))

    title_field.fill.assert_not_called()


def test_resume_layout_title_is_verified_from_first_card_not_body() -> None:
    title_field = MagicMock()
    title_field.count = AsyncMock(return_value=0)
    locator = MagicMock()
    locator.first = title_field
    platform = _platform()
    platform._editing_existing_draft = True
    platform.page.locator = MagicMock(return_value=locator)
    platform._layout_snapshot = AsyncMock(
        return_value={
            "root_visible": True,
            "cover_title": "冻结标题",
            "first_card_text": "正文第一页",
            "text": "正文不含标题",
        }
    )

    run(platform.fill_title("冻结标题"))

    title_field.fill.assert_not_called()


def test_resume_raw_content_validates_without_writing_or_uploading() -> None:
    blocks = [
        {"type": "text", "text": "前文"},
        {"type": "image", "local_path": "ignored.png"},
        {"type": "heading", "level": 2, "text": "后文"},
    ]
    editor = MagicMock()
    editor.count = AsyncMock(return_value=1)
    editor.is_visible = AsyncMock(return_value=True)
    editor.inner_text = AsyncMock(return_value="前文\n后文")
    locator = MagicMock()
    locator.first = editor
    platform = _platform()
    platform._editing_existing_draft = True
    platform.page.locator = MagicMock(return_value=locator)
    platform._layout_snapshot = AsyncMock(return_value={"root_visible": False})
    platform._assert_editor_body_tokens = AsyncMock()
    platform._raw_editor_image_state = AsyncMock(return_value={"count": 1, "loaded_count": 1})
    platform._upload_image = AsyncMock()

    result = run(platform.fill_content(blocks, []))

    assert result["media_status"] == "completed"
    assert result["uploaded_images"] == 1
    platform._assert_editor_body_tokens.assert_awaited_once_with(blocks)
    platform._upload_image.assert_not_awaited()


def test_resume_raw_content_rejects_broken_images_without_rewrite() -> None:
    blocks = [
        {"type": "text", "text": "前文"},
        {"type": "image", "local_path": "ignored.png"},
    ]
    editor = MagicMock()
    editor.count = AsyncMock(return_value=1)
    editor.is_visible = AsyncMock(return_value=True)
    editor.inner_text = AsyncMock(return_value="前文")
    locator = MagicMock()
    locator.first = editor
    platform = _platform()
    platform._editing_existing_draft = True
    platform.page.locator = MagicMock(return_value=locator)
    platform._layout_snapshot = AsyncMock(return_value={"root_visible": False})
    platform._assert_editor_body_tokens = AsyncMock()
    platform._raw_editor_image_state = AsyncMock(return_value={"count": 1, "loaded_count": 0})
    platform._upload_image = AsyncMock()

    with pytest.raises(ContentValidationError, match="XHS_RESUME_IMAGE_INVALID"):
        run(platform.fill_content(blocks, []))

    platform._upload_image.assert_not_awaited()


def test_resume_repairs_only_strict_trailing_plain_text() -> None:
    blocks = [
        {"type": "text", "text": "前文"},
        {"type": "image"},
        {"type": "text", "text": "唯一缺失尾段"},
    ]
    platform = _platform()
    platform._editor_body_tokens = AsyncMock(
        return_value=[
            {"kind": "text", "text": "前文", "tag": "p"},
            {"kind": "image", "text": "", "tag": "img"},
        ]
    )
    platform._place_body_caret_at_end = AsyncMock()
    platform.page.keyboard = SimpleNamespace(
        press=AsyncMock(),
        insert_text=AsyncMock(),
    )
    editor = MagicMock()
    editor.inner_text = AsyncMock(return_value="前文\n唯一缺失尾段")
    locator = MagicMock()
    locator.first = editor
    platform.page.locator = MagicMock(return_value=locator)
    platform._assert_editor_body_tokens = AsyncMock()

    repaired = run(platform._repair_strict_trailing_text_only(blocks))

    assert repaired is True
    platform.page.keyboard.insert_text.assert_awaited_once_with("唯一缺失尾段")
    platform._assert_editor_body_tokens.assert_awaited_once_with(blocks)


def test_resume_never_repairs_middle_or_image_gap() -> None:
    blocks = [
        {"type": "text", "text": "前文"},
        {"type": "image"},
        {"type": "text", "text": "尾段"},
    ]
    platform = _platform()
    platform._editor_body_tokens = AsyncMock(
        return_value=[{"kind": "text", "text": "前文", "tag": "p"}]
    )
    platform.page.keyboard = SimpleNamespace(
        press=AsyncMock(),
        insert_text=AsyncMock(),
    )

    repaired = run(platform._repair_strict_trailing_text_only(blocks))

    assert repaired is False
    platform.page.keyboard.insert_text.assert_not_awaited()
