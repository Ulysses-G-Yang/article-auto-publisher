from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from platforms.baijiahao import BaijiahaoPlatform
from platforms.base import BrowserLifecycleError, DraftResultUnknownError, SelectorError
from platforms.content_validation import ContentValidationError
from platforms.smzdm import SmzdmPlatform


class FakeLocator:
    def __init__(
        self,
        *,
        visible: bool = True,
        editable: bool = True,
        text: str = "",
        visible_sequence: list[bool] | None = None,
    ) -> None:
        self.visible = visible
        self.editable = editable
        self.text = text
        self.visible_sequence = list(visible_sequence or [])
        self.inner_text = AsyncMock(return_value=text)

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        if self.visible_sequence:
            return self.visible_sequence.pop(0)
        return self.visible

    async def is_editable(self) -> bool:
        return self.editable

    def nth(self, _index: int):
        return self

    async def click(self, **_kwargs) -> None:
        return None

    async def press(self, _key: str) -> None:
        return None

    async def evaluate(self, script: str, _arg=None):
        if "const tokens" in script:
            return [
                {"kind": "text", "level": 0, "text": line}
                for line in self.text.splitlines()
                if line.strip()
            ]
        return True


class FakeLocatorCollection:
    def __init__(self, locator: FakeLocator) -> None:
        self.locator = locator

    async def count(self) -> int:
        return 1

    def nth(self, _index: int) -> FakeLocator:
        return self.locator

    @property
    def first(self) -> FakeLocator:
        return self.locator


class FakeFrame:
    def __init__(self, body: FakeLocator, *, browser_closed: bool = False) -> None:
        self.body = body
        self.browser_closed = browser_closed

    async def evaluate(self, _script: str) -> bool:
        if self.browser_closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return True

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "body"
        return self.body


class FakeBaijiaPage:
    def __init__(self, title: FakeLocator, frames: list[FakeFrame]) -> None:
        self.title = title
        self.frames = frames
        self.goto = AsyncMock()

    def locator(self, selector: str) -> FakeLocatorCollection:
        assert "FeEditorApp" in selector
        return FakeLocatorCollection(self.title)


def test_baijia_ready_waits_for_visible_editable_body_after_title() -> None:
    title = FakeLocator()
    body = FakeLocator(visible_sequence=[False, True])
    platform = BaijiahaoPlatform()
    platform.page = FakeBaijiaPage(title, [FakeFrame(body)])

    with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
        ready_title, ready_body = asyncio.run(
            platform._wait_for_editor_ready(timeout_seconds=0.05)
        )

    assert ready_title is title
    assert ready_body is body


def test_baijia_ready_times_out_when_body_never_becomes_visible() -> None:
    platform = BaijiahaoPlatform()
    platform.page = FakeBaijiaPage(
        FakeLocator(),
        [FakeFrame(FakeLocator(visible=False))],
    )

    with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(SelectorError, match="0.001 秒内未就绪"):
            asyncio.run(platform._wait_for_editor_ready(timeout_seconds=0.001))


def test_baijia_body_locator_does_not_swallow_browser_closed() -> None:
    platform = BaijiahaoPlatform()
    platform.page = FakeBaijiaPage(
        FakeLocator(),
        [FakeFrame(FakeLocator(), browser_closed=True)],
    )

    with pytest.raises(BrowserLifecycleError):
        asyncio.run(platform._body_editor_locator())


class FakeKeyboard:
    async def press(self, _key: str) -> None:
        return None

    async def insert_text(self, _text: str) -> None:
        return None


def test_baijia_image_rerender_reads_new_body_and_fails_on_missing_text() -> None:
    old_body = FakeLocator(text="第一段\n第二段")
    new_body = FakeLocator(text="第一段")
    platform = BaijiahaoPlatform()
    platform.page = SimpleNamespace(keyboard=FakeKeyboard())
    platform.simulator.random_delay = AsyncMock()
    platform._body_editor_locator = AsyncMock(
        side_effect=[old_body, old_body, old_body, old_body, old_body, old_body, new_body]
    )
    platform._focus_editor = AsyncMock()
    platform._upload_image = AsyncMock(return_value={"success": True})

    with pytest.raises(ContentValidationError):
        asyncio.run(
            platform.fill_content(
                [
                    {"type": "text", "text": "第一段\n第二段"},
                    {"type": "image", "position": 1, "local_path": "photo.png"},
                ],
                [],
            )
        )

    assert platform._body_editor_locator.await_count == 7
    platform._upload_image.assert_awaited_once_with("photo.png")


