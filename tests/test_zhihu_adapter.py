from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.account_service import AccountSessionService
from account_sessions.database import AccountDatabase
from account_sessions.identity import extract_identity
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from platforms.base import (
    BrowserLifecycleError,
    DraftBaselineError,
    DraftResultUnknownError,
    LoginRequiredError,
    SelectorError,
)
from platforms.content_validation import ContentValidationError
from platforms.zhihu import (
    DRAFTS_URL,
    ZhihuPlatform,
    find_unique_exact_draft,
    parse_zhihu_draft_cards,
)


def run(coroutine):
    return asyncio.run(coroutine)


class FakeContext:
    def __init__(self, cookies: list[dict] | None = None) -> None:
        self._cookies = cookies or []

    async def cookies(self, *_args):
        return list(self._cookies)

    @property
    def pages(self):
        return []


class FakePage:
    def __init__(self, identities: list[dict]) -> None:
        self.identities = list(identities)
        self.url = "about:blank"
        self.goto_calls: list[str] = []
        self.identity_paths: list[str] = []

    def is_closed(self) -> bool:
        return False

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url
        self.goto_calls.append(url)

    async def evaluate(self, _script: str, identity_path: str) -> dict:
        self.identity_paths.append(identity_path)
        if not self.identities:
            return {
                "ok": False,
                "status": 401,
                "user_id": "",
                "url_token": "",
                "display_name": "",
            }
        return self.identities.pop(0)


def make_platform(
    identities: list[dict],
    *,
    cookies: list[dict] | None = None,
) -> ZhihuPlatform:
    platform = ZhihuPlatform()
    platform.page = FakePage(identities)
    platform.context = FakeContext(cookies)
    return platform


def valid_identity() -> dict:
    return {
        "ok": True,
        "status": 200,
        "user_id": "stable-id-123",
        "url_token": "night-sailor",
        "display_name": "夜航员",
    }


def invalid_identity(status: int = 401) -> dict:
    return {
        "ok": False,
        "status": status,
        "user_id": "",
        "url_token": "",
        "display_name": "",
    }


def test_check_login_requires_same_origin_identity_api_success() -> None:
    platform = make_platform([valid_identity()])

    assert run(platform.check_login()) is True
    assert platform.page.goto_calls == ["https://www.zhihu.com/"]
    assert platform.page.identity_paths == ["/api/v4/me"]
    assert platform._identity_payload["user_id"] == "stable-id-123"
    assert set(platform._identity_payload) == {
        "ok",
        "status",
        "user_id",
        "display_name",
    }


def test_cookie_is_only_weak_signal_and_cannot_prove_login() -> None:
    platform = make_platform(
        [invalid_identity()],
        cookies=[{"name": "z_c0", "value": "secret-must-not-leak"}],
    )

    assert run(platform.check_login()) is False
    assert platform.last_login_error.startswith("ZHIHU_SESSION_INVALID:")
    assert "secret-must-not-leak" not in platform.last_login_error
    assert platform._identity_payload is None


def test_extract_identity_uses_verified_api_fields_without_cookie_output() -> None:
    platform = make_platform([valid_identity()])

    identity = run(extract_identity(platform))

    assert identity.platform_user_id == "stable-id-123"
    assert identity.display_name == "夜航员"


def test_interactive_login_polls_until_identity_api_confirms_session(monkeypatch) -> None:
    platform = make_platform([invalid_identity(), valid_identity()])
    platform.LOGIN_POLL_INTERVAL_SECONDS = 0
    platform.LOGIN_POLL_ATTEMPTS = 2

    run(platform.login())

    assert platform.page.goto_calls == ["https://www.zhihu.com/signin"]
    assert len(platform.page.identity_paths) == 2
    assert platform.last_login_error == ""


def test_interactive_login_timeout_is_bounded_and_explicit() -> None:
    platform = make_platform([invalid_identity(), invalid_identity()])
    platform.LOGIN_POLL_INTERVAL_SECONDS = 0
    platform.LOGIN_POLL_ATTEMPTS = 2

    with pytest.raises(LoginRequiredError, match="知乎登录超时"):
        run(platform.login())

    assert platform.page.goto_calls == ["https://www.zhihu.com/signin"]
    assert len(platform.page.identity_paths) == 2
    assert platform.last_login_error.startswith("LOGIN_REQUIRED:")


