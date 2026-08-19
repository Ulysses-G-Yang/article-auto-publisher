from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from platforms.baijiahao import BaijiahaoPlatform
from platforms.base import DraftBaselineError, DraftResultUnknownError
from platforms.content_validation import ContentValidationError


class _Item:
    def __init__(
        self,
        *,
        visible: bool = True,
        enabled: bool = True,
        text: str = "",
    ) -> None:
        self.visible = visible
        self.enabled = enabled
        self.text = text
        self.click = AsyncMock()
        self.set_input_files = AsyncMock()
        self.parent: _Item | None = None

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def inner_text(self) -> str:
        return self.text

    def locator(self, selector: str) -> _Item:
        assert selector == ".."
        return self.parent or self

    async def wait_for(self, *, state: str, timeout: int) -> None:
        assert (state, timeout) == ("hidden", 5000)


class _Collection:
    def __init__(self, items: list[_Item]) -> None:
        self.items = items

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> _Item:
        return self.items[index]

    @property
    def first(self) -> _Item:
        return self.items[0]


class _DraftTab(_Item):
    def __init__(self, *, selected_after_click: bool = True) -> None:
        super().__init__()
        self.selected_after_click = selected_after_click

    async def get_attribute(self, name: str) -> str | None:
        if name == "aria-selected":
            return "true" if self.selected_after_click else "false"
        if name == "class":
            return "cheetah-tabs-tab"
        return None


class _DraftTabPage:
    def __init__(self, tabs: list[_DraftTab]) -> None:
        self.tabs = _Collection(tabs)

    def get_by_role(self, role: str, *, name: str, exact: bool) -> _Collection:
        assert (role, name, exact) == ("tab", "草稿", True)
        return self.tabs


class _SearchInput:
    def __init__(self) -> None:
        self.fill = AsyncMock()
        self.press = AsyncMock()

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True


class _SearchPage:
    def __init__(self, search: _SearchInput) -> None:
        self.search = search

    def locator(self, selector: str):
        assert selector == 'input[placeholder*="输入标题关键字"]'
        return SimpleNamespace(first=self.search)


class _Modal(_Item):
    def __init__(self, image_inputs: list[_Item], confirms: list[_Item]) -> None:
        super().__init__()
        self.image_inputs = _Collection(image_inputs)
        self.confirms = _Collection(confirms)

    def locator(self, selector: str) -> _Collection:
        assert selector == 'input[type="file"][accept*="image"]'
        return self.image_inputs

    def get_by_text(self, text: str, *, exact: bool) -> _Collection:
        assert (text, exact) == ("确认", True)
        return self.confirms


class _ImagePage:
    def __init__(
        self,
        triggers: list[_Item],
        modals: list[_Modal],
    ) -> None:
        self.triggers = _Collection(triggers)
        self.modals = _Collection(modals)

    def locator(self, selector: str) -> _Collection:
        if selector == ".edui-for-insertimage:visible":
            return self.triggers
        if selector == ".cheetah-ui-pro-image-modal:visible":
            return self.modals
        raise AssertionError(f"unexpected selector: {selector}")


class _FormatPage:
    def __init__(self, trigger: _Item, options: list[_Item]) -> None:
        self.trigger = _Collection([trigger])
        self.options = _Collection(options)

    def locator(self, selector: str) -> _Collection:
        if selector == ".edui-for-customfontsize:visible":
            return self.trigger
        if selector == (
            "div[class*='dropdownItem']:visible span[class*='label']:visible"
        ):
            return self.options
        raise AssertionError(f"unexpected selector: {selector}")

    async def wait_for_selector(
        self,
        selector: str,
        *,
        state: str,
        timeout: int,
    ) -> None:
        assert selector == "div[class*='dropdownItem'] span[class*='label']"
        assert (state, timeout) == ("visible", 5000)


def _build_image_platform(page: _ImagePage) -> BaijiahaoPlatform:
    platform = BaijiahaoPlatform()
    platform.page = page
    platform.simulator.random_delay = AsyncMock()
    platform._count_body_images = AsyncMock(side_effect=[0, 1, 1, 1])
    platform._new_body_images_ready = AsyncMock(return_value=True)
    return platform


