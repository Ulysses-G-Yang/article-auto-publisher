from __future__ import annotations

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from platforms.toutiao import DRAFT_BOX_URL, ToutiaoPlatform


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
        self.draft_box_text = ""

    def is_closed(self) -> bool:
        return False

    def on(self, event: str, handler) -> None:
        if event == "response":
            self._response_handlers.append(handler)
            # 模拟页面自动保存已在注册前完成：注册后立即投递配置的响应。
            for response in list(self.responses_on_goto):
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(handler(response))
                except RuntimeError:
                    pass

    def remove_listener(self, event: str, handler) -> None:
        if event == "response":
            self._response_handlers = [
                h for h in self._response_handlers if h is not handler
            ]

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        for response in list(self.responses_on_goto):
            for handler in list(self._response_handlers):
                await handler(response)

    async def evaluate(self, script: str, *args) -> object:
        if "includes" in script:
            keyword = args[0] if args else ""
            return keyword in self.draft_box_text
        if "querySelectorAll" in script and "texts[0]" in script:
            return self.dom_nickname
        return None


def _make_platform(
    page: FakePage,
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
PUBLISH_URL = "https://mp.toutiao.com/mp/agw/article/publish?type=article"


def _real_sleep_and_fake() -> tuple:
    real_sleep = asyncio.sleep

    async def fake_sleep(_: float) -> None:
        # 让出事件循环一次，使已调度的 response handler 得以执行。
        await real_sleep(0)

    return real_sleep, fake_sleep


def test_fetch_identity_parses_same_origin_api(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = FakePage()
    page.responses_on_goto = [
        FakeResponse(
            IDENTITY_API_URL,
            {
                "data": {"user_name": "率真海风gy504gO"},
                "err_no": 0,
            },
            method="GET",
        )
    ]
    platform = _make_platform(page)

    payload = run(platform.fetch_identity_payload())

    assert payload["ok"] is True
    assert payload["user_id"] == "2610667347771923"
    assert payload["display_name"] == "率真海风gy504gO"


def test_fetch_identity_falls_back_to_dom_nickname(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = FakePage()
    page.dom_nickname = "工作台昵称兜底"
    platform = _make_platform(page)

    payload = run(platform.fetch_identity_payload())

    assert payload["ok"] is True
    assert payload["user_id"] == ""
    assert payload["display_name"] == "工作台昵称兜底"


def test_fetch_identity_fails_when_nothing_confirms(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    platform = _make_platform(FakePage())

    payload = run(platform.fetch_identity_payload())

    assert payload["ok"] is False


def test_check_login_requires_cookie_and_identity(monkeypatch) -> None:
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


def test_save_draft_fails_honestly_on_risk_control_rejection(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = FakePage()
    page.responses_on_goto = [
        FakeResponse(PUBLISH_URL, {"code": 7050, "err_no": 7050, "reason": "保存失败"})
    ]
    platform = _make_platform(page)

    url = run(platform.save_draft("测试标题"))

    assert url == ""
    assert page.goto_calls == []


def test_save_draft_returns_draft_url_when_success_and_title_in_box(
    monkeypatch,
) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = FakePage()
    page.responses_on_goto = [
        FakeResponse(PUBLISH_URL, {"code": 0, "err_no": 0, "reason": "保存成功"})
    ]
    page.draft_box_text = "草稿箱\n测试标题\n共 1 条内容"
    platform = _make_platform(page)

    url = run(platform.save_draft("测试标题"))

    assert url == DRAFT_BOX_URL
    assert DRAFT_BOX_URL in page.goto_calls


def test_save_draft_empty_when_title_missing_in_box(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    page = FakePage()
    page.responses_on_goto = [
        FakeResponse(PUBLISH_URL, {"code": 0, "err_no": 0, "reason": "保存成功"})
    ]
    page.draft_box_text = "草稿箱\n另一篇文章\n共 1 条内容"
    platform = _make_platform(page)

    url = run(platform.save_draft("测试标题"))

    assert url == ""


def test_save_draft_empty_when_no_save_request(monkeypatch) -> None:
    _real, fake_sleep = _real_sleep_and_fake()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    platform = _make_platform(FakePage())

    url = run(platform.save_draft("测试标题"))

    assert url == ""