def test_closed_browser_is_not_downgraded_to_login_required() -> None:
    platform = make_platform([])
    platform.page.is_closed = lambda: True

    with pytest.raises(BrowserLifecycleError, match="页面已关闭"):
        run(platform.check_login())


def test_account_service_creates_and_verifies_isolated_zhihu_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_root = tmp_path / "account-sessions"
    monkeypatch.setenv("ACCOUNT_SESSION_DATA_DIR", str(data_root))
    database = AccountDatabase(f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}")
    created_platforms = []

    class FakeZhihuSessionPlatform:
        platform_name = "zhihu"

        def __init__(self, checks: list[bool]) -> None:
            self.checks = checks
            self.context = FakeContext()
            self.page = FakePage([])
            self.login_calls = 0
            self.cleaned = False

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return self.checks.pop(0)

        async def login(self) -> None:
            self.login_calls += 1

        async def fetch_identity_payload(self) -> dict:
            return valid_identity()

        async def cleanup(self) -> None:
            self.cleaned = True

    check_sequences = [[False, True], [True]]

    def platform_factory(_account):
        platform = FakeZhihuSessionPlatform(check_sequences.pop(0))
        created_platforms.append(platform)
        return platform

    service = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=platform_factory,
        allowed_profile_roots=(data_root / "profiles",),
    )
    run(service.initialize())
    candidate = run(service.create_login_candidate("zhihu", LOCAL_WEB_CONTEXT))
    verified = run(
        service.verify_account(
            candidate["account_id"],
            LOCAL_WEB_CONTEXT,
            allow_interactive_login=True,
        )
    )
    read_only_verified = run(
        service.verify_account(
            candidate["account_id"],
            LOCAL_WEB_CONTEXT,
            allow_interactive_login=False,
        )
    )

    assert candidate["session_status"] == "VERIFYING"
    assert verified["display_name"] == "夜航员"
    assert verified["session_status"] == "VALID"
    assert read_only_verified["session_status"] == "VALID"
    assert created_platforms[0].login_calls == 1
    assert created_platforms[1].login_calls == 0
    assert all(platform.cleaned for platform in created_platforms)
    profile_dir = data_root / "profiles" / "zhihu" / candidate["account_id"]
    assert profile_dir.is_dir()
    run(database.dispose())


def test_account_service_cleans_up_after_zhihu_login_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_root = tmp_path / "account-sessions"
    monkeypatch.setenv("ACCOUNT_SESSION_DATA_DIR", str(data_root))
    database = AccountDatabase(f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}")

    class TimeoutPlatform:
        platform_name = "zhihu"

        def __init__(self) -> None:
            self.cleaned = False

        async def initialize(self) -> None:
            return None

        async def check_login(self) -> bool:
            return False

        async def login(self) -> None:
            raise LoginRequiredError("LOGIN_REQUIRED: 知乎登录超时")

        async def cleanup(self) -> None:
            self.cleaned = True

    platform = TimeoutPlatform()
    service = AccountSessionService(
        database,
        seed_legacy_profiles=False,
        platform_factory=lambda _account: platform,
        allowed_profile_roots=(data_root / "profiles",),
    )
    run(service.initialize())
    candidate = run(service.create_login_candidate("zhihu", LOCAL_WEB_CONTEXT))

    with pytest.raises(LoginRequiredError, match="知乎登录超时"):
        run(
            service.verify_account(
                candidate["account_id"],
                LOCAL_WEB_CONTEXT,
                allow_interactive_login=True,
            )
        )

    stored = run(service.get_account(candidate["account_id"]))
    assert platform.cleaned is True
    assert stored.session_status == "LOGIN_REQUIRED"
    assert [
        item["action"]
        for item in run(service.list_activity(candidate["account_id"], LOCAL_WEB_CONTEXT))
    ] == ["SESSION_VERIFY_FAILED"]
    run(database.dispose())


# ==================== 草稿投递链路契约测试 ====================


class _InstantSimulator:
    async def random_delay(self, *_args, **_kwargs):
        return None


