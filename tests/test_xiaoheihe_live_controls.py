"""Observed empty drafts and exact topic names; synthetic HTML only."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from platforms.base import SelectorError
from platforms.xiaoheihe import XiaoheihePlatform, XiaoheihePublishUnknownError


@pytest.mark.parametrize(
    "case", ["success", "old_status", "wrong_route", "duplicate", "wrong_title"],
)
def test_publication_requires_this_article_and_clicks_at_most_once(case):
    async def check():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                title = "其他文章" if case == "wrong_title" else "测试文章"
                target = "/app/bbs/link/123" if case == "success" else "/login"
                script = "window.clicks=(window.clicks||0)+1;"
                if case in {"success", "wrong_route"}:
                    script += f"history.pushState(null,'',{json.dumps(target)});"
                    script += "document.body.innerHTML='<h1>测试文章</h1>';"
                button = '<button class="editor-publish__btn main-btn">发布</button>'
                html = (
                    '<div class="editor-title__container">'
                    f'<div contenteditable="true">{title}</div></div>'
                    + button * (2 if case == "duplicate" else 1)
                    + '<div>已发布</div><button onclick="window.unrelated=true">确定</button>'
                )

                async def serve(route):
                    await route.fulfill(body=html, content_type="text/html; charset=utf-8")

                await page.route("**/*", serve)
                await page.goto("https://www.xiaoheihe.cn/creator/editor/edit/article/123")
                await page.locator("button.main-btn").first.evaluate(
                    "(el,code)=>el.setAttribute('onclick',code)", script,
                )
                platform = XiaoheihePlatform()
                platform.page = page
                platform.PUBLICATION_RESULT_TIMEOUT_MS = 100
                platform._dismiss_overlays = AsyncMock()
                platform.simulator.random_delay = AsyncMock()
                if case == "success":
                    assert await platform.publish_now("测试文章") == (
                        "https://www.xiaoheihe.cn/app/bbs/link/123"
                    )
                else:
                    error = SelectorError if case in {"duplicate", "wrong_title"} else (
                        XiaoheihePublishUnknownError
                    )
                    with pytest.raises(error):
                        await platform.publish_now("测试文章")
                clicked = case not in {"duplicate", "wrong_title"}
                assert await page.evaluate("window.clicks||0") == int(clicked)
                assert await page.evaluate("window.unrelated||false") is False
                if clicked:
                    with pytest.raises(XiaoheihePublishUnknownError):
                        await platform.publish_now("测试文章")
                    assert await page.evaluate("window.clicks||0") == 1
            finally:
                await browser.close()

    asyncio.run(check())


def test_observed_empty_drafts_requires_visible_empty_inside_draft_list():
    async def check():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                html = ""

                async def serve(route):
                    await route.fulfill(body=html, content_type="text/html; charset=utf-8")

                await page.route("**/*", serve)
                platform = XiaoheihePlatform()
                platform.page = page
                for content, expected in [
                    ('<div class="creator-draft__list"><div class="hb-empty">'
                     '<div class="hb-empty_desc">暂无内容</div></div></div>', True),
                    ('<div class="creator-draft__list"></div>'
                     '<div class="hb-empty">暂无内容</div>', False),
                    ('<div class="creator-draft__list"><div class="hb-empty" '
                     'style="display:none">暂无内容</div></div>', False),
                    ('<div class="creator-draft__list"><div class="hb-empty">'
                     '加载中</div></div>', False),
                ]:
                    html = content
                    await page.goto(platform.DRAFTS_URL)
                    assert await platform._snapshot_draft_state(page) == ([], expected)
            finally:
                await browser.close()

    asyncio.run(check())


def test_community_search_never_clicks_unrelated_recommendation():
    async def check():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                platform = XiaoheihePlatform()
                platform.page = page
                platform.simulator.random_delay = AsyncMock()

                async def dismiss():
                    await page.locator(".modal").evaluate("el=>el.style.display='none'")

                platform._dismiss_overlays = dismiss
                for matching in [False, True]:
                    item = (
                        '<div class="editor-model__topic-list-item" '
                        'onclick="window.chosen=this.innerText; '
                        "document.getElementById('selected').innerText='数码硬件';"
                        ' document.querySelector(\'.modal\').style.display=\'none\'">'
                        '<div class="topic-list-item__title">数码硬件</div></div>'
                        if matching else ""
                    )
                    html = '''<button onclick="document.querySelector('.modal').style.display=''">
                    添加社区</button><div id="selected"></div>
                    <div class="modal" style="display:none">
                    <input placeholder="搜索社区"><div class="editor-model__topic-list-item"
                    onclick="window.chosen='unrelated'">
                    <div class="topic-list-item__title">不相关游戏</div></div>''' + item + "</div>"

                    async def serve(route, _request=None, *, body=html):
                        await route.fulfill(body=body, content_type="text/html; charset=utf-8")

                    await page.route("**/*", serve)
                    await page.goto("https://www.xiaoheihe.cn/creator/editor/edit/article/123")
                    result = await platform._select_from_editor_dialog_once(
                        "社区", "数码硬件", ".editor-model__topic-list-item",
                    )
                    assert result["success"] is matching
                    assert await page.evaluate("window.chosen || null") == (
                        "数码硬件" if matching else None
                    )
                    await page.unroute("**/*", serve)
            finally:
                await browser.close()

    asyncio.run(check())


def test_topic_exact_name_is_not_replaced_by_discussion_statistics():
    async def check():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('''
                    <div id="real" class="editor-model__hashtag-list-item">
                      <div class="hashtag-list-item__title">显示器</div>
                      <div class="hashtag-list-item__desc">143996讨论 56971人参与</div>
                    </div>
                    <div id="echo">显示器<br>143996讨论 56971人参与</div>
                    <div id="community" class="editor-model__topic-list-item">
                      <div class="topic-list-item__title">数码硬件</div>
                      <div class="topic-list-item__desc">热度:1234</div>
                    </div>
                ''')
                platform = XiaoheihePlatform()
                assert await platform._extract_candidate_name(
                    page.locator("#real"), "显示器"
                ) == "显示器"
                assert await platform._extract_candidate_name(
                    page.locator("#echo"), "显示器"
                ) == ""
                assert await platform._extract_candidate_name(
                    page.locator("#community"), "数码硬件"
                ) == "数码硬件"
            finally:
                await browser.close()

    asyncio.run(check())
