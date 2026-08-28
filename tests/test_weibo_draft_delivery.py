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


def test_weibo_writes_seven_images_and_five_h2_blocks_once_each() -> None:
    events: list[str] = []
    platform, _editor = _ordered_platform(events)
    blocks: list[dict] = [{"type": "text", "text": "开头", "position": 0}]
    images = []
    position = 1
    for image_index in range(7):
        blocks.append({"type": "image", "position": position})
        images.append(
            {
                "position_index": position,
                "local_path": f"D:/controlled/{image_index + 1}.png",
            }
        )
        position += 1
        if image_index < 5:
            blocks.append(
                {
                    "type": "heading",
                    "level": 2,
                    "text": f"章节 {image_index + 1}",
                    "position": position,
                }
            )
            position += 1
    blocks.append({"type": "text", "text": "结尾", "position": position})
    _editor.text = "\n".join(
        ["开头", *(f"章节 {index}" for index in range(1, 6)), "结尾"]
    )

    result = asyncio.run(platform.fill_content(blocks, images))

    assert result["media_status"] == "completed"
    assert result["expected_images"] == 7
    assert result["uploaded_images"] == 7
    assert platform._upload_image.await_count == 7
    assert platform._apply_h2_to_current_block.await_count == 5
    assert platform._validate_dom_prefix.await_count == 7


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


class _UploadItem:
    def __init__(self, page) -> None:
        self.page = page
        self.click = AsyncMock(side_effect=self._click)

    async def _click(self, **_kwargs) -> None:
        self.page.selected = True

    async def evaluate(self, _script: str) -> dict:
        return {"failed": self.page.upload_failed, "ready": not self.page.upload_failed}

    async def get_attribute(self, name: str) -> str:
        assert name == "class"
        return "image-item is-selected" if self.page.selected else "image-item"


class _UploadInput:
    def __init__(self, page) -> None:
        self.page = page
        self.first = self
        self.set_input_files = AsyncMock(side_effect=self._set_input_files)

    async def count(self) -> int:
        return 1

    async def get_attribute(self, name: str) -> str:
        assert name == "accept"
        return ".jpg,.jpeg,.bmp,.gif,.png,.heic"

    async def _set_input_files(self, _path: str, **_kwargs) -> None:
        self.page.uploaded = True


class _HiddenSpinner:
    first = None

    def __init__(self) -> None:
        self.first = self

    async def wait_for(self, *, state: str, timeout: int) -> None:
        assert (state, timeout) == ("hidden", 15000)


class _UploadItems:
    def __init__(self, page) -> None:
        self.page = page
        self.first = _UploadItem(page)

    async def count(self) -> int:
        if not self.page.uploaded:
            return 1
        return 3 if self.page.ambiguous_new_items else 2


class _SelectedUploadItems:
    def __init__(self, page) -> None:
        self.page = page

    async def count(self) -> int:
        return 1 if self.page.selected else 0


class _InsertButton:
    def __init__(self, page) -> None:
        self.page = page
        self.click = AsyncMock(side_effect=self._click)

    async def _click(self, **_kwargs) -> None:
        self.page.dialog_visible = False
        self.page.editor_image_count += 1

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return self.page.selected

    async def inner_text(self) -> str:
        return "插入"


class _UploadDialog:
    def __init__(self, page) -> None:
        self.page = page
        self.input = _UploadInput(page)
        self.items = _UploadItems(page)
        self.selected_items = _SelectedUploadItems(page)
        self.insert_button = _InsertButton(page)

    async def is_visible(self) -> bool:
        return self.page.dialog_visible

    async def inner_text(self) -> str:
        return "图片库 上传 取消 插入"

    def locator(self, selector: str):
        if selector == ".n-spin-body":
            return _HiddenSpinner()
        if selector == "input[type=file]":
            return self.input
        if selector == ".image-list .image-item":
            return self.items
        if selector == ".image-list .image-item.is-selected":
            return self.selected_items
        if selector == "button":
            return _Nodes([self.insert_button])
        raise AssertionError(f"unexpected dialog selector: {selector}")


class _UploadDialogs:
    def __init__(self, page, dialog: _UploadDialog) -> None:
        self.page = page
        self.dialog = dialog

    async def count(self) -> int:
        return 1 if self.page.dialog_visible else 0

    def nth(self, index: int):
        assert index == 0
        return self.dialog


