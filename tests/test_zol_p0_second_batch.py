"""ZOL 第二批草稿与正文证据契约的离线测试。

本文件只测试解析器、稳定错误语义和选择器边界，不启动浏览器，
不访问真实平台，也不触碰 Cookie、Profile 或仓库运行数据。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from PIL import Image, ImageDraw

from platforms.base import DraftBaselineError, DraftResultUnknownError, SelectorError
from platforms.content_validation import ContentValidationError
from platforms.zol import ZOLPlatform, _ZOLDraftSnapshot
from tests.test_regression import FakeLocator


def _payload(items: list[dict], total: int | str | None = None) -> dict:
    return {
        "errcode": 0,
        "data": {
            "list": items,
            "totalNum": len(items) if total is None else total,
        },
    }


def _snapshot(total: int, *pairs: tuple[str, str]) -> _ZOLDraftSnapshot:
    return _ZOLDraftSnapshot(
        total_num=total,
        draft_ids=frozenset(draft_id for draft_id, _ in pairs),
        title_to_ids={
            title: frozenset(
                draft_id for draft_id, candidate in pairs if candidate == title
            )
            for title in {title for _, title in pairs}
        },
    )


def test_draft_list_empty_success_is_a_reliable_baseline() -> None:
    snapshot = ZOLPlatform._parse_draft_list_payload(_payload([], total=0))

    assert snapshot.total_num == 0
    assert snapshot.draft_ids == frozenset()
    assert snapshot.title_to_ids == {}


def test_draft_list_accepts_observed_decimal_string_total() -> None:
    snapshot = ZOLPlatform._parse_draft_list_payload(
        _payload([{"draftId": "one", "title": "草稿"}], total="1")
    )

    assert snapshot.total_num == 1
    assert snapshot.title_to_ids == {"草稿": frozenset({"one"})}


def test_draft_list_errcode_failure_is_not_an_empty_baseline() -> None:
    with pytest.raises(DraftBaselineError, match="DRAFT_BASELINE_UNAVAILABLE"):
        ZOLPlatform._parse_draft_list_payload(
            {"errcode": 17, "data": {"list": [], "totalNum": 0}}
        )


def test_draft_list_requires_stable_id_per_entity() -> None:
    with pytest.raises(DraftBaselineError, match="顶层 draftId"):
        ZOLPlatform._parse_draft_list_payload(_payload([{"title": "无 ID"}]))


@pytest.mark.parametrize(
    "item, message",
    [
        ({"meta": {"draftId": "nested"}, "title": "嵌套 ID"}, "顶层 draftId"),
        ({"draftId": "id", "meta": {"title": "嵌套标题"}}, "顶层 title"),
    ],
)
def test_draft_list_does_not_accept_nested_identity_fields(item, message) -> None:
    with pytest.raises(DraftBaselineError, match=message):
        ZOLPlatform._parse_draft_list_payload(_payload([item]))


def test_new_draft_requires_total_increment_and_unique_exact_title_entity() -> None:
    before = _snapshot(1, ("old", "同名草稿"))
    same_title = _snapshot(
        2,
        ("old", "同名草稿"),
        ("new", "同名草稿"),
    )
    unchanged_total = _snapshot(1, ("old", "同名草稿"))
    exact_new = _snapshot(2, ("old", "旧草稿"), ("new", "新草稿"))

    assert ZOLPlatform._new_draft_id_for_title(before, same_title, "同名草稿") is None
    assert ZOLPlatform._new_draft_id_for_title(
        before, unchanged_total, "同名草稿"
    ) is None
    assert ZOLPlatform._new_draft_id_for_title(before, exact_new, "新草稿") == "new"


def test_zol_preflight_runs_before_auto_saving_editor_navigation() -> None:
    platform = ZOLPlatform()
    draft_page = AsyncMock()
    platform.page = SimpleNamespace(on=Mock(), remove_listener=Mock())
    platform._prepare_draft_verification_page = AsyncMock(return_value=draft_page)

    asyncio.run(platform.preflight_delivery("  唯一标题  "))

    platform._prepare_draft_verification_page.assert_awaited_once_with("唯一标题")
    platform.page.on.assert_called_once_with("response", platform._autosave_listener)
    draft_page.close.assert_awaited_once()


def test_media_binding_saves_once_then_reopens_exact_draft_id() -> None:
    platform = ZOLPlatform()
    platform._current_title = "绑定草稿"
    editor = FakeLocator(tag="body")
    control = FakeLocator(count=1, visible=True)
    platform.page = SimpleNamespace(
        locator=Mock(return_value=control),
        goto=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    platform._resolve_content_editor = AsyncMock(
        return_value=(editor, "contenteditable")
    )
    platform._dismiss_editor_overlays = AsyncMock()
    platform._commit_editor_dom_change = AsyncMock()
    platform._collect_draft_save_response = AsyncMock(return_value="bound-id")
    platform._editor_probe_count = AsyncMock(return_value=1)
    platform._read_content_editor_text = AsyncMock(return_value="第一段\n\n第二段")
    platform._autosave_responses = [object()]

    asyncio.run(platform._bind_draft_before_media("第一段\n\n第二段"))

    assert platform._bound_draft_id == "bound-id"
    assert platform._autosave_responses == []
    platform._collect_draft_save_response.assert_awaited_once_with(control)
    editor_url = platform.page.goto.await_args.args[0]
    assert "draftId=bound-id" in editor_url
    assert "businessType=1" in editor_url
    assert "editType=1" in editor_url
    assert "isSecond=0" in editor_url


def test_bound_image_autosaves_must_all_match_single_draft_id() -> None:
    platform = ZOLPlatform()
    platform._bound_draft_id = "bound-id"
    platform.DRAFT_RESPONSE_WAIT_SECONDS = 0
    platform._autosave_responses = [
        _SaveResponse({"errcode": 0, "data": {"draftId": "bound-id"}}),
        _SaveResponse({"errcode": 0, "data": {"draftId": "bound-id"}}),
    ]

    asyncio.run(platform._wait_for_bound_autosave(0))


def test_bound_image_autosave_rejects_new_draft_entity() -> None:
    platform = ZOLPlatform()
    platform._bound_draft_id = "bound-id"
    platform.DRAFT_RESPONSE_WAIT_SECONDS = 0
    platform._autosave_responses = [
        _SaveResponse({"errcode": 0, "data": {"draftId": "other-id"}})
    ]

    with pytest.raises(DraftResultUnknownError, match="未绑定草稿实体"):
        asyncio.run(platform._wait_for_bound_autosave(0))


class _SaveControl:
    def __init__(self, page) -> None:
        self.page = page
        self.click_count = 0

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def click(self, **_kwargs) -> None:
        self.click_count += 1
        for response in self.page.responses:
            for callback in list(self.page.response_callbacks):
                callback(response)


class _SaveResponse:
    def __init__(
        self,
        payload: object,
        *,
        status: int = 200,
        method: str = "POST",
        url: str = "https://post.zol.com.cn/api/v1/creator.content.draft.save.orther?trace=hidden",
    ) -> None:
        self.payload = payload
        self.status = status
        self.url = url
        self.request = SimpleNamespace(method=method)

    async def json(self):
        return self.payload


class _SaveCard(FakeLocator):
    def __init__(self, text: str, attributes: dict | None = None) -> None:
        super().__init__(tag="article", text=text, attributes=attributes)


class _CardCollection:
    def __init__(self, cards: list[_SaveCard]) -> None:
        self.cards = cards

    async def count(self) -> int:
        return len(self.cards)

    def nth(self, index: int):
        return self.cards[index]


class _DraftVerificationPage:
    def __init__(self, card_states: list[list[_SaveCard]]) -> None:
        self.card_states = list(card_states)
        self.cards = []
        self.closed = False
        self.goto_calls = []

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        if self.card_states:
            self.cards = self.card_states.pop(0)

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    async def wait_for_timeout(self, _timeout: int) -> None:
        return None

    def locator(self, selector: str):
        if selector == ".article-card":
            return _CardCollection(self.cards)
        return _CardCollection([])

    async def close(self) -> None:
        self.closed = True


class _SaveContext:
    def __init__(self, draft_page: _DraftVerificationPage) -> None:
        self.draft_page = draft_page
        self.new_page_count = 0

    async def new_page(self):
        self.new_page_count += 1
        return self.draft_page


class _SavePage:
    url = "https://post.zol.com.cn/v2/create/article"

    def __init__(self, responses: list[_SaveResponse], draft_page: _DraftVerificationPage) -> None:
        self.responses = responses
        self.response_callbacks = []
        self.context = _SaveContext(draft_page)
        self.control = _SaveControl(self)

    async def goto(self, _url: str, **_kwargs) -> None:
        return None

    def locator(self, selector: str):
        assert selector == ZOLPlatform.DRAFT_SAVE_SELECTOR
        return self.control

    def is_closed(self) -> bool:
        return False

    def on(self, event: str, callback) -> None:
        assert event == "response"
        self.response_callbacks.append(callback)

    def remove_listener(self, event: str, callback) -> None:
        assert event == "response"
        if callback in self.response_callbacks:
            self.response_callbacks.remove(callback)


def _save_platform(
    *,
    responses: list[_SaveResponse],
    card_states: list[list[_SaveCard]],
) -> tuple[ZOLPlatform, _DraftVerificationPage]:
    draft_page = _DraftVerificationPage(card_states)
    platform = ZOLPlatform()
    platform.page = _SavePage(responses, draft_page)
    platform.context = platform.page.context
    platform.simulator.random_delay = AsyncMock()
    platform.DRAFT_RESPONSE_WAIT_SECONDS = 0
    platform.DRAFT_CARD_POLL_DELAYS = (0, 0)
    return platform, draft_page


def test_navigate_to_editor_does_not_require_draft_list_baseline() -> None:
    platform, _draft_page = _save_platform(responses=[], card_states=[[]])
    platform._editor_probe_count = AsyncMock(return_value=1)
    platform._safe_simulate_scroll = AsyncMock()
    platform._fetch_draft_snapshot = AsyncMock(
        side_effect=AssertionError("进入编辑器不应读取草稿列表")
    )

    asyncio.run(platform.navigate_to_editor())

    platform._fetch_draft_snapshot.assert_not_awaited()
    platform._editor_probe_count.assert_awaited_once()


@pytest.mark.parametrize(
    "method,url,expected",
    [
        (
            "POST",
            "https://open-api.zol.com.cn/api/v1/creator.content.draft.save.orther",
            True,
        ),
        (
            "GET",
            "https://open-api.zol.com.cn/api/v1/creator.content.draft.save.orther",
            False,
        ),
        (
            "POST",
            "https://analytics.zol.com.cn/api/v1/creator.content.draft.save.orther",
            False,
        ),
        ("POST", "https://post.zol.com.cn/api/v1/creator.content.image.upload", False),
        (
            "POST",
            "https://post.zol.com.cn/api/v1/creator.content.Materials.uploadPic",
            False,
        ),
        (
            "POST",
            "https://post.zol.com.cn/api/v1/creator.content.Materials.bindDraftIdToPicMaterial",
            False,
        ),
        ("POST", "https://post.zol.com.cn/api/v1/creator.content.draft.getlist", False),
        ("POST", "https://post.zol.com.cn/api/v1/creator.content.publish", False),
    ],
)
def test_draft_save_response_filter_is_business_context_only(method, url, expected) -> None:
    response = SimpleNamespace(
        request=SimpleNamespace(method=method),
        url=url,
    )
    assert ZOLPlatform._is_draft_save_response(response) is expected


def test_save_response_requires_success_and_allowlisted_data_id() -> None:
    response = _SaveResponse({"errcode": 0, "data": {"draftId": "new-id"}})

    assert asyncio.run(ZOLPlatform._parse_draft_save_response(response)) == "new-id"
    assert asyncio.run(
        ZOLPlatform._parse_draft_save_response(
            _SaveResponse({"code": 200, "data": {"contentId": 42}})
        )
    ) == "42"


@pytest.mark.parametrize(
    "response",
    [
        _SaveResponse({"errcode": 0, "data": {}}),
        _SaveResponse({"errcode": 0, "data": {"draftId": "a", "id": "b"}}),
        _SaveResponse({"errcode": 1, "data": {"draftId": "new-id"}}),
        _SaveResponse({"errcode": 0, "data": {"draftId": "new-id"}}, status=500),
        _SaveResponse({"errcode": 0, "data": {"draft": {"draftId": "nested"}}}),
    ],
)
def test_save_response_missing_ambiguous_or_failed_proof_is_unknown(response) -> None:
    with pytest.raises(DraftResultUnknownError, match="DRAFT_RESULT_UNKNOWN"):
        asyncio.run(ZOLPlatform._parse_draft_save_response(response))


def test_save_draft_uses_exact_control_once_and_proves_new_entity() -> None:
    platform, draft_page = _save_platform(
        responses=[
            _SaveResponse(
                {"errcode": 0, "data": {"draftId": "new-id"}},
                url="https://open-api.zol.com.cn/api/v1/creator.content.draft.save.orther",
            )
        ],
        card_states=[[], [_SaveCard("新草稿", {"data-draft-id": "new-id"})]],
    )

    result = asyncio.run(platform.save_draft("新草稿"))

    assert result.endswith("/v2/manage/works/draft")
    assert platform.page.control.click_count == 1
    assert platform.context.new_page_count == 1
    assert draft_page.closed is True


def test_save_draft_accepts_unique_title_card_without_card_id() -> None:
    platform, _draft_page = _save_platform(
        responses=[_SaveResponse({"code": 0, "data": {"articleId": "article-1"}})],
        card_states=[[], [_SaveCard("新草稿"),]],
    )

    assert asyncio.run(platform.save_draft("新草稿")).endswith("/draft")


def test_save_draft_binds_matching_id_among_same_title_cards() -> None:
    platform, _draft_page = _save_platform(
        responses=[_SaveResponse({"errcode": 0, "data": {"draftId": "new-id"}})],
        card_states=[
            [],
            [
                _SaveCard("同名草稿", {"data-draft-id": "old-id"}),
                _SaveCard("同名草稿", {"data-draft-id": "new-id"}),
            ],
        ],
    )

    assert asyncio.run(platform.save_draft("同名草稿")).endswith("/draft")
    evidence = platform._last_draft_evidence.to_dict()
    assert evidence["draft_list_match_count"] == 2
    assert evidence["draft_entity_bound"] is True
    assert evidence["draft_entity_id_match"] is True


def test_save_draft_rejects_duplicate_title_cards() -> None:
    platform, draft_page = _save_platform(
        responses=[_SaveResponse({"errcode": 0, "data": {"draftId": "new-id"}})],
        card_states=[[], [_SaveCard("新草稿"), _SaveCard("新草稿")]],
    )

    with pytest.raises(DraftResultUnknownError, match="DRAFT_RESULT_UNKNOWN"):
        asyncio.run(platform.save_draft("新草稿"))

    assert platform.page.control.click_count == 1
    assert draft_page.closed is True


def test_save_draft_rejects_missing_card_and_mismatched_card_id() -> None:
    for after_cards in (
        [],
        [_SaveCard("另一篇", {"data-draft-id": "new-id"})],
        [_SaveCard("新草稿", {"data-draft-id": "other-id"})],
    ):
        platform, draft_page = _save_platform(
            responses=[_SaveResponse({"errcode": 0, "data": {"draftId": "new-id"}})],
            card_states=[[], after_cards, after_cards],
        )
        with pytest.raises(DraftResultUnknownError, match="DRAFT_RESULT_UNKNOWN"):
            asyncio.run(platform.save_draft("新草稿"))
        evidence = platform._last_draft_evidence.to_dict()
        if after_cards and after_cards[0].attributes.get("data-draft-id") == "other-id":
            assert evidence["draft_entity_bound"] is False
            assert evidence["draft_entity_id_match"] is False
        assert platform.page.control.click_count == 1
        assert draft_page.closed is True


def test_save_click_without_response_is_unknown_and_never_retries() -> None:
    platform, draft_page = _save_platform(responses=[], card_states=[[], []])

    with pytest.raises(DraftResultUnknownError, match="DRAFT_RESULT_UNKNOWN"):
        asyncio.run(platform.save_draft("未证明草稿"))

    assert platform.page.control.click_count == 1
    assert platform.context.new_page_count == 1
    assert draft_page.closed is True


def test_save_verification_page_is_closed_after_response_proof_failure() -> None:
    platform, draft_page = _save_platform(
        responses=[_SaveResponse({"errcode": 0, "data": {"draftId": "new-id"}})],
        card_states=[[], [_SaveCard("新草稿", {"data-draft-id": "other-id"})]],
    )

    with pytest.raises(DraftResultUnknownError, match="DRAFT_RESULT_UNKNOWN"):
        asyncio.run(platform.save_draft("新草稿"))

    assert draft_page.closed is True


def test_existing_same_title_after_editor_entry_is_unknown_and_never_clicked() -> None:
    platform, draft_page = _save_platform(
        responses=[_SaveResponse({"errcode": 0, "data": {"draftId": "new-id"}})],
        card_states=[[_SaveCard("新草稿")]],
    )

    with pytest.raises(DraftResultUnknownError, match="DRAFT_RESULT_UNKNOWN"):
        asyncio.run(platform.save_draft("新草稿"))

    assert platform.page.control.click_count == 0
    assert draft_page.closed is True


def test_autosave_same_entity_multiple_times_is_verified_without_click() -> None:
    platform, draft_page = _save_platform(
        responses=[],
        card_states=[
            [_SaveCard("自动保存草稿", {"data-draft-id": "same-id"})],
            [_SaveCard("自动保存草稿", {"data-draft-id": "same-id"})],
        ],
    )
    platform._autosave_responses = [
        _SaveResponse({"errcode": 0, "data": {"draftId": "same-id"}}),
        _SaveResponse({"errcode": 0, "data": {"draftId": "same-id"}}),
    ]

    result = asyncio.run(platform.save_draft("自动保存草稿"))

    assert result.endswith("/draft")
    assert platform.page.control.click_count == 0
    assert draft_page.closed is True


def test_autosave_multiple_entities_is_unknown_and_never_clicked() -> None:
    platform, draft_page = _save_platform(responses=[], card_states=[])
    platform._autosave_responses = [
        _SaveResponse({"errcode": 0, "data": {"draftId": "first-id"}}),
        _SaveResponse({"errcode": 0, "data": {"draftId": "second-id"}}),
    ]

    with pytest.raises(DraftResultUnknownError, match="多个草稿实体"):
        asyncio.run(platform.save_draft("重复自动保存"))

    assert platform.page.control.click_count == 0
    assert platform.context.new_page_count == 0
    assert draft_page.closed is False


def test_autosave_proof_does_not_require_visible_manual_save_control() -> None:
    platform, _draft_page = _save_platform(
        responses=[],
        card_states=[
            [_SaveCard("自动保存草稿", {"data-draft-id": "auto-id"})],
            [_SaveCard("自动保存草稿", {"data-draft-id": "auto-id"})],
        ],
    )
    platform.page.control = FakeLocator(count=0, visible=False)
    platform._autosave_responses = [
        _SaveResponse({"errcode": 0, "data": {"draftId": "auto-id"}})
    ]

    assert asyncio.run(platform.save_draft("自动保存草稿")).endswith("/draft")


def test_preview_response_is_ignored_before_unique_save_response() -> None:
    platform, _draft_page = _save_platform(
        responses=[
            _SaveResponse(
                {"errcode": 0, "data": {"id": "preview-id"}},
                url="https://open-api.zol.com.cn/api/v1/creator.content.preview",
            ),
            _SaveResponse(
                {"errcode": 0, "data": {"draftId": "new-id"}},
                url="https://open-api.zol.com.cn/api/v1/creator.content.draft.save.orther",
            ),
        ],
        card_states=[[], [_SaveCard("新草稿", {"data-draft-id": "new-id"})]],
    )

    assert asyncio.run(platform.save_draft("新草稿")).endswith("/draft")


def test_preview_wap_response_is_ignored_before_unique_save_response() -> None:
    platform, _draft_page = _save_platform(
        responses=[
            _SaveResponse(
                {"errcode": 0, "data": {"id": "preview-id"}},
                url="https://open-api.zol.com.cn/api/v1/creator.content.previewWap",
            ),
            _SaveResponse(
                {"errcode": 0, "data": {"draftId": "new-id"}},
                url="https://open-api.zol.com.cn/api/v1/creator.content.draft.save.orther",
            ),
        ],
        card_states=[[], [_SaveCard("新草稿", {"data-draft-id": "new-id"})]],
    )

    assert asyncio.run(platform.save_draft("新草稿")).endswith("/draft")


def test_title_match_requires_an_exact_nonempty_card_line() -> None:
    platform, _draft_page = _save_platform(
        responses=[_SaveResponse({"errcode": 0, "data": {"draftId": "new-id"}})],
        card_states=[[], [_SaveCard("新草稿 - 副本")]],
    )

    with pytest.raises(DraftResultUnknownError, match="DRAFT_RESULT_UNKNOWN"):
        asyncio.run(platform.save_draft("新草稿"))


def test_multiple_successful_save_responses_are_ambiguous() -> None:
    platform, _draft_page = _save_platform(
        responses=[
            _SaveResponse({"errcode": 0, "data": {"draftId": "first"}}),
            _SaveResponse({"errcode": 0, "data": {"draftId": "second"}}),
        ],
        card_states=[[], [_SaveCard("新草稿")]],
    )

    with pytest.raises(DraftResultUnknownError, match="DRAFT_RESULT_UNKNOWN"):
        asyncio.run(platform.save_draft("新草稿"))


def test_bound_draft_always_gets_one_final_explicit_save_on_exact_editor_id() -> None:
    platform, draft_page = _save_platform(
        responses=[_SaveResponse({"errcode": 0, "data": {"draftId": "bound-id"}})],
        card_states=[[_SaveCard("完整草稿", {"data-draft-id": "bound-id"})]],
    )
    platform.page.url = (
        "https://post.zol.com.cn/v2/create/article?draftId=bound-id&businessType=1"
    )
    platform._bound_draft_id = "bound-id"
    platform._autosave_responses = [
        _SaveResponse({"errcode": 0, "data": {"draftId": "bound-id"}})
    ]
    platform._final_content_response_index = len(platform._autosave_responses)

    assert asyncio.run(platform.save_draft("完整草稿")).endswith("/draft")
    assert platform.page.control.click_count == 1
    assert draft_page.closed is True


def test_bound_draft_final_save_rejects_mismatched_response_id() -> None:
    platform, _draft_page = _save_platform(
        responses=[_SaveResponse({"errcode": 0, "data": {"draftId": "other-id"}})],
        card_states=[],
    )
    platform.page.url = (
        "https://post.zol.com.cn/v2/create/article?draftId=bound-id&businessType=1"
    )
    platform._bound_draft_id = "bound-id"

    with pytest.raises(DraftResultUnknownError, match="最终保存响应与绑定草稿不一致"):
        asyncio.run(platform.save_draft("完整草稿"))
    assert platform.page.control.click_count == 1


def test_persisted_content_reopen_requires_full_ordered_tail_and_images() -> None:
    platform = ZOLPlatform()
    platform._expected_persisted_blocks = [
        {"type": "text", "text": "图片前"},
        {"type": "image", "position": 1},
        {"type": "heading", "level": 2, "text": "章节标题"},
        {"type": "text", "text": "图片后尾段"},
    ]
    platform.DRAFT_CONTENT_POLL_DELAYS = (0,)
    platform.page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock())
    platform._editor_probe_count = AsyncMock(return_value=1)
    platform._resolve_content_editor = AsyncMock(
        return_value=(FakeLocator(tag="body"), "iframe")
    )
    platform._read_editor_dom_tokens = AsyncMock(
        return_value=[
            {"kind": "text", "text": "图片前"},
            {"kind": "image", "fingerprint": "persisted"},
            {"kind": "heading", "tag": "h2", "text": "章节标题"},
            {"kind": "text", "text": "图片后尾段"},
        ]
    )

    asyncio.run(platform._verify_persisted_draft_content("bound-id"))

    platform._read_editor_dom_tokens = AsyncMock(
        return_value=[
            {"kind": "text", "text": "图片前"},
            {"kind": "image", "fingerprint": "persisted"},
            {"kind": "heading", "tag": "h2", "text": "章节标题"},
        ]
    )
    with pytest.raises(DraftResultUnknownError, match="重开后图文结构不完整"):
        asyncio.run(platform._verify_persisted_draft_content("bound-id"))


class _Modal:
    def __init__(self, inputs: list[FakeLocator]) -> None:
        self.inputs = inputs

    async def is_visible(self) -> bool:
        return True

    def locator(self, selector: str):
        assert selector == ZOLPlatform.IMAGE_INPUT
        return _InputCollection(
            [
                item
                for item in self.inputs
                if item.attributes.get("accept") == "image/*"
                and item.attributes.get("multiple") is not None
                and item.attributes.get("ancestor_role") == "button"
                and {"local_upload", "ant-upload"}.issubset(
                    set(item.attributes.get("ancestor_classes", ()))
                )
            ]
        )


class _InputCollection:
    def __init__(self, inputs: list[FakeLocator]) -> None:
        self.inputs = inputs

    @property
    def first(self):
        return self.inputs[0]

    async def count(self) -> int:
        return len(self.inputs)


def test_body_image_input_ignores_cover_input_and_requires_unique_multiple_input() -> None:
    cover = FakeLocator(
        count=1,
        visible=False,
        attributes={
            "accept": "image/*",
            "multiple": "",
            "ancestor_role": "button",
            "ancestor_classes": ("cover_upload", "ant-upload"),
        },
    )
    body = FakeLocator(
        count=1,
        visible=False,
        attributes={
            "accept": "image/*",
            "multiple": "",
            "ancestor_role": "button",
            "ancestor_classes": ("local_upload", "ant-upload"),
        },
    )
    video = FakeLocator(
        count=1,
        visible=False,
        attributes={
            "accept": "video/*",
            "multiple": "",
            "ancestor_role": "button",
            "ancestor_classes": ("local_upload", "ant-upload"),
        },
    )
    legacy_image_accept = FakeLocator(
        count=1,
        visible=False,
        attributes={
            "accept": "image",
            "multiple": "",
            "ancestor_role": "button",
            "ancestor_classes": ("local_upload", "ant-upload"),
        },
    )
    without_multiple = FakeLocator(
        count=1,
        visible=False,
        attributes={
            "accept": "image/*",
            "multiple": None,
            "ancestor_role": "button",
            "ancestor_classes": ("local_upload", "ant-upload"),
        },
    )
    wrong_ancestor = FakeLocator(
        count=1,
        visible=False,
        attributes={
            "accept": "image/*",
            "multiple": "",
            "ancestor_role": "button",
            "ancestor_classes": ("cover_upload", "ant-upload"),
        },
    )
    wrong_role = FakeLocator(
        count=1,
        visible=False,
        attributes={
            "accept": "image/*",
            "multiple": "",
            "ancestor_role": None,
            "ancestor_classes": ("local_upload", "ant-upload"),
        },
    )
    platform = ZOLPlatform()

    assert asyncio.run(platform._resolve_body_image_input(_Modal([body]))) is body
    assert asyncio.run(platform._resolve_body_image_input(_Modal([cover, body]))) is body
    assert asyncio.run(platform._resolve_body_image_input(_Modal([cover]))) is None
    assert asyncio.run(platform._resolve_body_image_input(_Modal([video]))) is None
    assert asyncio.run(platform._resolve_body_image_input(_Modal([legacy_image_accept]))) is None
    assert asyncio.run(platform._resolve_body_image_input(_Modal([without_multiple]))) is None
    assert asyncio.run(platform._resolve_body_image_input(_Modal([wrong_ancestor]))) is None
    assert asyncio.run(platform._resolve_body_image_input(_Modal([wrong_role]))) is None
    assert (
        asyncio.run(platform._resolve_body_image_input(_Modal([body, body]))) is None
    )


def test_upload_image_sets_files_once_on_unique_body_input(tmp_path) -> None:
    class _FileInput(FakeLocator):
        def __init__(self) -> None:
            super().__init__(
                count=1,
                visible=False,
                attributes={
                    "accept": "image/*",
                    "multiple": "",
                    "ancestor_role": "button",
                    "ancestor_classes": ("local_upload", "ant-upload"),
                },
            )
            self.set_calls: list[str] = []

        async def set_input_files(self, path: str) -> None:
            self.set_calls.append(str(path))

    class _InsertButton(FakeLocator):
        async def is_enabled(self) -> bool:
            return True

    class _UploadModal(_Modal):
        def __init__(self, file_input, insert_button) -> None:
            super().__init__([file_input])
            self.insert_button = insert_button

        async def wait_for(self, **_kwargs) -> None:
            return None

        def get_by_role(self, _role=None, **_kwargs):
            return self.insert_button

    class _LocatorProxy:
        def __init__(self, target) -> None:
            self.first = target
            self.last = target

    class _Page:
        def __init__(self, modal, button) -> None:
            self.modal = modal
            self.button = button

        def locator(self, selector: str):
            if selector == ZOLPlatform.IMAGE_BUTTON:
                return _LocatorProxy(self.button)
            if selector == ZOLPlatform.IMAGE_MODAL:
                return _LocatorProxy(self.modal)
            raise AssertionError(f"unexpected selector: {selector}")

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    file_input = _FileInput()
    insert_button = _InsertButton(count=1, visible=True)
    modal = _UploadModal(file_input, insert_button)
    upload_button = FakeLocator(count=1, visible=True)
    page = _Page(modal, upload_button)
    image_path = tmp_path / "body.png"
    image_path.write_bytes(b"fixture")

    platform = ZOLPlatform()
    platform.page = page
    platform._close_image_modal = AsyncMock()
    platform._editor_image_src_fingerprints = AsyncMock(
        side_effect=[[], ["new-image"]]
    )
    platform._verify_image_content = AsyncMock(return_value={"success": True})

    result = asyncio.run(platform._upload_image(str(image_path)))

    assert result["success"] is True
    assert file_input.set_calls == [str(image_path.resolve())]
    assert upload_button.click_count == 1
    assert insert_button.click_count == 1


def test_modal_close_failure_is_not_silently_ignored() -> None:
    class _UnclosableModal:
        async def count(self) -> int:
            return 1

        async def is_visible(self) -> bool:
            return True

        def locator(self, _selector):
            return FakeLocator(count=0)

        def get_by_role(self, **_kwargs):
            return FakeLocator(count=0)

    class _Page:
        def locator(self, _selector):
            return _UnclosableModal()

    platform = ZOLPlatform()
    platform.page = _Page()

    with pytest.raises(SelectorError, match="ZOL_IMAGE_MODAL_CLOSE_FAILED"):
        asyncio.run(platform._close_image_modal())


def test_repeated_same_source_image_is_still_identified_as_one_new_image() -> None:
    assert ZOLPlatform._new_image_fingerprint(["same"], ["same", "same"]) == "same"
    assert ZOLPlatform._new_image_fingerprint(
        ["same", "same"], ["same", "same", "other"]
    ) == "other"
    assert ZOLPlatform._new_image_fingerprint(["same"], ["same"]) is None


def test_first_image_prefix_mismatch_stops_before_second_upload() -> None:
    platform = ZOLPlatform()
    from tests.test_regression import FakePage

    platform.page = FakePage("contenteditable")
    platform.simulator.random_delay = AsyncMock()
    platform._bind_draft_before_media = AsyncMock()
    platform._bound_draft_id = "bound-id"
    platform._wait_for_bound_autosave = AsyncMock()
    platform._collapse_editor_selection_at_end = AsyncMock()
    platform._remove_delayed_duplicate_images = AsyncMock()
    platform._upload_image = AsyncMock(
        return_value={
            "success": True,
            "filename": "first.png",
            "image_src_fingerprint": "first-fingerprint",
        }
    )
    platform._verify_content_prefix = AsyncMock(
        side_effect=ContentValidationError(
            "ZOL_CONTENT_PREFIX_VERIFY_FAILED: DOM 中段被删除"
        )
    )

    with pytest.raises(ContentValidationError, match="ZOL_CONTENT_PREFIX_VERIFY_FAILED"):
        asyncio.run(
            platform.fill_content(
                [
                    {"type": "text", "text": "前文"},
                    {"type": "image", "position": 1},
                    {"type": "text", "text": "中段"},
                    {"type": "image", "position": 2},
                ],
                [
                    {"position_index": 1, "local_path": "D:/fixture/first.png"},
                    {"position_index": 2, "local_path": "D:/fixture/second.png"},
                ],
            )
        )

    platform._upload_image.assert_awaited_once()


def _pattern_bytes(*, mirror: bool = False, format_name: str = "PNG") -> bytes:
    image = Image.new("RGB", (96, 64), "white")
    draw = ImageDraw.Draw(image)
    if mirror:
        draw.rectangle((12, 8, 36, 56), fill="black")
        draw.line((68, 4, 80, 60), fill="red", width=6)
    else:
        draw.rectangle((60, 8, 84, 56), fill="black")
        draw.line((16, 4, 28, 60), fill="red", width=6)
    output = BytesIO()
    image.save(output, format=format_name, quality=72)
    return output.getvalue()


def test_image_content_compare_accepts_resize_compression_but_rejects_other_image() -> None:
    expected = _pattern_bytes()
    resized = Image.open(BytesIO(expected)).resize((48, 32))
    observed_output = BytesIO()
    resized.save(observed_output, format="JPEG", quality=68)

    assert ZOLPlatform._compare_image_bytes(expected, observed_output.getvalue()) is None
    assert (
        ZOLPlatform._compare_image_bytes(expected, _pattern_bytes(mirror=True))
        == "ZOL_IMAGE_CONTENT_VERIFY_FAILED"
    )
    assert (
        ZOLPlatform._compare_image_bytes(expected, b"not-an-image")
        == "ZOL_IMAGE_CONTENT_UNVERIFIED"
    )


def test_image_content_reresolves_editor_after_stale_screenshot() -> None:
    payload = _pattern_bytes()
    image = MagicMock()
    image.evaluate = AsyncMock(return_value=True)
    image.screenshot = AsyncMock(side_effect=[RuntimeError("stale"), payload])
    image.get_attribute = AsyncMock(return_value="")
    images = MagicMock()
    images.count = AsyncMock(return_value=1)
    images.nth.return_value = image
    editor = MagicMock()
    editor.locator.return_value = images
    platform = ZOLPlatform()
    platform._resolve_content_editor = AsyncMock(
        return_value=(editor, "iframe")
    )
    platform._editor_image_src_fingerprints = AsyncMock(return_value=["target"])

    with patch("platforms.zol.asyncio.sleep", new=AsyncMock()):
        observed = asyncio.run(
            platform._read_stable_editor_image_bytes(["target"], "target")
        )

    assert observed == payload
    assert platform._resolve_content_editor.await_count >= 2
    assert image.screenshot.await_count == 2


def test_image_content_prefers_cdn_bytes_over_css_rendered_screenshot() -> None:
    source_payload = _pattern_bytes()
    rendered_payload = _pattern_bytes(mirror=True)
    response = SimpleNamespace(
        ok=True,
        body=AsyncMock(return_value=source_payload),
    )
    request = SimpleNamespace(get=AsyncMock(return_value=response))
    image = MagicMock()
    image.evaluate = AsyncMock(return_value=True)
    image.get_attribute = AsyncMock(return_value="https://cdn.example/image.jpg")
    image.screenshot = AsyncMock(return_value=rendered_payload)
    images = MagicMock()
    images.count = AsyncMock(return_value=1)
    images.nth.return_value = image
    editor = MagicMock()
    editor.locator.return_value = images
    platform = ZOLPlatform()
    platform.context = SimpleNamespace(request=request)
    platform._resolve_content_editor = AsyncMock(return_value=(editor, "iframe"))
    platform._editor_image_src_fingerprints = AsyncMock(return_value=["target"])

    observed = asyncio.run(
        platform._read_stable_editor_image_bytes(["target"], "target")
    )

    assert observed == source_payload
    request.get.assert_awaited_once_with(
        "https://cdn.example/image.jpg",
        timeout=10000,
    )
    image.screenshot.assert_not_awaited()


def test_heading_experiment_only_accepts_verified_h2_widget_and_reads_dom() -> None:
    platform = ZOLPlatform(enable_heading_experiment=True)

    platform._validate_heading_contract(
        [{"type": "heading", "level": 2, "text": "一级"}]
    )
    with pytest.raises(ContentValidationError, match="ZOL_HEADING_UNSUPPORTED_LEVEL"):
        platform._validate_heading_contract(
            [{"type": "heading", "level": 3, "text": "不支持"}]
        )

    class _TokenEditor(FakeLocator):
        async def evaluate(self, script, *_args):
            if "const tokens" in script:
                return [{"kind": "heading", "tag": "h2", "text": "一级"}]
            return await super().evaluate(script, *_args)

    editor = _TokenEditor(tag="body")
    platform._resolve_content_editor = AsyncMock(return_value=(editor, "iframe"))
    asyncio.run(
        platform._verify_heading_nodes(
            [{"kind": "heading", "tag": "h2", "text": "一级"}]
        )
    )


def test_heading_experiment_inserts_platform_header_widget_and_requires_h2_token() -> None:
    from tests.test_regression import FakeFrame, FakePage

    class _FormattingEditor(FakeLocator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.tag_after_format = "p"

        async def evaluate(self, script, *args):
            if "instance.insertContent(markup)" in script:
                assert "wxeditor-title" in args[0]
                self.tag_after_format = "h2"
                return True
            if "const tokens" in script:
                return [
                    {
                        "kind": "heading",
                        "tag": self.tag_after_format,
                        "text": "真实标题",
                    }
                ]
            return await super().evaluate(script, *args)

    page = FakePage("iframe")
    editor = _FormattingEditor(page=page, tag="body")
    page.frame_body = editor
    page.frame = FakeFrame(page, editor)
    page.iframe_handle.content_frame = AsyncMock(return_value=page.frame)
    platform = ZOLPlatform(enable_heading_experiment=True)
    platform.page = page

    result = asyncio.run(
        platform._apply_heading_block(
            editor,
            "真实标题",
            2,
            [{"kind": "heading", "tag": "h2", "text": "真实标题"}],
        )
    )
    assert result[1] == "iframe"
    assert editor.tag_after_format == "h2"


def test_heading_experiment_rejects_widget_insert_when_dom_stays_paragraph() -> None:
    from tests.test_regression import FakeFrame, FakePage

    class _ParagraphEditor(FakeLocator):
        async def evaluate(self, script, *_args):
            if "instance.insertContent(markup)" in script:
                return True
            if "const tokens" in script:
                return [{"kind": "heading", "tag": "p", "text": "标题"}]
            return await super().evaluate(script, *_args)

    page = FakePage("iframe")
    editor = _ParagraphEditor(page=page, tag="body")
    page.frame_body = editor
    page.frame = FakeFrame(page, editor)
    page.iframe_handle.content_frame = AsyncMock(return_value=page.frame)
    platform = ZOLPlatform(enable_heading_experiment=True)
    platform.page = page

    with pytest.raises(ContentValidationError, match="ZOL_HEADING_DOM_VERIFY_FAILED"):
        asyncio.run(
            platform._apply_heading_block(
                editor,
                "标题",
                2,
                [{"kind": "heading", "tag": "h2", "text": "标题"}],
            )
        )


def test_seven_image_tokens_require_exact_interleaved_structure() -> None:
    expected = []
    for index in range(7):
        expected.append({"kind": "text", "text": f"段落{index}"})
        expected.append({"kind": "image", "fingerprint": f"fp-{index}"})

    assert ZOLPlatform._content_tokens_match(expected, list(expected))
    moved = list(expected)
    moved[1], moved[2] = moved[2], moved[1]
    assert not ZOLPlatform._content_tokens_match(expected, moved)


def test_image_src_fingerprint_drift_does_not_break_structural_match() -> None:
    expected = [
        {"kind": "text", "text": "图片前"},
        {"kind": "image", "fingerprint": "temporary-upload-url"},
        {"kind": "text", "text": "图片后"},
    ]
    actual = [
        {"kind": "text", "text": "图片前"},
        {"kind": "image", "fingerprint": "rewritten-cdn-url"},
        {"kind": "text", "text": "图片后"},
    ]

    assert ZOLPlatform._content_tokens_match(expected, actual)
    assert not ZOLPlatform._content_tokens_match(expected, actual[:-1])


def test_content_token_shape_exposes_only_bounded_structure() -> None:
    tokens = [
        {"kind": "text", "text": "敏感正文"},
        {"kind": "heading", "tag": "h2", "text": "敏感标题"},
        {"kind": "image", "fingerprint": "https://secret.invalid/token"},
    ]

    shape = ZOLPlatform._content_token_shape(tokens, limit=2)

    assert shape == "T:4,Hh2:4,+1"
    assert "敏感" not in shape
    assert "secret" not in shape


def test_dom_reader_excludes_editor_ui_chrome_without_dropping_images() -> None:
    captured = {}

    class _Editor(FakeLocator):
        async def evaluate(self, script, *_args):
            if "const tokens" in script:
                captured["script"] = script
                return [
                    {"kind": "text", "text": "正文"},
                    {"kind": "image", "src": "https://cdn.invalid/body.png"},
                ]
            return await super().evaluate(script, *_args)

    tokens = asyncio.run(
        ZOLPlatform()._read_editor_dom_tokens(_Editor(tag="body"), "iframe")
    )

    assert [token["kind"] for token in tokens] == ["text", "image"]
    assert "ignoredUiTags" in captured["script"]
    assert "contenteditable') === 'false'" in captured["script"]
    assert "node.querySelectorAll('img')" in captured["script"]


def test_editor_cursor_uses_collapsed_dom_range_instead_of_keyboard_shortcut() -> None:
    captured = {}

    class _Editor(FakeLocator):
        async def evaluate(self, script, *_args):
            captured["script"] = script
            return True

    platform = ZOLPlatform()
    asyncio.run(
        platform._collapse_editor_selection_at_end(_Editor(tag="body"), "iframe")
    )

    assert "createRange" in captured["script"]
    assert "selectNodeContents(root)" in captured["script"]
    assert "range.collapse(false)" in captured["script"]
    assert "selection.addRange(range)" in captured["script"]


def test_second_image_checks_existing_prefix_before_new_upload() -> None:
    from tests.test_regression import FakePage

    platform = ZOLPlatform()
    platform.page = FakePage("contenteditable")
    platform.simulator.random_delay = AsyncMock()
    platform._bind_draft_before_media = AsyncMock()
    platform._bound_draft_id = "bound-id"
    platform._wait_for_bound_autosave = AsyncMock()
    platform._collapse_editor_selection_at_end = AsyncMock()
    platform._remove_delayed_duplicate_images = AsyncMock()
    platform._upload_image = AsyncMock(
        return_value={
            "success": True,
            "filename": "first.png",
            "image_src_fingerprint": "first-fingerprint",
        }
    )
    platform._verify_content_prefix = AsyncMock(
        side_effect=[
            None,
            ContentValidationError("ZOL_CONTENT_PREFIX_VERIFY_FAILED: 旧前缀已损坏"),
        ]
    )

    with pytest.raises(ContentValidationError, match="ZOL_CONTENT_PREFIX_VERIFY_FAILED"):
        asyncio.run(
            platform.fill_content(
                [
                    {"type": "text", "text": "第一段"},
                    {"type": "image", "position": 1},
                    {"type": "text", "text": "第二段"},
                    {"type": "image", "position": 2},
                ],
                [
                    {"position_index": 1, "local_path": "D:/fixture/first.png"},
                    {"position_index": 2, "local_path": "D:/fixture/second.png"},
                ],
            )
        )

    platform._upload_image.assert_awaited_once()


def test_image_dom_change_is_committed_to_tinymce_model() -> None:
    captured = {}

    class _Editor(FakeLocator):
        async def evaluate(self, script, *_args):
            captured["script"] = script

    asyncio.run(
        ZOLPlatform()._commit_editor_dom_change(_Editor(tag="body"), "iframe")
    )

    script = captured["script"]
    assert "InputEvent('input'" in script
    assert "new Event('change'" in script
    assert "instance.nodeChanged()" in script
    assert "instance.setDirty(true)" in script
    assert "instance.save()" in script


def test_image_and_following_content_use_independent_dom_block_anchor() -> None:
    captured = {}

    class _Editor(FakeLocator):
        async def evaluate(self, script, *_args):
            captured["script"] = script
            return True

    asyncio.run(
        ZOLPlatform()._append_editor_block_anchor(_Editor(tag="body"), "iframe")
    )

    script = captured["script"]
    assert "doc.createElement('p')" in script
    assert "root.appendChild(paragraph)" in script
    assert "range.setStart(paragraph, 0)" in script
    assert "instance.selection.setRng(range)" in script


def test_tinymce_structured_block_escapes_text_and_commits_model() -> None:
    captured = {}

    class _Editor(FakeLocator):
        async def evaluate(self, script, *args):
            captured["script"] = script
            captured["markup"] = args[0]
            return True

    asyncio.run(
        ZOLPlatform()._insert_tinymce_block(
            _Editor(tag="body"),
            "iframe",
            block_type="heading",
            text="A < B & C",
            level=2,
        )
    )

    assert "wxeditor-title wxeditor-title_06" in captured["markup"]
    assert "wxeditor-title-number" in captured["markup"]
    assert "wxeditor-text-con mceEditable" in captured["markup"]
    assert "&nbsp;A &lt; B &amp; C" in captured["markup"]
    assert "<h2>" not in captured["markup"]
    assert "instance.insertContent(markup)" in captured["script"]
    assert "instance.save()" in captured["script"]
    assert "root.insertAdjacentHTML('beforeend', markup)" in captured["script"]
    assert "inputType: 'insertHTML'" in captured["script"]


def test_delayed_same_src_image_clone_is_removed_fail_closed() -> None:
    platform = ZOLPlatform()
    platform._editor_image_src_fingerprints = AsyncMock(
        side_effect=[["first", "first"], ["first"]]
    )
    editor = FakeLocator(tag="body")
    images = FakeLocator(tag="img", count=2)
    editor.locator = lambda _selector: images
    platform._resolve_content_editor = AsyncMock(return_value=(editor, "iframe"))
    platform._commit_editor_dom_change = AsyncMock()

    asyncio.run(platform._remove_delayed_duplicate_images(1))

    platform._commit_editor_dom_change.assert_awaited_once()


def test_expected_text_tokens_keep_three_paragraph_boundaries() -> None:
    blocks = [
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
        {"type": "text", "text": "第三段"},
    ]
    expected = ZOLPlatform._expected_content_tokens(blocks)

    assert expected == [
        {"kind": "text", "text": "第一段"},
        {"kind": "text", "text": "第二段"},
        {"kind": "text", "text": "第三段"},
    ]
    assert ZOLPlatform._content_tokens_match(expected, expected)
    assert not ZOLPlatform._content_tokens_match(
        expected,
        [{"kind": "text", "text": "第一段 第二段 第三段"}],
    )
    assert not ZOLPlatform._content_tokens_match(
        expected,
        [expected[1], expected[0], expected[2]],
    )


def test_dom_mixed_image_block_keeps_text_on_both_sides() -> None:
    class _MixedEditor(FakeLocator):
        async def evaluate(self, script, *_args):
            if "const tokens" in script:
                return [
                    {"kind": "text", "text": "图片前"},
                    {"kind": "image", "src": "https://cdn.invalid/mixed.png"},
                    {"kind": "text", "text": "图片后"},
                ]
            return await super().evaluate(script, *_args)

    tokens = asyncio.run(
        ZOLPlatform()._read_editor_dom_tokens(_MixedEditor(tag="body"), "iframe")
    )

    assert [token["kind"] for token in tokens] == ["text", "image", "text"]


def test_dom_text_token_is_split_like_expected_paragraphs_without_mutating_input() -> None:
    image_src = "https://cdn.invalid/paragraph.png"

    class _ParagraphEditor(FakeLocator):
        async def evaluate(self, script, *_args):
            if "const tokens" in script:
                return [
                    {
                        "kind": "text",
                        "text": "第一段\r\n\r\n第二段\n\n\n第三段",
                    },
                    {"kind": "image", "src": image_src},
                ]
            return await super().evaluate(script, *_args)

    blocks = [
        {"type": "text", "text": "第一段\r\n\r\n第二段\n\n\n第三段"},
        {"type": "image", "position": 2},
    ]
    original = copy.deepcopy(blocks)
    image_fingerprint = hashlib.sha256(image_src.encode()).hexdigest()
    expected = ZOLPlatform._expected_content_tokens(blocks, [image_fingerprint])
    actual = asyncio.run(
        ZOLPlatform()._read_editor_dom_tokens(_ParagraphEditor(tag="body"), "iframe")
    )

    assert actual == expected
    assert blocks == original


@pytest.mark.parametrize(
    "actual",
    [
        [
            {"kind": "text", "text": "第一段"},
            {"kind": "text", "text": "第三段"},
        ],
        [
            {"kind": "text", "text": "第二段"},
            {"kind": "text", "text": "第一段"},
            {"kind": "text", "text": "第三段"},
        ],
        [
            {"kind": "text", "text": "第一段"},
            {"kind": "text", "text": "第二段"},
            {"kind": "text", "text": "第二段"},
        ],
    ],
)
def test_dom_text_missing_reordered_or_duplicated_paragraph_stays_failed(actual) -> None:
    expected = [
        {"kind": "text", "text": "第一段"},
        {"kind": "text", "text": "第二段"},
        {"kind": "text", "text": "第三段"},
    ]

    assert not ZOLPlatform._content_tokens_match(expected, actual)
