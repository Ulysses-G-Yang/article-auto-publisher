from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.base import DraftBaselineError
from platforms.content_validation import ContentValidationError
from platforms.xiaohongshu import (
    XiaohongshuPlatform,
    choose_verified_body_image_index,
)


class FakeUploadPage:
    def __init__(self, chooser=None) -> None:
        self.locator_calls: list[str] = []
        self.chooser = chooser

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        raise AssertionError("fail-closed path must not locate an unverified input")

    def expect_file_chooser(self, **_kwargs):
        chooser = self.chooser

        class Pending:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            @property
            async def value(self):
                return chooser

        return Pending()


class FakeElement:
    def __init__(self, metadata=None) -> None:
        self.metadata = metadata or {
            "type": "file",
            "accept": "image/jpeg,image/jpg,image/png,image/webp",
            "multiple": False,
            "cover": False,
        }

    async def evaluate(self, _script):
        return self.metadata


class FakeChooser:
    def __init__(self, error: Exception | None = None, metadata=None) -> None:
        self.element = FakeElement(metadata)
        self.set_files = AsyncMock(side_effect=error)


class FakeButton:
    click = AsyncMock()


class FakeKeyboard:
    async def press(self, _key: str) -> None:
        return None

    async def insert_text(self, _value: str) -> None:
        return None


class FakeEditor:
    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def click(self) -> None:
        return None

    async def inner_text(self) -> str:
        return "正文"


class FakeEditorPage:
    def __init__(self) -> None:
        self.keyboard = FakeKeyboard()
        self.editor = FakeEditor()

    def locator(self, selector: str):
        if "tiptap.ProseMirror" in selector:
            return type("First", (), {"first": self.editor})()
        raise AssertionError(f"unexpected selector: {selector}")


def run(coroutine):
    return asyncio.run(coroutine)


def test_multiple_file_inputs_choose_only_proven_body_input() -> None:
    evidence = [
        {
            "type": "file",
            "accept": "image/*",
            "in_body_editor": False,
            "neighbor_labels": ["封面"],
        },
        {
            "type": "file",
            "accept": "image/png,image/jpeg",
            "in_body_editor": True,
            "neighbor_labels": ["正文"],
        },
    ]
    assert choose_verified_body_image_index(evidence) == 1


def test_no_qualified_input_fails_closed() -> None:
    evidence = [
        {
            "type": "file",
            "accept": "image/*",
            "in_body_editor": False,
            "neighbor_labels": ["封面"],
        },
        {
            "type": "file",
            "accept": "video/*",
            "in_body_editor": True,
            "neighbor_labels": ["正文"],
        },
    ]
    assert choose_verified_body_image_index(evidence) is None


def test_upload_does_not_fallback_to_first_input() -> None:
    platform = XiaohongshuPlatform()
    platform.page = FakeUploadPage()
    result = run(platform._upload_image(r"D:\secret\photo.png"))
    assert result["success"] is False
    assert result["error_code"] == "XHS_BODY_IMAGE_BUTTON_UNVERIFIED"
    assert platform.page.locator_calls == ["div.tiptap.ProseMirror"]
    assert not any("input" in selector for selector in platform.page.locator_calls)


def test_image_count_not_increasing_is_failure() -> None:
    platform = XiaohongshuPlatform()
    chooser = FakeChooser()
    platform.page = FakeUploadPage(chooser)
    platform._get_verified_body_image_button = AsyncMock(return_value=FakeButton())
    platform._editor_image_count = AsyncMock(return_value=0)
    with patch("platforms.xiaohongshu.asyncio.sleep", new=AsyncMock()):
        result = run(platform._upload_image("photo.png"))
    assert result["success"] is False
    assert result["error_code"] == "XHS_EDITOR_IMAGE_COUNT_UNCHANGED"
    chooser.set_files.assert_awaited_once()


def test_upload_exception_redacts_physical_path_and_token() -> None:
    platform = XiaohongshuPlatform()
    platform.page = FakeUploadPage()
    chooser = FakeChooser(
        RuntimeError(
            r"set_input_files D:\secret\private\photo.png "
            "token=abcdefghijklmnopqrstuv"
        )
    )
    platform.page = FakeUploadPage(chooser)
    platform._get_verified_body_image_button = AsyncMock(return_value=FakeButton())
    platform._editor_image_count = AsyncMock(return_value=0)
    result = run(platform._upload_image("photo.png"))
    serialized = repr(result)
    assert r"D:\secret" not in serialized
    assert "abcdefghijklmnopqrstuv" not in serialized


def _fill_content_with_upload_results(results: list[dict]) -> dict:
    platform = XiaohongshuPlatform()
    platform.page = FakeEditorPage()
    platform.simulator.random_delay = AsyncMock()
    platform._upload_image = AsyncMock(side_effect=results)
    platform._place_body_caret_at_end = AsyncMock()
    platform._assert_editor_body_tokens = AsyncMock()
    return run(
        platform.fill_content(
            [
                {"type": "text", "text": "正文"},
                {"type": "image", "position": 1, "local_path": "one.png"},
                {"type": "image", "position": 2, "local_path": "two.png"},
            ],
            [],
        )
    )