class FakeSaveResponse:
    def __init__(
        self,
        status: int,
        payload: dict | None = None,
        url: str = "https://post.smzdm.com/api/draft/save",
    ) -> None:
        self.status = status
        self.payload = payload or {}
        self.url = url
        self.request = SimpleNamespace(method="POST")

    async def json(self) -> dict:
        return self.payload


class FakeSaveKeyboard:
    def __init__(self, page) -> None:
        self.page = page

    async def press(self, _key: str) -> None:
        return None

    async def type(self, _text: str, **_kwargs) -> None:
        if self.page.response_callback:
            for response in self.page.responses:
                await self.page.response_callback(response)


class FakeSaveEditor:
    press = AsyncMock()
    evaluate = AsyncMock()


class FakeSavePage:
    def __init__(
        self,
        response: FakeSaveResponse | list[FakeSaveResponse] | None,
        url: str = "https://post.smzdm.com/tougao/",
    ) -> None:
        self.responses = (
            list(response)
            if isinstance(response, list)
            else ([] if response is None else [response])
        )
        self.response_callback = None
        self.keyboard = FakeSaveKeyboard(self)
        self.url = url

    def on(self, event: str, callback) -> None:
        assert event == "response"
        self.response_callback = callback

    def remove_listener(self, event: str, _callback) -> None:
        assert event == "response"
        self.response_callback = None

def build_save_platform(
    response: FakeSaveResponse | list[FakeSaveResponse] | None,
    *,
    current_url: str = "https://post.smzdm.com/tougao/",
    preflight_shell_id: str | None = None,
) -> SmzdmPlatform:
    platform = SmzdmPlatform()
    platform.page = FakeSavePage(response, current_url)
    platform.simulator.random_delay = AsyncMock()
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    platform._preflight_title = "同名草稿"
    platform._preflight_draft_ids = frozenset({"existing-draft-id"})
    platform._preflight_editor_draft_id = preflight_shell_id
    platform._current_body_editor = AsyncMock(return_value=FakeSaveEditor())
    platform._find_unique_new_draft = AsyncMock(
        return_value="https://post.smzdm.com/edit/safe-draft-id"
    )
    platform._verify_persisted_draft = AsyncMock()
    return platform


def run_save(
    response: FakeSaveResponse | list[FakeSaveResponse] | None,
    *,
    current_url: str = "https://post.smzdm.com/tougao/",
    preflight_shell_id: str | None = None,
) -> tuple[SmzdmPlatform, str]:
    platform = build_save_platform(
        response,
        current_url=current_url,
        preflight_shell_id=preflight_shell_id,
    )
    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(platform.save_draft("同名草稿"))
    return platform, result


def test_smzdm_binds_editor_shell_only_after_new_article_navigation() -> None:
    class FakeNavigatePage:
        def __init__(self) -> None:
            self.url = "https://www.smzdm.com/"

        async def goto(self, url: str, **_kwargs) -> None:
            self.url = url

        async def wait_for_function(self, *_args, **_kwargs) -> None:
            return None

        async def evaluate(self, *_args, **_kwargs) -> bool:
            self.url = "https://post.smzdm.com/edit/existing-draft-id"
            return True

        async def wait_for_selector(self, *_args, **_kwargs) -> None:
            return None

    platform = SmzdmPlatform()
    platform.page = FakeNavigatePage()
    platform._load_draft_entities_once = AsyncMock(
        return_value={
            "existing-draft-id": "https://post.smzdm.com/edit/existing-draft-id"
        }
    )

    asyncio.run(platform.preflight_delivery("同名草稿"))
    assert platform._preflight_editor_draft_id is None

    asyncio.run(platform.navigate_to_editor())

    assert platform._preflight_editor_draft_id == "existing-draft-id"
    assert platform._preflight_draft_ids == frozenset({"existing-draft-id"})


def test_smzdm_missing_current_save_response_is_result_unknown() -> None:
    platform = build_save_platform(None)

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError):
            asyncio.run(platform.save_draft("同名草稿"))
    platform._find_unique_new_draft.assert_not_awaited()