class _UploadPage:
    def __init__(self, *, ambiguous_new_items: bool = False) -> None:
        self.dialog_visible = True
        self.uploaded = False
        self.selected = False
        self.upload_failed = False
        self.ambiguous_new_items = ambiguous_new_items
        self.editor_image_count = 2
        self.dialog = _UploadDialog(self)
        self.dialogs = _UploadDialogs(self, self.dialog)

    async def evaluate(self, _script: str):
        return {
            "semantic_count": self.editor_image_count,
            "unsupported_count": 0,
        }

    def locator(self, selector: str):
        assert selector == ".n-dialog:visible"
        return self.dialogs


def test_weibo_upload_selects_only_the_new_ready_album_item() -> None:
    page = _UploadPage()
    trigger = _UiNode("")
    platform = WeiboPlatform()
    platform.page = page
    platform._find_body_image_trigger = AsyncMock(return_value=trigger)
    platform._semantic_editor_image_count = AsyncMock(side_effect=[2, 3, 3])

    with patch("platforms.weibo.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(platform._upload_image("D:/controlled/third.png"))

    assert result == {"success": True, "error": "", "observed_image_count": 3}
    page.dialog.input.set_input_files.assert_awaited_once_with(
        "D:/controlled/third.png",
        timeout=15000,
    )
    page.dialog.items.first.click.assert_awaited_once_with(timeout=5000)
    page.dialog.insert_button.click.assert_awaited_once_with(timeout=5000)


def test_weibo_dom_snapshot_ignores_prosemirror_separators() -> None:
    snapshot = [
        {
            "tag": "p",
            "class_name": "",
            "text": "第一段",
            "images": [
                {"is_separator": True, "is_body_image": False},
            ],
        },
        {
            "tag": "figure",
            "class_name": "wb-node-image",
            "text": "",
            "images": [
                {"is_separator": False, "is_body_image": True},
            ],
        },
        {
            "tag": "p",
            "class_name": "",
            "text": "第二段",
            "images": [
                {"is_separator": True, "is_body_image": False},
            ],
        },
    ]

    assert WeiboPlatform._normalize_editor_dom_snapshot(snapshot) == [
        {"kind": "text", "text": "第一段"},
        {"kind": "image"},
        {"kind": "text", "text": "第二段"},
    ]


def test_weibo_dom_snapshot_rejects_unknown_non_separator_image() -> None:
    snapshot = [
        {
            "tag": "p",
            "class_name": "",
            "text": "正文",
            "images": [
                {"is_separator": False, "is_body_image": True},
            ],
        }
    ]

    with pytest.raises(ContentValidationError, match="正文图片结构无法确认"):
        WeiboPlatform._normalize_editor_dom_snapshot(snapshot)


def test_weibo_semantic_image_count_uses_verified_figure_state() -> None:
    editor = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={"semantic_count": 7, "unsupported_count": 0}
        )
    )
    platform = WeiboPlatform()
    platform._current_body_editor = AsyncMock(return_value=editor)

    assert asyncio.run(platform._semantic_editor_image_count()) == 7


def test_weibo_semantic_image_count_rejects_unknown_images() -> None:
    editor = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={"semantic_count": 1, "unsupported_count": 1}
        )
    )
    platform = WeiboPlatform()
    platform._current_body_editor = AsyncMock(return_value=editor)

    with pytest.raises(ContentValidationError, match="正文图片结构无法确认"):
        asyncio.run(platform._semantic_editor_image_count())


