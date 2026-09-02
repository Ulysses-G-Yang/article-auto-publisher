from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import platforms.toutiao as toutiao_module
from platforms.base import PlatformAutomationError
from platforms.toutiao import ToutiaoPlatform


def run(coroutine):
    return asyncio.run(coroutine)


class _FakePage:
    def __init__(self, *, webdriver: bool = False) -> None:
        self.url = "about:blank"
        self.webdriver = webdriver
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    async def evaluate(self, script: str) -> bool:
        assert script == "() => navigator.webdriver === true"
        return self.webdriver


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.pages = [page]
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self.contexts = [context]
        self.closed = False
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.browser = browser
        self.endpoints: list[str] = []

    async def connect_over_cdp(self, endpoint: str, *, timeout: int):
        assert timeout == 2000
        self.endpoints.append(endpoint)
        return self.browser


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.chromium = _FakeChromium(browser)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _FakeStarter:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> _FakePlaywright:
        return self.playwright


class _FakeProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _runtime(monkeypatch, tmp_path: Path, *, webdriver: bool = False):
    chrome = tmp_path / "chrome.exe"
    chrome.touch()
    page = _FakePage(webdriver=webdriver)
    browser = _FakeBrowser(_FakeContext(page))
    playwright = _FakePlaywright(browser)
    process = _FakeProcess()
    launch_calls: list[tuple] = []

    async def fake_launch(*args, **kwargs):
        launch_calls.append((args, kwargs))
        return process

    monkeypatch.setattr(toutiao_module, "_find_system_chrome", lambda: chrome)
    monkeypatch.setattr(toutiao_module, "async_playwright", lambda: _FakeStarter(playwright))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_launch)
    platform = ToutiaoPlatform(profile_dir=tmp_path, strict_profile_lock=True)
    platform._read_cdp_listener_pid = AsyncMock(return_value=process.pid)
    return platform, browser, playwright, process, launch_calls


def test_initialize_uses_owned_normal_chrome_cdp(monkeypatch, tmp_path: Path) -> None:
    platform, browser, playwright, process, launch_calls = _runtime(
        monkeypatch,
        tmp_path,
    )

    run(platform.initialize())

    assert platform.page.url == "about:blank"
    assert platform.browser is browser
    assert len(launch_calls) == 1
    args, kwargs = launch_calls[0]
    assert args[0].endswith("chrome.exe")
    debug_arg = next(arg for arg in args if arg.startswith("--remote-debugging-port="))
    assert re.fullmatch(r"--remote-debugging-port=[1-9]\d*", debug_arg)
    assert "--remote-debugging-address=127.0.0.1" in args
    assert not any("enable-automation" in arg for arg in args)
    assert not any("headless" in arg for arg in args)
    assert args[-1] == "about:blank"
    assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
    assert set(platform.page.handlers) == {"close", "crash"}
    assert set(platform.context.handlers) == {"close"}
    assert set(browser.handlers) == {"disconnected"}

    platform._lifecycle_stage = "正文写入"
    platform.page.handlers["close"](platform.page)
    assert platform._lifecycle_events == [
        {
            "event": "page_close",
            "stage": "正文写入",
            "native_process_returncode": None,
            "expected_cleanup": False,
        }
    ]

    run(platform.cleanup())

    assert browser.closed is True
    assert playwright.stopped is True
    assert process.returncode == 0
    assert process.terminated is False
    assert process.killed is False


def test_toutiao_skips_generic_pre_save_pointer_actions(tmp_path: Path) -> None:
    platform = ToutiaoPlatform(profile_dir=tmp_path, strict_profile_lock=True)
    platform.page = MagicMock()
    platform.page.is_closed.return_value = False
    platform.context = MagicMock()
    platform.context.pages = [platform.page]

    run(platform._safe_simulate_scroll(stage="发布前滚动检查"))
    run(platform._safe_random_mouse_movement(stage="发布前鼠标检查"))

    platform.page.mouse.wheel.assert_not_called()
    platform.page.mouse.move.assert_not_called()


def test_initialize_rejects_webdriver_exposure(monkeypatch, tmp_path: Path) -> None:
    platform, browser, playwright, process, _calls = _runtime(
        monkeypatch,
        tmp_path,
        webdriver=True,
    )

    with pytest.raises(PlatformAutomationError, match="TOUTIAO_CDP_START_FAILED"):
        run(platform.initialize())

    assert browser.closed is True
    assert playwright.stopped is True
    assert process.returncode == 0


def test_owner_mismatch_never_closes_connected_browser(
    monkeypatch,
    tmp_path: Path,
) -> None:
    platform, browser, playwright, process, _calls = _runtime(
        monkeypatch,
        tmp_path,
    )
    platform._read_cdp_listener_pid = AsyncMock(return_value=process.pid + 1)

    with pytest.raises(PlatformAutomationError, match="TOUTIAO_CDP_START_FAILED"):
        run(platform.initialize())

    assert browser.closed is False
    assert playwright.stopped is True
    assert process.returncode == 0


def test_profile_lock_blocks_before_browser_start(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "SingletonLock").touch()
    starter = AsyncMock()
    monkeypatch.setattr(toutiao_module, "async_playwright", starter)
    platform = ToutiaoPlatform(profile_dir=tmp_path, strict_profile_lock=True)

    with pytest.raises(PlatformAutomationError, match="PROFILE_IN_USE"):
        run(platform.initialize())

    starter.assert_not_called()