def test_image_upload_opens_exact_body_modal_and_uses_its_image_input() -> None:
    trigger = _Item()
    image_input = _Item(visible=False)
    confirm = _Item()
    modal = _Modal([image_input], [confirm])
    platform = _build_image_platform(_ImagePage([trigger], [modal]))

    with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(platform._upload_image(r"D:\secret folder\photo.png"))

    assert result == {"success": True, "error": ""}
    trigger.click.assert_awaited_once()
    image_input.set_input_files.assert_awaited_once_with(
        r"D:\secret folder\photo.png", timeout=15000
    )
    confirm.click.assert_awaited_once()


def test_draft_tab_requires_one_visible_selected_tab() -> None:
    tab = _DraftTab()
    platform = BaijiahaoPlatform()
    platform.page = _DraftTabPage([tab])

    asyncio.run(platform._select_draft_tab())

    tab.click.assert_awaited_once_with(timeout=5000)


def test_draft_search_uses_debounced_fill_without_enter() -> None:
    search = _SearchInput()
    platform = BaijiahaoPlatform()
    platform.page = _SearchPage(search)

    with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
        asyncio.run(platform._search_works("唯一标题"))

    search.fill.assert_awaited_once_with("唯一标题")
    search.press.assert_not_awaited()


def test_matching_draft_rows_are_scoped_to_article_item_containers() -> None:
    page = SimpleNamespace(
        evaluate=AsyncMock(
            return_value=[
                {
                    "index": 0,
                    "preview_href": (
                        "https://baijiahao.baidu.com/builder/preview/s?id=123"
                    ),
                }
            ]
        )
    )
    platform = BaijiahaoPlatform()
    platform.page = page

    assert asyncio.run(platform._matching_work_rows("唯一标题")) == [
        {
            "index": 0,
            "preview_href": "https://baijiahao.baidu.com/builder/preview/s?id=123",
        }
    ]
    script = page.evaluate.await_args.args[0]
    assert 'div[class*="articleItem"]' in script
    assert "actions.includes('修改')" in script
    assert "preview_href" in script


def test_unique_draft_uses_preview_id_instead_of_clicking_react_action() -> None:
    platform = BaijiahaoPlatform()
    platform.page = SimpleNamespace()
    platform._open_works_page = AsyncMock()
    platform._search_works = AsyncMock()
    platform._matching_work_rows = AsyncMock(
        return_value=[
            {
                "index": 0,
                "preview_href": (
                    "https://baijiahao.baidu.com/builder/preview/s?id=123456789"
                ),
            }
        ]
    )

    result = asyncio.run(platform._find_unique_exact_draft("唯一标题"))

    assert result == (
        "https://baijiahao.baidu.com/builder/rc/edit?"
        "type=news&article_id=123456789&is_pay_training_camp="
    )
    platform._open_works_page.assert_awaited_once()
    platform._search_works.assert_awaited_once_with("唯一标题")


def test_unique_draft_uses_scoped_modify_when_preview_link_is_absent() -> None:
    platform = BaijiahaoPlatform()
    platform.page = SimpleNamespace()
    platform._open_works_page = AsyncMock()
    platform._search_works = AsyncMock()
    platform._matching_work_rows = AsyncMock(
        return_value=[{"index": 0, "preview_href": ""}]
    )
    platform._open_exact_draft_via_modify = AsyncMock(
        return_value=(
            "https://baijiahao.baidu.com/builder/rc/edit?"
            "type=news&article_id=react-draft"
        )
    )

    result = asyncio.run(platform._find_unique_exact_draft("唯一标题"))

    assert "article_id=react-draft" in result
    platform._open_exact_draft_via_modify.assert_awaited_once_with("唯一标题")


def test_scoped_modify_accepts_only_one_new_same_origin_editor_page() -> None:
    original = SimpleNamespace(
        url="https://baijiahao.baidu.com/builder/rc/content",
        is_closed=lambda: False,
    )
    popup = SimpleNamespace(
        url=(
            "https://baijiahao.baidu.com/builder/rc/edit?"
            "type=news&article_id=react-draft"
        ),
        is_closed=lambda: False,
        close=AsyncMock(),
    )
    context = SimpleNamespace(pages=[original])

    async def click_and_open(*_args):
        context.pages.append(popup)
        return {"status": "CLICKED"}

    original.evaluate = AsyncMock(side_effect=click_and_open)
    platform = BaijiahaoPlatform()
    platform.page = original
    platform.context = context

    result = asyncio.run(platform._open_exact_draft_via_modify("唯一标题"))

    assert "article_id=react-draft" in result
    popup.close.assert_awaited_once()


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/builder/preview/s?id=1",
        "https://baijiahao.baidu.com/builder/preview/s",
        "https://baijiahao.baidu.com/builder/preview/s?id=1&id=2",
        "https://baijiahao.baidu.com/builder/preview/s?id=1&foo=bar",
        "https://baijiahao.baidu.com/builder/preview/s?id=1&token=secret",
        "https://baijiahao.baidu.com/builder/preview/s?id=1#fragment",
    ],
)
def test_preview_href_fails_closed_without_one_safe_same_origin_id(value: str) -> None:
    with pytest.raises(DraftResultUnknownError):
        BaijiahaoPlatform._edit_url_from_preview_href(value)


