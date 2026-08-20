"""Content Studio 浏览器布局回归。

该测试需要显式设置 ``ARTICLEOPS_FRONTEND_BASE_URL``，只访问隔离 QA 服务。
它不登录、不创建 DeliveryPlan，也不触发平台操作。
"""

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
                    lambda message, errors=console_errors: errors.append(
                        {"text": message.text, "location": message.location}
                    ) if message.type == "error" else None,
                )
                await page.goto(
                    f"{BASE_URL}/upload?draft_id={draft_id}",
                    wait_until="networkidle",
                )
                await page.locator("#studio-workspace:not(.d-none)").wait_for()

                platform_rows = page.locator("#target-switcher-list .target-platform-row")
                assert await platform_rows.count() == 10
                assert await page.locator(
                    '.target-platform-row[data-platform-id="xiaoheihe"] '
                    'input[role="switch"]:not(:disabled)'
                ).count() == 1
                assert await page.locator(
                    '.target-platform-row[data-platform-id="zhihu"] '
                    'input[role="switch"]:not(:disabled)'
                ).count() == 1
                assert await page.locator(
                    "#target-switcher-list input[role='switch']:disabled"
                ).count() == 4
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
            xhh_row = page.locator('.target-platform-row[data-platform-id="xiaoheihe"]')
            xhh_switch = xhh_row.locator('input[role="switch"]')
            assert not await xhh_switch.is_checked()
            await xhh_switch.check()
            account_checks = xhh_row.locator('.target-account-check input[type="checkbox"]')
            await account_checks.nth(1).wait_for(state="attached")
            account_count = await account_checks.count()
            assert account_count == len(valid_accounts)
            assert not any(
                [await account_checks.nth(index).is_checked() for index in range(account_count)]
            )

            selected = valid_accounts[0]
            await xhh_row.locator(
                f'input[aria-label="选择账号 {selected["display_name"]}"]'
            ).check()
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