def test_smzdm_requires_current_2xx_response_and_persisted_reopen() -> None:
    platform, result = run_save(FakeSaveResponse(204))

    assert result == "https://post.smzdm.com/edit/safe-draft-id"
    platform._find_unique_new_draft.assert_awaited_once_with()
    platform._verify_persisted_draft.assert_awaited_once()
    assert platform._last_draft_evidence.draft_entity_source == "baseline_new_id"


def test_smzdm_prefers_safe_save_response_id_and_redacts_payload() -> None:
    platform, result = run_save(
        FakeSaveResponse(
            200,
            {
                "code": 0,
                "data": {
                    "draftId": "response-draft-id",
                    "private": "must-not-be-recorded",
                },
            },
        ),
        current_url="https://post.smzdm.com/edit/response-draft-id",
    )

    assert result == "https://post.smzdm.com/edit/response-draft-id"
    platform._find_unique_new_draft.assert_not_awaited()
    platform._verify_persisted_draft.assert_awaited_once_with(
        "同名草稿",
        "https://post.smzdm.com/edit/response-draft-id",
    )
    evidence = platform._last_draft_evidence.to_dict()
    assert evidence["save_platform_code"] == "0"
    assert evidence["draft_entity_source"] == "save_response_id"
    assert evidence["draft_entity_id_match"] is True
    assert "must-not-be-recorded" not in repr(evidence)


def test_smzdm_ignores_cross_origin_save_like_response() -> None:
    platform = build_save_platform(
        FakeSaveResponse(
            200,
            {"code": 0, "data": {"draftId": "foreign-id"}},
            url="https://metrics.example/save-edit-event",
        )
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError, match="未捕获到成功自动保存响应"):
            asyncio.run(platform.save_draft("同名草稿"))

    platform._find_unique_new_draft.assert_not_awaited()
    platform._verify_persisted_draft.assert_not_awaited()


def test_smzdm_ignores_same_origin_other_save_like_path() -> None:
    platform = build_save_platform(
        FakeSaveResponse(
            200,
            {"code": 0, "data": {"id": "wrong-endpoint-id"}},
            url="https://post.smzdm.com/api/image/save",
        )
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError, match="未捕获到成功自动保存响应"):
            asyncio.run(platform.save_draft("同名草稿"))

    platform._find_unique_new_draft.assert_not_awaited()
    platform._verify_persisted_draft.assert_not_awaited()


def test_smzdm_duplicate_identical_save_responses_are_deduplicated() -> None:
    payload = {"code": 0, "data": {"id": "same-response-id"}}
    platform, result = run_save(
        [FakeSaveResponse(200, payload), FakeSaveResponse(200, payload)],
        current_url="https://post.smzdm.com/edit/same-response-id",
    )

    assert result == "https://post.smzdm.com/edit/same-response-id"
    platform._find_unique_new_draft.assert_not_awaited()
    assert platform._last_draft_evidence.draft_entity_source == "save_response_id"


def test_smzdm_conflicting_atomic_save_responses_fail_closed() -> None:
    platform = build_save_platform(
        [
            FakeSaveResponse(200, {"code": 0, "data": {"id": "first-id"}}),
            FakeSaveResponse(200, {"code": 0, "data": {"id": "second-id"}}),
        ]
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError, match="冲突的保存响应") as caught:
            asyncio.run(platform.save_draft("同名草稿"))

    evidence = caught.value.evidence.to_dict()
    assert evidence["save_http_status"] is None
    assert evidence["save_platform_code"] == "ambiguous"
    assert evidence["draft_entity_id_match"] is False
    platform._find_unique_new_draft.assert_not_awaited()
    platform._verify_persisted_draft.assert_not_awaited()


def test_smzdm_uses_safe_current_editor_id_when_response_has_no_id() -> None:
    platform, result = run_save(
        FakeSaveResponse(200, {"code": 0}),
        current_url="https://post.smzdm.com/edit/current-draft-id",
    )

    assert result == "https://post.smzdm.com/edit/current-draft-id"
    platform._find_unique_new_draft.assert_not_awaited()
    assert platform._last_draft_evidence.draft_entity_source == "existing_draft_id"
    assert platform._last_draft_evidence.draft_entity_id_match is True


def test_smzdm_ambiguous_response_ids_fail_closed() -> None:
    platform = build_save_platform(
        FakeSaveResponse(
            200,
            {"code": 0, "data": {"draftId": "first-id", "id": "second-id"}},
        )
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError, match="冲突的草稿 ID"):
            asyncio.run(platform.save_draft("同名草稿"))

    platform._find_unique_new_draft.assert_not_awaited()
    platform._verify_persisted_draft.assert_not_awaited()