def test_weibo_upload_fails_closed_when_more_than_one_new_item_appears() -> None:
    page = _UploadPage(ambiguous_new_items=True)
    trigger = _UiNode("")
    platform = WeiboPlatform()
    platform.page = page
    platform._find_body_image_trigger = AsyncMock(return_value=trigger)
    platform._semantic_editor_image_count = AsyncMock(return_value=2)

    with patch("platforms.weibo.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(platform._upload_image("D:/controlled/third.png"))

    assert result["error_code"] == "WEIBO_BODY_IMAGE_ITEM_COUNT_AMBIGUOUS"
    page.dialog.input.set_input_files.assert_awaited_once()
    page.dialog.items.first.click.assert_not_awaited()
    page.dialog.insert_button.click.assert_not_awaited()


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


class _DraftCardPage:
    def __init__(self) -> None:
        self.expression = ""

    async def evaluate(self, expression: str):
        self.expression = expression
        return [
            {
                "title": "同名标题",
                "edit_urls": [
                    "https://card.weibo.com/article/v5/editor#/draft/4183001",
                ],
            },
            {
                "title": "同名标题",
                "edit_urls": [
                    "https://card.weibo.com/article/v5/editor#/draft/4183002",
                ],
            },
        ]


def test_weibo_preflight_keeps_javascript_newline_regex_literal() -> None:
    page = _DraftCardPage()
    platform = WeiboPlatform()
    platform.page = page

    cards = asyncio.run(platform._read_visible_draft_cards())

    assert r".split(/\r?\n/, 1)" in page.expression
    assert "\r" not in page.expression
    assert [card["draft_id"] for card in cards] == ["4183001", "4183002"]


@pytest.mark.parametrize(
    ("url", "draft_id"),
    [
        (
            "https://card.weibo.com/article/v5/editor#/draft/4183001",
            "4183001",
        ),
        ("https://card.weibo.com/article/v5/editor#/draft/0", ""),
        ("http://card.weibo.com/article/v5/editor#/draft/4183001", ""),
        ("https://example.com/article/v5/editor#/draft/4183001", ""),
        (
            "https://card.weibo.com/article/v5/editor?next=1#/draft/4183001",
            "",
        ),
    ],
)
def test_weibo_accepts_only_canonical_numeric_draft_urls(
    url: str,
    draft_id: str,
) -> None:
    assert WeiboPlatform._draft_id_from_editor_url(url) == draft_id


class _ResumeField:
    def __init__(self, title: str) -> None:
        self.title = title

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def input_value(self) -> str:
        return self.title


class _ResumeLocator:
    def __init__(self, node: _ResumeField) -> None:
        self.first = node


class _ResumePage:
    def __init__(self, *, draft_id: str = "4183864") -> None:
        self.draft_id = draft_id
        self.title_field = _ResumeField("唯一标题")
        self.body = _ResumeField("")
        self.goto = AsyncMock()
        self.wait_for_function = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.evaluate_calls = 0

    async def evaluate(self, script: str, _argument=None):
        self.evaluate_calls += 1
        if "location.hash.match" in script:
            return self.draft_id
        raise AssertionError("unexpected resume evaluate call")

    def locator(self, selector: str):
        if selector == "textarea[placeholder='请输入标题']":
            return _ResumeLocator(self.title_field)
        if selector == "div.tiptap.ProseMirror:visible":
            return _ResumeLocator(self.body)
        raise AssertionError(f"unexpected resume selector: {selector}")

    def get_by_role(self, *_args, **_kwargs):
        raise AssertionError("恢复现有草稿不得触发写文章按钮")

    def expect_response(self, *_args, **_kwargs):
        raise AssertionError("恢复现有草稿不得等待 create 请求")


def test_weibo_preflight_allows_existing_same_title_for_new_draft() -> None:
    page = _ResumePage()
    platform = WeiboPlatform()
    platform.page = page
    platform._collect_draft_ids_by_title = AsyncMock(
        return_value={"唯一标题": frozenset({"4183001", "4183002"})}
    )

    asyncio.run(platform.preflight_delivery("唯一标题"))

    platform._collect_draft_ids_by_title.assert_awaited_once_with(
        only_title="唯一标题",
        guard_mutations=True,
    )
    assert platform._editing_existing_draft is False
    assert platform._preflight_draft_ids_by_title == {
        "唯一标题": frozenset({"4183001", "4183002"})
    }
    assert platform._preflight_all_draft_ids == frozenset(
        {"4183001", "4183002"}
    )
    page.goto.assert_not_awaited()


def test_weibo_resume_uses_exact_id_when_same_title_has_multiple_drafts() -> None:
    page = _ResumePage()
    platform = WeiboPlatform(
        resume_existing_title="唯一标题",
        resume_existing_draft_id="4183864",
    )
    platform.page = page
    platform._collect_draft_ids_by_title = AsyncMock(
        return_value={"唯一标题": frozenset({"4183001", "4183864"})}
    )

    asyncio.run(platform.preflight_delivery("唯一标题"))
    asyncio.run(platform.navigate_to_editor())
    asyncio.run(platform.fill_title("唯一标题"))

    assert platform._editing_existing_draft is True
    assert platform._active_draft_id == "4183864"
    assert platform._preflight_draft_ids_by_title == {
        "唯一标题": frozenset({"4183001", "4183864"})
    }
    page.goto.assert_awaited_once_with(
        "https://card.weibo.com/article/v5/editor#/draft/4183864",
        wait_until="domcontentloaded",
        timeout=30000,
    )


def test_weibo_resume_rejects_id_missing_from_same_title_baseline() -> None:
    page = _ResumePage()
    platform = WeiboPlatform(
        resume_existing_title="唯一标题",
        resume_existing_draft_id="4183864",
    )
    platform.page = page
    platform._collect_draft_ids_by_title = AsyncMock(
        return_value={"唯一标题": frozenset({"4183001", "4183002"})}
    )

    with pytest.raises(DraftBaselineError, match="ID 不在"):
        asyncio.run(platform.preflight_delivery("唯一标题"))
    page.goto.assert_not_awaited()


def test_weibo_resume_rejects_unexpected_draft_id() -> None:
    page = _ResumePage(draft_id="4183999")
    platform = WeiboPlatform(
        resume_existing_title="唯一标题",
        resume_existing_draft_id="4183864",
    )
    platform.page = page
    platform._collect_draft_ids_by_title = AsyncMock(
        return_value={"唯一标题": frozenset({"4183864", "4183999"})}
    )

    with pytest.raises(DraftBaselineError, match="ID"):
        asyncio.run(platform.preflight_delivery("唯一标题"))


class _CreateResponse:
    status = 200
    url = "https://card.weibo.com/article/v5/aj/editor/draft/create"
    request = SimpleNamespace(method="POST")


class _ResponseInfo:
    def __init__(self, response: _CreateResponse) -> None:
        async def resolve():
            return response

        self.value = resolve()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None


class _WriteButton:
    def __init__(self, page, *, change_id: bool) -> None:
        self.page = page
        self.change_id = change_id
        self.click = AsyncMock(side_effect=self._click)

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def _click(self, **_kwargs) -> None:
        if self.change_id:
            self.page.draft_id = "4182999"


class _NewDraftPage:
    def __init__(self, *, change_id: bool = True) -> None:
        self.draft_id = "4181973"
        self.goto = AsyncMock()
        self.wait_for_function = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.write_button = _WriteButton(self, change_id=change_id)

    async def wait_for_function(self, *_args, **_kwargs) -> None:
        if "beforeId" in _args[0] and self.draft_id == _kwargs["arg"]:
            raise TimeoutError("new draft id not observed")

    async def evaluate(self, script: str, _argument=None):
        if "location.hash.match" in script:
            return self.draft_id
        if "textarea[placeholder='请输入标题']" in script:
            return {"title": "", "body": ""}
        raise AssertionError("unexpected evaluate call")

    def get_by_role(self, role: str, *, name: str, exact: bool):
        assert (role, name, exact) == ("button", "写文章", True)
        return self.write_button

    def expect_response(self, predicate, *, timeout: int):
        response = _CreateResponse()
        assert timeout == 20000
        assert predicate(response) is True
        return _ResponseInfo(response)


def test_weibo_new_draft_requires_create_response_and_changed_positive_id() -> None:
    page = _NewDraftPage()
    platform = WeiboPlatform()
    platform.page = page
    platform._preflight_title = "唯一标题"
    platform._preflight_draft_ids_by_title = {
        "唯一标题": frozenset({"4181001", "4181002"})
    }
    platform._preflight_all_draft_ids = frozenset({"4181001", "4181002"})

    asyncio.run(platform.navigate_to_editor())

    assert platform._active_draft_id == "4182999"
    page.write_button.click.assert_awaited_once_with(timeout=10000)


def test_weibo_create_response_without_new_id_is_result_unknown() -> None:
    page = _NewDraftPage(change_id=False)
    platform = WeiboPlatform()
    platform.page = page
    platform._preflight_title = "唯一标题"
    platform._preflight_draft_ids_by_title = {
        "唯一标题": frozenset({"4181973"})
    }
    platform._preflight_all_draft_ids = frozenset({"4181973"})

    with pytest.raises(DraftResultUnknownError, match="基线之外"):
        asyncio.run(platform.navigate_to_editor())


def test_weibo_create_response_cannot_reuse_any_baseline_draft_id() -> None:
    page = _NewDraftPage()
    platform = WeiboPlatform()
    platform.page = page
    platform._preflight_title = "唯一标题"
    platform._preflight_draft_ids_by_title = {
        "其他标题": frozenset({"4182999"})
    }
    platform._preflight_all_draft_ids = frozenset({"4182999"})

    with pytest.raises(DraftResultUnknownError, match="基线之外"):
        asyncio.run(platform.navigate_to_editor())


def test_weibo_media_failure_after_draft_creation_is_result_unknown() -> None:
    events: list[str] = []
    platform, _editor = _ordered_platform(events)
    platform._active_draft_id = "4182999"
    platform._upload_image = AsyncMock(
        return_value={
            "success": False,
            "error_code": "WEIBO_EDITOR_IMAGE_COUNT_UNCHANGED",
            "error": "图片数量未增加",
        }
    )
    blocks = [
        {"type": "text", "text": "开头", "position": 0},
        {"type": "image", "position": 1},
    ]
    images = [{"position_index": 1, "local_path": "D:/one.png"}]

    with pytest.raises(DraftResultUnknownError) as caught:
        asyncio.run(platform.fill_content(blocks, images))

    assert caught.value.error_code == "DRAFT_RESULT_UNKNOWN"
    assert caught.value.media_error_code == "WEIBO_EDITOR_IMAGE_COUNT_UNCHANGED"
    assert "stage=WEIBO_EDITOR_IMAGE_COUNT_UNCHANGED" in str(caught.value)
    assert caught.value.media_progress == {
        "expected_images": 1,
        "uploaded_images": 0,
        "failed_image_count": 1,
        "media_status": "failed",
    }


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
        self.wait_for_function = AsyncMock()
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
            assert r".split(/\r?\n/, 1)" in script
            assert "\r" not in script
            assert argument == "唯一标题"
            return {"clicked": True, "count": 1}
        raise AssertionError("unexpected evaluate call")


def _save_platform(page: _SavePage) -> WeiboPlatform:
    platform = WeiboPlatform()
    platform.page = page
    platform.simulator.random_delay = AsyncMock()
    platform._preflight_title = "唯一标题"
    platform._preflight_draft_ids_by_title = {
        "唯一标题": frozenset({"76543210"})
    }
    platform._preflight_all_draft_ids = frozenset({"76543210"})
    platform._active_draft_id = "87654321"
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    platform._expected_persisted_image_count = 0
    platform._validate_dom_exact = AsyncMock()
    platform._collect_draft_ids_by_title = AsyncMock(
        return_value={"唯一标题": frozenset({"76543210", "87654321"})}
    )
    return platform


def test_weibo_draft_binds_active_id_even_when_same_title_is_not_unique() -> None:
    page = _SavePage()
    platform = _save_platform(page)

    result = asyncio.run(platform.save_draft("唯一标题"))

    assert result.endswith("#/draft/87654321")
    evidence = platform._last_draft_evidence.to_dict()
    assert evidence["draft_list_match_count"] == 2
    assert evidence["draft_list_title_unique"] is False
    assert evidence["draft_entity_bound"] is True
    assert evidence["draft_entity_source"] == "baseline_new_id"
    platform._collect_draft_ids_by_title.assert_awaited_once_with(
        only_title="唯一标题"
    )
    page.route.assert_awaited_once()
    page.unroute.assert_awaited_once()
    page.goto.assert_awaited_once_with(
        "https://card.weibo.com/article/v5/editor#/draft/87654321",
        wait_until="domcontentloaded",
        timeout=30000,
    )


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


def test_weibo_readonly_returns_exact_url_for_unique_title_id() -> None:
    platform = WeiboPlatform()
    platform.page = SimpleNamespace()
    platform._collect_draft_ids_by_title = AsyncMock(
        return_value={"唯一标题": frozenset({"87654321"})}
    )

    result = asyncio.run(platform.verify_draft_readonly("唯一标题"))

    assert result == {
        "title_matched": True,
        "match_count": 1,
        "draft_url": (
            "https://card.weibo.com/article/v5/editor#/draft/87654321"
        ),
        "structure": {"source": "draft_list_id", "id_targeted": False},
    }


def test_weibo_readonly_uses_known_id_when_title_is_duplicated() -> None:
    platform = WeiboPlatform()
    platform.page = SimpleNamespace()
    platform._active_draft_id = "87654321"
    platform._collect_draft_ids_by_title = AsyncMock(
        return_value={
            "同名标题": frozenset({"76543210", "87654321"}),
        }
    )

    result = asyncio.run(platform.verify_draft_readonly("同名标题"))

    assert result["match_count"] == 2
    assert result["draft_url"].endswith("#/draft/87654321")
    assert result["structure"]["id_targeted"] is True


def test_weibo_readonly_rejects_same_title_without_target_id() -> None:
    platform = WeiboPlatform()
    platform.page = SimpleNamespace()
    platform._collect_draft_ids_by_title = AsyncMock(
        return_value={
            "同名标题": frozenset({"76543210", "87654321"}),
        }
    )

    result = asyncio.run(platform.verify_draft_readonly("同名标题"))

    assert result["error_code"] == "PROBE_TITLE_AMBIGUOUS"


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