def test_image_upload_fails_closed_without_exact_body_trigger() -> None:
    platform = _build_image_platform(_ImagePage([], []))

    result = asyncio.run(platform._upload_image(r"D:\secret\photo.png"))

    assert result["success"] is False
    assert result["error_code"] == "BAIJIAHAO_BODY_IMAGE_TRIGGER_AMBIGUOUS"
    assert "D:\\secret" not in result["error"]


def test_image_mapping_never_falls_back_to_first_unrelated_image() -> None:
    block = {"type": "image", "position": 9}
    images = [{"position_index": 1, "local_path": "wrong.png"}]

    assert BaijiahaoPlatform._image_path_for_block(block, images) is None


def test_heading_formats_only_current_ueditor_block_without_opening_menu() -> None:
    editor = _Item()
    editor.evaluate = AsyncMock(return_value=True)
    platform = BaijiahaoPlatform()
    platform.page = object()
    platform._current_body_editor = AsyncMock(return_value=editor)

    asyncio.run(platform._apply_h2_to_current_block())

    platform._current_body_editor.assert_awaited_once()
    editor.evaluate.assert_awaited_once()
    script = editor.evaluate.await_args.args[0]
    assert "block.parentElement !== root" in script
    assert "block.style.fontSize = fontSize" in script
    assert "InputEvent('input'" in script
    assert editor.evaluate.await_args.args[1] == "21px"


def test_heading_fails_closed_when_current_root_block_is_not_identifiable() -> None:
    editor = _Item()
    editor.evaluate = AsyncMock(return_value=False)
    platform = BaijiahaoPlatform()
    platform.page = object()
    platform._current_body_editor = AsyncMock(return_value=editor)

    with pytest.raises(ContentValidationError, match="BAIJIAHAO_HEADING_APPLY_FAILED"):
        asyncio.run(platform._apply_h2_to_current_block())


def test_body_style_reset_removes_inherited_heading_size() -> None:
    editor = _Item()
    editor.evaluate = AsyncMock(return_value=True)
    platform = BaijiahaoPlatform()
    platform.page = object()
    platform._current_body_editor = AsyncMock(return_value=editor)

    asyncio.run(platform._apply_body_to_current_block())

    script, font_size = editor.evaluate.await_args.args
    assert font_size is None
    assert "block.style.removeProperty('font-size')" in script
    assert "child.style.removeProperty('font-size')" in script


def test_appinfo_identity_requires_matching_stable_ids_and_name() -> None:
    payload = {
        "data": {
            "user": {
                "id": "stable-5072",
                "userid": "stable-5072",
                "name": "真实昵称",
            }
        }
    }

    assert BaijiahaoPlatform._extract_appinfo_identity(payload) == (
        "stable-5072",
        "真实昵称",
    )


@pytest.mark.parametrize(
    "user",
    [
        {"id": "one", "userid": "two", "name": "昵称"},
        {"id": "one", "userid": "one", "name": ""},
        {"id": "", "userid": "", "name": "昵称"},
    ],
)
def test_appinfo_identity_fails_closed_on_inconsistent_payload(user: dict) -> None:
    assert BaijiahaoPlatform._extract_appinfo_identity({"data": {"user": user}}) is None


def test_expected_tokens_preserve_word_text_heading_image_order() -> None:
    tokens = BaijiahaoPlatform._expected_content_tokens(
        [
            {"type": "text", "text": "第一段\n第二段"},
            {"type": "image", "position": 2},
            {"type": "heading", "level": 2, "text": "小标题"},
        ]
    )

    assert tokens == [
        {"kind": "text", "text": "第一段"},
        {"kind": "text", "text": "第二段"},
        {"kind": "image"},
        {"kind": "heading", "level": 2, "text": "小标题"},
    ]


