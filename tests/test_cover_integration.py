from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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


class _RoleInput:
    def __init__(self, role: str) -> None:
        self.role = role

    async def evaluate(self, _script: str) -> str:
        return self.role


class _RoleInputs:
    def __init__(self, *items: _RoleInput) -> None:
        self.items = items

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> _RoleInput:
        return self.items[index]


class _RoleModal:
    def __init__(self, inputs: _RoleInputs) -> None:
        self.inputs = inputs

    def locator(self, _selector: str) -> _RoleInputs:
        return self.inputs


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
    page = _FakePage([])
    platform = _make_baijiahao(page)
    trigger = AsyncMock()
    modal = Mock()
    platform._unique_visible_text = AsyncMock(return_value=trigger)
    platform._wait_for_cover_modal = AsyncMock(return_value=modal)
    platform._wait_for_unique_cover_input = AsyncMock(
        return_value=page.file_input.first
    )
    platform._cover_preview_state = AsyncMock(
        return_value={"selected_files": 0, "visual_count": 0, "visual_hash": 0}
    )
    platform._wait_for_cover_preview = AsyncMock(return_value=True)
    platform._confirm_cover_dialogs = AsyncMock(return_value=True)

    result = run(platform.set_cover(r"C:\tmp\cover.png"))

    assert result["success"] is True
    assert result["cover_status"] == "completed"
    assert result["safe_to_continue"] is True
    trigger.click.assert_awaited_once()
    assert page.file_input.set_files == [r"C:\tmp\cover.png"]


def test_baijiahao_set_cover_fails_when_button_missing() -> None:
    page = _FakePage([])
    platform = _make_baijiahao(page)
    platform._unique_visible_text = AsyncMock(return_value=None)
    platform._dismiss_cover_dialogs = AsyncMock(return_value=True)

    result = run(platform.set_cover(r"C:\tmp\cover.png"))

    assert result["success"] is False
    assert result["error_code"] == "BAIJIAHAO_COVER_TRIGGER_NOT_FOUND"
    assert "按钮未找到" in result["error"]


def test_baijiahao_cover_chooses_local_upload_over_body_cropper() -> None:
    platform = _make_baijiahao(_FakePage([]))
    cropper = _RoleInput("BODY_CROPPER")
    local_upload = _RoleInput("LOCAL_UPLOAD")
    modal = _RoleModal(_RoleInputs(cropper, local_upload))

    result = run(
        platform._wait_for_unique_cover_input(modal, timeout_seconds=0.1)
    )

    assert result is local_upload


def test_baijiahao_cover_preview_failure_closes_modal_before_continuing() -> None:
    page = _FakePage([])
    platform = _make_baijiahao(page)
    trigger = AsyncMock()
    modal = Mock()
    platform._unique_visible_text = AsyncMock(return_value=trigger)
    platform._wait_for_cover_modal = AsyncMock(return_value=modal)
    platform._wait_for_unique_cover_input = AsyncMock(
        return_value=page.file_input.first
    )
    platform._cover_preview_state = AsyncMock(return_value={})
    platform._wait_for_cover_preview = AsyncMock(return_value=False)
    platform._dismiss_cover_dialogs = AsyncMock(return_value=True)

    result = run(platform.set_cover(r"D:\Secret Folder\cover.png"))

    assert result["success"] is False
    assert result["safe_to_continue"] is True
    assert result["error_code"] == "BAIJIAHAO_COVER_PREVIEW_NOT_READY"
    assert "D:\\Secret Folder" not in str(result)
    platform._dismiss_cover_dialogs.assert_awaited_once()


def test_baijiahao_cover_stuck_is_not_safe_to_continue() -> None:
    platform = _make_baijiahao(_FakePage([]))
    platform._dismiss_cover_dialogs = AsyncMock(return_value=False)

    result = run(platform._cover_failure("UPLOAD_FAILED", "上传失败"))

    assert result["safe_to_continue"] is False
    assert result["cover_status"] == "unverified"
    assert result["error_code"] == "BAIJIAHAO_COVER_DIALOG_STUCK"


