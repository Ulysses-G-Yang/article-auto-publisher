"""小黑盒草稿箱保存真值校验测试。

测试只使用内存页面桩，不启动浏览器、不访问平台，也不写运行中的服务数据。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from platforms.base import BrowserLifecycleError, DraftResultUnknownError
from platforms.xiaoheihe import XiaoheihePlatform


class _NoDelay:
    async def random_delay(self, *_args, **_kwargs):
        return None


class _FakePage:
    def __init__(
        self,
        *,
        url: str,
        snapshots=None,
        evaluate_error=None,
        click_error=None,
        save_button_count: int = 1,
        save_button_text: str = "保存草稿",
        open_result_url: str = (
            "https://www.xiaoheihe.cn/creator/editor/edit/article/987654321"
        ),
        open_match_count: int = 1,
        reopen_result_url: str | None = None,
        title_text: str = "",
    ):
        self.url = url
        self.snapshots = list(snapshots or [])
        self.evaluate_error = evaluate_error
        self.click_error = click_error
        self.closed = False
        self.click_count = 0
        self.evaluate_scripts: list[str] = []
        self.goto_count = 0
        self.goto_urls: list[str] = []
        self.save_button_count = save_button_count
        self.save_button_text = save_button_text
        self.open_result_url = open_result_url
        self.open_match_count = open_match_count
        self.reopen_result_url = reopen_result_url
        self.title_text = title_text
        self.open_payloads: list[str] = []

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, url: str, **_kwargs):
        if "/creator/editor/edit/article/" in url and self.reopen_result_url:
            self.url = self.reopen_result_url
        else:
            self.url = url
        self.goto_count += 1
        self.goto_urls.append(url)

    async def evaluate(self, script: str, *args):
        self.evaluate_scripts.append(script)
        if self.evaluate_error is not None:
            raise self.evaluate_error
        if "expectedOpaque" in script:
            self.open_payloads.append(args[0] if args else {})
            if self.open_match_count == 1:
                self.url = self.open_result_url
            return self.open_match_count
        if self.snapshots:
            return self.snapshots.pop(0)
        return []

    async def click(self, *_args, **_kwargs):
        self.click_count += 1
        if self.click_error is not None:
            raise self.click_error

    def locator(self, selector: str):
        return _FakeButtonList(self, selector)

    async def close(self):
        self.closed = True


class _FakeButton:
    def __init__(self, page: _FakePage, selector: str):
        self.page = page
        self.selector = selector

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def inner_text(self):
        if self.selector == XiaoheihePlatform.TITLE_FIELD:
            return self.page.title_text
        return self.page.save_button_text

    async def get_attribute(self, name: str):
        return "editor-publish__save-draft" if name == "class" else None

    async def click(self, *_args, **_kwargs):
        await self.page.click()


class _FakeButtonList:
    def __init__(self, page: _FakePage, selector: str):
        self.page = page
        self.selector = selector

    async def count(self):
        return self.page.save_button_count

    def nth(self, _index: int):
        return _FakeButton(self.page, self.selector)


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
    platform.DRAFT_ROUTE_VERIFY_INTERVAL_SECONDS = 0
    platform.DRAFT_CONTENT_POLL_DELAYS = (0,)
    return platform


def _candidate(opaque: str, preview: str) -> dict[str, str]:
    return {"opaque": opaque, "preview": preview}


def _main_page(*snapshots, title_text: str = "") -> _FakePage:
    return _FakePage(
        url="https://www.xiaoheihe.cn/creator/editor/fixture",
        snapshots=list(snapshots),
        title_text=title_text,
    )


def _baseline_page(*snapshots) -> _FakePage:
    return _FakePage(
        url=XiaoheihePlatform.DRAFTS_URL,
        snapshots=list(snapshots),
    )


@pytest.mark.asyncio
async def test_save_draft_requires_new_exact_matching_card_and_closes_baseline_page():
    expected = "一个带空格的测试标题"
    baseline = _baseline_page([_candidate("old", "旧正文摘要")])
    main = _main_page(
        [_candidate("old", "旧正文摘要")],
        [_candidate("old", "旧正文摘要"), _candidate("new", "新正文摘要")],
        title_text=expected,
    )
    platform = _platform(main, baseline)

    result = await platform.save_draft(expected)

    assert result == "https://www.xiaoheihe.cn/creator/editor/edit/article/987654321"
    assert main.click_count == 1
    assert baseline.closed is True
    assert platform.context.new_page_count == 1
    assert main.open_payloads[-1] == "new"
    assert main.goto_urls[-1] == result
    evidence = platform._last_draft_evidence.to_dict()
    assert evidence["draft_entity_bound"] is True
    assert evidence["draft_entity_source"] == "baseline_new_id"
    assert evidence["draft_entity_id_match"] is True
    assert evidence["draft_url"] == result


@pytest.mark.asyncio
async def test_save_draft_reloads_slow_draft_list_without_repeating_save():
    expected = "慢加载草稿"
    baseline = _baseline_page([_candidate("old", "旧草稿")])
    main = _main_page(
        {"candidates": [], "reliable": True},
        {"candidates": [], "reliable": True},
        {"candidates": [_candidate("new", expected)], "reliable": True},
        title_text=expected,
    )
    platform = _platform(main, baseline)

    assert await platform.save_draft(expected) == (
        "https://www.xiaoheihe.cn/creator/editor/edit/article/987654321"
    )
    assert main.click_count == 1
    # 打开草稿箱、两次只读刷新、按数字 ID 显式重开编辑页。
    assert main.goto_count == 4


@pytest.mark.asyncio
async def test_save_draft_fails_before_side_effect_when_button_is_ambiguous():
    baseline = _baseline_page([_candidate("old", "旧草稿")])
    main = _FakePage(
        url="https://www.xiaoheihe.cn/creator/editor/fixture",
        save_button_count=2,
    )
    platform = _platform(main, baseline)

    assert await platform.save_draft("按钮候选不唯一") == ""
    assert main.click_count == 0


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

    with pytest.raises(DraftResultUnknownError):
        await platform.save_draft("正文中出现的标题")
    assert main.click_count == 1
    assert baseline.closed is True
    assert all("document.body.innerText" not in script for script in main.evaluate_scripts)


@pytest.mark.asyncio
async def test_old_same_title_card_does_not_count_as_new_draft():
    title = "已有同名标题"
    baseline = _baseline_page([_candidate("old", title)])
    main = _main_page([_candidate("old", title)], [_candidate("old", title)])
    platform = _platform(main, baseline)

    with pytest.raises(DraftResultUnknownError):
        await platform.save_draft(title)
    assert main.click_count == 1


@pytest.mark.asyncio
async def test_hidden_or_non_card_candidate_is_ignored_by_snapshot_contract():
    baseline = _baseline_page([_candidate("old", "旧草稿")])
    main = _main_page([_candidate("old", "旧草稿")], [])
    platform = _platform(main, baseline)

    with pytest.raises(DraftResultUnknownError):
        await platform.save_draft("隐藏卡片标题")
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
    assert "occurrence_count" in snapshot_script
    assert ".creator-draft__image" in snapshot_script
    assert "preview" in snapshot_script
    assert "hasStableListRoot" not in snapshot_script
    assert r"replace(/\s+/g" in snapshot_script
    assert "edit/article/[0-9]+" in snapshot_script
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
        title_text=title,
    )
    platform = _platform(main, baseline)

    assert await platform.save_draft(title) == (
        "https://www.xiaoheihe.cn/creator/editor/edit/article/987654321"
    )
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

    with pytest.raises(DraftResultUnknownError):
        await platform.save_draft("保存时关闭")
    assert main.click_count == 1
    assert baseline.closed is True


@pytest.mark.asyncio
async def test_non_browser_error_after_save_attempt_is_result_unknown():
    baseline = _baseline_page([_candidate("old", "旧草稿")])
    main = _FakePage(
        url="https://www.xiaoheihe.cn/creator/editor/fixture",
        click_error=RuntimeError("click result unavailable"),
    )
    platform = _platform(main, baseline)

    with pytest.raises(DraftResultUnknownError, match="可能已触发") as caught:
        await platform.save_draft("保存结果未知")
    assert caught.value.evidence.to_dict()["unknown"] is True
    assert main.click_count == 1
    assert baseline.closed is True


def test_new_draft_requires_exactly_one_new_entity_without_title_filter():
    title = "编辑页真实标题"
    baseline = [_candidate("old", "旧正文摘要")]
    assert XiaoheihePlatform._has_new_matching_draft(
        baseline,
        [_candidate("old", "旧正文摘要"), _candidate("new", "新正文摘要")],
        title,
    )
    assert not XiaoheihePlatform._has_new_matching_draft(
        baseline,
        [
            _candidate("old", "旧正文摘要"),
            _candidate("new-1", "新摘要一"),
            _candidate("new-2", "新摘要二"),
        ],
        title,
    )


def test_old_card_and_one_distinct_new_preview_can_bind() -> None:
    title = "标题不会出现在草稿卡摘要里"
    baseline = [_candidate("fingerprint:old", "旧摘要")]
    current = [
        _candidate("fingerprint:old", "旧摘要"),
        _candidate("fingerprint:new", "新摘要"),
    ]

    candidate = XiaoheihePlatform._new_matching_draft_candidate(
        baseline,
        current,
        title,
    )

    assert candidate == current[1]


def test_duplicate_fingerprint_cannot_bind_a_specific_new_card() -> None:
    title = "完全相同的同名草稿"
    current = [
        {
            "opaque": "fingerprint:same",
            "preview": title,
            "occurrence_count": 2,
        }
    ]

    assert XiaoheihePlatform._new_matching_draft_candidate([], current, title) is None


@pytest.mark.parametrize(
    ("url", "draft_id"),
    [
        (
            "https://www.xiaoheihe.cn/creator/editor/edit/article/188000366",
            "188000366",
        ),
        ("https://www.xiaoheihe.cn/creator/editor/edit/article/not-a-number", ""),
        ("https://example.com/creator/editor/edit/article/188000366", ""),
        ("https://www.xiaoheihe.cn/creator/draft", ""),
    ],
)
def test_real_editor_route_extracts_only_stable_numeric_id(
    url: str,
    draft_id: str,
) -> None:
    assert XiaoheihePlatform._draft_id_from_editor_url(url) == draft_id


@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        ("https://www.xiaoheihe.cn/creator/draft", True),
        ("https://www.xiaoheihe.cn/creator/draft?article_type=all", True),
        ("http://www.xiaoheihe.cn/creator/draft", False),
        ("https://www.xiaoheihe.cn/creator/draft-old", False),
        ("https://example.com/?next=/creator/draft", False),
    ],
)
def test_drafts_route_requires_exact_https_origin_and_path(
    url: str,
    accepted: bool,
) -> None:
    assert XiaoheihePlatform._is_drafts_route(url) is accepted


@pytest.mark.asyncio
async def test_save_draft_rejects_card_navigation_without_stable_id() -> None:
    title = "路由缺少稳定 ID"
    baseline = _baseline_page([_candidate("old", "旧草稿")])
    main = _FakePage(
        url="https://www.xiaoheihe.cn/creator/editor/fixture",
        snapshots=[[_candidate("old", "旧草稿"), _candidate("new", title)]],
        open_result_url=(
            "https://www.xiaoheihe.cn/creator/editor/edit/article/not-a-number"
        ),
    )
    platform = _platform(main, baseline)

    with pytest.raises(DraftResultUnknownError, match="稳定 ID"):
        await platform.save_draft(title)
    assert main.click_count == 1
    assert len(main.open_payloads) == 1


@pytest.mark.asyncio
async def test_save_draft_rejects_redirect_to_different_id_after_reopen() -> None:
    title = "重开后不能串到另一篇草稿"
    baseline = _baseline_page([_candidate("old", "旧摘要")])
    main = _FakePage(
        url="https://www.xiaoheihe.cn/creator/editor/fixture",
        snapshots=[[_candidate("old", "旧摘要"), _candidate("new", "新摘要")]],
        reopen_result_url=(
            "https://www.xiaoheihe.cn/creator/editor/edit/article/222222222"
        ),
    )
    platform = _platform(main, baseline)

    with pytest.raises(DraftResultUnknownError, match="ID 不一致"):
        await platform.save_draft(title)
    assert len(main.open_payloads) == 1


@pytest.mark.asyncio
async def test_wrong_new_card_title_never_becomes_bound_success() -> None:
    expected_title = "本次真正标题"
    baseline = _baseline_page([_candidate("old", "旧摘要")])
    main = _FakePage(
        url="https://www.xiaoheihe.cn/creator/editor/fixture",
        snapshots=[[_candidate("old", "旧摘要"), _candidate("new", "无关摘要")]],
        title_text="另一篇并发新增草稿",
    )
    platform = _platform(main, baseline)
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]

    with pytest.raises(DraftResultUnknownError, match="标题不一致"):
        await platform.save_draft(expected_title)

    evidence = platform._last_draft_evidence.to_dict()
    assert evidence["draft_entity_bound"] is False
    assert evidence["draft_entity_id_match"] is False
    assert evidence["reopen_title_match"] is False


def test_snapshot_projection_keeps_only_opaque_entity_metadata():
    page = SimpleNamespace(
        url=XiaoheihePlatform.DRAFTS_URL,
        is_closed=lambda: False,
    )

    async def evaluate(_script):
        return [
            {
                "opaque": "draft-key",
                "preview": "  正文摘要\n\t带空格  ",
                "cookie": "should-not-survive",
                "path": "D:\\Secret Folder\\article.png",
            },
            {"opaque": "", "preview": "无实体"},
        ]

    page.evaluate = evaluate
    platform = XiaoheihePlatform()
    platform.page = page
    platform.context = SimpleNamespace(pages=[])

    async def run():
        return await platform._snapshot_draft_candidates(page)

    import asyncio

    result = asyncio.run(run())
    assert result == [
        {
            "opaque": "draft-key",
            "occurrence_count": 1,
            "card_index": -1,
        }
    ]
    assert "cookie" not in result[0]
    assert "path" not in result[0]
    assert "preview" not in result[0]