class _FakeLocator:
    def __init__(self, page, text="", count=1, visible=True):
        self.page = page
        self.text = text
        self.value = text
        self._count = count
        self.visible = visible

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self.visible

    async def click(self, **_kwargs):
        self.page.active = self

    async def fill(self, value):
        self.value = value
        self.text = value

    async def inner_text(self):
        if self is self.page.body:
            return "\n".join(
                item.get("text", "") for item in self.page.dom_tokens if item.get("kind") != "image"
            )
        return self.text

    async def input_value(self):
        return self.value

    async def evaluate(self, script):
        if "const tokens" in script:
            return [dict(item) for item in self.page.dom_tokens]
        if "selected_all" in script:
            tail = self.page.dom_tokens[-1] if self.page.dom_tokens else {}
            return {
                "key": str(len(self.page.dom_tokens)), "tag": "div", "at_end": True,
                "empty": not tail.get("text") and tail.get("kind") != "image",
                "selected_all": self.page.selected_all,
            }
        return None

    async def press(self, key):
        self.page.locator_presses.append(key)
        await self.page.keyboard.press(key)


class _FakeRoleButton:
    def __init__(self, page, name):
        self.page = page
        self.name = name

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def wait_for(self, **_kwargs):
        return None

    async def click(self, **_kwargs):
        if self.name == "二级标题":
            for item in reversed(self.page.dom_tokens):
                if item.get("kind") != "image" and item.get("text"):
                    item["kind"] = "heading"
                    item["level"] = 2
                    break


class _FakeEditorKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        if key in ("Control+A", "Meta+A", "Control+Shift+Home"):
            self.page.selected_all = True
        elif key == "Backspace" and self.page.selected_all:
            self.page.body.text = ""
            self.page.body.value = ""
            self.page.dom_tokens = []
            self.page.selected_all = False
        elif key == "Enter":
            self.page.body.text += "\n"
            self.page.body.value = self.page.body.text
            self.page.dom_tokens.append({"kind": "text", "text": ""})

    async def insert_text(self, value):
        if self.page.selected_all:
            self.page.body.text = ""
            self.page.body.value = ""
            self.page.selected_all = False
        self.page.body.text += value
        self.page.body.value = self.page.body.text
        if not self.page.dom_tokens or self.page.dom_tokens[-1].get("kind") == "image":
            self.page.dom_tokens.append({"kind": "text", "text": ""})
        self.page.dom_tokens[-1]["text"] += value


class _FakeDraftsResponse:
    def __init__(self, records, *, status=200, payload=None):
        self._records = records
        self.status = status
        self.url = "https://www.zhihu.com/api/v4/articles/my_drafts"
        self.request = type("Request", (), {"method": "GET"})()
        self._payload = payload

    async def json(self):
        if self._payload is not None:
            return self._payload
        return {
            "paging": {"totals": len(self._records)},
            "data": list(self._records),
        }


class _FakeResponseInfo:
    def __init__(self, records, *, status=200, payload=None):
        self._records = records
        self._status = status
        self._payload = payload

    @property
    def value(self):
        async def _resolve():
            return _FakeDraftsResponse(
                self._records,
                status=self._status,
                payload=self._payload,
            )

        return _resolve()


class _FakeEditorPage:
    """模拟知乎 /write 页面：标题 textarea + Draft.js contenteditable + 草稿箱。"""

    def __init__(
        self,
        body_text="",
        title_text="",
        drafts_text="",
        drafts_api_titles=None,
        drafts_html="",
        drafts_api_status=200,
        drafts_api_payload=None,
    ):
        self.url = ""
        self.goto_urls: list[str] = []
        self.keyboard = _FakeEditorKeyboard(self)
        self.title = _FakeLocator(self, text=title_text)
        self.body = _FakeLocator(self, text=body_text)
        self.drafts_text = drafts_text
        self.drafts_api_records = [
            item if isinstance(item, dict) else {"id": str(index + 1), "title": item}
            for index, item in enumerate(drafts_api_titles or [])
        ]
        self.drafts_html = drafts_html
        self.drafts_api_status = drafts_api_status
        self.drafts_api_payload = drafts_api_payload
        self.selected_all = False
        self.active = None
        self.dom_tokens: list[dict] = []
        self.locator_presses: list[str] = []

    def is_closed(self) -> bool:
        return False

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_urls.append(url)
        self.url = url

    async def reload(self, **_kwargs) -> None:
        return None

    async def content(self) -> str:
        return self.drafts_html

    async def wait_for_selector(self, selector: str, **_kwargs):
        if ".DraftStatusTip" in selector:
            raise TimeoutError("DraftStatusTip detached timeout")
        return True

    def locator(self, selector: str):
        if selector == ".Modal-backdrop":
            return _FakeLocator(self, count=0, visible=False)
        if "textarea" in selector or "placeholder" in selector:
            return self.title
        return self.body

    def get_by_role(self, _role: str, *, name: str, exact: bool = False):
        return _FakeRoleButton(self, name)

    async def evaluate(self, script: str, *args):
        if "includes" in script:
            keyword = args[0] if args else ""
            return keyword in self.drafts_text
        if "草稿箱" in script:
            self.url = DRAFTS_URL
            return True
        if "img" in script and "querySelector" in script:
            return 0
        return None

    def expect_response(self, predicate, **_kwargs):
        class _Expect:
            def __init__(self, page):
                self.page = page

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            @property
            def value(self):
                return _FakeResponseInfo(
                    self.page.drafts_api_records,
                    status=self.page.drafts_api_status,
                    payload=self.page.drafts_api_payload,
                ).value

        return _Expect(self)


