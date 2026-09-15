from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from human.simulator import HumanSimulator
from platforms.base import BasePlatform
from platforms.xiaohongshu import XiaohongshuPlatform


def run(coroutine):
    return asyncio.run(coroutine)


class _ConcretePlatform(BasePlatform):
    platform_name = "xiaoheihe"

    async def check_login(self):
        return False

    async def login(self):
        return None

    async def navigate_to_editor(self):
        return None

    async def fill_title(self, _title):
        return None

    async def fill_content(self, _blocks, _images):
        return None

    async def select_topic(
        self,
        topic="",
        community="",
        selection_query="",
        selection_override=None,
    ):
        return None

    async def save_draft(self, _title=""):
        return ""


class _FakeContext:
    def __init__(self) -> None:
        self.pages = [SimpleNamespace()]
        self.add_init_script = AsyncMock()
        self.close = AsyncMock()


class _FakeChromium:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.calls: list[dict] = []

    async def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        return self.context


class _FakePlaywright:
    def __init__(self, context: _FakeContext) -> None:
        self.chromium = _FakeChromium(context)
        self.stop = AsyncMock()


class _FakeStarter:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self):
        return self.playwright


def _initialize(platform, tmp_path):
    profile = tmp_path / "isolated-profile"
    profile.mkdir()
    context = _FakeContext()
    playwright = _FakePlaywright(context)
    starter = _FakeStarter(playwright)
    with patch("platforms.base.async_playwright", return_value=starter):
        run(platform.initialize())
    return playwright, context


def test_base_initialize_keeps_fixed_viewport_for_other_platforms(tmp_path) -> None:
    platform = _ConcretePlatform(
        profile_dir=tmp_path / "isolated-profile",
        strict_profile_lock=True,
    )
    playwright, context = _initialize(platform, tmp_path)

    kwargs = playwright.chromium.calls[0]
    assert kwargs["headless"] is False
    assert kwargs["viewport"] == {"width": 1366, "height": 900}
    assert "no_viewport" not in kwargs
    assert kwargs["channel"] == "chrome"
    assert kwargs["user_data_dir"] == str((tmp_path / "isolated-profile").resolve())
    assert kwargs["args"] == [
        "--no-first-run", "--no-default-browser-check", "--no-proxy-server",
    ]
    context.add_init_script.assert_not_awaited()


def test_xiaohongshu_initialize_uses_native_window_viewport(tmp_path) -> None:
    platform = XiaohongshuPlatform(
        profile_dir=tmp_path / "isolated-profile",
        strict_profile_lock=True,
    )
    playwright, _context = _initialize(platform, tmp_path)

    kwargs = playwright.chromium.calls[0]
    assert kwargs["headless"] is False
    assert kwargs["no_viewport"] is True
    assert "viewport" not in kwargs


def test_mouse_movement_reads_live_viewport_when_native_context_has_none() -> None:
    page = SimpleNamespace(
        viewport_size=None,
        evaluate=AsyncMock(return_value={"width": 80, "height": 150}),
    )
    simulator = HumanSimulator(config={"delay": {"min": 0, "max": 0}})
    simulator.move_mouse_to = AsyncMock()
    simulator.random_delay = AsyncMock()

    run(simulator.random_mouse_movement(page, count=1))

    page.evaluate.assert_awaited_once_with(
        "() => ({width: window.innerWidth, height: window.innerHeight})"
    )
    _, x, y = simulator.move_mouse_to.await_args.args[:3]
    assert 0 <= x < 80
    assert 0 <= y < 150


def test_mouse_movement_does_not_create_invalid_range_for_tiny_viewport() -> None:
    page = SimpleNamespace(viewport_size={"width": 1, "height": 1})
    simulator = HumanSimulator(config={"delay": {"min": 0, "max": 0}})
    simulator.move_mouse_to = AsyncMock()
    simulator.random_delay = AsyncMock()

    run(simulator.random_mouse_movement(page, count=1))

    _, x, y = simulator.move_mouse_to.await_args.args[:3]
    assert (x, y) == (0, 0)
