from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from platforms.base import (
    BrowserLifecycleError,
    DraftBaselineError,
    DraftResultUnknownError,
)
from platforms.toutiao import (
    AUTOSAVE_PATH,
    BODY_IMAGE_INPUT_IN_DRAWER_SELECTOR,
    BODY_IMAGE_INPUT_SELECTOR,
    BODY_SELECTOR,
    CREATE_ARTICLE_LINK_SELECTOR,
    DRAFT_BOX_URL,
    EDITOR_EXIT_SELECTOR,
    HEADING_BUTTON_SELECTOR,
    HOME_URL,
    IMAGE_BUTTON_SELECTOR,
    IMAGE_CONFIRM_IN_DRAWER_SELECTOR,
    IMAGE_CONFIRM_SELECTOR,
    IMAGE_DRAWER_CLOSE_IN_DRAWER_SELECTOR,
    IMAGE_DRAWER_CLOSE_SELECTOR,
    IMAGE_DRAWER_SELECTOR,
    IMAGE_UPLOAD_TAB_SELECTOR,
    IMAGE_UPLOAD_ERROR_IN_DRAWER_SELECTOR,
    IMAGE_UPLOAD_ERROR_SELECTOR,
    IMAGE_UPLOAD_TAB_IN_DRAWER_SELECTOR,
    IMAGE_UPLOAD_PATH,
    PUBLISH_URL,
    TITLE_SELECTOR,
    ToutiaoDraftSaveRejectedError,
    ToutiaoPlatform,
    _ToutiaoDraftSnapshot,
)


def run(coroutine):
    return asyncio.run(coroutine)


class _InstantSimulator:
    async def random_delay(self, *_args, **_kwargs):
        return None


class _FakeRequest:
    def __init__(self, method: str, content_type: str) -> None:
        self.method = method
        self._content_type = content_type

    async def header_value(self, name: str) -> str:
        return self._content_type if name.lower() == "content-type" else ""


class FakeResponse:
    def __init__(
        self,
        url: str,
        body: dict,
        method: str = "POST",
        status: int = 200,
        content_type: str = "multipart/form-data; boundary=test",
    ):
        self.url = url
        self._body = body
        self.status = status
        self.request = _FakeRequest(method, content_type)

    async def json(self) -> dict:
        return self._body

    async def text(self) -> str:
        return json.dumps(self._body, ensure_ascii=False)


class FakeContext:
    def __init__(self, cookies: list[dict] | None = None) -> None:
        self._cookies = cookies or []

    async def cookies(self, *_args):
        return list(self._cookies)


class FakePage:
    def __init__(self) -> None:
        self._response_handlers: list = []
        self.goto_calls: list[str] = []
        self.responses_on_goto: list[FakeResponse] = []
        self.dom_nickname = ""

    def is_closed(self) -> bool:
        return False

    def on(self, event: str, handler) -> None:
        if event == "request":
            return
        if event == "response":
            self._response_handlers.append(handler)

    def remove_listener(self, event: str, handler) -> None:
        if event == "response":
            self._response_handlers = [
                item for item in self._response_handlers if item is not handler
            ]

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        for response in list(self.responses_on_goto):
            for handler in list(self._response_handlers):
                await handler(response)

    async def evaluate(self, script: str, *_args) -> object:
        if "texts[0]" in script:
            return self.dom_nickname
        return None


def _make_platform(
    page,
    *,
    cookies: list[dict] | None = None,
) -> ToutiaoPlatform:
    platform = ToutiaoPlatform()
    platform.page = page
    platform.context = FakeContext(cookies)
    platform.simulator = _InstantSimulator()
    platform._wait_for_content_block_autosave = AsyncMock()
    return platform


IDENTITY_API_URL = (
    "https://mp.toutiao.com/user/profile/auth/info/v2/?__user_id=2610667347771923"
)


def _real_sleep_and_fake() -> tuple:
    real_sleep = asyncio.sleep

    async def fake_sleep(_: float) -> None:
        # 让出事件循环一次，使已调度的 response handler 得以执行。
        await real_sleep(0)

    return real_sleep, fake_sleep


def _snapshot(
    *,
    total: int,
    title: str,
    title_count: int,
    ids: set[str],
) -> _ToutiaoDraftSnapshot:
    all_ids = set(ids)
    filler = 900000
    while len(all_ids) < total:
        all_ids.add(str(filler))
        filler += 1
    return _ToutiaoDraftSnapshot(
        loaded=True,
        total_count=total,
        title_counts={title: title_count} if title_count else {},
        draft_ids=frozenset(all_ids),
        title_to_ids={title: frozenset(ids)} if ids else {},
    )


class _CreateArticleLink:
    def __init__(self, page, destination: str) -> None:
        self.page = page
        self.destination = destination
        self.click_calls = 0

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def click(self, **_kwargs) -> None:
        self.click_calls += 1
        self.page.url = self.destination


class _CreateArticlePage(FakePage):
    def __init__(self, destination: str) -> None:
        super().__init__()
        self.url = DRAFT_BOX_URL
        self.create_link = _CreateArticleLink(self, destination)

    def locator(self, selector: str):
        assert selector == CREATE_ARTICLE_LINK_SELECTOR
        return self.create_link

    async def wait_for_selector(self, selector: str, **_kwargs) -> None:
        assert selector == TITLE_SELECTOR


def test_navigate_to_editor_clicks_native_create_article_link() -> None:
    page = _CreateArticlePage(PUBLISH_URL)
    platform = _make_platform(page)
    platform._draft_baseline = _snapshot(
        total=1,
        title="旧文章",
        title_count=1,
        ids={"7680498739710149160"},
    )

    run(platform.navigate_to_editor())

    assert page.create_link.click_calls == 1
    assert page.goto_calls == []
    assert page.url == PUBLISH_URL


def test_navigate_to_editor_rejects_recovered_existing_draft() -> None:
    old_id = "7680498739710149160"
    page = _CreateArticlePage(f"{PUBLISH_URL}?pgc_id={old_id}")
    platform = _make_platform(page)
    platform._draft_baseline = _snapshot(
        total=1,
        title="旧文章",
        title_count=1,
        ids={old_id},
    )

    with pytest.raises(DraftBaselineError, match="恢复了已有草稿"):
        run(platform.navigate_to_editor())

    assert page.create_link.click_calls == 1