def test_smzdm_preflight_shell_is_allowed_even_when_in_baseline() -> None:
    platform, result = run_save(
        FakeSaveResponse(
            200,
            {"code": 0, "data": {"id": "existing-draft-id"}},
        ),
        current_url="https://post.smzdm.com/edit/existing-draft-id",
        preflight_shell_id="existing-draft-id",
    )

    assert result == "https://post.smzdm.com/edit/existing-draft-id"
    platform._find_unique_new_draft.assert_not_awaited()
    assert platform._last_draft_evidence.draft_entity_source == "save_response_id"


def test_smzdm_unrelated_baseline_id_is_rejected() -> None:
    platform = build_save_platform(
        FakeSaveResponse(
            200,
            {"code": 0, "data": {"id": "existing-draft-id"}},
        ),
        current_url="https://post.smzdm.com/edit/existing-draft-id",
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError, match="无关的保存前草稿") as caught:
            asyncio.run(platform.save_draft("同名草稿"))

    evidence = caught.value.evidence.to_dict()
    assert evidence["draft_entity_source"] == "save_response_id"
    assert evidence["draft_entity_bound"] is False
    assert evidence["draft_entity_id_match"] is False
    platform._find_unique_new_draft.assert_not_awaited()
    platform._verify_persisted_draft.assert_not_awaited()


def test_smzdm_response_and_editor_id_conflict_is_result_unknown() -> None:
    platform = build_save_platform(
        FakeSaveResponse(200, {"code": 0, "data": {"draftId": "response-id"}}),
        current_url="https://post.smzdm.com/edit/editor-id",
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError, match="ID 不一致") as caught:
            asyncio.run(platform.save_draft("同名草稿"))

    platform._find_unique_new_draft.assert_not_awaited()
    platform._verify_persisted_draft.assert_not_awaited()
    evidence = caught.value.evidence.to_dict()
    assert evidence["draft_entity_source"] == "save_response_id"
    assert evidence["draft_entity_bound"] is False
    assert evidence["draft_entity_id_match"] is False


def test_smzdm_http_200_with_nonzero_platform_code_is_result_unknown() -> None:
    platform = build_save_platform(
        FakeSaveResponse(200, {"code": 123, "data": {"draftId": "not-saved"}})
    )

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError, match="平台码未确认成功") as caught:
            asyncio.run(platform.save_draft("同名草稿"))

    platform._find_unique_new_draft.assert_not_awaited()
    platform._verify_persisted_draft.assert_not_awaited()
    assert caught.value.evidence.save_platform_code == "nonzero"


def test_smzdm_non_2xx_save_response_is_result_unknown() -> None:
    platform = build_save_platform(FakeSaveResponse(500))

    with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DraftResultUnknownError):
            asyncio.run(platform.save_draft("同名草稿"))
    platform._find_unique_new_draft.assert_not_awaited()


class _BaijiaSaveButtons:
    def __init__(self) -> None:
        self.button = FakeLocator()

    async def count(self) -> int:
        return 1

    def nth(self, _index: int):
        return self.button


class _BaijiaSavePage:
    def __init__(self) -> None:
        self.buttons = _BaijiaSaveButtons()
        self.response_callback = None

    def is_closed(self) -> bool:
        return False

    def on(self, event: str, callback) -> None:
        assert event == "response"
        self.response_callback = callback

    def remove_listener(self, event: str, _callback) -> None:
        assert event == "response"
        self.response_callback = None

    def get_by_text(self, text: str, *, exact: bool):
        assert (text, exact) == ("存草稿", True)
        return self.buttons

    async def evaluate(self, _script: str):
        raise RuntimeError("execution context was destroyed by navigation")


def test_baijia_save_toast_navigation_race_still_uses_persisted_reopen() -> None:
    platform = BaijiahaoPlatform()
    platform.page = _BaijiaSavePage()
    platform._preflight_title = "唯一标题"
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    platform._find_unique_exact_draft = AsyncMock(
        return_value="https://baijiahao.baidu.com/builder/rc/edit?type=news&article_id=123"
    )
    platform._verify_persisted_draft = AsyncMock()

    with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(platform.save_draft("唯一标题"))

    assert "article_id=123" in result
    platform._find_unique_exact_draft.assert_awaited_once_with("唯一标题")
    platform._verify_persisted_draft.assert_awaited_once()