def test_base_pipeline_stops_before_save_when_cover_modal_cannot_close() -> None:
    class _Log:
        def add_task_log(self, *_args) -> None:
            return None

    platform = _make_baijiahao(_FakePage([]))
    platform.check_login = AsyncMock(return_value=True)
    platform.preflight_delivery = AsyncMock()
    platform.navigate_to_editor = AsyncMock()
    platform.fill_title = AsyncMock()
    platform.fill_content = AsyncMock(
        return_value={
            "text_ok": True,
            "media_status": "completed",
            "expected_images": 1,
            "uploaded_images": 1,
            "failed_images": [],
        }
    )
    platform.apply_cover = AsyncMock(
        return_value={
            "success": False,
            "cover_status": "unverified",
            "safe_to_continue": False,
            "error_code": "BAIJIAHAO_COVER_DIALOG_STUCK",
            "error": "封面弹窗无法安全关闭",
        }
    )
    platform.save_draft = AsyncMock()

    with (
        patch.object(platform, "_safe_simulate_scroll", new=AsyncMock()),
        patch.object(platform, "_safe_random_mouse_movement", new=AsyncMock()),
    ):
        result = run(
            platform.publish(
                title="不可点穿遮罩",
                content_blocks=[{"type": "text", "text": "正文", "position": 0}],
                images=[],
                cover={"strategy": "FIRST_BODY_IMAGE"},
                delivery_mode="DRAFT",
                auto_login=False,
                task_id=0,
                db=_Log(),
            )
        )

    assert result["success"] is False
    assert result["error_code"] == "BAIJIAHAO_COVER_DIALOG_STUCK"
    platform.save_draft.assert_not_awaited()


def test_baijiahao_apply_cover_uses_exact_frozen_asset(tmp_path: Path) -> None:
    page = _FakePage([])
    platform = _make_baijiahao(page)
    cover_path = tmp_path / "cover.png"
    cover_path.write_bytes(b"cover")
    platform.set_cover = AsyncMock(
        return_value={"success": True, "cover_status": "completed"}
    )

    result = run(
        platform.apply_cover(
            {
                "strategy": "EXPLICIT",
                "asset_id": "frozen-cover",
                "local_path": str(cover_path),
            }
        )
    )

    assert result["cover_status"] == "completed"
    platform.set_cover.assert_awaited_once_with(str(cover_path))


def test_baijiahao_apply_cover_fails_closed_when_asset_is_missing(tmp_path: Path) -> None:
    platform = _make_baijiahao(_FakePage([]))
    platform.set_cover = AsyncMock()

    result = run(
        platform.apply_cover(
            {
                "strategy": "FIRST_BODY_IMAGE",
                "asset_id": "missing-cover",
                "local_path": str(tmp_path / "missing.png"),
            }
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "BAIJIAHAO_COVER_ASSET_UNAVAILABLE"
    platform.set_cover.assert_not_awaited()


def test_baijiahao_apply_cover_none_is_not_required() -> None:
    platform = _make_baijiahao(_FakePage([]))
    platform.set_cover = AsyncMock()

    result = run(platform.apply_cover({"strategy": "NONE"}))

    assert result == {"success": True, "cover_status": "not_required"}
    platform.set_cover.assert_not_awaited()


def test_baijiahao_base_pipeline_dispatches_exact_frozen_cover(tmp_path: Path) -> None:
    class _Log:
        entries: list[tuple[str, str]] = []

        def add_task_log(self, _task_id: int, level: str, message: str) -> None:
            self.entries.append((level, message))

    platform = _make_baijiahao(_FakePage([]))
    cover_path = tmp_path / "frozen-cover.png"
    cover_path.write_bytes(b"cover")
    platform.check_login = AsyncMock(return_value=True)
    platform.preflight_delivery = AsyncMock()
    platform.navigate_to_editor = AsyncMock()
    platform.fill_title = AsyncMock()
    platform.fill_content = AsyncMock(
        return_value={
            "text_ok": True,
            "media_status": "completed",
            "expected_images": 1,
            "uploaded_images": 1,
            "failed_images": [],
        }
    )
    platform.set_cover = AsyncMock(
        return_value={"success": True, "cover_status": "completed"}
    )
    platform.save_draft = AsyncMock(
        return_value="https://baijiahao.baidu.com/builder/preview/s?id=cover-test"
    )
    platform._safe_simulate_scroll = AsyncMock()
    platform._safe_random_mouse_movement = AsyncMock()

    result = run(
        platform.publish(
            title="冻结封面流水线",
            content_blocks=[{"type": "text", "text": "正文", "position": 0}],
            images=[],
            cover={
                "strategy": "EXPLICIT",
                "asset_id": "frozen-cover-id",
                "local_path": str(cover_path),
            },
            delivery_mode="DRAFT",
            auto_login=False,
            task_id=0,
            db=_Log(),
        )
    )

    assert result["success"] is True
    assert result["cover_status"] == "completed"
    platform.set_cover.assert_awaited_once_with(str(cover_path))
    platform.save_draft.assert_awaited_once_with("冻结封面流水线")
