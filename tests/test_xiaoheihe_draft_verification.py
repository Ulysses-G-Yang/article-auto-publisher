"""小黑盒草稿箱保存真值校验测试。

测试只使用内存页面桩，不启动浏览器、不访问平台，也不写运行中的服务数据。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from platforms.base import BrowserLifecycleError
from platforms.xiaoheihe import XiaoheihePlatform


class _NoDelay:
    async def random_delay(self, *_args, **_kwargs):
        return None


class _FakePage:
    def __init__(self, *, url: str, snapshots=None, evaluate_error=None, click_error=None):
        self.url = url
        self.snapshots = list(snapshots or [])
        self.evaluate_error = evaluate_error
        self.click_error = click_error
        self.closed = False
        self.click_count = 0
        self.evaluate_scripts: list[str] = []

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, url: str, **_kwargs):
        self.url = url

    async def evaluate(self, script: str, *_args):
        self.evaluate_scripts.append(script)
        if self.evaluate_error is not None:
            raise self.evaluate_error
        if self.snapshots:
            return self.snapshots.pop(0)
        return []

    async def click(self, *_args, **_kwargs):
        self.click_count += 1
        if self.click_error is not None:
            raise self.click_error

    async def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, baseline_page: _FakePage):
        self.baseline_page = baseline_page
        self.pages = []
        self.new_page_count = 0

    async def new_page(self):
        self.new_page_count += 1
        self.pages.append(self.baseline_page)
        return self.baseline_page


def _platform(main_page: _FakePage, baseline_page: _FakePage) -> XiaoheihePlatform:
    platform = XiaoheihePlatform()
    platform.page = main_page
    platform.context = _FakeContext(baseline_page)
    platform.simulator = _NoDelay()
    platform._dismiss_overlays = _NoDelay().random_delay
    return platform


def _candidate(opaque: str, title: str) -> dict[str, str]:
    return {"opaque": opaque, "title": title}


def _main_page(*snapshots) -> _FakePage:
    return _FakePage(
        url="https://www.xiaoheihe.cn/creator/editor/fixture",
        snapshots=list(snapshots),
    )


def _baseline_page(*snapshots) -> _FakePage:
    return _FakePage(
        url=XiaoheihePlatform.DRAFTS_URL,
        snapshots=list(snapshots),
    )


@pytest.mark.asyncio
async def test_save_draft_requires_new_exact_matching_card_and_closes_baseline_page():
    expected = "一个带空格的测试标题"
    baseline = _baseline_page([_candidate("old", "旧草稿")])
    main = _main_page(
        [_candidate("old", "旧草稿")],
        [_candidate("old", "旧草稿"), _candidate("new", expected)],
    )
    platform = _platform(main, baseline)

    result = await platform.save_draft(expected)

    assert result == XiaoheihePlatform.DRAFTS_URL
    assert main.click_count == 1
    assert baseline.closed is True
    assert platform.context.new_page_count == 1


@pytest.mark.asyncio
async def test_body_only_title_is_not_a_draft_card_and_never_succeeds():
    baseline = _baseline_page([_candidate("old", "旧草稿")])
    # 页面全局文字即使包含标题，候选快照没有新增具体实体也必须失败。
    main = _main_page(
        [_candidate("old", "旧草稿")],
        [],
        [],
    )
    platform = _platform(main, baseline)

    assert await platform.save_draft("正文中出现的标题") == ""
    assert main.click_count == 1
    assert baseline.closed is True
    assert all("document.body.innerText" not in script for script in main.evaluate_scripts)


@pytest.mark.asyncio
async def test_old_same_title_card_does_not_count_as_new_draft():
    title = "已有同名标题"
    baseline = _baseline_page([_candidate("old", title)])
    main = _main_page([_candidate("old", title)], [_candidate("old", title)])
    platform = _platform(main, baseline)

    assert await platform.save_draft(title) == ""
    assert main.click_count == 1


@pytest.mark.asyncio
async def test_hidden_or_non_card_candidate_is_ignored_by_snapshot_contract():
    baseline = _baseline_page([_candidate("old", "旧草稿")])
    main = _main_page([_candidate("old", "旧草稿")], [])
    platform = _platform(main, baseline)

    assert await platform.save_draft("隐藏卡片标题") == ""
    snapshot_script = next(
        script for script in main.evaluate_scripts if "candidateSelector" in script
    )
    assert "getComputedStyle" in snapshot_script
    assert "getBoundingClientRect" in snapshot_script
    assert "aria-hidden" in snapshot_script
    assert "document.querySelectorAll" in snapshot_script
    assert "article.creator-draft__item" in snapshot_script
    assert ".creator-draft__list" in snapshot_script
    assert ".creator-draft__content" in snapshot_script
    assert "fingerprintOccurrences" in snapshot_script
    assert r"replace(/\s+/g" in snapshot_script
    assert r"return /\/creator\/editor\/draft\/" in snapshot_script
    assert "[role='status']" not in snapshot_script
    assert "document.body.innerText" not in snapshot_script


@pytest.mark.asyncio
async def test_playwright_evaluate_syntax_error_fails_closed_before_save():
    baseline = _FakePage(
        url=XiaoheihePlatform.DRAFTS_URL,
        evaluate_error=RuntimeError(
            "Page.evaluate: SyntaxError: missing ) after argument list"
        )
    )
    main = _main_page()
    platform = _platform(main, baseline)

    assert await platform.save_draft("脚本语法异常") == ""
    assert main.click_count == 0
    assert baseline.closed is True


@pytest.mark.asyncio
async def test_empty_baseline_fails_closed_without_clicking_save():
    baseline = _baseline_page([])
    main = _main_page()
    platform = _platform(main, baseline)

    assert await platform.save_draft("有标题但没有基线") == ""
    assert main.click_count == 0
    assert baseline.closed is True


@pytest.mark.asyncio
async def test_explicit_visible_empty_state_is_valid_baseline_for_first_new_card():
    title = "空箱中的第一篇草稿"
    baseline = _baseline_page({"candidates": [], "reliable": True})
    main = _main_page(
        {"candidates": [_candidate("new", title)], "reliable": True},
    )
    platform = _platform(main, baseline)

    assert await platform.save_draft(title) == XiaoheihePlatform.DRAFTS_URL
    assert main.click_count == 1
    assert baseline.closed is True


@pytest.mark.asyncio
async def test_empty_title_fails_before_opening_baseline_or_clicking():
    baseline = _baseline_page([_candidate("old", "旧草稿")])
    main = _main_page()
    platform = _platform(main, baseline)

    assert await platform.save_draft("   \n\t") == ""
    assert main.click_count == 0
    assert platform.context.new_page_count == 0
    assert baseline.closed is False


@pytest.mark.asyncio
async def test_baseline_failure_closes_page_and_does_not_click_save():
    baseline = _FakePage(
        url=XiaoheihePlatform.DRAFTS_URL,
        evaluate_error=RuntimeError("network response unavailable"),
    )
    main = _main_page()
    platform = _platform(main, baseline)

    assert await platform.save_draft("基线读取失败") == ""
    assert main.click_count == 0
    assert baseline.closed is True


@pytest.mark.asyncio
async def test_browser_close_during_baseline_is_terminal_and_still_closes_page():
    baseline = _FakePage(
        url=XiaoheihePlatform.DRAFTS_URL,
        evaluate_error=RuntimeError("Target page, context or browser has been closed"),
    )
    main = _main_page()
    platform = _platform(main, baseline)

    with pytest.raises(BrowserLifecycleError):
        await platform.save_draft("浏览器关闭")
    assert main.click_count == 0
    assert baseline.closed is True


@pytest.mark.asyncio
async def test_browser_close_during_save_is_terminal_without_retry():
    baseline = _baseline_page([_candidate("old", "旧草稿")])
    main = _FakePage(
        url="https://www.xiaoheihe.cn/creator/editor/fixture",
        click_error=RuntimeError("Target page, context or browser has been closed"),
    )
    platform = _platform(main, baseline)

    with pytest.raises(BrowserLifecycleError):
        await platform.save_draft("保存时关闭")
    assert main.click_count == 1
    assert baseline.closed is True


def test_new_matching_draft_requires_exactly_one_new_entity():
    title = "精确标题"
    baseline = [_candidate("old", title)]
    assert XiaoheihePlatform._has_new_matching_draft(
        baseline,
        [_candidate("old", title), _candidate("new", title)],
        title,
    )
    assert not XiaoheihePlatform._has_new_matching_draft(
        baseline,
        [_candidate("old", title), _candidate("new-1", title), _candidate("new-2", title)],
        title,
    )


def test_snapshot_projection_discards_untrusted_fields_and_normalizes_title():
    page = SimpleNamespace(
        url=XiaoheihePlatform.DRAFTS_URL,
        is_closed=lambda: False,
    )

    async def evaluate(_script):
        return [
            {
                "opaque": "draft-key",
                "title": "  标题\n\t带空格  ",
                "cookie": "should-not-survive",
                "path": "D:\\Secret Folder\\article.png",
            },
            {"opaque": "", "title": "无实体"},
        ]

    page.evaluate = evaluate
    platform = XiaoheihePlatform()
    platform.page = page
    platform.context = SimpleNamespace(pages=[])

    async def run():
        return await platform._snapshot_draft_candidates(page)

    import asyncio

    result = asyncio.run(run())
    assert result == [{"opaque": "draft-key", "title": "标题 带空格"}]
    assert "cookie" not in result[0]
    assert "path" not in result[0]
