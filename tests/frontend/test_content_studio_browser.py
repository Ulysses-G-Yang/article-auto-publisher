"""Content Studio 浏览器布局回归。

该测试需要显式设置 ``ARTICLEOPS_FRONTEND_BASE_URL``，只访问隔离 QA 服务。
它不登录、不创建 DeliveryPlan，也不触发平台操作。
"""

import asyncio
import json
import os
import uuid
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
        platform_response = await api.get("/api/platforms")
        assert platform_response.ok
        platform_catalog = (await platform_response.json())["platforms"]
        expected_disabled = sum(
            platform.get("delivery_enabled") is not True
            for platform in platform_catalog
        )
        await api.dispose()

        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable) if executable else None,
        )
        try:
            for width, height in ((320, 740), (390, 844), (1024, 768)):
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
                assert await platform_rows.count() == len(platform_catalog)
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
                ).count() == expected_disabled
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
        if len(valid_accounts) < 2:
            await api.dispose()
            pytest.skip("隔离 QA 服务没有至少两个 VALID 小黑盒测试账号")

        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable) if executable else None,
        )
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        execute_requests: list[str] = []
        target_put_requests: list[str] = []
        page.on(
            "request",
            lambda request: execute_requests.append(request.url)
            if "/delivery-plans" in request.url
            else None,
        )
        page.on(
            "request",
            lambda request: target_put_requests.append(request.url)
            if request.method == "PUT"
            and urlparse(request.url).path.endswith(f"/{draft_id}/targets")
            else None,
        )
        try:
            await page.goto(f"{BASE_URL}/upload?draft_id={draft_id}", wait_until="networkidle")
            await page.locator("#studio-workspace:not(.d-none)").wait_for()
            assert await page.locator("#toggle-all-accounts").is_disabled()
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

            assert not await page.locator("#toggle-all-accounts").is_disabled()
            await page.locator("#toggle-all-accounts").click()
            await page.locator("#targets-list .target-row").wait_for()
            await page.wait_for_function(
                "count => document.querySelector('#target-count')?.textContent "
                "=== `${count} 个目标`",
                account_count,
            )
            assert len(target_put_requests) == 1
            assert await page.locator("#toggle-all-accounts").text_content() == "取消全选所有账户"
            account_card_box = await xhh_row.locator(".target-account-check").first.bounding_box()
            platform_head_box = await xhh_row.locator(".target-platform-head").bounding_box()
            assert account_card_box and account_card_box["height"] >= 44
            assert platform_head_box and platform_head_box["height"] >= 44

            await page.locator("#toggle-all-accounts").click()
            await page.wait_for_function(
                "() => document.querySelector('#target-count')?.textContent === '0 个目标'"
            )
            assert len(target_put_requests) == 2

            await page.locator("#toggle-all-accounts").click()
            await page.wait_for_function(
                "count => document.querySelector('#target-count')?.textContent "
                "=== `${count} 个目标`",
                account_count,
            )
            assert len(target_put_requests) == 3

            await page.reload(wait_until="networkidle")
            await page.locator("#studio-workspace:not(.d-none)").wait_for()
            assert await page.locator("#target-count").text_content() == f"{account_count} 个目标"
            target_text = await page.locator("#targets-list").text_content()
            assert all(account["display_name"] in target_text for account in valid_accounts)
            assert execute_requests == []

            persisted = await api.get(f"/api/content-drafts/{draft_id}")
            assert persisted.ok
            targets = (await persisted.json())["targets"]
            assert len(targets) == account_count
            assert {target["account_id"] for target in targets} == {
                account["account_id"] for account in valid_accounts
            }
        finally:
            await page.close()
            await browser.close()
            await api.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(not BASE_URL, reason="需要显式隔离 QA URL")
