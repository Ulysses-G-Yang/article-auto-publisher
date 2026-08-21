from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from platforms.smzdm import SmzdmPlatform
from platforms.weibo import WeiboPlatform


class FakeFileInput:
    def __init__(
        self,
        accept: str,
        error: Exception | None = None,
        *,
        visible: bool = True,
    ) -> None:
        self.accept = accept
        self.visible = visible
        self.error = error
        self.page = None
        self.set_input_files = AsyncMock(side_effect=self._set_input_files)
        self.set_files = self.set_input_files

    async def _set_input_files(self, *_args, **_kwargs) -> None:
        if self.error is not None:
            raise self.error
        if self.page is not None:
            self.page.uploaded = True

    async def get_attribute(self, name: str) -> str | None:
        return self.accept if name == "accept" else None

    async def is_visible(self) -> bool:
        return self.visible


class FakeFileInputs:
    def __init__(self, inputs: list[FakeFileInput]) -> None:
        self.inputs = inputs

    async def count(self) -> int:
        return len(self.inputs)

    def nth(self, index: int) -> FakeFileInput:
        return self.inputs[index]

    @property
    def first(self):
        assert len(self.inputs) == 1
        return self.inputs[0]


class FakeInsertButton:
    def __init__(self, page) -> None:
        self.page = page
        self.click = AsyncMock(side_effect=self._click)

    async def _click(self, **_kwargs) -> None:
        self.page.dialog_visible = False

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return self.page.selected

    async def inner_text(self) -> str:
        return "插入"


class FakeItems:
    def __init__(self, items: list) -> None:
        self.items = items

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int):
        return self.items[index]


class FakeHiddenSpinner:
    first = None

    def __init__(self) -> None:
        self.first = self

    async def wait_for(self, *, state: str, timeout: int) -> None:
        assert (state, timeout) == ("hidden", 15000)


class FakeAlbumItem:
    def __init__(self, page) -> None:
        self.page = page
        self.click = AsyncMock(side_effect=self._click)

    async def _click(self, **_kwargs) -> None:
        self.page.selected = True

    async def evaluate(self, _script: str) -> dict:
        return {"failed": False, "ready": True}

    async def get_attribute(self, name: str) -> str:
        assert name == "class"
        return "image-item is-selected" if self.page.selected else "image-item"


class FakeAlbumItems:
    def __init__(self, page) -> None:
        self.page = page
        self.first = FakeAlbumItem(page)

    async def count(self) -> int:
        return 1 if self.page.uploaded else 0


class FakeSelectedAlbumItems:
    def __init__(self, page) -> None:
        self.page = page

    async def count(self) -> int:
        return 1 if self.page.selected else 0


class FakeImageDialog:
    def __init__(self, page, inputs: list[FakeFileInput]) -> None:
        self.page = page
        self.inputs = FakeFileInputs(inputs)
        self.items = FakeAlbumItems(page)
        self.selected_items = FakeSelectedAlbumItems(page)
        self.insert = FakeInsertButton(page)

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self) -> str:
        return "图片库 上传 手机传图 插入"

    def locator(self, selector: str):
        if selector == ".n-spin-body":
            return FakeHiddenSpinner()
        if selector == "input[type=file]":
            return self.inputs
        if selector == ".image-list .image-item":
            return self.items
        if selector == ".image-list .image-item.is-selected":
            return self.selected_items
        if selector == "button":
            return FakeItems([self.insert])
        raise AssertionError(f"unexpected dialog selector: {selector}")


class FakeDialogs:
    def __init__(self, page) -> None:
        self.page = page

    async def count(self) -> int:
        return 1 if self.page.dialog_visible else 0

    def nth(self, index: int):
        assert index == 0
        return self.page.dialog


class FakeUploadPage:
    def __init__(self, inputs: list[FakeFileInput], image_counts: list[int]) -> None:
        self.dialog_visible = True
        self.uploaded = False
        self.selected = False
        image_inputs = [
            item
            for item in inputs
            if any(
                suffix in item.accept.lower()
                for suffix in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic")
            )
        ]
        for item in image_inputs:
            item.page = self
        self.dialog = FakeImageDialog(self, image_inputs)
        self.dialogs = FakeDialogs(self)
        self.image_counts = iter(image_counts)
        self.last_image_count = image_counts[-1] if image_counts else 0
        self.trigger = type("Trigger", (), {"click": AsyncMock()})()

    def locator(self, selector: str):
        if selector == ".n-dialog:visible":
            return self.dialogs
        raise AssertionError(f"unexpected page selector: {selector}")

    async def evaluate(self, _script: str) -> dict:
        try:
            self.last_image_count = next(self.image_counts)
        except StopIteration:
            pass
        return {
            "semantic_count": self.last_image_count,
            "unsupported_count": 0,
        }


