from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from platforms.base import BasePlatform
from platforms.media_progress import safe_media_progress


def progress(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "expected_images": 2,
        "uploaded_images": 1,
        "failed_image_count": 0,
        "media_status": "in_progress",
    }
    value.update(overrides)
    return value


def test_media_progress_projection_drops_extra_fields() -> None:
    projected = safe_media_progress(
        progress(
            path="D:\\secret\\image.png",
            token="secret-token",
            body="用户正文不应进入审计字段",
        )
    )

    assert projected == {
        "expected_images": 2,
        "uploaded_images": 1,
        "failed_image_count": 0,
        "media_status": "in_progress",
    }


def test_base_publish_exposes_only_projected_media_progress() -> None:
    class FakeDatabase:
        def add_task_log(self, *_args: object) -> None:
            return None

    class FailingPlatform(BasePlatform):
        platform_name = "test"

        async def check_login(self) -> bool:
            return True

        async def login(self) -> None:
            return None

        async def navigate_to_editor(self) -> None:
            return None

        async def fill_title(self, _title: str) -> None:
            return None

        async def fill_content(self, _content_blocks: list, _images: list) -> dict:
            error = RuntimeError("模拟正文校验失败")
            error.media_progress = progress(
                path="D:\\secret\\image.png",
                token="secret-token",
                body="用户正文不应进入失败结果",
            )
            raise error

        async def select_topic(self, **_kwargs: object) -> dict:
            return {"success": True}

        async def save_draft(self, _title: str = "") -> str:
            return ""

    platform = FailingPlatform()
    platform.simulator.random_delay = AsyncMock()
    result = asyncio.run(
        platform.publish(
            title="测试",
            content_blocks=[{"type": "text", "text": "正文"}],
            images=[],
            db=FakeDatabase(),
            auto_login=False,
            delivery_mode="DRAFT",
        )
    )

    assert result["success"] is False
    assert result["media_progress"] == {
        "expected_images": 2,
        "uploaded_images": 1,
        "failed_image_count": 0,
        "media_status": "in_progress",
    }
    assert set(result["media_progress"]) == {
        "expected_images",
        "uploaded_images",
        "failed_image_count",
        "media_status",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_images": True},
        {"uploaded_images": False},
        {"failed_image_count": -1},
        {"expected_images": 10001},
        {"uploaded_images": 3},
        {"failed_image_count": 3},
        {"uploaded_images": 2, "failed_image_count": 1},
        {"media_status": "partial"},
        {"media_status": "completed"},
    ],
)
def test_media_progress_projection_rejects_invalid_counts_and_status(
    overrides: dict[str, object],
) -> None:
    assert safe_media_progress(progress(**overrides)) is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "expected_images": 0,
            "uploaded_images": 0,
            "failed_image_count": 0,
            "media_status": "not_required",
        },
        {
            "expected_images": 2,
            "uploaded_images": 2,
            "failed_image_count": 0,
            "media_status": "completed",
        },
        {
            "expected_images": 2,
            "uploaded_images": 0,
            "failed_image_count": 2,
            "media_status": "failed",
        },
        {
            "expected_images": 2,
            "uploaded_images": 1,
            "failed_image_count": 1,
            "media_status": "partial",
        },
    ],
)
def test_media_progress_projection_accepts_only_derived_status(
    payload: dict[str, object],
) -> None:
    assert safe_media_progress(payload) == payload