async def test_delayed_patch_cannot_overwrite_draft_opened_by_browser_back() -> None:
    executable = Path(BROWSER_PATH) if BROWSER_PATH else None
    if executable and not executable.is_file():
        pytest.fail(f"Chromium 不存在: {executable}")

    patch_started = asyncio.Event()
    release_patch = asyncio.Event()

    def draft_payload(draft_id: str, title: str, revision: int) -> dict:
        return {
            "draft_id": draft_id,
            "source_type": "BLANK",
            "source_ref": None,
            "title": title,
            "content_schema_version": 1,
            "document": None,
            "blocks": [{
                "block_id": f"{draft_id}-text",
                "type": "text",
                "text": f"{title}正文",
                "position": 0,
            }],
            "cover": {"strategy": "NONE", "asset_id": None},
            "status": "ACTIVE",
            "revision": revision,
            "targets": [],
            "created_at": "2026-08-25T00:00:00Z",
            "updated_at": "2026-08-25T00:00:00Z",
        }

    draft_a = draft_payload("qa-patch-a", "草稿 A", 3)
    draft_b = draft_payload("qa-patch-b", "草稿 B", 7)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable) if executable else None,
        )
        page = await browser.new_page(viewport={"width": 1024, "height": 768})

        async def platforms_route(route) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"platforms": []}),
            )

        async def list_route(route) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"drafts": [draft_a, draft_b]}, ensure_ascii=False),
            )

        async def draft_a_route(route, request) -> None:
            if request.method == "PATCH":
                patch_started.set()
                await release_patch.wait()
                response = {
                    **draft_a,
                    "title": "草稿 A 已保存",
                    "source_type": "DOCX",
                    "revision": 99,
                }
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(response, ensure_ascii=False),
                )
                return
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(draft_a, ensure_ascii=False),
            )

        async def draft_b_route(route) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(draft_b, ensure_ascii=False),
            )

        await page.route("**/api/platforms", platforms_route)
        await page.route("**/api/content-drafts?*", list_route)
        await page.route("**/api/content-drafts/qa-patch-a", draft_a_route)
        await page.route("**/api/content-drafts/qa-patch-b", draft_b_route)
        try:
            await page.goto(
                f"{BASE_URL}/upload?draft_id={draft_b['draft_id']}",
                wait_until="networkidle",
            )
            await page.locator("#open-source-library").click()
            item_a = page.locator(".library-item").filter(has_text="草稿 A")
            await item_a.get_by_role("button", name="继续编辑").click()
            await page.wait_for_function(
                "draftId => new URL(location.href).searchParams.get('draft_id') === draftId",
                arg=draft_a["draft_id"],
            )
            await page.locator("#source-library-modal:not(.show)").wait_for()
            await page.locator(".modal-backdrop").wait_for(state="detached")

            await page.locator("#draft-title").fill("草稿 A 修改中")
            await page.locator("#save-draft-now:not(:disabled)").click()
            await asyncio.wait_for(patch_started.wait(), timeout=5)

            await page.evaluate("history.back()")
            await page.wait_for_function(
                "draftId => new URL(location.href).searchParams.get('draft_id') === draftId",
                arg=draft_b["draft_id"],
            )
            await page.wait_for_function(
                "title => document.querySelector('#draft-title')?.value === title",
                arg=draft_b["title"],
            )
            source_before = await page.locator("#side-draft-source").text_content()
            release_patch.set()
            await page.wait_for_timeout(150)

            assert await page.locator("#draft-title").input_value() == draft_b["title"]
            assert await page.locator("#side-revision").text_content() == "7"
            assert await page.locator("#side-draft-source").text_content() == source_before
            assert await page.locator("#save-indicator-text").text_content() == "已同步"
        finally:
            release_patch.set()
            await page.close()
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not BASE_URL, reason="需要显式隔离 QA URL")
async def test_three_publish_confirmations_advance_exactly_once() -> None:
    executable = Path(BROWSER_PATH) if BROWSER_PATH else None
    if executable and not executable.is_file():
        pytest.fail(f"Chromium 不存在: {executable}")

    draft_id = "qa-confirm-draft"
    plan_id = "qa-confirm-plan"
    account_ids = ["account-a", "account-b", "account-c"]
    account_names = ["确认账号 A", "确认账号 B", "确认账号 C"]
    execute_payloads: list[dict] = []

    def target_payload(index: int, status: str, *, confirm: bool = False) -> dict:
        target = {
            "target_id": f"target-{index}",
            "platform": "xiaoheihe",
            "account_id": account_ids[index],
            "account_display_name": account_names[index],
            "mode": "PUBLISH",
            "status": status,
            "error_code": None,
            "error_message": None,
            "operation_id": None,
        }
        if confirm:
            target.update({
                "confirmation_required": True,
                "confirmation_token": f"token-{index}",
                "expires_at": None,
            })
        return target

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable) if executable else None,
        )
        page = await browser.new_page(viewport={"width": 1024, "height": 768})

        async def draft_route(route) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "draft_id": draft_id,
                    "source_type": "BLANK",
                    "source_ref": None,
                    "title": "三个公开目标逐条确认",
                    "content_schema_version": 1,
                    "document": None,
                    "blocks": [{
                        "block_id": "qa-confirm-text",
                        "type": "text",
                        "text": "只验证确认队列，不调用任何真实平台。",
                        "position": 0,
                    }],
                    "cover": {"strategy": "NONE", "asset_id": None},
                    "status": "ACTIVE",
                    "revision": 1,
                    "targets": [{
                        "target_id": f"target-{index}",
                        "platform": "xiaoheihe",
                        "account_id": account_id,
                        "account_display_name": account_names[index],
                        "mode": "PUBLISH",
                        "persist_login": False,
                    } for index, account_id in enumerate(account_ids)],
                    "created_at": None,
                    "updated_at": None,
                }, ensure_ascii=False),
            )

        async def platforms_route(route) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"platforms": [{
                    "id": "xiaoheihe",
                    "display_name": "小黑盒",
                    "delivery_enabled": True,
                    "account_enabled": True,
                    "sort_order": 1,
                    "logo_url": "",
                }]}, ensure_ascii=False),
            )

        async def accounts_route(route) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "platform": "xiaoheihe",
                    "accounts": [{
                        "account_id": account_id,
                        "display_name": account_names[index],
                        "masked_platform_user_id": f"***{index}",
                        "session_status": "VALID",
                        "persist_login": False,
                    } for index, account_id in enumerate(account_ids)],
                }, ensure_ascii=False),
            )

        async def plan_route(route) -> None:
            await route.fulfill(
                status=201,
                content_type="application/json",
                body=json.dumps({
                    "plan_id": plan_id,
                    "status": "CREATING",
                    "targets": [
                        target_payload(index, "CREATING") for index in range(3)
                    ],
                }, ensure_ascii=False),
            )

        async def execute_route(route, request) -> None:
            execute_payloads.append(json.loads(request.post_data or "{}"))
            confirmed = max(0, len(execute_payloads) - 1)
            targets = [
                target_payload(
                    index,
                    "QUEUED" if index < confirmed else "CONFIRMATION_REQUIRED",
                    confirm=index >= confirmed,
                )
                for index in range(3)
            ]
            await route.fulfill(
                status=428 if confirmed < 3 else 200,
                content_type="application/json",
                body=json.dumps({
                    "plan_id": plan_id,
                    "status": (
                        "CONFIRMATION_REQUIRED" if confirmed < 3 else "RUNNING"
                    ),
                    "targets": targets,
                }, ensure_ascii=False),
            )

        await page.route(f"**/api/content-drafts/{draft_id}", draft_route)
        await page.route("**/api/platforms", platforms_route)
        await page.route(
            "**/api/platforms/xiaoheihe/accounts?usable=true",
            accounts_route,
        )
        await page.route(
            f"**/api/content-drafts/{draft_id}/delivery-plans",
            plan_route,
        )
        await page.route(
            f"**/api/delivery-plans/{plan_id}/execute",
            execute_route,
        )
        try:
            await page.goto(
                f"{BASE_URL}/upload?draft_id={draft_id}",
                wait_until="networkidle",
            )
            await page.locator("#studio-workspace:not(.d-none)").wait_for()
            await page.locator("#create-plan").click()
            modal = page.locator("#publish-confirm-modal.show")
            await modal.wait_for()
            assert "确认账号 A" in await page.locator(
                "#publish-target-summary"
            ).inner_text()

            for expected_name in account_names[1:]:
                await page.locator("#confirm-publish-target").click()
                await page.wait_for_function(
                    "name => document.querySelector('#publish-target-summary')"
                    "?.textContent.includes(name)",
                    arg=expected_name,
                )
                await modal.wait_for()

            await page.locator("#confirm-publish-target").click()
            await page.locator("#publish-confirm-modal:not(.show)").wait_for()

            confirmation_payloads = execute_payloads[1:]
            assert [payload["target_ids"] for payload in confirmation_payloads] == [
                ["target-0"],
                ["target-1"],
                ["target-2"],
            ]
            assert [
                list(payload["confirmations"]) for payload in confirmation_payloads
            ] == [
                ["target-0"],
                ["target-1"],
                ["target-2"],
            ]
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
                "blocks": [{"type": "text", "text": "只读恢复验证。", "position": 0}],
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
                    arg=expected_id,
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
        run_token = uuid.uuid4().hex[:8]
        first_title = f"QA 历史导航一 {run_token}"
        second_title = f"QA 历史导航二 {run_token}"
        api = await playwright.request.new_context(base_url=BASE_URL)
        first_response = await api.post(
            "/api/content-drafts",
            data={
                "title": first_title,
                "blocks": [{"type": "text", "text": "第一份", "position": 0}],
            },
        )
        second_response = await api.post(
            "/api/content-drafts",
            data={
                "title": second_title,
                "blocks": [{"type": "text", "text": "第二份", "position": 0}],
            },
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
            history_item = page.locator(".library-item").filter(has_text=second_title)
            await history_item.get_by_role("button", name="继续编辑").click()
            await page.wait_for_function(
                "expected => new URL(window.location.href).searchParams.get("
                "'draft_id') === expected",
                arg=second_id,
            )
            assert await page.locator("#draft-title").input_value() == second_title

            await page.evaluate("window.history.back()")
            await page.wait_for_function(
                "expected => new URL(window.location.href).searchParams.get("
                "'draft_id') === expected",
                arg=first_id,
            )
            await page.wait_for_function(
                "expected => document.querySelector('#draft-title')?.value === expected",
                arg=first_title,
            )
        finally:
            await browser.close()
            await api.dispose()
