"""Content Studio 浏览器布局回归。

该测试需要显式设置 ``ARTICLEOPS_FRONTEND_BASE_URL``，只访问隔离 QA 服务。
它不登录、不创建 DeliveryPlan，也不触发平台操作。
"""

import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright


BASE_URL = os.getenv("ARTICLEOPS_FRONTEND_BASE_URL", "").rstrip("/")
BROWSER_PATH = os.getenv("ARTICLEOPS_CHROMIUM_EXECUTABLE", "").strip()


def _outside_viewport(row: dict, viewport_width: int) -> bool:
    return row["left"] < -0.5 or row["right"] > viewport_width + 0.5


@pytest.mark.asyncio
@pytest.mark.skipif(not BASE_URL, reason="需要显式隔离 QA URL")
async def test_studio_key_controls_stay_inside_mobile_and_tablet_viewports() -> None:
    executable = Path(BROWSER_PATH) if BROWSER_PATH else None
    if executable and not executable.is_file():
        pytest.fail(f"Chromium 不存在: {executable}")

    selectors = {
        "studio": "#content-studio",
        "workspace": "#studio-workspace",
        "main": ".studio-main",
        "cards": ".studio-main > .studio-card",
        "card_headers": ".studio-card-header",
        "title": "#draft-title",
        "blocks": ".content-block",
        "textareas": ".block-content textarea",
        "block_actions": ".block-actions",
        "target_builder": ".target-builder",
        "execute_bar": ".execute-bar",
    }

    async with async_playwright() as playwright:
        api = await playwright.request.new_context(base_url=BASE_URL)
        draft_response = await api.post(
            "/api/content-drafts",
            data={
                "title": "QA 边界测试长标题：移动端关键编辑控件必须完整可见",
                "blocks": [
                    {
                        "block_id": "qa-browser-text",
                        "type": "text",
                        "position": 0,
                        "text": "这是一段只用于隔离浏览器布局回归的正文。",
                    }
                ],
            },
        )
        assert draft_response.ok
        draft_id = (await draft_response.json())["draft_id"]
        await api.dispose()

        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable) if executable else None,
        )
        try:
            for width, height in ((390, 844), (1024, 768)):
                page = await browser.new_page(viewport={"width": width, "height": height})
                console_errors: list[dict] = []
                page.on(
                    "console",
                    lambda message: console_errors.append(
                        {"text": message.text, "location": message.location}
                    )
                    if message.type == "error"
                    else None,
                )
                await page.goto(
                    f"{BASE_URL}/upload?draft_id={draft_id}",
                    wait_until="networkidle",
                )
                await page.locator("#studio-workspace:not(.d-none)").wait_for()

                platform_cards = page.locator("#platform-selector-grid .platform-card")
                assert await platform_cards.count() == 10
                assert await page.locator(
                    '.platform-card[data-platform-id="xiaoheihe"]:not(:disabled)'
                ).count() == 1
                assert await page.locator(
                    '.platform-card[data-platform-id="zhihu"]:not(:disabled)'
                ).count() == 1
                assert await page.locator(
                    "#platform-selector-grid .platform-card:disabled"
                ).count() == 7
                rows = await page.evaluate(
                    """selectors => Object.entries(selectors).flatMap(([name, selector]) =>
                        [...document.querySelectorAll(selector)].map((element, index) => {
                            const rect = element.getBoundingClientRect();
                            return {name, index, left: rect.left, right: rect.right,
                                width: rect.width, top: rect.top, bottom: rect.bottom};
                        }))""",
                    selectors,
                )
                clipped = [row for row in rows if _outside_viewport(row, width)]
                body_overflow = await page.evaluate(
                    """() => ({innerWidth, documentWidth: document.documentElement.scrollWidth,
                        bodyWidth: document.body.scrollWidth,
                        bodyOverflowX: getComputedStyle(document.body).overflowX})"""
                )

                assert body_overflow["bodyOverflowX"] != "hidden"
                assert body_overflow["documentWidth"] <= width
                assert body_overflow["bodyWidth"] <= width
                assert not clipped, f"{width}px 关键元素超出视口: {clipped}"
                relevant_errors = [
                    error
                    for error in console_errors
                    if not error["location"].get("url", "").endswith("/favicon.ico")
                ]
                assert not relevant_errors
                await page.close()
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not BASE_URL, reason="需要显式隔离 QA URL")
async def test_account_target_selection_persists_without_creating_plan() -> None:
    executable = Path(BROWSER_PATH) if BROWSER_PATH else None
    if executable and not executable.is_file():
        pytest.fail(f"Chromium 不存在: {executable}")

    async with async_playwright() as playwright:
        api = await playwright.request.new_context(base_url=BASE_URL)
        draft_response = await api.post(
            "/api/content-drafts",
            data={
                "title": "QA 多账号目标持久化",
                "blocks": [
                    {
                        "block_id": "qa-target-text",
                        "type": "text",
                        "position": 0,
                        "text": "只保存系统投递目标，不执行任何计划。",
                    }
                ],
            },
        )
        assert draft_response.ok
        draft_id = (await draft_response.json())["draft_id"]

        accounts_response = await api.get(
            "/api/platforms/xiaoheihe/accounts?usable=true"
        )
        assert accounts_response.ok
        accounts = (await accounts_response.json())["accounts"]
        valid_accounts = [
            account for account in accounts if account["session_status"] == "VALID"
        ]
        assert len(valid_accounts) == 2

        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable) if executable else None,
        )
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        execute_requests: list[str] = []
        page.on(
            "request",
            lambda request: execute_requests.append(request.url)
            if "/delivery-plans" in request.url
            else None,
        )
        try:
            await page.goto(f"{BASE_URL}/upload?draft_id={draft_id}", wait_until="networkidle")
            await page.locator("#studio-workspace:not(.d-none)").wait_for()
            account_select = page.locator("#target-account")
            assert await account_select.is_disabled()

            await page.locator(
                '.platform-card[data-platform-id="xiaoheihe"]'
            ).click()
            await account_select.locator("option").nth(2).wait_for(state="attached")
            assert await account_select.input_value() == ""
            option_labels = await account_select.locator("option").all_text_contents()
            assert [account["display_name"] for account in valid_accounts] == [
                label.split(" · ****", 1)[0] for label in option_labels[1:]
            ]

            selected = valid_accounts[0]
            await account_select.select_option(selected["account_id"])
            await page.locator("#add-target").click()
            await page.locator("#targets-list .target-row").wait_for()
            assert await page.locator("#target-count").text_content() == "1 个目标"

            await page.reload(wait_until="networkidle")
            await page.locator("#studio-workspace:not(.d-none)").wait_for()
            assert await page.locator("#target-count").text_content() == "1 个目标"
            assert selected["display_name"] in await page.locator(
                "#targets-list"
            ).text_content()
            assert execute_requests == []

            persisted = await api.get(f"/api/content-drafts/{draft_id}")
            assert persisted.ok
            targets = (await persisted.json())["targets"]
            assert len(targets) == 1
            assert targets[0]["account_id"] == selected["account_id"]
        finally:
            await page.close()
            await browser.close()
            await api.dispose()