def test_media_status_completed() -> None:
    result = _fill_content_with_upload_results(
        [{"success": True}, {"success": True}]
    )
    assert result["media_status"] == "completed"
    assert result["uploaded_images"] == 2


def test_media_status_partial() -> None:
    result = _fill_content_with_upload_results(
        [{"success": True}, {"success": False, "error": "控件未验证"}]
    )
    assert result["media_status"] == "partial"
    assert result["uploaded_images"] == 1
    assert len(result["failed_images"]) == 1


def test_media_status_failed() -> None:
    result = _fill_content_with_upload_results(
        [{"success": False, "error": "控件未验证"}, {"success": False}]
    )
    assert result["media_status"] == "failed"
    assert result["uploaded_images"] == 0
    assert len(result["failed_images"]) == 1


def test_expected_tokens_preserve_text_image_h2_order() -> None:
    tokens = XiaohongshuPlatform._expected_body_tokens(
        [
            {"type": "text", "text": "前文"},
            {"type": "image"},
            {"type": "heading", "level": 2, "text": "二级标题"},
        ]
    )
    assert [(item["kind"], item["tag"]) for item in tokens] == [
        ("text", "text"),
        ("image", "img"),
        ("text", "h2"),
    ]


def test_heading_requires_single_line_level_two() -> None:
    platform = XiaohongshuPlatform()
    platform.page = FakeEditorPage()
    platform.simulator.random_delay = AsyncMock()
    platform._place_body_caret_at_end = AsyncMock()
    platform._assert_editor_body_tokens = AsyncMock()
    with pytest.raises(ContentValidationError, match="XHS_HEADING_UNSUPPORTED"):
        run(
            platform.fill_content(
                [{"type": "heading", "level": 3, "text": "不支持的标题"}],
                [],
            )
        )


def test_level_two_heading_uses_verified_toolbar_action() -> None:
    platform = XiaohongshuPlatform()
    platform.page = FakeEditorPage()
    platform.simulator.random_delay = AsyncMock()
    platform._place_body_caret_at_end = AsyncMock()
    platform._apply_h2_to_current_block = AsyncMock()
    platform._assert_editor_body_tokens = AsyncMock()
    result = run(
        platform.fill_content(
            [{"type": "heading", "level": 2, "text": "正文"}],
            [],
        )
    )
    platform._apply_h2_to_current_block.assert_awaited_once()
    assert result["text_ok"] is True

def test_two_body_candidates_are_ambiguous() -> None:
    evidence = [
        {
            "type": "file",
            "accept": "image/*",
            "in_body_editor": True,
            "neighbor_labels": ["正文"],
        },
        {
            "type": "file",
            "accept": "image/png",
            "in_body_editor": True,
            "neighbor_labels": ["正文"],
        },
    ]
    assert choose_verified_body_image_index(evidence) is None


def test_unverified_file_chooser_metadata_fails_closed() -> None:
    platform = XiaohongshuPlatform()
    chooser = FakeChooser(metadata={
        "type": "file",
        "accept": "image/png",
        "multiple": False,
        "cover": True,
    })
    platform.page = FakeUploadPage(chooser)
    platform._get_verified_body_image_button = AsyncMock(return_value=FakeButton())
    platform._editor_image_count = AsyncMock(return_value=0)
    result = run(platform._upload_image("photo.png"))
    assert result["error_code"] == "XHS_BODY_FILE_CHOOSER_UNVERIFIED"
    chooser.set_files.assert_not_awaited()


def _preflight_platform(*, resume_title: str = ""):
    platform = XiaohongshuPlatform(resume_existing_title=resume_title)
    platform.page = MagicMock()
    platform.page.is_closed.return_value = False
    platform.page.goto = AsyncMock()
    platform.page.wait_for_selector = AsyncMock()
    platform._draft_box_count = AsyncMock(return_value=10)
    platform._open_long_draft_drawer = AsyncMock()
    action = MagicMock()
    action.count = AsyncMock(return_value=1)
    action.click = AsyncMock()
    filtered = MagicMock()
    filtered.filter.return_value = action
    card = MagicMock()
    card.locator.return_value = filtered
    platform._matching_long_draft_cards = AsyncMock(return_value=[card])
    return platform, action


def test_same_title_still_fails_closed_without_explicit_resume() -> None:
    platform, _action = _preflight_platform()
    with pytest.raises(DraftBaselineError, match="禁止自动重复创建"):
        run(platform.preflight_delivery("唯一标题"))
    assert platform._editing_existing_draft is False


def test_explicit_exact_resume_opens_only_unique_existing_draft() -> None:
    platform, action = _preflight_platform(resume_title="唯一标题")
    run(platform.preflight_delivery("  唯一标题  "))
    action.click.assert_awaited_once()
    assert platform._editing_existing_draft is True
    assert platform._draft_box_count_before == 10
