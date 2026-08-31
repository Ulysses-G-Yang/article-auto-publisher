from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from platforms.base import DraftBaselineError, DraftResultUnknownError
from platforms.toutiao import (
    BODY_IMAGE_INPUT_SELECTOR,
    BODY_SELECTOR,
    HEADING_BUTTON_SELECTOR,
    IMAGE_BUTTON_SELECTOR,
    IMAGE_DRAWER_CLOSE_SELECTOR,
    IMAGE_DRAWER_SELECTOR,
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


class FakeResponse:
    def __init__(self, url: str, body: dict, method: str = "POST", status: int = 200):
        self.url = url
        self._body = body
        self.status = status
        self.request = type("R", (), {"method": method})()

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


def test_check_login_false_without_session_cookie(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    platform = _make_platform(FakePage())

    assert run(platform.check_login()) is False
    assert platform.last_login_error.startswith("LOGIN_REQUIRED:")


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

    async def fetch_snapshot():
        return baseline

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
        run(platform._fetch_draft_snapshot())

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
        run(platform._fetch_draft_snapshot())

    assert "草稿 ID 未完整稳定加载" in str(raised.value)


class _ModelEditorLocator:
    def __init__(self, page: _ModelPage) -> None:
        self.page = page

    async def click(self, **_kwargs) -> None:
        return None

    async def press(self, key: str) -> None:
        if key == "Backspace":
            self.page.tokens.clear()

    async def inner_text(self) -> str:
        return "\n".join(
            token["text"] for token in self.page.tokens if token["kind"] != "I"
        )


class _ModelHeadingLocator:
    def __init__(self, page: _ModelPage) -> None:
        self.page = page

    async def count(self) -> int:
        return 1

    async def click(self, **_kwargs) -> None:
        if not self.page.tokens:
            self.page.tokens.append({"kind": "H2", "text": ""})
        else:
            self.page.tokens[-1]["kind"] = "H2"


class _ModelKeyboard:
    def __init__(self, page: _ModelPage) -> None:
        self.page = page

    async def press(self, key: str) -> None:
        if key == "Enter":
            self.page.tokens.append({"kind": "P", "text": ""})
        elif key == "Shift+Enter":
            if not self.page.tokens:
                self.page.tokens.append({"kind": "P", "text": ""})
            self.page.tokens[-1]["text"] += "\n"

    async def insert_text(self, text: str) -> None:
        if not self.page.tokens:
            self.page.tokens.append({"kind": "P", "text": ""})
        self.page.tokens[-1]["text"] += text


class _ModelPage:
    def __init__(self) -> None:
        self.tokens: list[dict[str, str]] = []
        self.keyboard = _ModelKeyboard(self)

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str):
        if selector == BODY_SELECTOR:
            return _ModelEditorLocator(self)
        if selector == HEADING_BUTTON_SELECTOR:
            return _ModelHeadingLocator(self)
        raise AssertionError(f"unexpected selector: {selector}")

    async def evaluate(self, script: str, *_args):
        if "lastElementChild" in script:
            return bool(self.tokens and self.tokens[-1]["kind"] == "H2")
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


class _UploadLocator:
    def __init__(self, page: _UploadPage, selector: str) -> None:
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        if self.selector == IMAGE_DRAWER_SELECTOR:
            return self.page.drawer_count
        if self.selector == IMAGE_DRAWER_CLOSE_SELECTOR:
            return self.page.close_count
        return 1

    async def wait_for(self, **_kwargs) -> None:
        self.page.waited_selectors.append(self.selector)
        if self.selector == IMAGE_DRAWER_SELECTOR:
            assert _kwargs.get("state") == "hidden"
            if not self.page.drawer_hides:
                raise TimeoutError("drawer remained visible")
            assert self.page.image_panel_open is False

    async def click(self, **_kwargs) -> None:
        if self.selector == IMAGE_BUTTON_SELECTOR:
            self.page.image_panel_open = True
        elif self.selector == IMAGE_DRAWER_CLOSE_SELECTOR:
            self.page.image_panel_open = False

    async def set_input_files(self, path: str, **_kwargs) -> None:
        assert self.selector == BODY_IMAGE_INPUT_SELECTOR
        assert self.page.image_panel_open is True
        self.page.upload_paths.append(path)
        self.page.fingerprints.append("p3-sign.example/article-image.png")


class _UploadPage:
    def __init__(self) -> None:
        self.fingerprints: list[str] = []
        self.image_panel_open = False
        self.drawer_count = 1
        self.close_count = 1
        self.drawer_hides = True
        self.upload_paths: list[str] = []
        self.locator_calls: list[str] = []
        self.waited_selectors: list[str] = []

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        if selector in {
            IMAGE_BUTTON_SELECTOR,
            BODY_IMAGE_INPUT_SELECTOR,
            IMAGE_DRAWER_CLOSE_SELECTOR,
            IMAGE_DRAWER_SELECTOR,
        }:
            return _UploadLocator(self, selector)
        raise AssertionError(f"unexpected selector: {selector}")

    async def evaluate(self, script: str, *_args):
        if ".ProseMirror img" in script:
            return list(self.fingerprints)
        return None


def test_upload_image_uses_only_exact_body_image_input(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
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
        BODY_IMAGE_INPUT_SELECTOR,
        IMAGE_DRAWER_SELECTOR,
        IMAGE_DRAWER_CLOSE_SELECTOR,
    ]
    assert page.waited_selectors == [BODY_IMAGE_INPUT_SELECTOR, IMAGE_DRAWER_SELECTOR]
    assert BODY_IMAGE_INPUT_SELECTOR == (
        ".upload-image-panel [data-e2e='image-upload'] "
        "input[type='file'][accept*='image']"
    )
    assert IMAGE_DRAWER_SELECTOR == (
        ".byte-drawer-wrapper:has("
        ".upload-image-panel [data-e2e='image-upload'] "
        "input[type='file'][accept*='image'])"
    )


@pytest.mark.parametrize(
    ("drawer_count", "close_count", "drawer_hides", "error"),
    [
        (0, 1, True, "头条号正文图片抽屉不存在或不唯一"),
        (2, 1, True, "头条号正文图片抽屉不存在或不唯一"),
        (1, 0, True, "头条号图片抽屉关闭按钮不存在或不唯一"),
        (1, 2, True, "头条号图片抽屉关闭按钮不存在或不唯一"),
        (1, 1, False, "头条号图片抽屉关闭后仍遮挡编辑器"),
    ],
)
def test_upload_image_fails_closed_when_exact_drawer_cannot_close(
    monkeypatch,
    drawer_count: int,
    close_count: int,
    drawer_hides: bool,
    error: str,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = _UploadPage()
    page.drawer_count = drawer_count
    page.close_count = close_count
    page.drawer_hides = drawer_hides
    platform = _make_platform(page)

    result = run(platform._upload_image("D:/controlled/article-image.png"))

    assert result == {"success": False, "error": error}


class _BlurLocator:
    async def evaluate(self, _script: str) -> None:
        return None


class _SavePage:
    def __init__(self) -> None:
        self.url = PUBLISH_URL
        self.response_handlers: list = []

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str):
        assert selector == BODY_SELECTOR
        return _BlurLocator()

    def on(self, event: str, handler) -> None:
        assert event == "response"
        self.response_handlers.append(handler)


class _ReplayTitleLocator:
    def __init__(self, value: str) -> None:
        self.value = value

    async def input_value(self) -> str:
        return self.value


class _ReplayPage(_SavePage):
    def __init__(self, *, title: str, tokens: list[dict[str, str]]) -> None:
        super().__init__()
        self.persisted_title = title
        self.persisted_tokens = tokens

    def locator(self, selector: str):
        if selector == BODY_SELECTOR:
            return _BlurLocator()
        if selector == TITLE_SELECTOR:
            return _ReplayTitleLocator(self.persisted_title)
        raise AssertionError(f"unexpected selector: {selector}")

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        return None

    async def evaluate(self, script: str, *_args):
        if "const tokens = []" in script:
            return [dict(token) for token in self.persisted_tokens]
        return None


def _prepare_save_platform(
    *,
    baseline: _ToutiaoDraftSnapshot,
    latest: _ToutiaoDraftSnapshot,
    title: str = "测试标题",
) -> ToutiaoPlatform:
    platform = _make_platform(_SavePage())
    platform._preflight_title = title
    platform._draft_baseline = baseline
    platform._expected_persisted_tokens = [{"kind": "P", "text": "正文"}]
    platform._last_editor_mutation_at = 0.0

    async def fetch_snapshot():
        return latest

    platform._fetch_draft_snapshot = fetch_snapshot
    return platform


def _record(*, code: int, draft_ids: set[str] | None = None) -> dict[str, object]:
    return {
        "received_at": 1.0,
        "status": 200,
        "code": code,
        "err_no": code,
        "reason": "",
        "draft_ids": frozenset(draft_ids or set()),
    }


def test_save_draft_raises_on_explicit_autosave_rejection(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    baseline = _snapshot(total=1, title="测试标题", title_count=1, ids={"111111"})
    platform = _prepare_save_platform(baseline=baseline, latest=baseline)
    platform._autosave_records = [_record(code=7050)]

    with pytest.raises(ToutiaoDraftSaveRejectedError):
        run(platform.save_draft("测试标题"))

    evidence = platform._last_draft_evidence
    assert evidence.save_response_2xx is True
    assert evidence.save_platform_code == "nonzero"
    assert evidence.draft_entity_bound is False


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
    evidence = platform._last_draft_evidence
    assert evidence.draft_entity_bound is True
    assert evidence.draft_entity_source == "save_response_id"
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
