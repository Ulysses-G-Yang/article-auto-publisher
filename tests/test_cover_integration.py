from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from platforms.baijiahao import BaijiahaoPlatform
from platforms.weibo import WeiboPlatform


def run(coroutine):
    return asyncio.run(coroutine)


class _InstantSimulator:
    async def random_delay(self, *_args, **_kwargs):
        return None


class _FakeFileInput:
    def __init__(self, count: int = 1) -> None:
        self._count = count
        self.set_files: list[str] = []

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self._count

    async def set_input_files(self, path: str, **_kwargs) -> None:
        self.set_files.append(path)


class _FakeLocator:
    def __init__(self, count: int = 1) -> None:
        self._count = count
        self._files: list[str] = []

    async def count(self) -> int:
        return self._count


class _FakeFileChooser:
    def __init__(self) -> None:
        self.files: list[str] = []

    async def set_files(self, path: str, **_kwargs) -> None:
        self.files.append(path)


class _FakePage:
    """evaluate 按调用顺序消费预设返回值。"""

    def __init__(self, evaluate_results: list) -> None:
        self._results = list(evaluate_results)
        self.evaluate_calls: list[str] = []
        self.file_input = _FakeFileInput()
        self.file_chooser = _FakeFileChooser()
        self._chooser_triggered = True

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, script: str, *_args) -> object:
        self.evaluate_calls.append(script[:120])
        if "querySelectorAll('input[type=file][accept" in script or "input[type=file]" in script:
            return None
        if self._results:
            return self._results.pop(0)
        return None

    def locator(self, selector: str):
        if "input[type=file]" in selector:
            return self.file_input
        return _FakeLocator()

    def expect_file_chooser(self, **_kwargs):
        class _Expect:
            def __init__(self, page):
                self.page = page

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            @property
            def value(self):
                async def _resolve():
                    return self.page.file_chooser

                return _resolve()

        return _Expect(self)


def _make_weibo(page: _FakePage) -> WeiboPlatform:
    platform = WeiboPlatform()
    platform.page = page
    platform.simulator = _InstantSimulator()
    return platform


def _make_baijiahao(page: _FakePage) -> BaijiahaoPlatform:
    platform = BaijiahaoPlatform()
    platform.page = page
    platform.simulator = _InstantSimulator()
    return platform


# ==================== 微博封面 ====================


def test_weibo_set_cover_success_flow() -> None:
    page = _FakePage(["clicked", "picked", "clicked", False])
    platform = _make_weibo(page)

    result = run(platform.set_cover())

    assert result["success"] is True
    assert len(page.evaluate_calls) == 4


def test_weibo_set_cover_fails_when_button_missing() -> None:
    page = _FakePage(["not-found"])
    platform = _make_weibo(page)

    result = run(platform.set_cover())

    assert result["success"] is False
    assert "按钮未找到" in result["error"]


def test_weibo_set_cover_fails_when_dialog_has_no_images() -> None:
    page = _FakePage(["clicked", "no-images"])
    platform = _make_weibo(page)

    result = run(platform.set_cover())

    assert result["success"] is False
    assert "未找到正文图片" in result["error"]


# ==================== 百家号封面 ====================


def test_baijiahao_set_cover_success_flow() -> None:
    page = _FakePage(["clicked", False])
    platform = _make_baijiahao(page)
    platform._content_blocks = [
        {"type": "text", "text": "正文"},
        {"type": "image", "local_path": r"C:\tmp\cover.png", "position": 1},
    ]

    result = run(platform.set_cover())

    assert result["success"] is True
    assert page.file_chooser.files == [r"C:\tmp\cover.png"]


def test_baijiahao_set_cover_fails_when_button_missing() -> None:
    page = _FakePage(["not-found"])
    platform = _make_baijiahao(page)
    platform._content_blocks = []

    result = run(platform.set_cover())

    assert result["success"] is False
    assert "按钮未找到" in result["error"]


def test_baijiahao_set_cover_fails_without_image_material() -> None:
    page = _FakePage(["clicked"])
    platform = _make_baijiahao(page)
    platform._content_blocks = [{"type": "text", "text": "只有文字"}]

    result = run(platform.set_cover())

    assert result["success"] is False
    assert "缺少本地图片素材" in result["error"]
