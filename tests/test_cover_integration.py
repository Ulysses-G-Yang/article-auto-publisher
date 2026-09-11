from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from platforms.baijiahao import BaijiahaoPlatform
from platforms.base import DraftResultUnknownError
from platforms.smzdm import SmzdmPlatform
from platforms.xiaoheihe import XiaoheihePlatform
from platforms.zhihu import ZhihuPlatform
from platforms.zol import ZOLPlatform


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


def _make_baijiahao(page: _FakePage) -> BaijiahaoPlatform:
    platform = BaijiahaoPlatform()
    platform.page = page
    platform.simulator = _InstantSimulator()
    return platform


# 微博封面已迁移到 test_weibo_publication.py 的原生页面契约，
# 不再使用按 evaluate 调用次数返回预设成功值的模拟。


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


def test_baijiahao_cover_preview_accepts_react_cleared_file_input() -> None:
    platform = _make_baijiahao(_FakePage([]))
    modal = Mock()
    platform._cover_preview_state = AsyncMock(
        return_value={
            "selected_files": 0,
            "visual_count": 15,
            "visual_hash": 978689830,
            "confirm_count": 1,
        }
    )

    result = run(
        platform._wait_for_cover_preview(
            modal,
            {
                "selected_files": 0,
                "visual_count": 14,
                "visual_hash": 3315740119,
                "confirm_count": 0,
            },
        )
    )

    assert result is True


def test_baijiahao_cover_preview_rejects_visual_change_without_confirm() -> None:
    platform = _make_baijiahao(_FakePage([]))
    modal = Mock()
    platform._cover_preview_state = AsyncMock(
        return_value={
            "selected_files": 0,
            "visual_count": 15,
            "visual_hash": 978689830,
            "confirm_count": 0,
        }
    )

    with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
        result = run(
            platform._wait_for_cover_preview(
                modal,
                {
                    "selected_files": 0,
                    "visual_count": 14,
                    "visual_hash": 3315740119,
                    "confirm_count": 0,
                },
            )
        )

    assert result is False


def test_baijiahao_cover_confirm_label_accepts_platform_counter() -> None:
    assert BaijiahaoPlatform._is_cover_confirm_label("确定 (1)") is True
    assert BaijiahaoPlatform._is_cover_confirm_label("确认") is True
    assert BaijiahaoPlatform._is_cover_confirm_label("确定  ( 1 )") is False
    assert BaijiahaoPlatform._is_cover_confirm_label("发布") is False


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
    assert platform._expected_persisted_cover is True
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
    assert platform._expected_persisted_cover is False
    platform.set_cover.assert_not_awaited()


def test_baijiahao_persisted_cover_requires_unique_loaded_thumbnail() -> None:
    platform = BaijiahaoPlatform()
    platform.page = SimpleNamespace(
        is_closed=lambda: False,
        evaluate=AsyncMock(side_effect=[1, 0, 2]),
    )

    assert run(platform._has_persisted_cover()) is True
    assert run(platform._has_persisted_cover()) is False
    assert run(platform._has_persisted_cover()) is False


def test_baijiahao_reopen_rejects_missing_persisted_cover() -> None:
    title = "持久化封面校验"
    blocks = [{"type": "text", "text": "正文", "position": 0}]
    platform = BaijiahaoPlatform()
    platform.page = SimpleNamespace(
        is_closed=lambda: False,
        goto=AsyncMock(),
    )
    platform._expected_persisted_blocks = blocks
    platform._expected_persisted_cover = True
    platform._wait_for_editor_ready = AsyncMock()
    platform._title_editor_locator = AsyncMock(
        return_value=SimpleNamespace(inner_text=AsyncMock(return_value=title))
    )
    platform._read_editor_dom_tokens = AsyncMock(
        return_value=platform._expected_content_tokens(blocks)
    )
    platform._has_persisted_cover = AsyncMock(return_value=False)

    with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError, match="封面未持久化"):
            run(
                platform._verify_persisted_draft(
                    title,
                    "https://baijiahao.baidu.com/builder/rc/edit?type=news"
                    "&article_id=123",
                )
            )


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


def test_base_pipeline_verifies_pending_cover_only_after_draft_save() -> None:
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
            "success": True,
            "cover_status": "pending_verification",
            "cover_mode": "AUTO_FIRST_BODY_IMAGE",
        }
    )
    platform.save_draft = AsyncMock(return_value="https://example.test/draft/1")
    platform.verify_persisted_cover = AsyncMock(
        return_value={
            "success": True,
            "cover_status": "completed",
            "cover_mode": "AUTO_FIRST_BODY_IMAGE",
        }
    )
    platform._safe_simulate_scroll = AsyncMock()
    platform._safe_random_mouse_movement = AsyncMock()

    result = run(
        platform.publish(
            title="保存后封面核验",
            content_blocks=[{"type": "text", "text": "正文"}],
            images=[],
            cover={"strategy": "FIRST_BODY_IMAGE"},
            delivery_mode="DRAFT",
            auto_login=False,
            task_id=0,
            db=_Log(),
        )
    )

    assert result["success"] is True
    assert result["cover_status"] == "completed"
    assert result["cover_mode"] == "AUTO_FIRST_BODY_IMAGE"
    platform.save_draft.assert_awaited_once()
    platform.verify_persisted_cover.assert_awaited_once()