class _LossyEditorPage(_FakeEditorPage):
    """正文写入会丢失（模拟编辑器吞字），用于验证缺失段落检测。"""

    def __init__(self):
        super().__init__(body_text="第一段")

        class _LossyKeyboard:
            def __init__(self, page):
                self.page = page

            async def press(self, _key):
                return None

            async def insert_text(self, _value):
                return None

        self.keyboard = _LossyKeyboard(self)


def _make_delivery_platform(page=None):
    platform = ZhihuPlatform()
    platform.page = page or _FakeEditorPage()
    platform.context = FakeContext()
    platform.simulator = _InstantSimulator()
    platform.PERSIST_VERIFY_INTERVAL_SECONDS = 0
    platform.TEXT_CHUNK_INTERVAL_SECONDS = 0
    platform.BLOCK_SETTLE_SECONDS = 0
    platform.EDITOR_WAIT_INTERVAL_SECONDS = 0
    platform.EDITOR_WAIT_ATTEMPTS = 2
    return platform


def _set_draft_baseline(platform, title: str, *draft_ids: str) -> None:
    platform._preflight_title = " ".join(title.split())
    platform._preflight_draft_ids = frozenset(draft_ids)
    platform._preflight_draft_sources = frozenset({"api"})
    platform._preflight_draft_ids_by_source = {
        "api": frozenset(draft_ids),
    }


def test_navigate_to_editor_opens_write_page_and_waits_for_title() -> None:
    page = _FakeEditorPage()
    platform = _make_delivery_platform(page)

    run(platform.navigate_to_editor())

    assert "write" in page.url


def test_navigate_to_editor_avoids_legacy_404_route_as_first_choice() -> None:
    page = _FakeEditorPage()
    platform = _make_delivery_platform(page)
    platform.platform_cfg["editor_url"] = "https://www.zhihu.com/write"

    run(platform.navigate_to_editor())

    assert page.goto_urls == ["https://zhuanlan.zhihu.com/write"]


def test_fill_title_writes_into_title_field() -> None:
    page = _FakeEditorPage()
    platform = _make_delivery_platform(page)

    run(platform.fill_title("深夜食堂"))

    assert page.title.text == "深夜食堂"


def test_fill_content_types_blocks_and_validates_in_order() -> None:
    page = _FakeEditorPage()
    platform = _make_delivery_platform(page)
    blocks = [
        {"type": "text", "text": "第一段\n第二段"},
        {"type": "heading", "level": 2, "text": "小标题"},
    ]

    result = run(platform.fill_content(blocks, []))

    assert result["text_ok"] is True
    assert result["media_status"] == "not_required"
    assert result["expected_images"] == 0
    assert result["uploaded_images"] == 0
    assert page.body.text == "第一段\n第二段\n小标题"
    assert page.dom_tokens[-1] == {
        "kind": "heading",
        "level": 2,
        "text": "小标题",
    }


def test_fill_content_detects_missing_paragraph() -> None:
    page = _LossyEditorPage()
    platform = _make_delivery_platform(page)
    blocks = [{"type": "text", "text": "第一段\n第二段"}]

    with pytest.raises(ContentValidationError):
        run(platform.fill_content(blocks, []))


def test_image_path_mapping_never_falls_back_to_first_image() -> None:
    block = {"type": "image", "position": 7}
    images = [
        {"position_index": 1, "local_path": "first.png"},
        {"position_index": 8, "local_path": "other.png"},
    ]

    assert ZhihuPlatform._image_path_for_block(block, images) is None


def test_image_path_mapping_rejects_ambiguous_position() -> None:
    block = {"type": "image", "position": 7}
    images = [
        {"position_index": 7, "local_path": "one.png"},
        {"position_index": 7, "local_path": "two.png"},
    ]

    assert ZhihuPlatform._image_path_for_block(block, images) is None


