from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from platforms.smzdm import SmzdmPlatform
from platforms.weibo import WeiboPlatform


class FakeFileInput:
    def __init__(self, accept: str, error: Exception | None = None) -> None:
        self.accept = accept
        self.set_input_files = AsyncMock(side_effect=error)

    async def get_attribute(self, name: str) -> str | None:
        return self.accept if name == "accept" else None


class FakeFileInputs:
    def __init__(self, inputs: list[FakeFileInput]) -> None:
        self.inputs = inputs

    async def count(self) -> int:
        return len(self.inputs)

    def nth(self, index: int) -> FakeFileInput:
        return self.inputs[index]

    @property
    def first(self):
        raise AssertionError("image input selection must never fall back to first")


class FakeUploadPage:
    def __init__(self, inputs: list[FakeFileInput], image_counts: list[int]) -> None:
        self.file_inputs = FakeFileInputs(inputs)
        self.image_counts = iter(image_counts)
        self.last_image_count = image_counts[-1] if image_counts else 0

    def locator(self, selector: str) -> FakeFileInputs:
        assert selector == "input[type=file]"
        return self.file_inputs

    async def evaluate(self, _script: str) -> int:
        try:
            self.last_image_count = next(self.image_counts)
        except StopIteration:
            pass
        return self.last_image_count


PLATFORMS = (
    (SmzdmPlatform, "platforms.smzdm", "SMZDM"),
    (WeiboPlatform, "platforms.weibo", "WEIBO"),
)


def run_upload(platform_class, module_name: str, inputs, image_counts):
    platform = platform_class()
    platform.page = FakeUploadPage(inputs, image_counts)
    with patch(f"{module_name}.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(platform._upload_image("photo.png"))
    return platform, result


@pytest.mark.parametrize(("platform_class", "module_name", "prefix"), PLATFORMS)
def test_video_or_cover_before_unique_body_image_is_selected(
    platform_class, module_name: str, prefix: str
) -> None:
    cover_or_video = FakeFileInput("video/*")
    body_image = FakeFileInput("image/png,image/jpeg")

    _platform, result = run_upload(
        platform_class,
        module_name,
        [cover_or_video, body_image],
        [0, 1, 1],
    )

    assert result["success"] is True
    cover_or_video.set_input_files.assert_not_awaited()
    body_image.set_input_files.assert_awaited_once_with("photo.png", timeout=15000)


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
    assert result["error_code"] == f"{prefix}_BODY_IMAGE_INPUT_NOT_FOUND"
    video_input.set_input_files.assert_not_awaited()


@pytest.mark.parametrize(("platform_class", "module_name", "prefix"), PLATFORMS)
def test_cover_image_first_and_body_image_later_are_ambiguous(
    platform_class, module_name: str, prefix: str
) -> None:
    first_image = FakeFileInput("image/*")
    second_image = FakeFileInput("image/png")

    _platform, result = run_upload(
        platform_class,
        module_name,
        [first_image, second_image],
        [0],
    )

    assert result["success"] is False
    assert result["error_code"] == f"{prefix}_BODY_IMAGE_INPUT_AMBIGUOUS"
    first_image.set_input_files.assert_not_awaited()
    second_image.set_input_files.assert_not_awaited()


@pytest.mark.parametrize(("platform_class", "module_name", "prefix"), PLATFORMS)
def test_image_count_not_increasing_is_failure(
    platform_class, module_name: str, prefix: str
) -> None:
    body_image = FakeFileInput("image/*")

    _platform, result = run_upload(
        platform_class,
        module_name,
        [body_image],
        [0] + [0] * 10,
    )

    assert result["success"] is False
    assert result["error_code"] == f"{prefix}_EDITOR_IMAGE_COUNT_UNCHANGED"
    body_image.set_input_files.assert_awaited_once()


@pytest.mark.parametrize(("platform_class", "module_name", "prefix"), PLATFORMS)
def test_upload_exception_redacts_physical_path(
    platform_class, module_name: str, prefix: str
) -> None:
    body_image = FakeFileInput(
        "image/*",
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
