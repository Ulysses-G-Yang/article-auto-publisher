from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.content_validation import ContentValidationError
from platforms.xiaohongshu import (
    XiaohongshuCloudDraftUnavailableError,
    XiaohongshuPlatform,
)


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


def test_first_body_image_cover_uses_platform_generated_longform_mode(tmp_path) -> None:
    first = tmp_path / "first.png"
    first.write_bytes(b"first")
    platform = _platform()
    platform._expected_persisted_blocks = [{"type": "image", "local_path": str(first)}]
    platform._layout_expected_image_count = 1
    platform._finalize_long_text_layout = AsyncMock(return_value={"success": True})

    result = run(platform.apply_cover({"strategy": "FIRST_BODY_IMAGE", "local_path": str(first)}))

    assert result["success"] is True
    assert result["cover_status"] == "pending_verification"
    assert result["safe_to_continue"] is True
    assert result["cover_mode"] == "PLATFORM_GENERATED_LONGFORM"
    assert platform._layout_finalized is True
    assert platform._expected_persisted_cover is True
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

    assert result["success"] is True
    assert result["cover_status"] == "pending_verification"
    assert result["safe_to_continue"] is True
    assert result["cover_mode"] == "PLATFORM_GENERATED_LONGFORM"
    assert platform._expected_persisted_cover is True
    platform._finalize_long_text_layout.assert_not_awaited()


def test_platform_generated_cover_requires_preview_and_quality_pass() -> None:
    platform = _platform()
    next_button = AsyncMock()
    platform._unique_visible_button = AsyncMock(return_value=next_button)
    preview_heading = MagicMock(wait_for=AsyncMock())
    assessment_button = MagicMock(
        count=AsyncMock(return_value=1),
        nth=MagicMock(
            return_value=MagicMock(
                is_visible=AsyncMock(return_value=True),
                click=AsyncMock(),
            )
        ),
    )
    quality_text = MagicMock(count=AsyncMock(return_value=1))

    text_lookups: list[str] = []

    def get_by_text(text: str, **_kwargs):
        text_lookups.append(text)
        if text == "封面预览":
            return preview_heading
        if text == "获取封面建议":
            return assessment_button
        if text == "封面效果评估通过，未发现封面质量问题":
            return quality_text
        raise AssertionError(f"unexpected text lookup: {text}")

    platform.page = MagicMock(
        is_closed=lambda: False,
        get_by_text=get_by_text,
        evaluate=AsyncMock(
            return_value={"pagePreviews": 10, "phonePreviews": 10}
        ),
    )
    with patch("platforms.xiaohongshu.asyncio.sleep", new=AsyncMock()):
        result = run(
            platform.verify_persisted_cover(
                title="测试",
                draft_url="https://example.test/draft",
                cover={"strategy": "FIRST_BODY_IMAGE"},
                apply_result={
                    "success": True,
                    "cover_status": "pending_verification",
                },
            )
        )

    assert result["cover_status"] == "completed"
    assert result["cover_mode"] == "PLATFORM_GENERATED_LONGFORM"
    next_button.click.assert_awaited_once()
    assessment_button.nth.return_value.click.assert_awaited_once()
    assert "发布" not in text_lookups


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


def test_save_draft_refuses_profile_local_card_as_cloud_draft() -> None:
    platform = _platform()
    platform._draft_box_count_before = 10
    platform._unique_visible_button = AsyncMock()

    with pytest.raises(
        XiaohongshuCloudDraftUnavailableError,
        match="XHS_CLOUD_DRAFT_UNAVAILABLE",
    ):
        run(platform.save_draft("标题"))
    platform._unique_visible_button.assert_not_awaited()


def test_save_draft_never_clicks_local_leave_action() -> None:
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

    with pytest.raises(XiaohongshuCloudDraftUnavailableError):
        run(platform.save_draft("唯一标题"))

    action.click.assert_not_awaited()
    platform._verify_saved_long_draft.assert_not_awaited()