def test_base_pipeline_does_not_retry_saved_draft_when_cover_reopen_fails() -> None:
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
        return_value={"success": True, "cover_status": "pending_verification"}
    )
    platform.save_draft = AsyncMock(return_value="https://example.test/draft/2")
    platform.verify_persisted_cover = AsyncMock(side_effect=RuntimeError("reopen failed"))
    platform._safe_simulate_scroll = AsyncMock()
    platform._safe_random_mouse_movement = AsyncMock()

    result = run(
        platform.publish(
            title="封面结果失败不重存",
            content_blocks=[{"type": "text", "text": "正文"}],
            images=[],
            cover={"strategy": "FIRST_BODY_IMAGE"},
            delivery_mode="DRAFT",
            auto_login=False,
            task_id=0,
            db=_Log(),
        )
    )

    assert result["success"] is True
    assert result["cover_status"] == "failed"
    assert result["cover_error_code"] == "PLATFORM_COVER_PERSISTENCE_FAILED"
    assert "reopen failed" not in str(result)
    platform.save_draft.assert_awaited_once()


def test_xiaoheihe_cover_requires_the_frozen_first_body_image(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    other = tmp_path / "other.png"
    first.write_bytes(b"first")
    other.write_bytes(b"other")
    platform = XiaoheihePlatform()
    platform._expected_persisted_blocks = [
        {"type": "image", "local_path": str(first)}
    ]

    rejected = run(
        platform.apply_cover(
            {"strategy": "EXPLICIT", "local_path": str(other)}
        )
    )
    accepted = run(
        platform.apply_cover(
            {"strategy": "FIRST_BODY_IMAGE", "local_path": str(first)}
        )
    )

    assert rejected["error_code"] == "XHH_COVER_MUST_BE_FIRST_BODY_IMAGE"
    assert accepted["cover_status"] == "pending_verification"
    assert accepted["cover_mode"] == "AUTO_FIRST_BODY_IMAGE"


def test_zhihu_cover_selects_only_the_independent_upload_wrapper(tmp_path: Path) -> None:
    cover_path = tmp_path / "cover.png"
    cover_path.write_bytes(b"cover")
    platform = ZhihuPlatform()
    wrapper = Mock()
    wrapper.count = AsyncMock(return_value=1)
    wrapper.evaluate = AsyncMock(return_value=True)
    cover_input = Mock()
    cover_input.count = AsyncMock(return_value=1)
    cover_input.first = Mock(set_input_files=AsyncMock())
    wrapper.locator.return_value = cover_input
    platform.page = Mock()
    platform.page.locator.return_value = wrapper

    result = run(
        platform.apply_cover(
            {"strategy": "FIRST_BODY_IMAGE", "local_path": str(cover_path)}
        )
    )

    assert result["cover_status"] == "pending_verification"
    platform.page.locator.assert_called_once_with(".UploadPicture-wrapper")
    wrapper.locator.assert_called_once_with(
        "input[type=file][accept='.jpeg, .jpg, .png']"
    )
    cover_input.first.set_input_files.assert_awaited_once()


def test_zol_cover_rejects_ambiguous_single_file_inputs(tmp_path: Path) -> None:
    cover_path = tmp_path / "cover.png"
    cover_path.write_bytes(b"cover")
    platform = ZOLPlatform()
    inputs = Mock(count=AsyncMock(return_value=2))
    platform.page = Mock()
    platform.page.locator.return_value = inputs

    result = run(
        platform.apply_cover(
            {"strategy": "FIRST_BODY_IMAGE", "local_path": str(cover_path)}
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "ZOL_COVER_INPUT_AMBIGUOUS"
    platform.page.locator.assert_called_once_with(
        "input[type=file][accept='image/*']:not([multiple])"
    )


def test_smzdm_cover_requires_both_long_and_square_variants(tmp_path: Path) -> None:
    cover_path = tmp_path / "cover.png"
    cover_path.write_bytes(b"cover")
    platform = SmzdmPlatform()
    platform._set_cover_variant = AsyncMock()

    result = run(
        platform.apply_cover(
            {"strategy": "FIRST_BODY_IMAGE", "local_path": str(cover_path)}
        )
    )

    assert result["cover_status"] == "pending_verification"
    assert result["cover_mode"] == "EXPLICIT_LONG_AND_SQUARE"
    platform._set_cover_variant.assert_has_awaits(
        [
            call("添加长图", str(cover_path.resolve())),
            call("添加方图", str(cover_path.resolve())),
        ]
    )
