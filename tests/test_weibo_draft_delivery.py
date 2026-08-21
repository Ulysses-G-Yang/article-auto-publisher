"""微博 Word 图文顺序与草稿安全门测试。"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from platforms.base import DraftBaselineError, DraftResultUnknownError
from platforms.content_validation import ContentValidationError
from platforms.weibo import WeiboPlatform


class _Editor:
    def __init__(self, text: str) -> None:
        self.text = text
        self.click = AsyncMock()
        self.evaluate = AsyncMock(return_value=True)
        self.press = AsyncMock()

    async def inner_text(self) -> str:
        return self.text


class _Keyboard:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def press(self, key: str) -> None:
        self.events.append(f"press:{key}")

    async def insert_text(self, text: str) -> None:
        self.events.append(f"text:{text}")


def _ordered_platform(events: list[str]) -> tuple[WeiboPlatform, _Editor]:
    platform = WeiboPlatform()
    editor = _Editor("开头\n二级标题\n结尾")
    platform.page = SimpleNamespace(
        keyboard=_Keyboard(events),
        evaluate=AsyncMock(return_value=True),
    )
    platform.simulator.random_delay = AsyncMock()
    platform._current_body_editor = AsyncMock(return_value=editor)
    platform._place_body_caret_at_end = AsyncMock()
    platform._create_paragraph_after_image = AsyncMock(
        side_effect=lambda: events.append("paragraph-after-image")
    )
    platform._apply_h2_to_current_block = AsyncMock(
        side_effect=lambda: events.append("heading:h2")
    )
    platform._validate_dom_prefix = AsyncMock()
    platform._validate_dom_exact = AsyncMock()

    async def upload(path: str) -> dict:
        events.append(f"image:{path}")
        return {"success": True}

    platform._upload_image = AsyncMock(side_effect=upload)
    return platform, editor


def test_weibo_writes_text_and_images_in_frozen_block_order() -> None:
    events: list[str] = []
    platform, _editor = _ordered_platform(events)
    blocks = [
        {"type": "text", "text": "开头", "position": 0},
        {"type": "image", "position": 1},
        {"type": "heading", "level": 2, "text": "二级标题", "position": 2},
        {"type": "image", "position": 3},
        {"type": "text", "text": "结尾", "position": 4},
    ]
    images = [
        {"position_index": 99, "local_path": "D:/decoy.png"},
        {"position_index": 1, "local_path": "D:/one.png"},
        {"position_index": 3, "local_path": "D:/two.png"},
    ]

    result = asyncio.run(platform.fill_content(blocks, images))

    assert result["media_status"] == "completed"
    assert result["uploaded_images"] == 2
    assert events.index("text:开头") < events.index("image:D:/one.png")
    assert events.index("image:D:/one.png") < events.index("text:二级标题")
    assert events.index("text:二级标题") < events.index("heading:h2")
    assert events.index("text:二级标题") < events.index("image:D:/two.png")
    assert events.index("image:D:/two.png") < events.index("text:结尾")
    assert platform._upload_image.await_args_list == [
        call("D:/one.png"),
        call("D:/two.png"),
    ]
    assert platform._validate_dom_prefix.await_count == 2
    platform._validate_dom_exact.assert_awaited_once_with(blocks, phase="正文最终")


def test_weibo_never_falls_back_to_the_first_unmatched_image() -> None:
    events: list[str] = []
    platform, _editor = _ordered_platform(events)
    blocks = [
        {"type": "text", "text": "开头", "position": 0},
        {"type": "image", "position": 7},
        {"type": "text", "text": "结尾", "position": 8},
    ]

    with pytest.raises(ContentValidationError, match="图片块没有唯一受控文件"):
        asyncio.run(
            platform.fill_content(
                blocks,
                [{"position_index": 1, "local_path": "D:/wrong.png"}],
            )
        )

    platform._upload_image.assert_not_awaited()
    platform._validate_dom_exact.assert_not_awaited()


def test_weibo_dom_validation_rejects_reordered_image() -> None:
    platform = WeiboPlatform()
    platform._read_editor_dom_tokens = AsyncMock(
        return_value=[
            {"kind": "text", "text": "开头"},
            {"kind": "text", "text": "结尾"},
            {"kind": "image"},
        ]
    )
    blocks = [
        {"type": "text", "text": "开头"},
        {"type": "image"},
        {"type": "text", "text": "结尾"},
    ]

    with pytest.raises(ContentValidationError, match="图文顺序不完整"):
        asyncio.run(platform._validate_dom_exact(blocks, phase="测试"))


def test_weibo_expected_tokens_preserve_h2_and_image_order() -> None:
    assert WeiboPlatform._expected_content_tokens(
        [
            {"type": "text", "text": "开头"},
            {"type": "heading", "level": 2, "text": "章节"},
            {"type": "image"},
        ]
    ) == [
        {"kind": "text", "text": "开头"},
        {"kind": "heading", "level": 2, "text": "章节"},
        {"kind": "image"},
    ]


@pytest.mark.parametrize(
    "heading",
    [
        {"type": "heading", "level": 3, "text": "三级标题"},
        {"type": "heading", "level": 2, "text": "两行\n标题"},
    ],
)
def test_weibo_heading_contract_fails_before_editor_input(heading: dict) -> None:
    platform = WeiboPlatform()

    with pytest.raises(ContentValidationError, match="仅支持单行二级标题"):
        platform._validate_delivery_blocks([heading], [])


class _Nodes:
    def __init__(self, nodes: list) -> None:
        self.nodes = nodes

    async def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int):
        return self.nodes[index]


class _UiNode:
    def __init__(self, text: str, *, paths: list[str] | None = None) -> None:
        self.text = text
        self.paths = paths or []
        self.click = AsyncMock()
        self.hover = AsyncMock()

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self) -> str:
        return self.text

    def locator(self, selector: str):
        assert selector == "svg path"
        return SimpleNamespace(
            evaluate_all=AsyncMock(return_value=self.paths)
        )


def test_weibo_h2_uses_unique_verified_menu_and_checks_dom_tag() -> None:
    trigger = _UiNode("正文")
    option = _UiNode("标题 2")
    editor = _Editor("章节")
    editor.evaluate = AsyncMock(return_value="h2")
    platform = WeiboPlatform()
    platform.page = SimpleNamespace(
        locator=lambda selector: (
            _Nodes([trigger])
            if selector == ".main-editor-toolbar .wb-cursor-pointer"
            else _Nodes([option])
        )
    )
    platform._current_body_editor = AsyncMock(return_value=editor)

    with patch("platforms.weibo.asyncio.sleep", new=AsyncMock()):
        asyncio.run(platform._apply_h2_to_current_block())

    trigger.click.assert_awaited_once_with(timeout=5000)
    option.click.assert_awaited_once_with(timeout=5000)


def test_weibo_body_image_trigger_requires_svg_and_semantic_evidence() -> None:
    decoy = _UiNode("", paths=["decoy"])
    target = _UiNode("", paths=["target"])
    target_fingerprint = hashlib.sha256(b"target").hexdigest()
    platform = WeiboPlatform()
    platform.page = SimpleNamespace(
        locator=lambda _selector: _Nodes([decoy, target]),
        evaluate=AsyncMock(return_value=["插入图片"]),
    )

    with (
        patch(
            "platforms.weibo.BODY_IMAGE_ICON_FINGERPRINT",
            target_fingerprint,
        ),
        patch("platforms.weibo.asyncio.sleep", new=AsyncMock()),
    ):
        result = asyncio.run(platform._find_body_image_trigger())

    assert result is target
    target.hover.assert_awaited_once_with(timeout=5000)
    decoy.hover.assert_not_awaited()


def test_weibo_identity_uses_authenticated_navigation_state_only() -> None:
    platform = WeiboPlatform()

    class IdentityPage:
        async def evaluate(self, _script: str) -> dict:
            return {"user_id": "1234567890", "display_name": "当前微博账号"}

        def on(self, *_args, **_kwargs) -> None:
            raise AssertionError("身份确认不得监听内容流响应")

        async def goto(self, *_args, **_kwargs) -> None:
            raise AssertionError("身份确认不得二次导航")

    platform.page = IdentityPage()

    result = asyncio.run(platform.fetch_identity_payload())

    assert result == {
        "ok": True,
        "user_id": "1234567890",
        "display_name": "当前微博账号",
    }


class _Request:
    def __init__(self, url: str, method: str) -> None:
        self.url = url
        self.method = method


class _Response:
    url = "https://card.weibo.com/api/article/draft/save"
    status = 200
    request = _Request(url, "POST")

    async def json(self) -> dict:
        return {"code": 100000}


class _Route:
    def __init__(self) -> None:
        self.abort = AsyncMock()
        self.continue_ = AsyncMock()


class _SavePage:
    def __init__(self, *, trigger_publish: bool = False, button_count: int = 1) -> None:
        self.trigger_publish = trigger_publish
        self.button_count = button_count
        self.response_listener = None
        self.route_handler = None
        self.route = AsyncMock(side_effect=self._register_route)
        self.unroute = AsyncMock()
        self.goto = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.remove_listener = Mock()
        self.blocked_route = _Route()
        self.title_field = SimpleNamespace(
            count=AsyncMock(return_value=1),
            is_visible=AsyncMock(return_value=True),
            input_value=AsyncMock(return_value="唯一标题"),
        )

    def locator(self, selector: str):
        assert selector == "textarea[placeholder='请输入标题']"
        return SimpleNamespace(first=self.title_field)

    async def _register_route(self, _pattern: str, handler) -> None:
        self.route_handler = handler

    def on(self, event: str, listener) -> None:
        assert event == "response"
        self.response_listener = listener

    async def evaluate(self, script: str, argument=None):
        if "location.hash.match" in script:
            return "87654321"
        if "candidates.length" in script:
            if self.button_count != 1:
                return {"clicked": False, "count": self.button_count}
            if self.trigger_publish:
                await self.route_handler(
                    self.blocked_route,
                    _Request(
                        "https://card.weibo.com/api/article/publish?token=secret",
                        "POST",
                    ),
                )
            else:
                await self.response_listener(_Response())
            return {"clicked": True, "count": 1}
        if "document.querySelectorAll('.list-item')" in script:
            assert argument == "唯一标题"
            return {"clicked": True, "count": 1}
        raise AssertionError("unexpected evaluate call")


def _save_platform(page: _SavePage) -> WeiboPlatform:
    platform = WeiboPlatform()
    platform.page = page
    platform.simulator.random_delay = AsyncMock()
    platform._preflight_title = "唯一标题"
    platform._draft_title_baseline_count = 0
    platform._active_draft_id = "87654321"
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    platform._expected_persisted_image_count = 0
    platform._validate_dom_exact = AsyncMock()
    return platform


def test_weibo_draft_requires_current_id_and_title_entity() -> None:
    page = _SavePage()
    platform = _save_platform(page)

    result = asyncio.run(platform.save_draft("唯一标题"))

    assert result.endswith("#/draft/87654321")
    page.route.assert_awaited_once()
    page.unroute.assert_awaited_once()
    page.goto.assert_awaited_once()


def test_weibo_publication_request_is_aborted_and_never_reported_as_draft() -> None:
    page = _SavePage(trigger_publish=True)
    platform = _save_platform(page)

    with pytest.raises(DraftResultUnknownError, match="公开发布请求"):
        asyncio.run(platform.save_draft("唯一标题"))

    page.blocked_route.abort.assert_awaited_once_with("blockedbyclient")
    page.blocked_route.continue_.assert_not_awaited()
    page.goto.assert_not_awaited()


def test_weibo_ambiguous_save_button_does_not_click_or_navigate() -> None:
    page = _SavePage(button_count=2)
    platform = _save_platform(page)

    with pytest.raises(DraftBaselineError, match="保存草稿按钮"):
        asyncio.run(platform.save_draft("唯一标题"))

    page.goto.assert_not_awaited()
    page.unroute.assert_awaited_once()


@pytest.mark.parametrize(
    ("path", "method", "blocked"),
    [
        ("/api/article/publish", "POST", True),
        ("/api/article/publish", "GET", False),
        ("/api/article/draft/save", "POST", False),
    ],
)
def test_weibo_publish_guard_only_blocks_state_changing_publish_requests(
    path: str,
    method: str,
    blocked: bool,
) -> None:
    assert WeiboPlatform._is_public_publish_mutation(path, method) is blocked