def test_dom_token_validation_rejects_misplaced_image() -> None:
    page = _FakeEditorPage()
    page.dom_tokens = [
        {"kind": "image"},
        {"kind": "text", "text": "第一段"},
    ]
    platform = _make_delivery_platform(page)
    blocks = [
        {"type": "text", "text": "第一段"},
        {"type": "image", "local_path": "a.png"},
    ]

    with pytest.raises(ContentValidationError, match="图文顺序不完整"):
        run(platform._validate_dom_exact(blocks, phase="测试"))


def test_dom_reader_walks_nested_draftjs_blocks_instead_of_collapsing_parent() -> None:
    source = Path(PROJECT_ROOT / "platforms" / "zhihu.py").read_text(encoding="utf-8")

    assert "const visit = (node) =>" in source
    assert "if (node.matches('[data-block=\"true\"]'))" in source
    assert "for (const child of node.children) visit(child);" in source
    assert "if (images.length) {" in source


def test_caret_targets_last_draftjs_block_not_contenteditable_root() -> None:
    page = _FakeEditorPage()
    platform = _make_delivery_platform(page)

    run(platform._place_body_caret_at_end())

    assert page.locator_presses == ["Control+End"]


def test_media_overlay_must_be_gone_before_next_content_block() -> None:
    page = _FakeEditorPage()
    platform = _make_delivery_platform(page)

    run(platform._dismiss_media_overlay())

    assert page.selected_all is False


def test_media_error_never_exposes_physical_path(monkeypatch) -> None:
    page = _FakeEditorPage()
    platform = _make_delivery_platform(page)

    async def upload(_path):
        return {
            "success": False,
            "error_code": "UPLOAD_FAILED",
            "error": r"set_input_files D:\Secret Folder\private.png failed",
        }

    async def no_verify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(platform, "_upload_image", upload)
    monkeypatch.setattr(platform, "_validate_dom_prefix", no_verify)
    monkeypatch.setattr(platform, "_validate_dom_exact", no_verify)
    monkeypatch.setattr(platform, "_wait_dom_exact", no_verify)
    with pytest.raises(ContentValidationError, match="ZHIHU_IMAGE_UPLOAD_FAILED") as caught:
        run(platform.fill_content(
            [
                {"type": "image", "local_path": r"D:\Secret Folder\private.png"},
                {"type": "text", "text": "失败后不能继续写入"},
            ],
            [],
        ))
    rendered = str(caught.value)
    assert "D:\\Secret" not in rendered
    assert "失败后不能继续写入" not in page.body.text


def test_parse_draft_card_fixture_uses_semantic_classes_and_deduplicates_links() -> None:
    fixture = (
        PROJECT_ROOT / "tests" / "fixtures" / "zhihu" / "draft_card.html"
    ).read_text(encoding="utf-8")

    assert parse_zhihu_draft_cards(fixture) == [
        {
            "id": "9876543210123456789",
            "title": "脱敏测试标题：多屏工作流",
            "edit_url": (
                "https://zhuanlan.zhihu.com/p/9876543210123456789/edit"
            ),
        }
    ]


@pytest.mark.parametrize(
    "href",
    [
        "https://example.com/p/123/edit",
        "http://zhuanlan.zhihu.com/p/123/edit",
        "https://zhuanlan.zhihu.com/p/not-digits/edit",
        "https://zhuanlan.zhihu.com/question/123/edit",
    ],
)
def test_parse_draft_cards_rejects_untrusted_edit_links(href: str) -> None:
    html = f"""
        <div class="CreationManage-CreationCard css-anything">
          <div class="CreationCardTitle-wrapper css-random">测试标题</div>
          <a href="{href}">编辑</a>
        </div>
    """

    assert parse_zhihu_draft_cards(html) == []


def test_parse_draft_cards_drops_conflicting_duplicate_id() -> None:
    html = """
        <div class="CreationManage-CreationCard css-one">
          <div class="CreationCardTitle-wrapper css-title-one">标题一</div>
          <a href="/p/123/edit">编辑</a>
        </div>
        <div class="CreationManage-CreationCard css-two">
          <div class="CreationCardTitle-wrapper css-title-two">标题二</div>
          <a href="/p/123/edit">编辑</a>
        </div>
    """

    assert parse_zhihu_draft_cards(html) == []


