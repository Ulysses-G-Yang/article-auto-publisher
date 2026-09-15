"""Timing contracts use a virtual clock; editor checks use an isolated browser."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from platforms.action_pacing import ActionPacer
from tests.test_session_stability import ReceiptPlatform


def clocked_pacer(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("platforms.action_pacing.time.monotonic", lambda: clock[0])
    pacer = ActionPacer()

    async def sleep(seconds):
        clock[0] += seconds
        await asyncio.sleep(0)

    pacer._sleep = sleep
    return pacer, clock


def test_character_input_preserves_unicode_and_enforces_budget(monkeypatch):
    pacer, clock = clocked_pacer(monkeypatch)
    observed = []

    async def insert(value):
        observed.append((clock[0], value))

    text = "中文 &é\n第二段😀"
    asyncio.run(pacer.insert_text(SimpleNamespace(insert_text=insert), text))
    assert "".join(value for _, value in observed) == text
    assert all(len(value) == 1 for _, value in observed)
    assert clock[0] >= len(text) / 6 + 2.4 - 1e-8
    assert all(b[0] - a[0] >= 1 / 6 - 1e-8 for a, b in zip(observed, observed[1:], strict=False))


def test_partial_input_failure_never_retypes_or_falls_back(monkeypatch):
    pacer, _ = clocked_pacer(monkeypatch)
    written = []

    async def insert(value):
        if value == "坏":
            raise RuntimeError("editor interrupted")
        written.append(value)

    with pytest.raises(RuntimeError, match="interrupted"):
        asyncio.run(pacer.insert_text(SimpleNamespace(insert_text=insert), "好坏后续"))
    assert written == ["好"]


def test_clicks_serialize_with_interval_and_no_retry(monkeypatch):
    pacer, clock = clocked_pacer(monkeypatch)
    starts = []

    async def click():
        starts.append(clock[0])
        await asyncio.sleep(0)
        if len(starts) == 2:
            raise RuntimeError("unknown result")

    async def scenario():
        results = await asyncio.gather(
            pacer.perform(click), pacer.perform(click), return_exceptions=True
        )
        assert isinstance(results[1], RuntimeError)

    asyncio.run(scenario())
    assert len(starts) == 2
    assert starts[0] >= 5
    assert starts[1] - starts[0] >= 5


@pytest.mark.parametrize("method", ["fill", "insert_metadata"])
def test_title_is_filled_once_without_transient_draft_titles(monkeypatch, method):
    pacer, clock = clocked_pacer(monkeypatch)
    native = AsyncMock()
    target = SimpleNamespace(fill=native, insert_text=native)
    asyncio.run(getattr(pacer, method)(target, "完整标题"))
    native.assert_awaited_once_with("完整标题")
    assert clock[0] >= 4 / 6


def test_native_keyboard_shortcut_sequence_has_minimum_typing_delay(monkeypatch):
    pacer, clock = clocked_pacer(monkeypatch)
    keyboard = SimpleNamespace(type=AsyncMock())
    asyncio.run(pacer.type_text(keyboard, "## ", delay=1))
    keyboard.type.assert_awaited_once()
    assert keyboard.type.await_args.args == ("## ",)
    assert 1000 / 6 <= keyboard.type.await_args.kwargs["delay"] <= 1500 / 6
    assert clock[0] >= 5


def test_each_action_draws_a_new_random_interval_including_first_action(monkeypatch):
    pacer, clock = clocked_pacer(monkeypatch)
    samples = iter([5.1, 9.7, 6.4])
    draws, starts = [], []

    def uniform(low, high):
        draws.append((low, high))
        return next(samples)

    monkeypatch.setattr("platforms.action_pacing.random.uniform", uniform)

    async def click():
        starts.append(clock[0])

    async def scenario():
        for _ in range(3):
            await pacer.perform(click)

    asyncio.run(scenario())
    assert draws == [(5, 10)] * 3
    assert starts == pytest.approx([5.1, 14.8, 21.2])


def test_typing_and_paragraphs_are_random_without_weakening_minimum(monkeypatch):
    pacer, clock = clocked_pacer(monkeypatch)
    samples = iter([6, 1, 1.5, 2, 1.2, 2.4])
    monkeypatch.setattr("platforms.action_pacing.random.uniform", lambda *_: next(samples))
    starts = []

    async def insert(char):
        starts.append((char, clock[0]))

    asyncio.run(pacer.insert_text(SimpleNamespace(insert_text=insert), "甲\n乙"))
    assert [char for char, _ in starts] == ["甲", "\n", "乙"]
    assert starts[1][1] - starts[0][1] == pytest.approx(1 / 6)
    assert starts[2][1] - starts[1][1] == pytest.approx(1.5 / 6 + 2)


@pytest.mark.parametrize("legacy", [0.5, 1, 10])
def test_legacy_fixed_interval_migrates_to_five_to_ten_seconds(legacy):
    pacer = ActionPacer({"action_interval_seconds": legacy})
    assert (pacer.action_interval_min, pacer.action_interval_max) == (5, 10)


@pytest.mark.parametrize(
    "settings",
    [
        {"action_interval_min_seconds": 1},
        {"action_interval_min_seconds": 10},
        {"action_interval_max_seconds": 5},
        {"action_interval_min_seconds": 8, "action_interval_max_seconds": 7},
    ],
)
def test_fixed_or_fast_action_ranges_are_rejected(settings):
    with pytest.raises(ValueError):
        ActionPacer(settings)


def test_event_wait_accounts_for_configured_pause_before_the_native_click():
    pacer = ActionPacer({"action_interval_seconds": 10})
    assert pacer.event_timeout(4000) == 14000
    assert pacer.event_timeout(4000, actions=2) == 24000
    assert pacer.event_timeout(0) == 0


@pytest.mark.parametrize("value", [0, -1, 9, float("nan"), float("inf"), True])
def test_pacing_cannot_be_disabled_by_invalid_configuration(value):
    with pytest.raises(ValueError):
        ActionPacer({"characters_per_second": value})


def test_existing_platform_actions_cannot_skip_shared_pacer():
    root = Path(__file__).resolve().parents[1] / "platforms"
    native = {
        "click",
        "dblclick",
        "tap",
        "press",
        "check",
        "uncheck",
        "set_checked",
        "set_input_files",
        "set_files",
        "select_option",
        "goto",
        "reload",
        "go_back",
        "go_forward",
        "fill",
        "insert_text",
        "type",
        "focus",
        "hover",
        "drag_to",
        "drag_and_drop",
        "dispatch_event",
        "clear",
        "bring_to_front",
        "scroll_into_view_if_needed",
    }
    bypasses = []
    for path in root.glob("*.py"):
        if path.name in {"action_pacing.py", "session_state.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr in native:
                owner = ast.unparse(call.func.value)
                if owner != "self.actions":
                    bypasses.append(f"{path.name}:{node.lineno}")
            if call.func.attr == "evaluate" and any(
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and any(
                    marker in arg.value
                    for marker in (
                        ".click(",
                        ".dispatchEvent(",
                        ".insertContent(",
                        ".insertContentAt(",
                        ".insertAdjacentHTML(",
                        ".focus(",
                    )
                )
                for arg in call.args
            ):
                bypasses.append(f"{path.name}:{node.lineno}:script action")
    assert bypasses == []


def test_real_native_body_input_preserves_multiline_and_readback():
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                context = await browser.new_context()
                await context.route("**/*", lambda route: route.abort())
                page = await context.new_page()
                await page.set_content(
                    '<textarea id="body">旧正文</textarea>'
                    '<div contenteditable="true" id="rich"></div>'
                )
                pacer = ActionPacer()
                value = "第一段 & <测试>\n第二段 😀"
                await pacer.fill_body(page.locator("#body"), value)
                assert await page.locator("#body").input_value() == value
                await page.locator("#rich").focus()
                await pacer.insert_text(page.keyboard, "图前段落")
                await pacer.perform(page.keyboard.press, "Enter")
                await pacer.insert_text(page.keyboard, "图后段落")
                assert (await page.locator("#rich").inner_text()).splitlines() == [
                    "图前段落",
                    "图后段落",
                ]
                # Exercise an armed native event with a real pause longer than
                # its original timeout. No real account or network is involved.
                await page.set_content(
                    '<input id="file" type="file" style="display:none">'
                    "<button onclick=\"document.querySelector('#file').click()\">"
                    "选择文件</button>"
                )
                paced = ActionPacer()
                paced._sleep = asyncio.sleep
                await paced.perform(page.locator("button").focus)
                started = asyncio.get_running_loop().time()
                async with page.expect_file_chooser(timeout=paced.event_timeout(400)) as pending:
                    await paced.perform(page.locator("button").click)
                assert await (await pending.value).element.get_attribute("id") == "file"
                assert asyncio.get_running_loop().time() - started >= 5
                platform = ReceiptPlatform()
                platform.page = page
                await page.set_content("<p>文章提到安全验证，但没有要求用户操作</p>")
                assert await platform.login_obstacle_code() == "SESSION_CHECK_FAILED"
                await page.set_content('<div role="dialog">请完成安全验证</div>')
                assert await platform.login_obstacle_code() == "CHALLENGE"
                await page.set_content('<input type="password">')
                assert await platform.login_obstacle_code() == "LOGIN_REQUIRED"
            finally:
                await browser.close()

    asyncio.run(scenario())
