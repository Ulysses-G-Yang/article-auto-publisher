"""ZOL 第二批草稿与正文证据契约的离线测试。

本文件只测试解析器、稳定错误语义和选择器边界，不启动浏览器，
不访问真实平台，也不触碰 Cookie、Profile 或仓库运行数据。
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from platforms.base import DraftBaselineError, DraftResultUnknownError, SelectorError
from platforms.content_validation import ContentValidationError
from platforms.zol import ZOLPlatform, _ZOLDraftSnapshot
from tests.test_regression import FakeLocator


def _payload(items: list[dict], total: int | None = None) -> dict:
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


class _SaveControl:
    def __init__(self) -> None:
        self.click_count = 0

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def click(self, **_kwargs) -> None:
        self.click_count += 1


class _SavePage:
    url = "https://post.zol.com.cn/v2/create/article"

    def __init__(self) -> None:
        self.control = _SaveControl()

    def locator(self, selector: str):
        assert selector == ZOLPlatform.DRAFT_SAVE_SELECTOR
        return self.control

    def is_closed(self) -> bool:
        return False


def test_save_draft_uses_exact_control_once_and_proves_new_entity() -> None:
    platform = ZOLPlatform()
    platform.page = _SavePage()
    platform.simulator.random_delay = AsyncMock()
    platform.DRAFT_RESULT_POLL_DELAYS = (0,)
    platform._draft_baseline = _snapshot(0)
    platform._fetch_draft_snapshot = AsyncMock(
        return_value=_snapshot(1, ("new-id", "新草稿"))
    )

    result = asyncio.run(platform.save_draft("新草稿"))

    assert result.endswith("/v2/manage/works/draft")
    assert platform.page.control.click_count == 1
    platform._fetch_draft_snapshot.assert_awaited_once()


def test_save_click_without_proof_is_result_unknown_and_never_retries() -> None:
    platform = ZOLPlatform()
    platform.page = _SavePage()
    platform.simulator.random_delay = AsyncMock()
    platform.DRAFT_RESULT_POLL_DELAYS = (0,)
    platform._draft_baseline = _snapshot(0)
    platform._fetch_draft_snapshot = AsyncMock(return_value=_snapshot(0))

    with pytest.raises(DraftResultUnknownError, match="DRAFT_RESULT_UNKNOWN"):
        asyncio.run(platform.save_draft("未证明草稿"))

    assert platform.page.control.click_count == 1
    platform._fetch_draft_snapshot.assert_awaited_once()


def test_save_post_response_failure_after_click_is_result_unknown() -> None:
    platform = ZOLPlatform()
    platform.page = _SavePage()
    platform.simulator.random_delay = AsyncMock()
    platform.DRAFT_RESULT_POLL_DELAYS = (0,)
    platform._draft_baseline = _snapshot(0)
    platform._fetch_draft_snapshot = AsyncMock(
        side_effect=DraftBaselineError("DRAFT_BASELINE_UNAVAILABLE: HTTP 500")
    )

    with pytest.raises(DraftResultUnknownError, match="DRAFT_RESULT_UNKNOWN"):
        asyncio.run(platform.save_draft("响应失败"))

    assert platform.page.control.click_count == 1


def test_save_draft_polls_read_only_without_repeating_click() -> None:
    platform = ZOLPlatform()
    platform.page = _SavePage()
    platform.simulator.random_delay = AsyncMock()
    platform.DRAFT_RESULT_POLL_DELAYS = (0, 0)
    platform._draft_baseline = _snapshot(0)
    platform._fetch_draft_snapshot = AsyncMock(
        side_effect=[
            DraftBaselineError("DRAFT_BASELINE_UNAVAILABLE: not ready"),
            _snapshot(1, ("new-id", "轮询成功")),
        ]
    )

    result = asyncio.run(platform.save_draft("轮询成功"))

    assert result.endswith("/v2/manage/works/draft")
    assert platform.page.control.click_count == 1
    assert platform._fetch_draft_snapshot.await_count == 2


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
                if item.attributes.get("accept") == "image"
                and item.attributes.get("multiple") is not None
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
        attributes={"accept": "image", "multiple": None},
    )
    body = FakeLocator(
        count=1,
        visible=False,
        attributes={"accept": "image", "multiple": ""},
    )
    video = FakeLocator(
        count=1,
        visible=False,
        attributes={"accept": "video/*", "multiple": ""},
    )
    platform = ZOLPlatform()

    assert asyncio.run(platform._resolve_body_image_input(_Modal([body]))) is body
    assert asyncio.run(platform._resolve_body_image_input(_Modal([cover, body]))) is body
    assert asyncio.run(platform._resolve_body_image_input(_Modal([cover]))) is None
    assert asyncio.run(platform._resolve_body_image_input(_Modal([video]))) is None
    assert (
        asyncio.run(platform._resolve_body_image_input(_Modal([body, body]))) is None
    )


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


def test_heading_experiment_still_only_accepts_h2_h3_and_reads_actual_dom() -> None:
    platform = ZOLPlatform(enable_heading_experiment=True)

    platform._validate_heading_contract(
        [
            {"type": "heading", "level": 2, "text": "一级"},
            {"type": "heading", "level": 3, "text": "二级"},
        ]
    )
    with pytest.raises(ContentValidationError, match="ZOL_HEADING_UNSUPPORTED_LEVEL"):
        platform._validate_heading_contract(
            [{"type": "heading", "level": 4, "text": "不支持"}]
        )

    class _TokenEditor(FakeLocator):
        async def evaluate(self, script, *_args):
            if "const tokens" in script:
                return [
                    {"kind": "heading", "tag": "h2", "text": "一级"},
                    {"kind": "heading", "tag": "h3", "text": "二级"},
                ]
            return await super().evaluate(script, *_args)

    editor = _TokenEditor(tag="body")
    platform._resolve_content_editor = AsyncMock(return_value=(editor, "iframe"))
    asyncio.run(
        platform._verify_heading_nodes(
            [
                {"kind": "heading", "tag": "h2", "text": "一级"},
                {"kind": "heading", "tag": "h3", "text": "二级"},
            ]
        )
    )


def test_heading_experiment_applies_format_and_requires_h2_dom_node() -> None:
    from tests.test_regression import FakeFrame, FakePage

    class _FormattingEditor(FakeLocator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.tag_after_format = "p"

        async def evaluate(self, script, *args):
            if "execCommand" in script:
                self.tag_after_format = args[0]
                return True
            if "const tokens" in script:
                return [
                    {
                        "kind": "heading",
                        "tag": self.tag_after_format,
                        "text": self.text,
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


def test_heading_experiment_rejects_format_true_when_dom_stays_paragraph() -> None:
    from tests.test_regression import FakeFrame, FakePage

    class _ParagraphEditor(FakeLocator):
        async def evaluate(self, script, *_args):
            if "execCommand" in script:
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


def test_seven_image_tokens_require_exact_interleaved_order() -> None:
    expected = []
    for index in range(7):
        expected.append({"kind": "text", "text": f"段落{index}"})
        expected.append({"kind": "image", "fingerprint": f"fp-{index}"})

    assert ZOLPlatform._content_tokens_match(expected, list(expected))
    swapped = list(expected)
    swapped[1], swapped[3] = swapped[3], swapped[1]
    assert not ZOLPlatform._content_tokens_match(expected, swapped)


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