def test_unique_exact_draft_rejects_same_title_with_distinct_ids() -> None:
    candidates = [
        {"id": "123", "title": "同名草稿"},
        {"id": "456", "title": "同名草稿"},
    ]

    assert find_unique_exact_draft(candidates, "同名草稿") is None


def test_unique_exact_draft_deduplicates_repeated_same_entity() -> None:
    candidates = [
        {"id": "123", "title": "同一草稿"},
        {"id": "123", "title": "同一草稿"},
    ]

    assert find_unique_exact_draft(candidates, "同一草稿") == candidates[0]


def test_find_unique_exact_draft_keeps_api_as_primary_source() -> None:
    fixture = (
        PROJECT_ROOT / "tests" / "fixtures" / "zhihu" / "draft_card.html"
    ).read_text(encoding="utf-8")
    title = "脱敏测试标题：多屏工作流"
    page = _FakeEditorPage(
        drafts_api_titles=[{"id": "123", "title": title}],
        drafts_html=fixture,
    )
    platform = _make_delivery_platform(page)

    assert run(platform._find_unique_exact_draft(title)) == {
        "id": "123",
        "title": title,
    }


def test_find_unique_exact_draft_does_not_mask_ambiguous_api_with_dom() -> None:
    fixture = (
        PROJECT_ROOT / "tests" / "fixtures" / "zhihu" / "draft_card.html"
    ).read_text(encoding="utf-8")
    title = "脱敏测试标题：多屏工作流"
    page = _FakeEditorPage(
        drafts_api_titles=[
            {"id": "123", "title": title},
            {"id": "456", "title": title},
        ],
        drafts_html=fixture,
    )
    platform = _make_delivery_platform(page)

    assert run(platform._find_unique_exact_draft(title)) is None


def test_find_unique_exact_draft_falls_back_to_stable_dom_card() -> None:
    fixture = (
        PROJECT_ROOT / "tests" / "fixtures" / "zhihu" / "draft_card.html"
    ).read_text(encoding="utf-8")
    title = "脱敏测试标题：多屏工作流"
    page = _FakeEditorPage(
        drafts_api_titles=[{"id": "111", "title": "其他草稿"}],
        drafts_html=fixture,
    )
    platform = _make_delivery_platform(page)

    assert run(platform._find_unique_exact_draft(title)) == {
        "id": "9876543210123456789",
        "title": title,
        "edit_url": "https://zhuanlan.zhihu.com/p/9876543210123456789/edit",
    }


def test_preflight_delivery_freezes_existing_draft_ids_before_editor_write() -> None:
    page = _FakeEditorPage(
        drafts_api_titles=[
            {"id": "100", "title": "已有草稿"},
            {"id": "200", "title": "允许同名"},
        ]
    )
    platform = _make_delivery_platform(page)

    run(platform.preflight_delivery("  允许同名  "))

    assert platform._preflight_title == "允许同名"
    assert platform._preflight_draft_ids == frozenset({"100", "200"})
    assert platform._preflight_draft_sources == frozenset({"api"})
    assert platform._preflight_draft_ids_by_source == {
        "api": frozenset({"100", "200"}),
    }
    assert DRAFTS_URL in page.goto_urls


def test_preflight_delivery_accepts_successfully_read_empty_draft_box(
    monkeypatch,
) -> None:
    platform = _make_delivery_platform()

    async def empty_api():
        return []

    async def empty_dom():
        return []

    monkeypatch.setattr(platform, "_read_draft_candidates_from_api", empty_api)
    monkeypatch.setattr(platform, "_read_draft_candidates_from_dom", empty_dom)

    run(platform.preflight_delivery("首次保存"))

    assert platform._preflight_draft_ids == frozenset()
    assert platform._preflight_draft_sources == frozenset({"api"})
    assert platform._preflight_draft_ids_by_source == {"api": frozenset()}


@pytest.mark.parametrize(
    "page",
    [
        _FakeEditorPage(drafts_api_status=403),
        _FakeEditorPage(drafts_api_payload={"error": "risk_control"}),
    ],
)
def test_preflight_delivery_rejects_unverified_empty_api_response(page) -> None:
    platform = _make_delivery_platform(page)

    with pytest.raises(DraftBaselineError, match="DRAFT_BASELINE_UNAVAILABLE"):
        run(platform.preflight_delivery("不能伪造空基线"))