def test_saved_layout_without_cover_uses_unique_drawer_title_evidence() -> None:
    title = "无封面长文草稿"
    actions = MagicMock(count=AsyncMock(return_value=1), click=AsyncMock())
    card = MagicMock()
    card.locator.return_value.filter.return_value = actions
    platform = _platform()
    platform._preflight_title = title
    platform._layout_finalized = True
    platform._expected_persisted_cover = False
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    platform._open_long_draft_drawer = AsyncMock()
    platform._matching_long_draft_cards = AsyncMock(return_value=[card])
    platform.page.wait_for_selector = AsyncMock()
    platform._layout_snapshot = AsyncMock(
        return_value={
            "cover_title": "",
            "image_count": 0,
            "loaded_image_count": 0,
            "first_card_loaded_images": 0,
        }
    )
    platform._assert_layout_body_tokens = AsyncMock()

    run(platform._verify_saved_long_draft(title))

    actions.click.assert_awaited_once_with(timeout=15000)
    platform._assert_layout_body_tokens.assert_awaited_once()


def test_saved_layout_allows_one_new_card_after_existing_same_title() -> None:
    title = "允许同名草稿"
    new_actions = MagicMock(count=AsyncMock(return_value=1), click=AsyncMock())
    new_card = MagicMock()
    new_card.locator.return_value.filter.return_value = new_actions
    old_card = MagicMock()
    platform = _platform()
    platform._preflight_title = title
    platform._preflight_matching_draft_count = 1
    platform._layout_finalized = True
    platform._expected_persisted_cover = False
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    platform._open_long_draft_drawer = AsyncMock()
    platform._matching_long_draft_cards = AsyncMock(
        return_value=[new_card, old_card]
    )
    platform.page.wait_for_selector = AsyncMock()
    platform._layout_snapshot = AsyncMock(
        return_value={
            "cover_title": "",
            "image_count": 0,
            "loaded_image_count": 0,
            "first_card_loaded_images": 0,
        }
    )
    platform._assert_layout_body_tokens = AsyncMock()

    run(platform._verify_saved_long_draft(title))

    new_actions.click.assert_awaited_once_with(timeout=15000)
    platform._assert_layout_body_tokens.assert_awaited_once()


def test_layout_title_key_accepts_platform_inserted_wrap_space_only() -> None:
    assert XiaohongshuPlatform._draft_title_key("排版探测 -20260820") == (
        XiaohongshuPlatform._draft_title_key("排版探测-20260820")
    )


def test_matching_cards_remains_fail_closed_when_space_normalization_collides() -> None:
    cards = []
    for title in ("同 名", "同名"):
        card = MagicMock()
        title_field = MagicMock(
            count=AsyncMock(return_value=1),
            inner_text=AsyncMock(return_value=title),
        )
        card.locator.return_value.first = title_field
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


def test_long_draft_drawer_waits_for_tab_count_to_load_stably() -> None:
    entry = MagicMock(
        wait_for=AsyncMock(),
        is_visible=AsyncMock(return_value=True),
        click=AsyncMock(),
    )
    entries = MagicMock(
        first=entry,
        count=AsyncMock(return_value=1),
        nth=MagicMock(return_value=entry),
    )
    tab = MagicMock(
        is_visible=AsyncMock(return_value=True),
        inner_text=AsyncMock(return_value="长文笔记(2)"),
        click=AsyncMock(),
    )
    tabs = MagicMock(
        count=AsyncMock(return_value=1),
        nth=MagicMock(return_value=tab),
    )
    card_items = [
        MagicMock(is_visible=AsyncMock(return_value=True)),
        MagicMock(is_visible=AsyncMock(return_value=True)),
    ]
    cards = MagicMock(
        count=AsyncMock(return_value=2),
        nth=MagicMock(side_effect=lambda index: card_items[index]),
    )
    page = MagicMock(wait_for_selector=AsyncMock())
    page.locator.side_effect = lambda selector: (
        entries if selector == ".draft-title-box" else cards
    )
    page.get_by_text.return_value = tabs
    platform = _platform()
    platform.page = page

    with patch("platforms.xiaohongshu.asyncio.sleep", new=AsyncMock()):
        run(platform._open_long_draft_drawer())

    assert cards.count.await_count == 2
    entry.click.assert_awaited_once()
    tab.click.assert_awaited_once()


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


def test_resume_layout_without_cover_uses_preflight_exact_title() -> None:
    title = "无封面排版草稿"
    title_field = MagicMock()
    title_field.count = AsyncMock(return_value=0)
    locator = MagicMock()
    locator.first = title_field
    platform = _platform()
    platform._editing_existing_draft = True
    platform._expected_persisted_cover = False
    platform._preflight_title = title
    platform.page.locator = MagicMock(return_value=locator)
    platform._layout_snapshot = AsyncMock(
        return_value={"root_visible": True, "cover_title": ""}
    )

    run(platform.fill_title(title))

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