def test_fetch_identity_parses_same_origin_api(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = FakePage()
    page.responses_on_goto = [
        FakeResponse(
            IDENTITY_API_URL,
            {"data": {"user_name": "率真海风gy504gO"}, "err_no": 0},
            method="GET",
        )
    ]
    platform = _make_platform(page)

    payload = run(platform.fetch_identity_payload())

    assert payload == {
        "ok": True,
        "user_id": "2610667347771923",
        "display_name": "率真海风gy504gO",
    }
    assert run(platform.fetch_identity_payload()) == payload
    assert page.goto_calls == [HOME_URL]


def test_fetch_identity_dom_nickname_is_display_only(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = FakePage()
    page.dom_nickname = "工作台昵称兜底"
    platform = _make_platform(page)

    payload = run(platform.fetch_identity_payload())

    assert payload == {
        "ok": False,
        "user_id": "",
        "display_name": "工作台昵称兜底",
    }


def test_fetch_identity_fails_when_nothing_confirms(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    platform = _make_platform(FakePage())

    assert run(platform.fetch_identity_payload()) == {"ok": False}


def test_check_login_requires_cookie_and_stable_identity(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = FakePage()
    page.responses_on_goto = [
        FakeResponse(
            IDENTITY_API_URL,
            {"data": {"user_name": "率真海风gy504gO"}},
            method="GET",
        )
    ]
    platform = _make_platform(
        page,
        cookies=[{"name": "sessionid", "value": "secret-value"}],
    )

    assert run(platform.check_login()) is True
    assert "secret-value" not in platform.last_login_error
    assert page.goto_calls == [HOME_URL]


def test_check_login_false_without_session_cookie(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = FakePage()
    platform = _make_platform(page)

    assert run(platform.check_login()) is False
    assert platform.last_login_error.startswith("LOGIN_REQUIRED:")
    assert page.goto_calls == []


def test_pgc_id_and_edit_url_use_strict_allowlist() -> None:
    valid = "7665272216262623790"
    edit_url = f"{PUBLISH_URL}?pgc_id={valid}"

    assert ToutiaoPlatform._safe_pgc_id(valid) == valid
    assert ToutiaoPlatform._edit_url(valid) == edit_url
    assert ToutiaoPlatform._pgc_id_from_url(edit_url) == valid
    assert ToutiaoPlatform._safe_pgc_id("../../etc/passwd") is None
    assert ToutiaoPlatform._safe_pgc_id(True) is None
    assert ToutiaoPlatform._pgc_id_from_url(
        f"https://example.com/profile_v4/graphic/publish?pgc_id={valid}"
    ) is None
    assert ToutiaoPlatform._pgc_id_from_url(
        f"{PUBLISH_URL}?pgc_id={valid}&pgc_id=123456"
    ) is None
    assert ToutiaoPlatform._pgc_id_from_url(
        f"{PUBLISH_URL}/other?pgc_id={valid}"
    ) is None


def test_save_id_extraction_rejects_generic_id_fields() -> None:
    valid = "7665272216262623790"

    extracted = ToutiaoPlatform._extract_save_ids(
        {
            "id": "111111",
            "item_id": "222222",
            "nested": {"id": "333333", "gid": valid},
        }
    )

    assert extracted == frozenset({valid})


def test_draft_item_extraction_accepts_verified_creator_center_gid() -> None:
    valid = "7673817215686230543"

    assert ToutiaoPlatform._extract_draft_items(
        {
            "draft_list": [
                {
                    "title": "头条草稿测试",
                    "gid": valid,
                    "draft_type": 2,
                }
            ]
        }
    ) == [("头条草稿测试", valid)]


def test_preflight_freezes_normalized_title_and_draft_baseline() -> None:
    title = "这是一个超过三十个字的头条号测试标题用于验证统一标题归一规则不会漂移"
    expected_title = ToutiaoPlatform._normalize_platform_title(title)
    baseline = _snapshot(total=3, title=expected_title, title_count=1, ids={"111111"})
    platform = _make_platform(FakePage())

    async def fetch_snapshot(*, expected_title: str | None = None):
        assert expected_title == expected_title_value
        return baseline

    expected_title_value = expected_title
    platform._fetch_draft_snapshot = fetch_snapshot

    run(platform.preflight_delivery(title))

    assert len(expected_title) == 30
    assert platform._preflight_title == expected_title
    assert platform._draft_baseline is baseline


class _UnstableDraftPage(FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.states = [
            {
                "loaded": True,
                "totalCount": 1,
                "titleCounts": {"旧草稿": 1},
                "hrefItems": [],
            },
            {
                "loaded": True,
                "totalCount": 2,
                "titleCounts": {"旧草稿": 1, "加载中草稿": 1},
                "hrefItems": [],
            },
            {
                "loaded": True,
                "totalCount": 3,
                "titleCounts": {"旧草稿": 1, "加载中草稿": 2},
                "hrefItems": [],
            },
        ]

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    async def evaluate(self, script: str, *_args) -> object:
        if "titleCounts" in script:
            return self.states.pop(0)
        return await super().evaluate(script)


def test_draft_snapshot_rejects_list_that_never_stabilizes(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    platform = _make_platform(_UnstableDraftPage())

    with pytest.raises(DraftBaselineError) as raised:
        run(platform._fetch_draft_snapshot(expected_title="旧草稿"))

    assert "草稿列表仍在变化" in str(raised.value)


class _StableIncompleteDraftPage(FakePage):
    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    async def evaluate(self, script: str, *_args) -> object:
        if "titleCounts" in script:
            return {
                "loaded": True,
                "totalCount": 1,
                "titleCounts": {"旧草稿": 1},
                "hrefItems": [],
            }
        return await super().evaluate(script)


def test_draft_snapshot_rejects_stable_list_with_missing_pgc_ids(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    platform = _make_platform(_StableIncompleteDraftPage())

    with pytest.raises(DraftBaselineError) as raised:
        run(platform._fetch_draft_snapshot(expected_title="旧草稿"))

    assert "目标标题的草稿 ID 未完整稳定加载" in str(raised.value)


class _ActiveDraftVisiblePage(FakePage):
    def __init__(self, *, title: str, draft_id: str) -> None:
        super().__init__()
        self.title = title
        self.draft_id = draft_id

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    async def evaluate(self, script: str, *_args) -> object:
        if "titleCounts" in script:
            return {
                "loaded": True,
                "totalCount": 13,
                "titleCounts": {self.title: 13},
                "hrefItems": [
                    {
                        "title": self.title,
                        "href": f"{PUBLISH_URL}?pgc_id={self.draft_id}",
                    }
                ],
            }
        return await super().evaluate(script)


def test_draft_snapshot_accepts_bound_new_id_without_all_historical_ids(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    new_id = "7680801349465899554"
    platform = _make_platform(_ActiveDraftVisiblePage(title=title, draft_id=new_id))
    platform._active_draft_id = new_id
    platform._draft_baseline = _snapshot(
        total=12,
        title=title,
        title_count=12,
        ids={str(index) for index in range(100000, 100012)},
    )

    snapshot = run(platform._fetch_draft_snapshot(expected_title=title))

    assert snapshot.title_counts[title] == 13
    assert snapshot.title_to_ids[title] == frozenset({new_id})


def test_draft_snapshot_allows_response_bound_id_while_card_is_delayed(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    old_id = "111111"
    new_id = "7680806313923756578"
    platform = _make_platform(_StableIncompleteDraftPage())
    platform._active_draft_id = new_id
    platform._draft_baseline = _snapshot(
        total=1,
        title="旧草稿",
        title_count=1,
        ids={old_id},
    )
    platform._autosave_records = [_record(code=0, draft_ids={new_id})]

    snapshot = run(platform._fetch_draft_snapshot(expected_title="旧草稿"))

    assert snapshot.title_counts["旧草稿"] == 1
    assert snapshot.title_to_ids.get("旧草稿", frozenset()) == frozenset()


class _NativeExitControl:
    def __init__(self, page) -> None:
        self.page = page

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def click(self, **_kwargs) -> None:
        self.page.exit_click_calls += 1
        self.page.url = HOME_URL


class _ExitToEmptyDraftPage(FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.url = PUBLISH_URL
        self.back_calls = 0
        self.exit_click_calls = 0

    async def go_back(self, **_kwargs) -> None:
        self.back_calls += 1
        self.url = DRAFT_BOX_URL

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        self.url = url

    def locator(self, selector: str):
        assert selector == EDITOR_EXIT_SELECTOR
        return _NativeExitControl(self)

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    async def evaluate(self, script: str, *_args) -> object:
        if "titleCounts" in script:
            return {
                "loaded": True,
                "totalCount": 0,
                "titleCounts": {},
                "hrefItems": [],
            }
        return await super().evaluate(script)


def test_draft_snapshot_exits_editor_with_native_control_once(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _ExitToEmptyDraftPage()
    platform = _make_platform(page)

    snapshot = run(platform._fetch_draft_snapshot(exit_editor=True))

    assert snapshot.loaded is True
    assert snapshot.total_count == 0
    assert page.exit_click_calls == 1
    assert page.back_calls == 0
    assert page.goto_calls == [DRAFT_BOX_URL]


class _DelayedNewDraftPage(_ExitToEmptyDraftPage):
    def __init__(self, title: str, draft_id: str) -> None:
        super().__init__()
        self.title = title
        self.draft_id = draft_id
        self.reads = 0

    async def evaluate(self, script: str, *_args) -> object:
        if "titleCounts" not in script:
            return await super().evaluate(script)
        self.reads += 1
        if self.reads < 3:
            return {
                "loaded": True,
                "totalCount": 0,
                "titleCounts": {},
                "hrefItems": [],
            }
        return {
            "loaded": True,
            "totalCount": 1,
            "titleCounts": {self.title: 1},
            "hrefItems": [
                {
                    "title": self.title,
                    "href": f"{PUBLISH_URL}?pgc_id={self.draft_id}",
                }
            ],
        }


def test_draft_snapshot_waits_for_new_entity_after_native_exit(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    draft_id = "7680498739710149160"
    page = _DelayedNewDraftPage(title, draft_id)
    platform = _make_platform(page)
    platform._draft_baseline = _snapshot(
        total=0,
        title=title,
        title_count=0,
        ids=set(),
    )

    snapshot = run(
        platform._fetch_draft_snapshot(
            exit_editor=True,
            expected_title=title,
        )
    )

    assert snapshot.title_to_ids[title] == frozenset({draft_id})
    assert page.exit_click_calls == 1
    assert page.reads == 3


class _ModelEditorLocator:
    def __init__(self, page: _ModelPage) -> None:
        self.page = page

    async def click(self, **_kwargs) -> None:
        return None

    async def focus(self, **_kwargs) -> None:
        return None

    async def count(self) -> int:
        return 1

    async def press(self, key: str) -> None:
        if key == "Backspace":
            self.page.tokens.clear()
            self.page.selected_index = None
        elif key == "Control+End" and self.page.tokens:
            self.page.selected_index = len(self.page.tokens) - 1

    async def inner_text(self) -> str:
        return "\n".join(
            token["text"] for token in self.page.tokens if token["kind"] != "I"
        )

    def locator(self, selector: str):
        if selector == ":scope > p:last-child":
            return _ModelTailLocator(self.page)
        if selector.startswith(":scope > :nth-child("):
            index = int(selector.removeprefix(":scope > :nth-child(").removesuffix(")"))
            return _ModelBlockLocator(self.page, index - 1)
        raise AssertionError(f"unexpected editor selector: {selector}")


class _ModelTailLocator:
    def __init__(self, page: _ModelPage) -> None:
        self.page = page

    async def count(self) -> int:
        return 1

    async def evaluate(self, script: str) -> bool:
        assert "breaks.length <= 1" in script
        assert "ProseMirror-trailingBreak" not in script
        assert "syl-placeholder.ProseMirror-widget" in script
        assert "placeholders.length <= 1" in script
        self.page.tail_checks += 1
        tail_is_empty = not self.page.tokens or (
            self.page.tokens[-1]["kind"] == "P"
            and not self.page.tokens[-1]["text"]
        )
        tail_is_safe = self.page.tail_is_safe
        if self.page.tail_safety_reads is not None:
            if len(self.page.tail_safety_reads) > 1:
                tail_is_safe = self.page.tail_safety_reads.pop(0)
            else:
                tail_is_safe = self.page.tail_safety_reads[0]
        return tail_is_safe and tail_is_empty

    async def click(self, **_kwargs) -> None:
        if not self.page.tokens:
            self.page.tokens.append({"kind": "P", "text": ""})
        self.page.selected_index = len(self.page.tokens) - 1


class _ModelBlockLocator:
    def __init__(self, page: _ModelPage, index: int) -> None:
        self.page = page
        self.index = index

    async def count(self) -> int:
        return int(0 <= self.index < len(self.page.tokens))

    async def click(self, **_kwargs) -> None:
        self.page.selected_index = self.index


class _ModelHeadingLocator:
    def __init__(self, page: _ModelPage) -> None:
        self.page = page

    async def count(self) -> int:
        return 1

    async def click(self, **_kwargs) -> None:
        if not self.page.heading_click_applies:
            return
        if not self.page.tokens:
            self.page.tokens.append({"kind": "H2", "text": ""})
            self.page.selected_index = 0
        else:
            index = self.page.selected_index
            if index is None:
                index = len(self.page.tokens) - 1
            self.page.tokens[index]["kind"] = "H2"


class _ModelKeyboard:
    def __init__(self, page: _ModelPage) -> None:
        self.page = page

    async def press(self, key: str) -> None:
        if key == "Enter":
            self.page.tokens.append({"kind": "P", "text": ""})
            self.page.selected_index = len(self.page.tokens) - 1
        elif key == "Shift+Enter":
            if not self.page.tokens:
                self.page.tokens.append({"kind": "P", "text": ""})
            self.page.tokens[-1]["text"] += "\n"
        elif key == "Control+Alt+2":
            assert self.page.tokens
            self.page.h2_shortcut_calls += 1
            index = self.page.selected_index
            if index is None:
                index = len(self.page.tokens) - 1
            self.page.tokens[index]["kind"] = "H2"

    async def insert_text(self, text: str) -> None:
        if not self.page.tokens:
            self.page.tokens.append({"kind": "P", "text": ""})
            self.page.selected_index = 0
        index = self.page.selected_index
        if index is None:
            index = len(self.page.tokens) - 1
            self.page.selected_index = index
        self.page.tokens[index]["text"] += text


class _ModelPage:
    def __init__(self) -> None:
        self.tokens: list[dict[str, str]] = []
        self.keyboard = _ModelKeyboard(self)
        self.selection_ready = True
        self.selection_checks = 0
        self.tail_checks = 0
        self.tail_is_safe = True
        self.tail_safety_reads: list[bool] | None = None
        self.selection_ready_reads: list[bool] | None = None
        self.heading_click_applies = True
        self.selected_index: int | None = None
        self.h2_shortcut_calls = 0

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str):
        if selector == BODY_SELECTOR:
            return _ModelEditorLocator(self)
        if selector == HEADING_BUTTON_SELECTOR:
            return _ModelHeadingLocator(self)
        raise AssertionError(f"unexpected selector: {selector}")

    async def evaluate(self, script: str, *_args):
        if "const reuseExistingTail = true" in script:
            return bool(
                self.tokens
                and self.tokens[-1]["kind"] == "P"
                and not self.tokens[-1]["text"]
            )
        if "requestAnimationFrame" in script:
            return None
        if "selection.rangeCount" in script:
            if "placeholders.length" in script:
                assert "syl-placeholder.ProseMirror-widget" in script
            self.selection_checks += 1
            if _args and "expected.index" in script:
                target = _args[0]
                index = int(target["index"])
                return bool(
                    self.selection_ready
                    and self.selected_index == index
                    and 0 <= index < len(self.tokens)
                    and self.tokens[index]["text"] == target["text"]
                )
            if self.selection_ready_reads is not None:
                if len(self.selection_ready_reads) > 1:
                    return self.selection_ready_reads.pop(0)
                return self.selection_ready_reads[0]
            return self.selection_ready
        if "return index >= 0 && text ? {index, text} : null" in script:
            if not self.tokens:
                return None
            index = self.selected_index
            if index is None:
                index = len(self.tokens) - 1
            token = self.tokens[index]
            return {"index": index, "text": token["text"]} if token["text"] else None
        if "const block = root?.children?.[expected.index]" in script:
            target = _args[0]
            index = int(target["index"])
            return bool(
                0 <= index < len(self.tokens)
                and self.tokens[index]["kind"] == "H2"
                and self.tokens[index]["text"] == target["text"]
            )
        if "const tokens = []" in script:
            return [
                dict(token)
                for token in self.tokens
                if token["text"] or token["kind"] == "I"
            ]
        return None

    def append_image(self, fingerprint: str) -> None:
        token = {"kind": "I", "text": "", "fingerprint": fingerprint}
        if self.tokens and not self.tokens[-1]["text"]:
            self.tokens[-1] = token
        else:
            self.tokens.append(token)
        self.selected_index = len(self.tokens) - 1


def test_heading_selector_targets_only_static_editor_toolbar() -> None:
    assert HEADING_BUTTON_SELECTOR == (
        ".syl-editor-toolbar .syl-toolbar-tool.header.static "
        "button.syl-toolbar-button"
    )


def test_image_selector_targets_only_static_editor_toolbar() -> None:
    assert IMAGE_BUTTON_SELECTOR == (
        ".syl-editor-toolbar .syl-toolbar-tool.image.static "
        "button.syl-toolbar-button"
    )


def test_apply_h2_uses_editor_shortcut_when_static_button_does_not_apply() -> None:
    page = _ModelPage()
    page.tokens = [{"kind": "P", "text": "二级标题"}]
    page.selected_index = 0
    page.heading_click_applies = False
    platform = _make_platform(page)
    platform.simulator.random_delay = AsyncMock()

    run(platform._apply_h2_to_current_block())

    assert page.tokens == [{"kind": "H2", "text": "二级标题"}]
    assert page.h2_shortcut_calls == 1


def test_apply_h2_does_not_use_shortcut_when_button_already_changed_target() -> None:
    page = _ModelPage()
    page.tokens = [{"kind": "P", "text": "二级标题"}]
    page.selected_index = 0
    platform = _make_platform(page)
    platform.simulator.random_delay = AsyncMock()

    run(platform._apply_h2_to_current_block())

    assert page.tokens == [{"kind": "H2", "text": "二级标题"}]
    assert page.h2_shortcut_calls == 0


def test_fill_content_preserves_text_h2_and_image_token_order() -> None:
    page = _ModelPage()
    platform = _make_platform(page)
    content_blocks = [
        {"type": "text", "text": "第一段"},
        {"type": "image", "position": 1},
        {"type": "heading", "text": "二级标题"},
        {"type": "text", "text": "标题后的正文"},
        {"type": "image", "position": 4},
    ]
    images = [
        {"position_index": 1, "local_path": "D:/controlled/one.png"},
        {"position_index": 4, "local_path": "D:/controlled/two.png"},
    ]

    fingerprints = iter(("p3-sign.example/one.png", "p3-sign.example/two.png"))

    async def upload_once(_path: str) -> dict:
        fingerprint = next(fingerprints)
        page.append_image(fingerprint)
        return {"success": True, "error": "", "fingerprint": fingerprint}

    platform._upload_image = upload_once

    result = run(platform.fill_content(content_blocks, images))

    expected = [
        {"kind": "P", "text": "第一段"},
        {"kind": "I", "text": "", "fingerprint": "p3-sign.example/one.png"},
        {"kind": "H2", "text": "二级标题"},
        {"kind": "P", "text": "标题后的正文"},
        {"kind": "I", "text": "", "fingerprint": "p3-sign.example/two.png"},
    ]
    assert page.tokens == expected
    assert platform._expected_persisted_tokens == expected
    assert result["media_status"] == "completed"
    assert result["uploaded_images"] == 2
    assert page.tail_checks == 0
    assert page.selection_checks == 0


def test_fill_content_stops_before_upload_when_image_selection_is_unstable() -> None:
    page = _ModelPage()
    page.selection_ready_reads = [False]
    platform = _make_platform(page)
    upload_calls: list[str] = []

    async def upload_once(path: str) -> dict:
        upload_calls.append(path)
        return {"success": True, "error": "", "fingerprint": "example/image.png"}

    platform._upload_image = upload_once

    with pytest.raises(DraftResultUnknownError) as raised:
        run(
            platform.fill_content(
                [
                    {"type": "image", "position": 1},
                ],
                [{"position_index": 1, "local_path": "D:/controlled/one.png"}],
            )
        )

    assert "图片插入位置未稳定" in str(raised.value)
    assert raised.value.media_progress == {
        "expected_images": 1,
        "uploaded_images": 0,
        "failed_image_count": 1,
        "media_status": "failed",
    }
    assert page.tail_checks == 1
    assert page.selection_checks == 24
    assert upload_calls == []


def test_fill_content_stops_before_upload_when_tail_paragraph_is_not_safe() -> None:
    page = _ModelPage()
    page.tail_safety_reads = [False]
    platform = _make_platform(page)
    upload_calls: list[str] = []

    async def upload_once(path: str) -> dict:
        upload_calls.append(path)
        return {"success": True, "error": "", "fingerprint": "example/image.png"}

    platform._upload_image = upload_once

    with pytest.raises(DraftResultUnknownError) as raised:
        run(
            platform.fill_content(
                [
                    {"type": "image", "position": 1},
                ],
                [{"position_index": 1, "local_path": "D:/controlled/one.png"}],
            )
        )

    assert "图片插入位置未稳定" in str(raised.value)
    assert page.tail_checks == 1
    assert page.selection_checks == 0
    assert upload_calls == []


def test_publish_stops_after_first_image_failure_before_save_or_publish() -> None:
    page = _ModelPage()
    platform = _make_platform(page)
    upload_calls: list[str] = []
    save_calls: list[str] = []
    publish_calls: list[str] = []

    async def check_login() -> bool:
        return True

    async def no_op(*_args, **_kwargs) -> None:
        return None

    async def upload_once(path: str) -> dict:
        upload_calls.append(path)
        return {"success": False, "error": "头条号图片确认失败"}

    async def save_draft(title: str = "") -> str:
        save_calls.append(title)
        return "https://mp.toutiao.com/profile_v4/manage/draft"

    async def publish_now(title: str = "") -> str:
        publish_calls.append(title)
        return "https://example.invalid/published"

    platform.check_login = check_login
    platform.preflight_delivery = no_op
    platform.navigate_to_editor = no_op
    platform.fill_title = no_op
    platform._upload_image = upload_once
    platform.save_draft = save_draft
    platform.publish_now = publish_now

    class _Db:
        @staticmethod
        def add_task_log(*_args, **_kwargs) -> None:
            return None

    result = run(
        platform.publish(
            title="头条号失败停止测试",
            content_blocks=[
                {"type": "text", "text": "第一段"},
                {"type": "image", "position": 1},
                {"type": "image", "position": 2},
            ],
            images=[
                {"position_index": 1, "local_path": "D:/controlled/one.png"},
                {"position_index": 2, "local_path": "D:/controlled/two.png"},
            ],
            delivery_mode="DRAFT",
            auto_login=False,
            db=_Db(),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "DRAFT_RESULT_UNKNOWN"
    assert result["error"] == (
        "DRAFT_RESULT_UNKNOWN: 头条号图片确认失败；已停止后续写入"
    )
    assert result["media_progress"] == {
        "expected_images": 1,
        "uploaded_images": 0,
        "failed_image_count": 1,
        "media_status": "failed",
    }
    assert upload_calls == ["D:/controlled/one.png"]
    assert save_calls == []
    assert publish_calls == []


class _UploadLocator:
    def __init__(
        self,
        page: _UploadPage,
        selector: str,
        *,
        index: int | None = None,
    ) -> None:
        self.page = page
        self.selector = selector
        self.index = index

    def locator(self, selector: str):
        assert self.selector == IMAGE_DRAWER_SELECTOR
        mapping = {
            IMAGE_UPLOAD_TAB_IN_DRAWER_SELECTOR: IMAGE_UPLOAD_TAB_SELECTOR,
            BODY_IMAGE_INPUT_IN_DRAWER_SELECTOR: BODY_IMAGE_INPUT_SELECTOR,
            IMAGE_CONFIRM_IN_DRAWER_SELECTOR: IMAGE_CONFIRM_SELECTOR,
            IMAGE_DRAWER_CLOSE_IN_DRAWER_SELECTOR: IMAGE_DRAWER_CLOSE_SELECTOR,
            IMAGE_UPLOAD_ERROR_IN_DRAWER_SELECTOR: IMAGE_UPLOAD_ERROR_SELECTOR,
            ".upload-image-panel": ".upload-image-panel",
        }
        canonical = mapping[selector]
        self.page.locator_calls.append(canonical)
        return _UploadLocator(self.page, canonical)

    async def count(self) -> int:
        if self.selector == IMAGE_DRAWER_SELECTOR:
            return self.page.drawer_count
        if self.selector == IMAGE_CONFIRM_SELECTOR:
            return self.page.confirm_count
        if self.selector == IMAGE_DRAWER_CLOSE_SELECTOR:
            return self.page.close_count
        if self.selector == IMAGE_UPLOAD_ERROR_SELECTOR:
            return len(self.page.upload_error_texts)
        if self.selector == BODY_IMAGE_INPUT_SELECTOR:
            return int(self.page.local_upload_mounted)
        if self.selector == ".upload-image-panel":
            return 1
        return 1

    async def wait_for(self, **_kwargs) -> None:
        self.page.waited_selectors.append(self.selector)
        state = _kwargs.get("state")
        configured_error = self.page.wait_errors.get((self.selector, state))
        if configured_error is not None:
            raise configured_error
        if self.selector == IMAGE_CONFIRM_SELECTOR:
            assert state == "attached"
            if not self.page.confirm_attaches:
                raise TimeoutError("confirm did not attach")
            return
        if self.selector == BODY_IMAGE_INPUT_SELECTOR:
            assert state == "attached"
            if not self.page.local_upload_mounted:
                raise TimeoutError("local upload input did not attach")
            return
        if self.selector == IMAGE_DRAWER_SELECTOR:
            if state in {"attached", "visible"}:
                assert self.page.image_panel_open is True
                return
            assert state == "hidden"
            if not self.page.drawer_hides:
                raise TimeoutError("drawer remained visible")
            assert self.page.image_panel_open is False

    async def is_enabled(self) -> bool:
        assert self.selector == IMAGE_CONFIRM_SELECTOR
        self.page.confirm_enabled_checks += 1
        enabled_after = self.page.confirm_enabled_after
        return enabled_after is not None and self.page.confirm_enabled_checks > enabled_after

    async def is_visible(self) -> bool:
        if self.selector == IMAGE_DRAWER_SELECTOR:
            return self.page.image_panel_open
        if self.selector == BODY_IMAGE_INPUT_SELECTOR:
            return self.page.local_upload_visible
        if self.selector == ".upload-image-panel":
            return self.page.local_upload_visible
        raise AssertionError(f"unexpected visible selector: {self.selector}")

    def nth(self, index: int):
        assert self.selector == IMAGE_UPLOAD_TAB_SELECTOR
        return _UploadLocator(self.page, self.selector, index=index)

    async def get_attribute(self, name: str) -> str | None:
        assert self.selector == IMAGE_UPLOAD_TAB_SELECTOR
        assert self.index is not None
        text = self.page.tab_texts[self.index]
        if name == "class":
            return (
                "byte-tabs-header-title active"
                if text == self.page.active_tab_text
                else "byte-tabs-header-title"
            )
        if name == "aria-selected":
            return "true" if text == self.page.active_tab_text else "false"
        return None

    async def click(self, **_kwargs) -> None:
        if self.selector == IMAGE_BUTTON_SELECTOR:
            self.page.image_panel_open = True
        elif self.selector == IMAGE_UPLOAD_TAB_SELECTOR:
            assert self.index is not None
            text = self.page.tab_texts[self.index]
            if text == "上传图片":
                self.page.local_upload_tab_clicks += 1
                self.page.active_tab_text = text
                self.page.local_upload_visible = True
                self.page.local_upload_mounted = True
            else:
                self.page.search_tab_clicks += 1
        elif self.selector == IMAGE_CONFIRM_SELECTOR:
            self.page.confirm_clicks += 1
            if self.page.confirm_hides_drawer:
                self.page.image_panel_open = False
            if self.page.fingerprint_reads is None:
                self.page.fingerprints.append("p3-sign.example/article-image.png")
        elif self.selector == IMAGE_DRAWER_CLOSE_SELECTOR:
            self.page.close_clicks += 1
            self.page.image_panel_open = False

    async def set_input_files(self, path: str, **_kwargs) -> None:
        assert self.selector == BODY_IMAGE_INPUT_SELECTOR
        assert self.page.image_panel_open is True
        self.page.upload_paths.append(path)
        if self.page.set_input_error is not None:
            raise self.page.set_input_error
        if self.page.upload_response is not None:
            for handler in list(self.page.response_handlers):
                await handler(self.page.upload_response)

    async def all_inner_texts(self) -> list[str]:
        if self.selector == IMAGE_UPLOAD_ERROR_SELECTOR:
            return list(self.page.upload_error_texts)
        if self.selector == IMAGE_UPLOAD_TAB_SELECTOR:
            return list(self.page.tab_texts)
        raise AssertionError(f"unexpected text selector: {self.selector}")


class _UploadPage:
    def __init__(self) -> None:
        self.fingerprints: list[str] = []
        self.fingerprint_reads: list[list[str]] | None = None
        self.image_panel_open = False
        self.drawer_count = 1
        self.confirm_count = 1
        self.close_count = 1
        self.confirm_attaches = True
        self.confirm_enabled_after: int | None = 0
        self.confirm_enabled_checks = 0
        self.confirm_hides_drawer = True
        self.confirm_clicks = 0
        self.close_clicks = 0
        self.drawer_hides = True
        self.upload_paths: list[str] = []
        self.locator_calls: list[str] = []
        self.waited_selectors: list[str] = []
        self.wait_errors: dict[tuple[str, str | None], Exception] = {}
        self.upload_error_texts: list[str] = []
        self.upload_response: FakeResponse | None = None
        self.set_input_error: Exception | None = None
        self.local_upload_visible = True
        self.local_upload_mounted = True
        self.local_upload_tab_clicks = 0
        self.search_tab_clicks = 0
        self.tab_texts = ["上传图片", "免费正版图片", "热点图库", "我的素材"]
        self.active_tab_text = "上传图片"
        self.response_handlers: list = []
        self.removed_response_handlers: list = []

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        if selector in {
            IMAGE_BUTTON_SELECTOR,
            BODY_IMAGE_INPUT_SELECTOR,
            IMAGE_CONFIRM_SELECTOR,
            IMAGE_DRAWER_CLOSE_SELECTOR,
            IMAGE_DRAWER_SELECTOR,
            IMAGE_UPLOAD_TAB_SELECTOR,
            IMAGE_UPLOAD_ERROR_SELECTOR,
        }:
            return _UploadLocator(self, selector)
        raise AssertionError(f"unexpected selector: {selector}")

    def on(self, event: str, handler) -> None:
        assert event == "response"
        self.response_handlers.append(handler)

    def remove_listener(self, event: str, handler) -> None:
        assert event == "response"
        self.removed_response_handlers.append(handler)
        if handler in self.response_handlers:
            self.response_handlers.remove(handler)

    async def evaluate(self, script: str, *_args):
        if "fingerprintsForBlock" in script:
            if self.fingerprint_reads is not None:
                if len(self.fingerprint_reads) > 1:
                    return list(self.fingerprint_reads.pop(0))
                return list(self.fingerprint_reads[0])
            return list(self.fingerprints)
        return None


def test_editor_image_fingerprints_scan_all_images_inside_editor() -> None:
    page = _UploadPage()
    page.fingerprints = ["p3-sign.example/nested-image.png"]
    platform = _make_platform(page)

    result = run(platform._read_editor_image_fingerprints())

    assert result == ["p3-sign.example/nested-image.png"]


def test_upload_image_uses_only_exact_body_image_input(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.upload_response = FakeResponse(
        "https://mp.toutiao.com/spice/image?upload_source=article&aid=1231&device_platform=web",
        {
            "code": 0,
            "data": {"origin_image_url": "https://image.example/article-image.png"},
        },
    )
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result == {
        "success": True,
        "error": "",
        "fingerprint": "p3-sign.example/article-image.png",
    }
    assert page.upload_paths == ["D:/controlled/article-image.png"]
    assert page.locator_calls == [
        IMAGE_BUTTON_SELECTOR,
        IMAGE_DRAWER_SELECTOR,
        IMAGE_UPLOAD_TAB_SELECTOR,
        ".upload-image-panel",
        BODY_IMAGE_INPUT_SELECTOR,
        BODY_IMAGE_INPUT_SELECTOR,
        IMAGE_CONFIRM_SELECTOR,
    ]
    assert page.waited_selectors == [
        IMAGE_DRAWER_SELECTOR,
        BODY_IMAGE_INPUT_SELECTOR,
        BODY_IMAGE_INPUT_SELECTOR,
        IMAGE_CONFIRM_SELECTOR,
        IMAGE_DRAWER_SELECTOR,
    ]
    assert page.confirm_clicks == 1
    assert page.close_clicks == 0
    assert page.response_handlers == []
    assert len(page.removed_response_handlers) == 1
    assert BODY_IMAGE_INPUT_SELECTOR == (
        ".byte-drawer-wrapper:has(.byte-tabs-header-title):visible "
        ".upload-image-panel [data-e2e='image-upload'] "
        "input[type='file'][accept*='image']"
    )


def test_upload_image_switches_from_search_to_local_upload_tab(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.local_upload_visible = False
    page.local_upload_mounted = False
    page.tab_texts = ["搜图", "上传图片", "免费正版图片", "热点图库"]
    page.active_tab_text = "搜图"
    page.upload_response = FakeResponse(
        "https://mp.toutiao.com/spice/image?upload_source=article&aid=1231&device_platform=web",
        {
            "code": 0,
            "data": {"origin_image_url": "https://image.example/article-image.png"},
        },
    )
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result["success"] is True
    assert page.local_upload_tab_clicks == 1
    assert page.search_tab_clicks == 0
    assert page.active_tab_text == "上传图片"
    assert page.upload_paths == ["D:/controlled/article-image.png"]
    assert IMAGE_DRAWER_SELECTOR == (
        ".byte-drawer-wrapper:has(.byte-tabs-header-title):visible"
    )


@pytest.mark.parametrize(
    ("url", "method", "matches"),
    [
        (
            "https://mp.toutiao.com/spice/image?upload_source=article&aid=1231&device_platform=web",
            "POST",
            True,
        ),
        (
            "https://mp.toutiao.com/spice/image?upload_source=article&aid=1231&device_platform=web",
            "GET",
            False,
        ),
        (
            "https://mp.toutiao.com/spice/image?aid=1231",
            "POST",
            False,
        ),
        (
            "https://evil.example/spice/image?upload_source=article&aid=1231&device_platform=web",
            "POST",
            False,
        ),
        (
            "https://mp.toutiao.com/spice/image?upload_source=article&aid=1231&device_platform=web&need_enhance=true",
            "POST",
            False,
        ),
    ],
)
def test_body_image_upload_response_match_is_exact(
    url: str,
    method: str,
    matches: bool,
) -> None:
    response = FakeResponse(url, {"code": 0}, method=method)

    assert run(ToutiaoPlatform._is_body_image_upload_response(response)) is matches
    assert IMAGE_UPLOAD_PATH == "/spice/image"


def test_body_image_upload_response_ignores_json_requests() -> None:
    response = FakeResponse(
        "https://mp.toutiao.com/spice/image?upload_source=article&aid=1231&device_platform=web",
        {"code": 0},
        content_type="application/json",
    )

    assert run(ToutiaoPlatform._is_body_image_upload_response(response)) is False


@pytest.mark.parametrize(
    ("response", "drawer_error", "expected"),
    [
        (
            FakeResponse(
                "https://mp.toutiao.com/spice/image?upload_source=article&aid=1231&device_platform=web",
                {"code": 4001, "message": "图片格式不支持"},
            ),
            "",
            "头条号图片上传被拒绝（平台码 4001）：图片格式不支持",
        ),
        (
            FakeResponse(
                "https://mp.toutiao.com/spice/image?upload_source=article&aid=1231&device_platform=web",
                {"code": 0},
                status=429,
            ),
            "",
            "头条号图片上传请求失败（HTTP 429）",
        ),
        (
            None,
            "上传失败，请重试",
            "头条号图片上传失败：上传失败，请重试",
        ),
        (
            FakeResponse(
                "https://mp.toutiao.com/spice/image?upload_source=article&aid=1231&device_platform=web",
                {"code": 0, "data": {}},
            ),
            "",
            "头条号图片上传返回成功码，但未形成可确认的图片",
        ),
    ],
    ids=[
        "platform-rejected",
        "http-rejected",
        "drawer-error",
        "missing-origin",
    ],
)
def test_upload_image_surfaces_sanitized_platform_evidence(
    monkeypatch,
    response: FakeResponse | None,
    drawer_error: str,
    expected: str,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.confirm_enabled_after = None
    page.upload_response = response
    page.upload_error_texts = [drawer_error] if drawer_error else []
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result == {"success": False, "error": expected}
    assert page.response_handlers == []
    assert len(page.removed_response_handlers) == 1


def test_upload_image_distinguishes_success_response_from_disabled_confirm(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.confirm_enabled_after = None
    page.upload_response = FakeResponse(
        "https://mp.toutiao.com/spice/image?upload_source=article&aid=1231&device_platform=web",
        {"code": 0, "data": {"origin_image_url": "https://image.example/x.png"}},
    )
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result == {
        "success": False,
        "error": "头条号图片上传接口已成功，但确认按钮仍不可用",
    }


def test_upload_image_distinguishes_inserted_image_without_remote_fingerprint(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.fingerprint_reads = [[], [""]]
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result == {
        "success": False,
        "error": "图片已插入，但远程图片指纹尚未生成",
    }
    assert page.confirm_clicks == 1


def test_upload_image_redacts_playwright_exception_and_removes_listener(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.set_input_error = RuntimeError(
        "set_input_files C:\\private\\article.png "
        "https://mp.toutiao.com/spice/image?token=secret-value"
    )
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result["success"] is False
    assert "C:\\private" not in result["error"]
    assert "secret-value" not in result["error"]
    assert "[路径]" in result["error"]
    assert page.response_handlers == []
    assert len(page.removed_response_handlers) == 1


@pytest.mark.parametrize(
    (
        "drawer_count",
        "close_count",
        "drawer_hides",
        "confirm_hides_drawer",
        "error",
    ),
    [
        (0, 1, True, True, "头条号正文图片抽屉不存在或不唯一"),
        (2, 1, True, True, "头条号正文图片抽屉不存在或不唯一"),
        (
            1,
            0,
            True,
            False,
            "头条号确认图片后抽屉未关闭；头条号图片抽屉关闭按钮不存在或不唯一",
        ),
        (
            1,
            2,
            True,
            False,
            "头条号确认图片后抽屉未关闭；头条号图片抽屉关闭按钮不存在或不唯一",
        ),
        (
            1,
            1,
            False,
            False,
            "头条号确认图片后抽屉未关闭；头条号图片抽屉关闭后仍遮挡编辑器",
        ),
    ],
)
def test_upload_image_fails_closed_when_exact_drawer_cannot_close(
    monkeypatch,
    drawer_count: int,
    close_count: int,
    drawer_hides: bool,
    confirm_hides_drawer: bool,
    error: str,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.drawer_count = drawer_count
    page.close_count = close_count
    page.drawer_hides = drawer_hides
    page.confirm_hides_drawer = confirm_hides_drawer
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result == {"success": False, "error": error}


@pytest.mark.parametrize(
    ("fingerprint_reads", "error"),
    [
        ([[], []], "上传后编辑器图片数量未增加"),
        (
            [
                [],
                ["p3-sign.example/new.png"],
                ["p3-sign.example/new.png"],
                ["p3-sign.example/changed.png"],
                ["p3-sign.example/new.png"],
                ["p3-sign.example/changed.png"],
                ["p3-sign.example/new.png"],
                ["p3-sign.example/changed.png"],
            ],
            "上传后编辑器图片顺序未稳定",
        ),
        (
            [
                ["p3-sign.example/old.png"],
                ["p3-sign.example/new.png", "p3-sign.example/new.png"],
                ["p3-sign.example/new.png", "p3-sign.example/new.png"],
                ["p3-sign.example/new.png", "p3-sign.example/new.png"],
            ],
            "无法绑定本次上传图片指纹",
        ),
    ],
    ids=["count-not-increased", "fingerprint-unstable", "binding-failed"],
)
def test_upload_image_does_not_click_close_after_confirm_auto_hides_drawer(
    monkeypatch,
    fingerprint_reads: list[list[str]],
    error: str,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.fingerprint_reads = [list(items) for items in fingerprint_reads]
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result == {"success": False, "error": error}
    assert page.image_panel_open is False
    assert page.locator_calls == [
        IMAGE_BUTTON_SELECTOR,
        IMAGE_DRAWER_SELECTOR,
        IMAGE_UPLOAD_TAB_SELECTOR,
        ".upload-image-panel",
        BODY_IMAGE_INPUT_SELECTOR,
        BODY_IMAGE_INPUT_SELECTOR,
        IMAGE_CONFIRM_SELECTOR,
    ]
    assert page.confirm_clicks == 1
    assert page.close_clicks == 0
    assert IMAGE_DRAWER_CLOSE_SELECTOR not in page.locator_calls
    assert page.waited_selectors[-1] == IMAGE_DRAWER_SELECTOR


@pytest.mark.parametrize(
    (
        "confirm_attaches",
        "confirm_count",
        "confirm_enabled_after",
        "confirm_hides_drawer",
        "error",
    ),
    [
        (False, 1, 0, True, "头条号图片上传后未出现确认按钮"),
        (True, 0, 0, True, "头条号图片确认按钮不存在或不唯一"),
        (True, 2, 0, True, "头条号图片确认按钮不存在或不唯一"),
        (True, 1, None, True, "头条号图片上传未完成，确认按钮仍不可用"),
        (True, 1, 0, False, "头条号确认图片后抽屉未关闭"),
    ],
    ids=[
        "missing",
        "count-zero",
        "not-unique",
        "disabled",
        "drawer-still-visible",
    ],
)
def test_upload_image_confirmation_failure_closes_only_exact_drawer(
    monkeypatch,
    confirm_attaches: bool,
    confirm_count: int,
    confirm_enabled_after: int | None,
    confirm_hides_drawer: bool,
    error: str,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.confirm_attaches = confirm_attaches
    page.confirm_count = confirm_count
    page.confirm_enabled_after = confirm_enabled_after
    page.confirm_hides_drawer = confirm_hides_drawer
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result == {"success": False, "error": error}
    assert page.image_panel_open is False
    assert page.close_clicks == 1
    assert page.locator_calls[-1] == IMAGE_DRAWER_CLOSE_SELECTOR


def test_upload_image_preserves_original_failure_when_drawer_close_fails(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.confirm_attaches = False
    page.drawer_hides = False
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result == {
        "success": False,
        "error": (
            "头条号图片上传后未出现确认按钮；"
            "头条号图片抽屉关闭后仍遮挡编辑器"
        ),
    }
    assert IMAGE_DRAWER_CLOSE_SELECTOR in page.locator_calls


def test_upload_image_propagates_browser_lifecycle_error_after_scoped_cleanup(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    platform = _make_platform(page)

    async def raise_browser_closed(*_args, **_kwargs) -> dict:
        raise BrowserLifecycleError(
            "BROWSER_CONTEXT_CLOSED: 头条号上传图片时页面已关闭"
        )

    platform._upload_image_from_open_drawer = raise_browser_closed

    with pytest.raises(BrowserLifecycleError, match="BROWSER_CONTEXT_CLOSED"):
        run(platform._upload_image("D:/controlled/article-image.png"))

    assert page.image_panel_open is False
    assert page.close_clicks == 1
    assert page.locator_calls[-1] == IMAGE_DRAWER_CLOSE_SELECTOR


def test_upload_image_preserves_primary_browser_error_when_cleanup_also_fails(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    platform = _make_platform(page)

    async def raise_browser_closed(*_args, **_kwargs) -> dict:
        raise BrowserLifecycleError(
            "BROWSER_CONTEXT_CLOSED: 头条号上传图片时页面已关闭"
        )

    async def raise_cleanup_error(_drawer) -> str | None:
        raise RuntimeError("清理图片抽屉失败")

    platform._upload_image_from_open_drawer = raise_browser_closed
    platform._close_exact_image_drawer = raise_cleanup_error

    with pytest.raises(
        BrowserLifecycleError,
        match="头条号上传图片时页面已关闭",
    ):
        run(platform._upload_image("D:/controlled/article-image.png"))


@pytest.mark.parametrize(
    ("selector", "state"),
    [
        (IMAGE_DRAWER_SELECTOR, "visible"),
        (BODY_IMAGE_INPUT_SELECTOR, "attached"),
        (IMAGE_CONFIRM_SELECTOR, "attached"),
        (IMAGE_DRAWER_SELECTOR, "hidden"),
    ],
    ids=["drawer-open", "body-input", "confirm", "confirm-close"],
)
def test_upload_image_translates_closed_page_during_local_waits(
    monkeypatch,
    selector: str,
    state: str,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.wait_errors[(selector, state)] = RuntimeError(
        "Target page, context or browser has been closed"
    )
    platform = _make_platform(page)

    with pytest.raises(BrowserLifecycleError, match="BROWSER_CONTEXT_CLOSED"):
        run(platform._upload_image("D:/controlled/article-image.png"))


class _BlurLocator:
    def __init__(self, page) -> None:
        self.page = page

    async def evaluate(self, _script: str) -> None:
        self.page.blur_calls += 1


class _SaveTitleLocator:
    def __init__(self, page) -> None:
        self.page = page

    async def count(self) -> int:
        return 1

    async def fill(self, value: str) -> None:
        self.page.title = value
        self.page.title_fill_calls += 1

    async def input_value(self) -> str:
        return self.page.title


class _SavePage:
    def __init__(self, title: str = "") -> None:
        self.url = PUBLISH_URL
        self.response_handlers: list = []
        self.title = title
        self.title_fill_calls = 0
        self.blur_calls = 0
        self.exit_click_calls = 0
        self.goto_calls: list[str] = []

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str):
        if selector == BODY_SELECTOR:
            return _BlurLocator(self)
        if selector == TITLE_SELECTOR:
            return _SaveTitleLocator(self)
        if selector == EDITOR_EXIT_SELECTOR:
            return _NativeExitControl(self)
        raise AssertionError(f"unexpected selector: {selector}")

    def on(self, event: str, handler) -> None:
        assert event in {"request", "response"}
        if event == "response":
            self.response_handlers.append(handler)

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_selector(self, selector: str, **_kwargs) -> None:
        assert selector in {TITLE_SELECTOR, BODY_SELECTOR}


def test_fill_title_binds_new_draft_without_leaving_editor() -> None:
    page = _SavePage()
    platform = _make_platform(page)
    platform._draft_baseline = _snapshot(
        total=0,
        title="测试标题",
        title_count=0,
        ids=set(),
    )

    async def bind_new_draft(**_kwargs) -> dict[str, object]:
        platform._active_draft_id = "7665272216262623790"
        return {
            "generation": 1,
            "status": 200,
            "code": 0,
            "err_no": 0,
            "draft_ids": frozenset({platform._active_draft_id}),
        }

    platform._wait_for_autosave_barrier = AsyncMock(side_effect=bind_new_draft)
    platform._fetch_draft_snapshot = AsyncMock(
        return_value=_snapshot(
            total=1,
            title="测试标题",
            title_count=1,
            ids={"7665272216262623790"},
        )
    )

    run(platform.fill_title("测试标题"))

    assert page.title == "测试标题"
    assert page.title_fill_calls == 1
    assert page.blur_calls == 0
    assert page.goto_calls == []
    platform._wait_for_autosave_barrier.assert_awaited_once_with(
        generation=1,
        stage="标题初始化",
        require_draft_id=True,
    )
    platform._fetch_draft_snapshot.assert_not_awaited()


def test_fill_title_rejects_binding_to_existing_draft() -> None:
    existing_id = "7665272216262623790"
    page = _SavePage()
    platform = _make_platform(page)
    platform._draft_baseline = _snapshot(
        total=1,
        title="测试标题",
        title_count=1,
        ids={existing_id},
    )

    async def bind_existing_draft(**_kwargs) -> dict[str, object]:
        platform._active_draft_id = existing_id
        return {
            "generation": 1,
            "status": 200,
            "code": 0,
            "err_no": 0,
            "draft_ids": frozenset({existing_id}),
        }

    platform._wait_for_autosave_barrier = AsyncMock(
        side_effect=bind_existing_draft
    )

    with pytest.raises(DraftBaselineError, match="绑定了已有草稿"):
        run(platform.fill_title("测试标题"))

    assert page.blur_calls == 0
    assert page.goto_calls == []


class _ReplayTitleLocator:
    def __init__(self, page) -> None:
        self.page = page

    async def count(self) -> int:
        return 1

    async def fill(self, value: str) -> None:
        self.page.persisted_title = value
        self.page.title_fill_calls += 1

    async def input_value(self) -> str:
        if self.page.persisted_title_reads:
            if len(self.page.persisted_title_reads) > 1:
                return self.page.persisted_title_reads.pop(0)
            return self.page.persisted_title_reads[0]
        return self.page.persisted_title


class _ReplayPage(_SavePage):
    def __init__(
        self,
        *,
        title: str,
        tokens: list[dict[str, str]],
        title_reads: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.persisted_title = title
        self.persisted_title_reads = list(title_reads or [])
        self.persisted_tokens = tokens

    def locator(self, selector: str):
        if selector == BODY_SELECTOR:
            return _BlurLocator(self)
        if selector == TITLE_SELECTOR:
            return _ReplayTitleLocator(self)
        if selector == EDITOR_EXIT_SELECTOR:
            return _NativeExitControl(self)
        raise AssertionError(f"unexpected selector: {selector}")

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    async def evaluate(self, script: str, *_args):
        if "const tokens = []" in script:
            return [dict(token) for token in self.persisted_tokens]
        return None


def test_verify_persisted_draft_waits_for_title_and_tokens(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    tokens = [{"kind": "P", "text": "正文"}]
    platform = _make_platform(
        _ReplayPage(
            title=title,
            tokens=tokens,
            title_reads=["", title],
        )
    )
    platform._expected_persisted_tokens = tokens

    result = run(
        platform._verify_persisted_draft(
            title,
            f"{PUBLISH_URL}?pgc_id=7665272216262623790",
        )
    )

    assert result == (True, True)


def _prepare_save_platform(
    *,
    baseline: _ToutiaoDraftSnapshot,
    latest: _ToutiaoDraftSnapshot,
    title: str = "测试标题",
) -> ToutiaoPlatform:
    platform = _make_platform(_SavePage(title))
    platform._preflight_title = title
    platform._draft_baseline = baseline
    platform._expected_persisted_tokens = [{"kind": "P", "text": "正文"}]
    platform._last_editor_mutation_at = 0.0
    platform._wait_for_editor_settle_before_exit = AsyncMock()

    platform._test_fetch_snapshot_calls = 0
    platform._test_exit_editor_values = []

    async def fetch_snapshot(
        *,
        exit_editor: bool = False,
        expected_title: str | None = None,
    ):
        assert expected_title == title
        platform._test_fetch_snapshot_calls += 1
        platform._test_exit_editor_values.append(exit_editor)
        return latest

    platform._fetch_draft_snapshot = fetch_snapshot
    return platform


def _record(*, code: int, draft_ids: set[str] | None = None) -> dict[str, object]:
    return {
        # 正文或图片完成后的自动保存响应才属于最终内容版本。
        "generation": 0,
        "received_at": float("inf"),
        "status": 200,
        "code": code,
        "err_no": code,
        "reason": "",
        "draft_ids": frozenset(draft_ids or set()),
    }


class _AutosaveRequest:
    def __init__(self) -> None:
        self.url = f"https://mp.toutiao.com{AUTOSAVE_PATH}?type=article"
        self.method = "POST"


class _AutosaveResponse:
    def __init__(self, request: _AutosaveRequest, *, code: int) -> None:
        self.request = request
        self.url = request.url
        self.status = 200
        self._code = code

    async def json(self) -> dict[str, object]:
        return {"code": self._code, "err_no": self._code}


def test_autosave_response_keeps_request_mutation_generation() -> None:
    platform = _make_platform(FakePage())
    old_request = _AutosaveRequest()
    new_request = _AutosaveRequest()
    platform._mutation_generation = 1
    platform._capture_autosave_request(old_request)
    platform._mutation_generation = 2
    platform._capture_autosave_request(new_request)

    # 旧请求晚于新请求返回，也不能被归到最新正文版本。
    run(platform._capture_autosave_response(_AutosaveResponse(new_request, code=0)))
    run(platform._capture_autosave_response(_AutosaveResponse(old_request, code=0)))

    assert [record["generation"] for record in platform._autosave_records] == [2, 1]


def test_autosave_barrier_rejects_latest_generation_nonzero(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    platform = _make_platform(FakePage())
    platform._mutation_generation = 2
    old = _record(code=0, draft_ids={"111111"})
    old["generation"] = 1
    rejected = _record(code=7050, draft_ids={"222222"})
    rejected["generation"] = 2
    platform._autosave_records = [old, rejected]

    with pytest.raises(ToutiaoDraftSaveRejectedError, match="平台码 nonzero"):
        run(
            platform._wait_for_autosave_barrier(
                generation=2,
                stage="第 1 张图片",
            )
        )


def test_content_block_autosave_rejection_does_not_preempt_native_exit() -> None:
    platform = ToutiaoPlatform()
    platform.page = FakePage()
    platform._mutation_generation = 4
    platform._wait_for_autosave_barrier = AsyncMock(
        side_effect=ToutiaoDraftSaveRejectedError(
            "TOUTIAO_DRAFT_SAVE_REJECTED: 中间保存失败"
        )
    )

    run(platform._wait_for_content_block_autosave(4))

    platform._wait_for_autosave_barrier.assert_awaited_once_with(
        generation=4,
        stage="第 4 个图文块",
    )


def test_autosave_barrier_binds_title_draft_id(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    platform = _make_platform(FakePage())
    platform._mutation_generation = 1
    first = _record(code=0, draft_ids={"7665272216262623790"})
    first["generation"] = 1
    second = _record(code=0, draft_ids={"7665272216262623790"})
    second["generation"] = 1
    platform._autosave_records = [first, second]

    result = run(
        platform._wait_for_autosave_barrier(
            generation=1,
            stage="标题初始化",
            require_draft_id=True,
        )
    )

    assert result is second
    assert platform._active_draft_id == "7665272216262623790"


def test_save_draft_raises_on_explicit_autosave_rejection(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    baseline = _snapshot(total=1, title="测试标题", title_count=1, ids={"111111"})
    platform = _prepare_save_platform(baseline=baseline, latest=baseline)
    platform._autosave_records = [_record(code=7050)]

    with pytest.raises(ToutiaoDraftSaveRejectedError):
        run(platform.save_draft("测试标题"))

    assert platform.page.blur_calls == 0
    platform._wait_for_editor_settle_before_exit.assert_awaited_once_with()
    assert platform._test_fetch_snapshot_calls == 1
    assert platform._test_exit_editor_values == [True]


def test_save_draft_binds_unique_new_pgc_id_and_reopens(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    old_id = "111111"
    new_id = "7665272216262623790"
    baseline = _snapshot(total=3, title=title, title_count=1, ids={old_id})
    latest = _snapshot(total=4, title=title, title_count=2, ids={old_id, new_id})
    platform = _prepare_save_platform(baseline=baseline, latest=latest, title=title)
    platform._autosave_records = [_record(code=0, draft_ids={new_id})]
    verified: list[tuple[str, str]] = []

    async def verify(expected_title: str, edit_url: str):
        verified.append((expected_title, edit_url))
        return True, True

    platform._verify_persisted_draft = verify

    result = run(platform.save_draft(title))

    expected_url = f"{PUBLISH_URL}?pgc_id={new_id}"
    assert result == expected_url
    assert verified == [(title, expected_url)]
    assert platform.page.title_fill_calls == 0
    assert platform.page.blur_calls == 0
    platform._wait_for_editor_settle_before_exit.assert_awaited_once_with()
    assert platform._test_fetch_snapshot_calls == 1
    assert platform._test_exit_editor_values == [True]
    evidence = platform._last_draft_evidence
    assert evidence.draft_entity_bound is True
    assert evidence.draft_entity_source == "save_response_id"
    assert evidence.reopen_title_match is True
    assert evidence.reopen_dom_blocks_match is True


def test_save_draft_prefers_bound_new_id_when_historical_ids_are_virtualized(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    old_ids = {str(index) for index in range(100000, 100012)}
    new_id = "7680801349465899554"
    baseline = _snapshot(
        total=12,
        title=title,
        title_count=12,
        ids=old_ids,
    )
    latest = _ToutiaoDraftSnapshot(
        loaded=True,
        total_count=13,
        title_counts={title: 13},
        draft_ids=frozenset({new_id}),
        title_to_ids={title: frozenset({new_id})},
    )
    platform = _prepare_save_platform(baseline=baseline, latest=latest, title=title)
    platform._active_draft_id = new_id
    platform._autosave_records = [_record(code=0, draft_ids={new_id})]
    platform._verify_persisted_draft = AsyncMock(return_value=(True, True))

    result = run(platform.save_draft(title))

    assert result == f"{PUBLISH_URL}?pgc_id={new_id}"
    assert platform._last_draft_evidence.draft_entity_bound is True
    assert platform._last_draft_evidence.reopen_dom_blocks_match is True


def test_save_draft_uses_bound_response_id_while_draft_card_is_delayed() -> None:
    title = "测试标题"
    old_id = "111111"
    new_id = "7680806313923756578"
    baseline = _snapshot(total=1, title=title, title_count=1, ids={old_id})
    # 平台已返回新 pgc_id，但退出后的草稿列表尚未渲染新卡片。
    latest = baseline
    platform = _prepare_save_platform(baseline=baseline, latest=latest, title=title)
    platform._active_draft_id = new_id
    platform._autosave_records = [_record(code=0, draft_ids={new_id})]
    platform._verify_persisted_draft = AsyncMock(return_value=(True, True))

    result = run(platform.save_draft(title))

    assert result == f"{PUBLISH_URL}?pgc_id={new_id}"
    evidence = platform._last_draft_evidence
    assert evidence.draft_list_match_count == 1
    assert evidence.draft_entity_bound is True
    assert evidence.draft_entity_source == "save_response_id"
    assert evidence.reopen_title_match is True
    assert evidence.reopen_dom_blocks_match is True


def test_save_draft_uses_native_exit_without_intermediate_autosave_gate() -> None:
    title = "测试标题"
    new_id = "7665272216262623790"
    baseline = _snapshot(total=0, title=title, title_count=0, ids=set())
    latest = _snapshot(total=1, title=title, title_count=1, ids={new_id})
    platform = _prepare_save_platform(baseline=baseline, latest=latest, title=title)
    platform._verify_persisted_draft = AsyncMock(return_value=(True, True))
    result = run(platform.save_draft(title))

    assert result == f"{PUBLISH_URL}?pgc_id={new_id}"
    assert platform.page.title_fill_calls == 0
    assert platform.page.blur_calls == 0
    platform._wait_for_editor_settle_before_exit.assert_awaited_once_with()
    assert platform._test_fetch_snapshot_calls == 1
    assert platform._test_exit_editor_values == [True]


def test_save_draft_accepts_exact_reopen_after_nonzero_intermediate_autosave(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    new_id = "7680498739710149160"
    baseline = _snapshot(total=3, title=title, title_count=0, ids=set())
    latest = _snapshot(total=4, title=title, title_count=1, ids={new_id})
    platform = _prepare_save_platform(baseline=baseline, latest=latest, title=title)
    platform._autosave_records = [_record(code=7050, draft_ids={new_id})]
    platform._verify_persisted_draft = AsyncMock(return_value=(True, True))

    result = run(platform.save_draft(title))

    assert result == f"{PUBLISH_URL}?pgc_id={new_id}"
    assert platform.page.blur_calls == 0
    platform._wait_for_editor_settle_before_exit.assert_awaited_once_with()
    assert platform._test_fetch_snapshot_calls == 1
    assert platform._test_exit_editor_values == [True]
    platform._verify_persisted_draft.assert_awaited_once()
    evidence = platform._last_draft_evidence
    assert evidence.save_platform_code == "nonzero"
    assert evidence.draft_entity_bound is True
    assert evidence.reopen_title_match is True
    assert evidence.reopen_dom_blocks_match is True


def test_save_draft_does_not_bind_response_id_from_another_entity(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    old_id = "111111"
    new_id = "7665272216262623790"
    wrong_response_id = "999999"
    baseline = _snapshot(total=1, title=title, title_count=1, ids={old_id})
    latest = _snapshot(total=2, title=title, title_count=2, ids={old_id, new_id})
    platform = _prepare_save_platform(baseline=baseline, latest=latest, title=title)
    platform._autosave_records = [
        _record(code=0, draft_ids={wrong_response_id})
    ]

    with pytest.raises(DraftResultUnknownError) as raised:
        run(platform.save_draft(title))

    assert "响应 ID 未绑定到唯一新增标题实体" in str(raised.value)
    assert raised.value.evidence.draft_entity_bound is False
    assert raised.value.evidence.unknown is True


def test_save_draft_does_not_treat_missing_baseline_id_as_new_entity(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    old_id = "7665272216262623790"
    baseline = _ToutiaoDraftSnapshot(
        loaded=True,
        total_count=1,
        title_counts={title: 1},
        draft_ids=frozenset(),
        title_to_ids={},
    )
    latest = _snapshot(total=1, title=title, title_count=1, ids={old_id})
    platform = _prepare_save_platform(baseline=baseline, latest=latest, title=title)
    platform._autosave_records = [_record(code=0, draft_ids={old_id})]

    async def must_not_reopen(_expected_title: str, _edit_url: str):
        raise AssertionError("旧草稿 ID 不得进入重开成功路径")

    platform._verify_persisted_draft = must_not_reopen

    with pytest.raises(DraftResultUnknownError) as raised:
        run(platform.save_draft(title))

    assert "响应 ID 未绑定到唯一新增标题实体" in str(raised.value)
    assert raised.value.evidence.draft_entity_bound is False


def test_save_draft_is_unknown_when_new_entity_is_not_unique(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    baseline = _snapshot(total=1, title=title, title_count=1, ids={"111111"})
    latest = _snapshot(
        total=2,
        title=title,
        title_count=2,
        ids={"111111", "222222", "333333"},
    )
    platform = _prepare_save_platform(baseline=baseline, latest=latest, title=title)
    platform._autosave_records = [_record(code=0)]

    with pytest.raises(DraftResultUnknownError) as raised:
        run(platform.save_draft(title))

    assert "唯一新增草稿实体" in str(raised.value)
    assert raised.value.evidence.draft_entity_bound is False
    assert raised.value.evidence.unknown is True


def test_save_draft_is_unknown_when_reopen_tokens_do_not_match(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    new_id = "7665272216262623790"
    baseline = _snapshot(total=0, title=title, title_count=0, ids=set())
    latest = _snapshot(total=1, title=title, title_count=1, ids={new_id})
    platform = _prepare_save_platform(baseline=baseline, latest=latest, title=title)
    platform._autosave_records = [_record(code=0, draft_ids={new_id})]

    async def verify(_expected_title: str, _edit_url: str):
        return True, False

    platform._verify_persisted_draft = verify

    with pytest.raises(DraftResultUnknownError) as raised:
        run(platform.save_draft(title))

    assert "图文结构不完整" in str(raised.value)
    assert raised.value.evidence.draft_entity_bound is False
    assert raised.value.evidence.reopen_title_match is True
    assert raised.value.evidence.reopen_dom_blocks_match is False


@pytest.mark.parametrize(
    "persisted_images",
    [
        ["p3-sign.example/two.png", "p3-sign.example/one.png"],
        ["p3-sign.example/one.png", "p3-sign.example/one.png"],
    ],
    ids=["reordered", "duplicated"],
)
def test_save_draft_is_unknown_when_reopen_image_fingerprints_drift(
    monkeypatch,
    persisted_images: list[str],
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    title = "测试标题"
    new_id = "7665272216262623790"
    baseline = _snapshot(total=0, title=title, title_count=0, ids=set())
    latest = _snapshot(total=1, title=title, title_count=1, ids={new_id})
    platform = _prepare_save_platform(baseline=baseline, latest=latest, title=title)
    platform.page = _ReplayPage(
        title=title,
        tokens=[
            {"kind": "P", "text": "正文"},
            *[
                {"kind": "I", "text": "", "fingerprint": fingerprint}
                for fingerprint in persisted_images
            ],
        ],
    )
    platform._expected_persisted_tokens = [
        {"kind": "P", "text": "正文"},
        {
            "kind": "I",
            "text": "",
            "fingerprint": "p3-sign.example/one.png",
        },
        {
            "kind": "I",
            "text": "",
            "fingerprint": "p3-sign.example/two.png",
        },
    ]
    platform._autosave_records = [_record(code=0, draft_ids={new_id})]

    with pytest.raises(DraftResultUnknownError) as raised:
        run(platform.save_draft(title))

    assert "图文结构不完整" in str(raised.value)
    assert raised.value.evidence.draft_entity_bound is False
    assert raised.value.evidence.reopen_dom_blocks_match is False


def test_save_draft_requires_frozen_preflight_and_editor_tokens() -> None:
    platform = _make_platform(_SavePage())

    with pytest.raises(DraftResultUnknownError):
        run(platform.save_draft("测试标题"))