@pytest.mark.asyncio
@pytest.mark.skipif(not BASE_URL, reason="需要显式隔离 QA URL")
async def test_plain_upload_is_blank_and_history_is_lazy_loaded() -> None:
    executable = Path(BROWSER_PATH) if BROWSER_PATH else None
    if executable and not executable.is_file():
        pytest.fail(f"Chromium 不存在: {executable}")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable) if executable else None,
        )
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            draft_requests: list[tuple[str, str]] = []
            page.on(
                "request",
                lambda request: draft_requests.append((request.method, request.url))
                if urlparse(request.url).path.rstrip("/") == "/api/content-drafts"
                else None,
            )
            await page.goto(f"{BASE_URL}/upload", wait_until="networkidle")
            await page.locator("#studio-workspace:not(.d-none)").wait_for()

            assert await page.locator("#draft-title").input_value() == ""
            assert await page.locator("#rich-editor").inner_text() == ""
            assert await page.locator("#cover-none").is_checked()
            assert await page.locator("#target-count").text_content() == "0 个目标"
            assert draft_requests == []

            async with page.expect_request(
                lambda request: request.method == "GET"
                and urlparse(request.url).path.rstrip("/") == "/api/content-drafts"
            ):
                await page.locator("#open-source-library").click()
            await page.locator("#source-library-modal.show").wait_for()
            assert len(draft_requests) == 1
            assert parse_qs(urlparse(draft_requests[0][1]).query) == {
                "limit": ["50"],
                "offset": ["0"],
            }
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not BASE_URL, reason="需要显式隔离 QA URL")
async def test_explicit_draft_restore_and_docx_import_use_distinct_urls() -> None:
    executable = Path(BROWSER_PATH) if BROWSER_PATH else None
    if executable and not executable.is_file():
        pytest.fail(f"Chromium 不存在: {executable}")

    async with async_playwright() as playwright:
        api = await playwright.request.new_context(base_url=BASE_URL)
        draft_response = await api.post(
            "/api/content-drafts",
            data={
                "title": "QA 显式恢复标题",
                "blocks": [{"type": "text", "text": "只读恢复验证。"}],
                "cover": {"strategy": "NONE", "asset_id": None},
            },
        )
        assert draft_response.ok
        draft_id = (await draft_response.json())["draft_id"]

        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable) if executable else None,
        )
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            await page.goto(f"{BASE_URL}/upload?draft_id={draft_id}", wait_until="networkidle")
            await page.locator("#studio-workspace:not(.d-none)").wait_for()
            assert await page.locator("#draft-title").input_value() == "QA 显式恢复标题"
            assert parse_qs(urlparse(page.url).query)["draft_id"] == [draft_id]
            await page.reload(wait_until="networkidle")
            assert await page.locator("#draft-title").input_value() == "QA 显式恢复标题"

            import_count = 0

            async def import_route(route) -> None:
                nonlocal import_count
                import_count += 1
                imported_id = f"qa-import-{import_count}"
                payload = {
                    "draft_id": imported_id,
                    "source_type": "DOCX",
                    "source_ref": f"{imported_id}.docx",
                    "title": f"导入标题 {import_count}",
                    "content_schema_version": 1,
                    "document": None,
                    "blocks": [{
                        "block_id": f"b{import_count}",
                        "type": "text",
                        "text": "导入正文",
                        "asset_id": None,
                        "asset_url": None,
                        "position": 0,
                    }],
                    "cover": {"strategy": "NONE", "asset_id": None, "asset_url": None},
                    "status": "ACTIVE",
                    "revision": 1,
                    "targets": [],
                    "created_at": None,
                    "updated_at": None,
                }
                await route.fulfill(
                    status=201,
                    content_type="application/json",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            await page.route("**/api/content-drafts/import-docx", import_route)
            docx_mime = (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            for expected_id in ("qa-import-1", "qa-import-2"):
                await page.locator("#docx-import").set_input_files(
                    {
                        "name": f"{expected_id}.docx",
                        "mimeType": docx_mime,
                        "buffer": b"qa",
                    }
                )
                await page.wait_for_function(
                    "expected => new URL(window.location.href).searchParams.get("
                    "'draft_id') === expected",
                    expected_id,
                )
                assert await page.locator("#cover-none").is_checked()
                assert await page.locator("#draft-title").input_value() == (
                    f"导入标题 {import_count}"
                )
            assert import_count == 2
            assert parse_qs(urlparse(page.url).query)["draft_id"] == ["qa-import-2"]
        finally:
            await browser.close()
            await api.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(not BASE_URL, reason="需要显式隔离 QA URL")
async def test_history_push_state_back_reload_restores_previous_draft() -> None:
    executable = Path(BROWSER_PATH) if BROWSER_PATH else None
    if executable and not executable.is_file():
        pytest.fail(f"Chromium 不存在: {executable}")

    async with async_playwright() as playwright:
        api = await playwright.request.new_context(base_url=BASE_URL)
        first_response = await api.post(
            "/api/content-drafts",
            data={"title": "QA 历史导航一", "blocks": [{"type": "text", "text": "第一份"}]},
        )
        second_response = await api.post(
            "/api/content-drafts",
            data={"title": "QA 历史导航二", "blocks": [{"type": "text", "text": "第二份"}]},
        )
        assert first_response.ok and second_response.ok
        first_id = (await first_response.json())["draft_id"]
        second_id = (await second_response.json())["draft_id"]

        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable) if executable else None,
        )
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            await page.goto(f"{BASE_URL}/upload?draft_id={first_id}", wait_until="networkidle")
            await page.locator("#studio-workspace:not(.d-none)").wait_for()
            await page.locator("#open-source-library").click()
            await page.locator("#draft-library-list").wait_for()
            history_item = page.locator(".library-item").filter(has_text="QA 历史导航二")
            await history_item.get_by_role("button", name="继续编辑").click()
            await page.wait_for_function(
                "expected => new URL(window.location.href).searchParams.get("
                "'draft_id') === expected",
                second_id,
            )
            assert await page.locator("#draft-title").input_value() == "QA 历史导航二"

            await page.evaluate("window.history.back()")
            await page.wait_for_function(
                "expected => new URL(window.location.href).searchParams.get("
                "'draft_id') === expected",
                first_id,
            )
            await page.wait_for_function(
                "expected => document.querySelector('#draft-title')?.value === expected",
                "QA 历史导航一",
            )
        finally:
            await browser.close()
            await api.dispose()
            await api.dispose()