def test_dom_reader_ignores_baijiahao_image_caption_and_blank_sentinels() -> None:
    editor = _Item()
    editor.evaluate = AsyncMock(return_value=[])
    platform = BaijiahaoPlatform()
    platform.page = object()
    platform._current_body_editor = AsyncMock(return_value=editor)

    assert asyncio.run(platform._read_editor_dom_tokens()) == []

    script = editor.evaluate.await_args.args[0]
    assert "className.includes('bjh-image-caption')" in script
    assert "[\\s\\p{M}\\p{Cf}\\p{Cc}\\uFFFC]" in script
    assert "node.nextElementSibling?.querySelector('img')" in script
    assert "range.selectNodeContents(node)" in script
    assert "shortTextLength <= 2 && renderedWidth < 0.5" in script
    assert "|| zeroWidthParagraph" in script


def test_dom_reader_removes_ueditor_object_placeholder_from_text_only() -> None:
    editor = _Item()
    editor.evaluate = AsyncMock(
        return_value=[
            {"kind": "text", "level": 0, "text": "冻结正文\uFFFC"},
            {"kind": "image"},
        ]
    )
    platform = BaijiahaoPlatform()
    platform.page = object()
    platform._current_body_editor = AsyncMock(return_value=editor)

    result = asyncio.run(platform._read_editor_dom_tokens())

    assert result == [
        {"kind": "text", "text": "冻结正文"},
        {"kind": "image"},
    ]


def test_current_body_editor_reacquires_after_transient_iframe_rebuild() -> None:
    body = _Item()
    body.count = AsyncMock(return_value=1)
    platform = BaijiahaoPlatform()
    platform.page = object()
    platform._body_editor_locator = AsyncMock(side_effect=[None, body])

    with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(platform._current_body_editor(timeout_seconds=1))

    assert result is body
    assert platform._body_editor_locator.await_count == 2


def test_preflight_rejects_existing_exact_title_before_editor_side_effect() -> None:
    platform = BaijiahaoPlatform()
    platform.page = object()
    platform._open_works_page = AsyncMock()
    platform._search_works = AsyncMock()
    platform._matching_work_rows = AsyncMock(return_value=[{"index": 0}])

    with pytest.raises(DraftBaselineError, match="已存在同名内容"):
        asyncio.run(platform.preflight_delivery(" 唯一标题 "))

    assert platform._preflight_title == ""


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/builder/rc/edit?id=1",
        "https://baijiahao.baidu.com/builder/rc/edit?token=secret",
        "https://user:pass@baijiahao.baidu.com/builder/rc/edit?id=1",
    ],
)
def test_draft_url_rejects_wrong_origin_and_sensitive_parameters(value: str) -> None:
    with pytest.raises(DraftResultUnknownError):
        BaijiahaoPlatform._safe_draft_url(value)


def test_draft_url_keeps_only_valid_same_origin_editor_url() -> None:
    assert BaijiahaoPlatform._safe_draft_url(
        "https://baijiahao.baidu.com/builder/rc/edit?type=news&id=123#ignored"
    ) == "https://baijiahao.baidu.com/builder/rc/edit?type=news&id=123"


def test_save_response_observer_records_only_same_origin_post_paths() -> None:
    captured = BaijiahaoPlatform._safe_save_response_evidence(
        SimpleNamespace(
            url="https://baijiahao.baidu.com/builder/api/draft/save?token=secret",
            status=200,
            request=SimpleNamespace(method="POST"),
        )
    )
    rejected = BaijiahaoPlatform._safe_save_response_evidence(
        SimpleNamespace(
            url="https://evil.example/upload?token=secret",
            status=200,
            request=SimpleNamespace(method="POST"),
        )
    )

    assert captured == {"path": "/builder/api/draft/save", "status": 200}
    assert rejected is None


def test_save_response_payload_exposes_only_business_code_and_structure() -> None:
    result = BaijiahaoPlatform._safe_save_response_payload(
        {
            "errno": 0,
            "errmsg": "保存成功",
            "token": "must-not-leak",
            "data": {
                "nid": "123456789",
                "title": "must-not-leak",
                "content": "must-not-leak",
            },
        }
    )

    assert result == {
        "top_level_keys": ["data", "errmsg", "errno", "token"],
        "errno": 0,
        "errmsg": "保存成功",
        "data_keys": ["content", "nid", "title"],
        "candidate_ids": [{"key": "nid", "length": 9}],
    }
    assert "must-not-leak" not in str(result)