PLATFORMS = (
    (WeiboPlatform, "platforms.weibo", "WEIBO"),
)


def run_upload(platform_class, module_name: str, inputs, image_counts):
    platform = platform_class()
    platform.page = FakeUploadPage(inputs, image_counts)
    platform._find_body_image_trigger = AsyncMock(return_value=platform.page.trigger)
    platform._semantic_editor_image_count = AsyncMock(side_effect=image_counts)
    with patch(f"{module_name}.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(platform._upload_image("photo.png"))
    return platform, result


@pytest.mark.parametrize(("platform_class", "module_name", "prefix"), PLATFORMS)
def test_video_or_cover_before_unique_body_image_is_selected(
    platform_class, module_name: str, prefix: str
) -> None:
    cover_or_video = FakeFileInput("video/*")
    body_image = FakeFileInput(".jpg,.jpeg,.bmp,.gif,.png,.heic")

    platform, result = run_upload(
        platform_class,
        module_name,
        [cover_or_video, body_image],
        [0, 1, 1],
    )

    assert result["success"] is True
    cover_or_video.set_input_files.assert_not_awaited()
    body_image.set_input_files.assert_awaited_once_with("photo.png", timeout=15000)
    platform.page.trigger.click.assert_awaited_once_with(timeout=5000)
    platform.page.dialog.insert.click.assert_awaited_once_with(timeout=5000)


@pytest.mark.parametrize(("platform_class", "module_name", "prefix"), PLATFORMS)
def test_missing_image_candidate_fails_closed(
    platform_class, module_name: str, prefix: str
) -> None:
    video_input = FakeFileInput("video/*")

    _platform, result = run_upload(
        platform_class,
        module_name,
        [video_input],
        [0],
    )

    assert result["success"] is False
    assert result["error_code"] == f"{prefix}_BODY_IMAGE_INPUT_NOT_UNIQUE"
    video_input.set_input_files.assert_not_awaited()


@pytest.mark.parametrize(("platform_class", "module_name", "prefix"), PLATFORMS)
def test_cover_image_first_and_body_image_later_are_ambiguous(
    platform_class, module_name: str, prefix: str
) -> None:
    first_image = FakeFileInput(".jpg,.jpeg,.png")
    second_image = FakeFileInput(".bmp,.gif,.heic")

    _platform, result = run_upload(
        platform_class,
        module_name,
        [first_image, second_image],
        [0],
    )

    assert result["success"] is False
    assert result["error_code"] == f"{prefix}_BODY_IMAGE_INPUT_NOT_UNIQUE"
    first_image.set_input_files.assert_not_awaited()
    second_image.set_input_files.assert_not_awaited()


@pytest.mark.parametrize(("platform_class", "module_name", "prefix"), PLATFORMS)
def test_image_count_not_increasing_is_failure(
    platform_class, module_name: str, prefix: str
) -> None:
    body_image = FakeFileInput(".jpg,.jpeg,.bmp,.gif,.png,.heic")

    _platform, result = run_upload(
        platform_class,
        module_name,
        [body_image],
        [0] + [0] * 10,
    )

    assert result["success"] is False
    assert result["error_code"] == (
        f"{prefix}_EDITOR_SEMANTIC_IMAGE_COUNT_UNCHANGED"
    )
    body_image.set_input_files.assert_awaited_once()


@pytest.mark.parametrize(("platform_class", "module_name", "prefix"), PLATFORMS)
def test_upload_exception_redacts_physical_path(
    platform_class, module_name: str, prefix: str
) -> None:
    body_image = FakeFileInput(
        ".jpg,.jpeg,.bmp,.gif,.png,.heic",
        RuntimeError(
            r"set_input_files failed for D:\Secret Folder\a.png "
            "token=abcdefghijklmnopqrstuv123456"
        ),
    )

    _platform, result = run_upload(
        platform_class,
        module_name,
        [body_image],
        [0],
    )

    serialized = repr(result)
    assert result["success"] is False
    assert result["error_code"] == f"{prefix}_IMAGE_UPLOAD_FAILED"
    assert r"D:\Secret Folder" not in serialized
    assert "abcdefghijklmnopqrstuv123456" not in serialized


class FakeSmzdmTrigger:
    def __init__(self, *, present: bool = True, visible: bool = True) -> None:
        self.present = present
        self.visible = visible
        self.click = AsyncMock()

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return int(self.present)

    async def is_visible(self) -> bool:
        return self.visible


class FakeSmzdmImageNodes:
    def __init__(self, counts: list[int]) -> None:
        self.counts = iter(counts)
        self.last = counts[-1] if counts else 0

    async def count(self) -> int:
        try:
            self.last = next(self.counts)
        except StopIteration:
            pass
        return self.last


class FakeSmzdmKeyboard:
    def __init__(self) -> None:
        self.press = AsyncMock()


class FakeSmzdmInsertButton:
    def __init__(self, inputs: list[FakeFileInput]) -> None:
        self.inputs = inputs
        self.click = AsyncMock(side_effect=self._close_panel)

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def _close_panel(self, **_kwargs) -> None:
        for item in self.inputs:
            item.visible = False


class FakeSmzdmUploadPage:
    def __init__(
        self,
        *,
        inputs: list[FakeFileInput],
        image_counts: list[int],
        trigger_present: bool = True,
    ) -> None:
        self.trigger = FakeSmzdmTrigger(present=trigger_present)
        self.inputs = FakeFileInputs(inputs)
        self.insert_button = FakeSmzdmInsertButton(inputs)
        self.images = FakeSmzdmImageNodes(image_counts)
        self.keyboard = FakeSmzdmKeyboard()

    def locator(self, selector: str):
        if selector == ".right-menu-bar:has(svg.zicon-picture)":
            return self.trigger
        if selector == 'input[type="file"][accept*="image"]':
            return self.inputs
        if selector == '.btn-item:has-text("插入正文")':
            return self.insert_button
        if selector == "div.ProseMirror img":
            return self.images
        raise AssertionError(f"unexpected selector: {selector}")

    async def evaluate(self, _script: str, *_args) -> bool:
        return True


def run_smzdm_upload(
    inputs: list[FakeFileInput],
    image_counts: list[int],
    *,
    trigger_present: bool = True,
) -> tuple[SmzdmPlatform, dict]:
    platform = SmzdmPlatform()
    platform.page = FakeSmzdmUploadPage(
        inputs=inputs,
        image_counts=image_counts,
        trigger_present=trigger_present,
    )
    platform.simulator.random_delay = AsyncMock()
    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(platform._upload_image("photo.png"))
    return platform, result


def test_smzdm_opens_real_picture_control_before_uploading() -> None:
    body_image = FakeFileInput("image/gif, image/png, image/jpeg")

    platform, result = run_smzdm_upload([body_image], [0, 1, 1])

    assert result["success"] is True
    platform.page.trigger.click.assert_awaited_once()
    body_image.set_input_files.assert_awaited_once_with("photo.png", timeout=15000)
    platform.page.insert_button.click.assert_awaited_once_with(timeout=5000)


def test_smzdm_missing_picture_trigger_fails_closed() -> None:
    body_image = FakeFileInput("image/png")

    _platform, result = run_smzdm_upload(
        [body_image],
        [0],
        trigger_present=False,
    )

    assert result["success"] is False
    assert result["error_code"] == "SMZDM_BODY_IMAGE_TRIGGER_NOT_FOUND"
    body_image.set_input_files.assert_not_awaited()


def test_smzdm_ambiguous_visible_body_inputs_fail_closed() -> None:
    first_image = FakeFileInput("image/png")
    second_image = FakeFileInput("image/jpeg")

    _platform, result = run_smzdm_upload([first_image, second_image], [0])

    assert result["success"] is False
    assert result["error_code"] == "SMZDM_BODY_IMAGE_INPUT_AMBIGUOUS"
    first_image.set_input_files.assert_not_awaited()
    second_image.set_input_files.assert_not_awaited()


def test_smzdm_image_count_must_stably_increase() -> None:
    body_image = FakeFileInput("image/png")

    _platform, result = run_smzdm_upload([body_image], [0] + [0] * 12)

    assert result["success"] is False
    assert result["error_code"] == "SMZDM_EDITOR_IMAGE_COUNT_UNCHANGED"
    body_image.set_input_files.assert_awaited_once()


def test_smzdm_upload_error_redacts_physical_path() -> None:
    body_image = FakeFileInput(
        "image/png",
        RuntimeError(
            r"set_input_files failed for D:\Secret Folder\a.png "
            "token=abcdefghijklmnopqrstuv123456"
        ),
    )

    _platform, result = run_smzdm_upload([body_image], [0])

    serialized = repr(result)
    assert result["success"] is False
    assert result["error_code"] == "SMZDM_IMAGE_UPLOAD_FAILED"
    assert r"D:\Secret Folder" not in serialized
    assert "abcdefghijklmnopqrstuv123456" not in serialized