def test_required_api_baseline_does_not_mix_in_dom_only_candidate() -> None:
    title = "同源差集"
    fixture = (
        PROJECT_ROOT / "tests" / "fixtures" / "zhihu" / "draft_card.html"
    ).read_text(encoding="utf-8").replace("脱敏测试标题：多屏工作流", title)
    page = _FakeEditorPage(
        drafts_api_titles=[{"id": "200", "title": title}],
        drafts_html=fixture,
    )
    platform = _make_delivery_platform(page)

    assert run(
        platform._find_unique_exact_draft(
            title,
            excluded_ids=frozenset({"200"}),
            baseline_ids_by_source={"api": frozenset({"200"})},
        )
    ) is None


def test_required_sources_must_identify_same_new_draft_id() -> None:
    title = "同源差集"
    fixture = (
        PROJECT_ROOT / "tests" / "fixtures" / "zhihu" / "draft_card.html"
    ).read_text(encoding="utf-8").replace("脱敏测试标题：多屏工作流", title)
    page = _FakeEditorPage(
        drafts_api_titles=[{"id": "300", "title": title}],
        drafts_html=fixture,
    )
    page.url = DRAFTS_URL
    platform = _make_delivery_platform(page)

    assert run(
        platform._find_unique_exact_draft(
            title,
            baseline_ids_by_source={
                "api": frozenset(),
                "dom": frozenset(),
            },
        )
    ) is None


def test_required_sources_accept_same_new_draft_id() -> None:
    title = "同源一致"
    html = f"""
        <div class="CreationManage-CreationCard css-random">
          <div class="CreationCardTitle-wrapper css-hash">{title}</div>
          <a href="https://zhuanlan.zhihu.com/p/300/edit">编辑</a>
        </div>
    """
    page = _FakeEditorPage(
        drafts_api_titles=[{"id": "300", "title": title}],
        drafts_html=html,
    )
    page.url = DRAFTS_URL
    platform = _make_delivery_platform(page)

    assert run(
        platform._find_unique_exact_draft(
            title,
            baseline_ids_by_source={
                "api": frozenset(),
                "dom": frozenset(),
            },
        )
    )["id"] == "300"


@pytest.mark.parametrize(
    ("url", "html"),
    [
        ("https://www.zhihu.com/signin", "<div>登录知乎</div>"),
        (DRAFTS_URL, "<div id='root'></div>"),
        ("https://www.zhihu.com/404", "<div>404</div>"),
    ],
)
def test_dom_draft_candidates_reject_login_error_and_unhydrated_pages(
    url: str,
    html: str,
) -> None:
    page = _FakeEditorPage(drafts_html=html)
    page.url = url
    platform = _make_delivery_platform(page)

    assert run(platform._read_draft_candidates_from_dom()) is None


def test_save_draft_binds_unique_new_id_even_when_old_same_title_exists() -> None:
    title = "允许同名"
    page = _FakeEditorPage(
        drafts_api_titles=[
            {"id": "200", "title": title},
            {"id": "300", "title": title},
        ],
        title_text=title,
    )
    page.dom_tokens = [{"kind": "text", "text": "正文"}]
    platform = _make_delivery_platform(page)
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    _set_draft_baseline(platform, title, "200")

    assert run(platform.save_draft(title)).endswith("/p/300/edit")
    assert platform._last_draft_evidence.to_dict()["draft_entity_bound"] is True
    assert (
        platform._last_draft_evidence.to_dict()["draft_entity_source"]
        == "baseline_new_id"
    )
    assert platform._last_draft_evidence.to_dict()["draft_entity_id_match"] is True


def test_save_draft_browser_close_after_autosave_is_result_unknown() -> None:
    title = "自动保存后关闭"
    page = _FakeEditorPage(drafts_api_titles=[{"id": "300", "title": title}])
    platform = _make_delivery_platform(page)
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    _set_draft_baseline(platform, title, "200")

    async def closed_goto(_url: str, **_kwargs) -> None:
        raise RuntimeError("Target page, context or browser has been closed")

    page.goto = closed_goto
    with pytest.raises(DraftResultUnknownError, match="自动保存后页面已关闭") as caught:
        run(platform.save_draft(title))
    assert caught.value.evidence.to_dict()["unknown"] is True


def test_save_draft_does_not_reuse_old_same_title_without_new_id() -> None:
    title = "已有同名草稿"
    page = _FakeEditorPage(
        drafts_api_titles=[{"id": "200", "title": title}],
        title_text=title,
    )
    platform = _make_delivery_platform(page)
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    _set_draft_baseline(platform, title, "200")

    with pytest.raises(DraftResultUnknownError, match="唯一草稿"):
        run(platform.save_draft(title))


