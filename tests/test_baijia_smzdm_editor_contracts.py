from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from platforms.baijiahao import BaijiahaoPlatform
from platforms.base import BrowserLifecycleError, SelectorError
from platforms.content_validation import ContentValidationError
from platforms.smzdm import SmzdmPlatform


class FakeLocator:
    def __init__(
        self,
        *,
        visible: bool = True,
        editable: bool = True,
        text: str = "",
        visible_sequence: list[bool] | None = None,
    ) -> None:
        self.visible = visible
        self.editable = editable
        self.text = text
        self.visible_sequence = list(visible_sequence or [])
        self.inner_text = AsyncMock(return_value=text)

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        if self.visible_sequence:
            return self.visible_sequence.pop(0)
        return self.visible

    async def is_editable(self) -> bool:
        return self.editable

    def nth(self, _index: int):
        return self

    async def click(self, **_kwargs) -> None:
        return None

    async def evaluate(self, _script: str):
        return True


class FakeLocatorCollection:
    def __init__(self, locator: FakeLocator) -> None:
        self.locator = locator

    async def count(self) -> int:
        return 1

    def nth(self, _index: int) -> FakeLocator:
        return self.locator

    @property
    def first(self) -> FakeLocator:
        return self.locator


class FakeFrame:
    def __init__(self, body: FakeLocator, *, browser_closed: bool = False) -> None:
        self.body = body
        self.browser_closed = browser_closed

    async def evaluate(self, _script: str) -> bool:
        if self.browser_closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return True

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "body"
        return self.body


class FakeBaijiaPage:
    def __init__(self, title: FakeLocator, frames: list[FakeFrame]) -> None:
        self.title = title
        self.frames = frames
        self.goto = AsyncMock()

    def locator(self, selector: str) -> FakeLocatorCollection:
        assert "FeEditorApp" in selector
        return FakeLocatorCollection(self.title)


def test_baijia_ready_waits_for_visible_editable_body_after_title() -> None:
    title = FakeLocator()
    body = FakeLocator(visible_sequence=[False, True])
    platform = BaijiahaoPlatform()
    platform.page = FakeBaijiaPage(title, [FakeFrame(body)])

    with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
        ready_title, ready_body = asyncio.run(
            platform._wait_for_editor_ready(timeout_seconds=0.05)
        )

    assert ready_title is title
    assert ready_body is body


def test_baijia_ready_times_out_when_body_never_becomes_visible() -> None:
    platform = BaijiahaoPlatform()
    platform.page = FakeBaijiaPage(
        FakeLocator(),
        [FakeFrame(FakeLocator(visible=False))],
    )

    with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(SelectorError, match="0.001 秒内未就绪"):
            asyncio.run(platform._wait_for_editor_ready(timeout_seconds=0.001))


def test_baijia_body_locator_does_not_swallow_browser_closed() -> None:
    platform = BaijiahaoPlatform()
    platform.page = FakeBaijiaPage(
        FakeLocator(),
        [FakeFrame(FakeLocator(), browser_closed=True)],
    )

    with pytest.raises(BrowserLifecycleError):
        asyncio.run(platform._body_editor_locator())


class FakeKeyboard:
    async def press(self, _key: str) -> None:
        return None

    async def insert_text(self, _text: str) -> None:
        return None


def test_baijia_image_rerender_reads_new_body_and_fails_on_missing_text() -> None:
    old_body = FakeLocator(text="第一段\n第二段")
    new_body = FakeLocator(text="第一段")
    platform = BaijiahaoPlatform()
    platform.page = SimpleNamespace(keyboard=FakeKeyboard())
    platform.simulator.random_delay = AsyncMock()
    platform._body_editor_locator = AsyncMock(side_effect=[old_body, new_body])
    platform._focus_editor = AsyncMock()
    platform._upload_image = AsyncMock(return_value={"success": True})

    with pytest.raises(ContentValidationError):
        asyncio.run(
            platform.fill_content(
                [
                    {"type": "text", "text": "第一段\n第二段"},
                    {"type": "image", "position": 1, "local_path": "photo.png"},
                ],
                [],
            )
        )

    assert platform._body_editor_locator.await_count == 2
    new_body.inner_text.assert_awaited_once()


class FakeSaveResponse:
    def __init__(self, status: int, url: str = "https://post.smzdm.com/api/draft/save") -> None:
        self.status = status
        self.url = url
        self.request = SimpleNamespace(method="POST")

    async def json(self) -> dict:
        return {}


class FakeSaveKeyboard:
    def __init__(self, page) -> None:
        self.page = page

    async def press(self, _key: str) -> None:
        return None

    async def type(self, _text: str, **_kwargs) -> None:
        if self.page.response is not None and self.page.response_callback:
            await self.page.response_callback(self.page.response)


class FakeSavePage:
    def __init__(self, response: FakeSaveResponse | None, title_present: bool) -> None:
        self.response = response
        self.title_present = title_present
        self.response_callback = None
        self.keyboard = FakeSaveKeyboard(self)
        self.goto = AsyncMock()

    def on(self, event: str, callback) -> None:
        assert event == "response"
        self.response_callback = callback

    def remove_listener(self, event: str, _callback) -> None:
        assert event == "response"
        self.response_callback = None

    async def evaluate(self, _script: str, *args):
        return self.title_present if args else True


def run_save(response: FakeSaveResponse | None, title_present: bool) -> tuple[SmzdmPlatform, str]:
    platform = SmzdmPlatform()
    platform.page = FakeSavePage(response, title_present)
    platform.simulator.random_delay = AsyncMock()
    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(platform.save_draft("同名草稿"))
    return platform, result


def test_smzdm_old_same_title_without_new_save_response_fails() -> None:
    platform, result = run_save(None, title_present=True)

    assert result == ""
    platform.page.goto.assert_not_awaited()


def test_smzdm_requires_current_2xx_response_and_title() -> None:
    platform, result = run_save(FakeSaveResponse(204), title_present=True)

    assert result == "https://post.smzdm.com/tougao/"
    platform.page.goto.assert_awaited_once()


def test_smzdm_non_2xx_save_response_fails_even_with_title() -> None:
    platform, result = run_save(FakeSaveResponse(500), title_present=True)

    assert result == ""
    platform.page.goto.assert_not_awaited()