def test_save_draft_returns_drafts_url_when_title_appears() -> None:
    title = "深夜食堂的标题"
    page = _FakeEditorPage(drafts_api_titles=[title], title_text=title)
    page.dom_tokens = [{"kind": "text", "text": "正文"}]
    platform = _make_delivery_platform(page)
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    _set_draft_baseline(platform, title)

    url = run(platform.save_draft(title))

    assert url.endswith("/p/1/edit")


def test_save_draft_rejects_duplicate_exact_titles() -> None:
    title = "完全相同的标题"
    page = _FakeEditorPage(
        drafts_api_titles=[
            {"id": "1", "title": title},
            {"id": "2", "title": title},
        ],
        title_text=title,
    )
    platform = _make_delivery_platform(page)
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    _set_draft_baseline(platform, title)

    with pytest.raises(DraftResultUnknownError, match="唯一草稿"):
        run(platform.save_draft(title))


def test_save_draft_reopen_rejects_missing_persisted_tail() -> None:
    title = "持久化正文缺尾"
    page = _FakeEditorPage(drafts_api_titles=[title], title_text=title)
    page.dom_tokens = [{"kind": "text", "text": "第一段"}]
    platform = _make_delivery_platform(page)
    platform._expected_persisted_blocks = [
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
    ]
    _set_draft_baseline(platform, title)

    with pytest.raises(DraftResultUnknownError, match="图文结构不完整"):
        run(platform.save_draft(title))


def test_persisted_reopen_waits_for_async_title_hydration(monkeypatch) -> None:
    title = "异步水合标题"
    page = _FakeEditorPage(drafts_api_titles=[title], title_text=title)
    page.dom_tokens = [{"kind": "text", "text": "正文"}]
    platform = _make_delivery_platform(page)
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    _set_draft_baseline(platform, title)
    values = iter(["", title])

    async def delayed_input_value():
        return next(values)

    async def instant_sleep(_seconds):
        return None

    monkeypatch.setattr(page.title, "input_value", delayed_input_value)
    monkeypatch.setattr("platforms.zhihu.asyncio.sleep", instant_sleep)

    assert run(platform.save_draft(title)).endswith("/p/1/edit")


def test_save_draft_returns_empty_when_title_missing() -> None:
    page = _FakeEditorPage(drafts_text="草稿箱(4)\n别的文章")
    platform = _make_delivery_platform(page)

    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    _set_draft_baseline(platform, "深夜食堂的标题")
    with pytest.raises(DraftResultUnknownError):
        run(platform.save_draft("深夜食堂的标题"))


def test_save_draft_requires_full_exact_title_from_api() -> None:
    page = _FakeEditorPage(
        drafts_text="草稿箱(4)\n系统升级中，请稍后再试",
        drafts_api_titles=["凌晨三点，公司的智能马桶开始给我做绩效面谈"],
    )
    platform = _make_delivery_platform(page)
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    _set_draft_baseline(platform, "凌晨三点，公司的智能马桶")

    with pytest.raises(DraftResultUnknownError):
        run(platform.save_draft("凌晨三点，公司的智能马桶"))


def test_save_draft_fails_honestly_when_api_also_misses_title() -> None:
    page = _FakeEditorPage(
        drafts_text="草稿箱(4)\n系统升级中，请稍后再试",
        drafts_api_titles=["别的文章的标题"],
    )
    platform = _make_delivery_platform(page)

    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    _set_draft_baseline(platform, "凌晨三点，公司的智能马桶")
    with pytest.raises(DraftResultUnknownError):
        run(platform.save_draft("凌晨三点，公司的智能马桶"))


def test_select_topic_is_not_required_for_drafts() -> None:
    platform = _make_delivery_platform()

    result = run(platform.select_topic())

    assert result["success"] is True
    assert result["selection_status"] == "not_required"


def test_select_topic_blocks_public_topic_selection_honestly() -> None:
    platform = _make_delivery_platform()

    result = run(platform.select_topic(topic="科技"))

    assert result["success"] is False
    assert result["needs_selection"] is True
    assert result["error_code"] == "TOPIC_SELECTION_NOT_IMPLEMENTED"


def test_publish_now_requires_verified_existing_draft() -> None:
    platform = _make_delivery_platform()

    with pytest.raises(SelectorError) as error:
        run(platform.publish_now())

    assert error.value.error_code == "SELECTOR_ERROR"
