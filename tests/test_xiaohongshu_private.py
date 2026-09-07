from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from platforms.xiaohongshu import (
    CREATOR_NOTE_MANAGER,
    CREATOR_PUBLISH,
    XiaohongshuPlatform,
)

CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
NOTE_TITLE = "最终笔记标题"
IDENTITY = {"ok": True, "user_id": "user-1", "display_name": "测试账号"}
LAYOUT_RESULT = {"success": True, "safe_to_continue": True}


@pytest.mark.parametrize(
    "suffix",
    ["", "?xsec_token=synthetic-signature&xsec_source=pc_user", "#synthetic-fragment"],
)
def test_private_view_url_extracts_id_without_signature(suffix: str) -> None:
    note_id = "abcdefabcdefabcdefabcdef"
    url = f"https://www.xiaohongshu.com/explore/{note_id}{suffix}"
    assert XiaohongshuPlatform._private_note_id_from_view_url(url) == note_id


@pytest.mark.parametrize(
    "url",
    [
        "http://www.xiaohongshu.com/explore/111111111111111111111111",
        "https://www.xiaohongshu.com.invalid/explore/111111111111111111111111",
        "https://www.xiaohongshu.com@invalid.test/explore/111111111111111111111111",
        "https://creator.xiaohongshu.com/new/note-manager",
        "https://www.xiaohongshu.com/explore/not-a-note-id",
        "https://www.xiaohongshu.com/explore/111111111111111111111111/edit",
        "/explore/111111111111111111111111",
        "javascript:alert(1)",
        "https://www.xiaohongshu.com/\nexplore/111111111111111111111111",
        "https://[invalid",
    ],
)
def test_private_view_url_rejects_unobserved_routes(url: str) -> None:
    assert XiaohongshuPlatform._private_note_id_from_view_url(url) is None


def _editor_html(
    *,
    visibility: str | None = "公开可见",
    readback: bool = True,
    final_delay_ms: int = 0,
    option_delay_ms: int = 0,
) -> str:
    trigger = (
        f'<button id="visibility-trigger">{visibility}</button>' if visibility else ""
    )
    options = """
      <div id="visibility-options" role="listbox" hidden>
        <div role="option">公开可见</div>
        <div role="option" id="self-only-option">仅自己可见</div>
        <div role="option">仅互关好友可见</div>
        <div role="option">只给谁看</div>
        <div role="option">不给谁看</div>
      </div>
    """ if visibility else ""
    readback_script = (
        "trigger.textContent = '仅自己可见'; options.hidden = true;"
        if readback
        else "trigger.textContent = '公开可见'; options.hidden = true;"
    )
    return f"""
    <style>
      html, body {{ margin: 0; padding: 0; }}
      .editor-cards-container {{ position: relative; width: 760px; min-height: 500px; }}
      .card-outer-container {{ width: 700px; min-height: 100px; }}
      #next, #save {{ position: absolute; width: 100px; height: 40px; }}
      #next {{ left: 10px; top: 150px; }}
      #save {{ left: 150px; top: 10px; }}
      #final-settings {{ padding: 16px; }}
      #visibility-options {{ border: 1px solid #999; padding: 4px; }}
      [role=option] {{ padding: 4px; }}
    </style>
    <div class="editor-cards-container">
      <div class="editor-cards-wrapper">
        <div class="card-outer-container">
          <div class="tiptap ProseMirror">正文</div>
        </div>
      </div>
      <button id="next">下一步</button>
      <button id="save">暂存离开</button>
    </div>
    <section id="final-settings" hidden>
      <button id="more-settings">更多设置</button>
      <input id="note-title" aria-label="最终笔记标题" value="{NOTE_TITLE}">
      <textarea id="description" aria-label="正文描述区">正文描述</textarea>
      {trigger}
      {options}
      <button id="publish">发布</button>
    </section>
    <script>
      const next = document.querySelector('#next');
      const finalSettings = document.querySelector('#final-settings');
      const trigger = document.querySelector('#visibility-trigger');
      const options = document.querySelector('#visibility-options');
      window.nextClicks = 0;
      window.publishClicks = 0;
      next.addEventListener('click', () => {{
        window.nextClicks += 1;
        next.remove();
        window.setTimeout(() => {{ finalSettings.hidden = false; }}, {final_delay_ms});
      }});
      if (trigger) {{
        trigger.addEventListener('click', () => {{
          window.setTimeout(() => {{ options.hidden = false; }}, {option_delay_ms});
        }});
        const selfOnly = document.querySelector('#self-only-option');
        if (selfOnly) selfOnly.addEventListener('click', () => {{
          window.setTimeout(() => {{ {readback_script} }}, {option_delay_ms});
        }});
      }}
      document.querySelector('#publish').addEventListener('click', () => {{
        window.publishClicks += 1;
      }});
    </script>
    """


@asynccontextmanager
async def _local_page(html: str, *, url: str = "about:blank"):
    if not CHROME.is_file():
        pytest.skip("本机未安装隔离测试用 Chrome")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(CHROME),
        )
        try:
            page = await browser.new_page(viewport={"width": 1000, "height": 800})
            if url == "about:blank":
                await page.set_content(html)
            else:
                async def route_local_html(route) -> None:
                    request_url = route.request.url.split("?", 1)[0]
                    if request_url == url:
                        await route.fulfill(
                            status=200,
                            content_type="text/html; charset=utf-8",
                            body=html,
                        )
                    else:
                        await route.abort()

                # 同一 Page/同一 HTTPS origin；除目标已观测 URL 外不允许网络。
                await page.route("**/*", route_local_html)
                await page.goto(url, wait_until="domcontentloaded")
            yield page
        finally:
            await browser.close()


def _ready_editor_platform(page) -> XiaohongshuPlatform:
    platform = XiaohongshuPlatform()
    platform.page = page
    platform._layout_finalized = True
    platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
    platform._layout_expected_image_count = 0
    platform._identity_payload = dict(IDENTITY)
    platform.simulator.random_delay = AsyncMock()
    return platform


async def _prepare(
    platform: XiaohongshuPlatform,
    *,
    baseline: tuple[str, ...] = ("old-id",),
) -> dict:
    return await platform.prepare_private_visibility(
        note_title=NOTE_TITLE,
        identity_snapshot=IDENTITY,
        layout_result=LAYOUT_RESULT,
        baseline_entity_ids=baseline,
    )


@pytest.mark.asyncio
async def test_private_prepare_public_selection_requires_self_only_readback() -> None:
    async with _local_page(_editor_html(), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)

        result = await _prepare(platform)

        assert result["success"] is True
        assert result["visibility"] == "SELF_ONLY"
        assert await page.locator("#visibility-trigger").inner_text() == "仅自己可见"
        assert await page.locator("#visibility-options").is_hidden()
        assert await page.evaluate("() => window.nextClicks") == 1


@pytest.mark.asyncio
async def test_private_prepare_unselected_visibility_is_unknown_without_publish() -> None:
    async with _local_page(_editor_html(visibility=None), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)

        result = await _prepare(platform)

        assert result["success"] is False
        assert result["status"] == "RESULT_UNKNOWN"
        assert result["error_code"] == "PUBLISH_RESULT_UNKNOWN"
        assert "SETTINGS" in result["detail_code"] or "VISIBILITY" in result["detail_code"]
        assert await page.evaluate("() => window.nextClicks") == 1
        assert await page.evaluate("() => window.publishClicks") == 0


@pytest.mark.asyncio
async def test_private_prepare_ambiguous_public_controls_fails_closed() -> None:
    html = _editor_html() + '<button id="duplicate-public">公开可见</button>'
    async with _local_page(html, url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)

        result = await _prepare(platform)

        assert result["success"] is False
        assert result["status"] == "RESULT_UNKNOWN"
        assert await page.locator("#visibility-options").is_hidden()
        assert await page.evaluate("() => window.nextClicks") == 1


@pytest.mark.asyncio
async def test_private_prepare_rejects_bad_visibility_readback_without_reclicking_next() -> None:
    async with _local_page(_editor_html(readback=False), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)

        first = await _prepare(platform)
        second = await _prepare(platform)

        assert first["status"] == "RESULT_UNKNOWN"
        assert second == first
        assert await page.evaluate("() => window.nextClicks") == 1


@pytest.mark.asyncio
async def test_private_prepare_serializes_concurrent_next_step_attempts() -> None:
    async with _local_page(_editor_html(), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)

        results = await asyncio.gather(_prepare(platform), _prepare(platform))

        assert results[0] == results[1]
        assert results[0]["success"] is True
        assert await page.evaluate("() => window.nextClicks") == 1


@pytest.mark.asyncio
async def test_private_prepare_requires_cached_identity_and_waits_for_delayed_dom() -> None:
    async with _local_page(
        _editor_html(final_delay_ms=150, option_delay_ms=150),
        url=CREATOR_PUBLISH,
    ) as page:
        platform = _ready_editor_platform(page)
        platform._identity_payload = None

        missing_identity = await _prepare(platform)

        assert missing_identity["status"] == "RESULT_UNKNOWN"
        assert missing_identity["detail_code"] == "XHS_PRIVATE_IDENTITY_CACHE_MISSING"
        assert await page.evaluate("() => window.nextClicks") == 0

    async with _local_page(
        _editor_html(final_delay_ms=150, option_delay_ms=150),
        url=CREATOR_PUBLISH,
    ) as page:
        platform = _ready_editor_platform(page)

        prepared = await _prepare(platform)

        assert prepared["success"] is True
        assert await page.locator("#visibility-trigger").inner_text() == "仅自己可见"


@pytest.mark.asyncio
async def test_private_prepare_stops_before_next_for_empty_or_unverified_layout() -> None:
    async with _local_page(_editor_html(), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)
        platform._expected_persisted_blocks = []

        empty = await _prepare(platform)

        assert empty["error_code"] == "XHS_PRIVATE_CONTENT_EMPTY"
        assert await page.evaluate("() => window.nextClicks") == 0

        platform._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
        platform._layout_finalized = False
        unverified = await _prepare(platform)

        assert unverified["error_code"] == "XHS_PRIVATE_LAYOUT_NOT_VERIFIED"
        assert await page.evaluate("() => window.nextClicks") == 0


@pytest.mark.asyncio
async def test_private_publish_requires_confirmation_and_exact_bindings() -> None:
    async with _local_page(_editor_html(), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)
        prepared = await _prepare(platform)
        assert prepared["success"] is True

        not_confirmed = await platform.publish_private(
            confirmed=False,
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            page_token=page,
        )
        title_mismatch = await platform.publish_private(
            confirmed=True,
            note_title="另一个标题",
            identity_snapshot=IDENTITY,
            page_token=page,
        )
        identity_mismatch = await platform.publish_private(
            confirmed=True,
            note_title=NOTE_TITLE,
            identity_snapshot={**IDENTITY, "user_id": "other-user"},
            page_token=page,
        )
        page_mismatch = await platform.publish_private(
            confirmed=True,
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            page_token=object(),
        )

        assert not_confirmed["error_code"] == "XHS_PRIVATE_CONFIRMATION_REQUIRED"
        assert title_mismatch["status"] == "RESULT_UNKNOWN"
        assert title_mismatch["error_code"] == "PUBLISH_RESULT_UNKNOWN"
        assert title_mismatch["detail_code"] == "XHS_PRIVATE_NOTE_TITLE_MISMATCH"
        assert identity_mismatch["status"] == "RESULT_UNKNOWN"
        assert page_mismatch["status"] == "RESULT_UNKNOWN"
        assert await page.evaluate("() => window.publishClicks") == 0


@pytest.mark.asyncio
async def test_private_publish_rechecks_page_identity_and_title_before_click() -> None:
    async with _local_page(_editor_html(), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)
        assert (await _prepare(platform))["success"] is True

        await page.locator("#note-title").fill("页面中被改过的标题")
        changed_title = await platform.publish_private(
            confirmed=True,
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            page_token=page,
        )
        assert changed_title["detail_code"] == "XHS_PRIVATE_NOTE_TITLE_READBACK_CHANGED"
        assert await page.evaluate("() => window.publishClicks") == 0

    async with _local_page(_editor_html(), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)
        assert (await _prepare(platform))["success"] is True
        platform._identity_payload = {**IDENTITY, "user_id": "other-user"}
        identity_changed = await platform.publish_private(
            confirmed=True,
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            page_token=page,
        )
        assert identity_changed["detail_code"] == "XHS_PRIVATE_IDENTITY_MISMATCH"
        assert await page.evaluate("() => window.publishClicks") == 0

    async with _local_page(_editor_html(), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)
        assert (await _prepare(platform))["success"] is True
        await page.evaluate("() => history.replaceState({}, '', '/not-the-publish-page')")
        wrong_url = await platform.publish_private(
            confirmed=True,
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            page_token=page,
        )
        assert wrong_url["detail_code"] == "XHS_PRIVATE_PUBLISH_URL_UNVERIFIED"
        assert await page.evaluate("() => window.publishClicks") == 0


@pytest.mark.asyncio
async def test_private_publish_clicks_once_even_with_async_concurrency() -> None:
    async with _local_page(_editor_html(), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)
        assert (await _prepare(platform))["success"] is True

        results = await asyncio.gather(
            *(
                platform.publish_private(
                    confirmed=True,
                    note_title=NOTE_TITLE,
                    identity_snapshot=IDENTITY,
                    page_token=page,
                )
                for _ in range(2)
            )
        )

        assert await page.evaluate("() => window.publishClicks") == 1
        assert all(result["status"] == "RESULT_UNKNOWN" for result in results)
        assert all(result["submitted"] is True for result in results)
        assert results[0] == results[1]


@pytest.mark.asyncio
async def test_private_publish_exception_locks_attempt_and_forbids_retry() -> None:
    async with _local_page(_editor_html(), url=CREATOR_PUBLISH) as page:
        platform = _ready_editor_platform(page)
        assert (await _prepare(platform))["success"] is True
        action = SimpleNamespace(click=AsyncMock(side_effect=TimeoutError("timeout")))
        platform._private_selected_visibility_confirmed = AsyncMock(return_value=True)
        platform._unique_visible_exact_button = AsyncMock(return_value=action)
        platform._private_scroll_and_check = AsyncMock(return_value=True)

        first = await platform.publish_private(
            confirmed=True,
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            page_token=page,
        )
        second = await platform.publish_private(
            confirmed=True,
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            page_token=page,
        )

        assert first["status"] == "RESULT_UNKNOWN"
        assert second == first
        action.click.assert_awaited_once_with(timeout=10000)


def _bound_manager_platform(
    page, *, baseline: tuple[str, ...] | None = ("old-id",)
) -> XiaohongshuPlatform:
    platform = XiaohongshuPlatform()
    platform.page = page
    platform._private_page_binding = page
    platform._private_title_binding = NOTE_TITLE
    platform._private_identity_binding = ("user-1", "测试账号")
    platform._identity_payload = dict(IDENTITY)
    platform._private_baseline_entity_ids = baseline
    platform._private_visibility_prepared = True
    platform._private_publish_attempted = True
    platform._private_publish_result = {
        "success": False,
        "status": "RESULT_UNKNOWN",
        "error_code": "PUBLISH_RESULT_UNKNOWN",
        "detail_code": "XHS_PRIVATE_SUBMIT_CLICKED_PENDING_MANAGER_VERIFICATION",
    }
    return platform


async def _verify_manager(
    platform: XiaohongshuPlatform,
    page,
    *,
    entity_id: str = "new-id",
    baseline: tuple[str, ...] = ("old-id",),
    card_selector: str = "#new-card",
    entity_selector: str = "#new-card .entity-id",
    entity_attribute: str | None = None,
    status_selector: str | None = "#new-card .status",
) -> dict:
    return await platform.verify_private_result(
        note_title=NOTE_TITLE,
        identity_snapshot=IDENTITY,
        entity_id=entity_id,
        baseline_entity_ids=baseline,
        card_locator=page.locator(card_selector),
        entity_id_locator=page.locator(entity_selector),
        entity_id_attribute=entity_attribute,
        status_locator=page.locator(status_selector) if status_selector else None,
        page_token=platform.page,
    )


def _manager_html(
    *,
    title: str = NOTE_TITLE,
    visibility: str = "仅自己可见",
    status: str | None = "审核中",
    card_id: str = "new-card",
    entity_id: str = "new-id",
) -> str:
    status_markup = f'<span class="status">{status}</span>' if status is not None else ""
    return f"""
    <article id="{card_id}" class="manager-card">
      <h3>{title}</h3>
      <span class="visibility">{visibility}</span>
      <span class="entity-id" data-note-id="{entity_id}">{entity_id}</span>
      {status_markup}
    </article>
    """


@pytest.mark.asyncio
async def test_private_manager_pending_is_submitted_not_failed() -> None:
    async with _local_page(_manager_html(), url=CREATOR_NOTE_MANAGER) as page:
        platform = _bound_manager_platform(page)

        result = await _verify_manager(platform, page)

        assert result["success"] is True
        assert result["status"] == "submitted"
        assert result["result_status"] == "审核中"


@pytest.mark.asyncio
async def test_private_manager_unobserved_success_and_rejected_labels_stay_unknown() -> None:
    for status in ("发布成功", "未通过"):
        async with _local_page(_manager_html(status=status), url=CREATOR_NOTE_MANAGER) as page:
            platform = _bound_manager_platform(page)
            result = await _verify_manager(platform, page)

            assert result["success"] is False
            assert result["status"] == "RESULT_UNKNOWN"
            assert result["error_code"] == "PUBLISH_RESULT_UNKNOWN"
            assert result["detail_code"] == "XHS_PRIVATE_ENTITY_STATUS_UNRECOGNIZED"


@pytest.mark.asyncio
async def test_private_manager_requires_new_entity_and_scopes_private_label_to_card() -> None:
    html = _manager_html() + _manager_html(
        card_id="neighbor",
        entity_id="neighbor-id",
        status="邻居状态",
    )
    async with _local_page(html, url=CREATOR_NOTE_MANAGER) as page:
        platform = _bound_manager_platform(page)

        same_old_id = await _verify_manager(platform, page, entity_id="old-id")
        neighbor_only = await _verify_manager(
            platform,
            page,
            entity_id="neighbor-id",
            card_selector="#neighbor",
            entity_selector="#neighbor .entity-id",
            status_selector="#new-card .status",
        )

        assert same_old_id["status"] == "RESULT_UNKNOWN"
        assert same_old_id["detail_code"] == "XHS_PRIVATE_ENTITY_NOT_NEW"
        assert neighbor_only["status"] == "RESULT_UNKNOWN"
        assert neighbor_only["detail_code"] == "XHS_PRIVATE_ENTITY_STATUS_NOT_DESCENDANT"


@pytest.mark.asyncio
async def test_private_manager_rejects_wrong_title_duplicates_and_unknown_status() -> None:
    async with _local_page(_manager_html(title="另一篇文章"), url=CREATOR_NOTE_MANAGER) as page:
        platform = _bound_manager_platform(page)
        wrong_title = await _verify_manager(platform, page)
        assert wrong_title["status"] == "RESULT_UNKNOWN"

    async with _local_page(
        _manager_html() + _manager_html(card_id="second-card", entity_id="second-id"),
        url=CREATOR_NOTE_MANAGER,
    ) as page:
        platform = _bound_manager_platform(page)
        duplicate = await _verify_manager(platform, page, card_selector="article.manager-card")
        assert duplicate["status"] == "RESULT_UNKNOWN"

    async with _local_page(_manager_html(status="审核徽章消失"), url=CREATOR_NOTE_MANAGER) as page:
        platform = _bound_manager_platform(page)
        unknown_status = await _verify_manager(platform, page)
        assert unknown_status["status"] == "RESULT_UNKNOWN"

    async with _local_page(_manager_html(status=None), url=CREATOR_NOTE_MANAGER) as page:
        platform = _bound_manager_platform(page)
        missing_status = await _verify_manager(platform, page)
        assert missing_status["success"] is True
        assert missing_status["status"] == "submitted"
        assert missing_status["result_status"] == "审核状态待确认"
        assert missing_status["status_verified"] is False
        omitted_status = await _verify_manager(platform, page, status_selector=None)
        assert omitted_status == missing_status


@pytest.mark.asyncio
async def test_private_manager_missing_baseline_or_card_never_guesses_result() -> None:
    async with _local_page(_manager_html(), url=CREATOR_NOTE_MANAGER) as page:
        platform = _bound_manager_platform(page)

        no_frozen_baseline = _bound_manager_platform(page, baseline=None)
        missing_baseline = await no_frozen_baseline.verify_private_result(
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            entity_id="new-id",
            baseline_entity_ids=None,
            card_locator=page.locator("#new-card"),
            entity_id_locator=page.locator("#new-card .entity-id"),
            status_locator=page.locator("#new-card .status"),
            page_token=no_frozen_baseline.page,
        )
        missing_card = await platform.verify_private_result(
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            entity_id="new-id",
            baseline_entity_ids=("old-id",),
            card_locator=None,
            entity_id_locator=None,
            status_locator=page.locator("#new-card .status"),
            page_token=platform.page,
        )

        assert missing_baseline["status"] == "RESULT_UNKNOWN"
        assert missing_baseline["detail_code"] == "XHS_PRIVATE_ENTITY_BASELINE_MISMATCH"
        assert missing_card["status"] == "RESULT_UNKNOWN"


@pytest.mark.asyncio
async def test_private_manager_reads_actual_entity_id_and_rejects_mismatch() -> None:
    async with _local_page(_manager_html(), url=CREATOR_NOTE_MANAGER) as page:
        platform = _bound_manager_platform(page)

        readback_mismatch = await _verify_manager(platform, page, entity_id="not-new-id")
        assert readback_mismatch["detail_code"] == "XHS_PRIVATE_ENTITY_ID_READBACK_MISMATCH"

        attr_readback = await _verify_manager(
            platform,
            page,
            entity_id="new-id",
            entity_attribute="data-note-id",
        )
        assert attr_readback["success"] is True
        assert attr_readback["result_status"] == "审核中"


@pytest.mark.asyncio
async def test_private_manager_verification_cache_prevents_later_publish_reclick() -> None:
    async with _local_page(_manager_html(), url=CREATOR_NOTE_MANAGER) as page:
        platform = _bound_manager_platform(page)
        verified = await _verify_manager(platform, page)
        replay = await platform.publish_private(
            confirmed=True,
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            page_token=page,
        )

        assert verified["status"] == "submitted"
        assert replay == verified


@pytest.mark.asyncio
async def test_private_manager_rejects_same_page_iframe_locators() -> None:
    html = """
    <iframe id="manager-frame" srcdoc='
      <article id="new-card" class="manager-card">
        <h3>最终笔记标题</h3>
        <span class="visibility">仅自己可见</span>
        <span class="entity-id">new-id</span>
        <span class="status">审核中</span>
      </article>'></iframe>
    """
    async with _local_page(html, url=CREATOR_NOTE_MANAGER) as page:
        platform = _bound_manager_platform(page)
        frame = page.frame_locator("#manager-frame")

        result = await platform.verify_private_result(
            note_title=NOTE_TITLE,
            identity_snapshot=IDENTITY,
            entity_id="new-id",
            card_locator=frame.locator("#new-card"),
            entity_id_locator=frame.locator(".entity-id"),
            status_locator=frame.locator(".status"),
            page_token=page,
        )

        assert result["status"] == "RESULT_UNKNOWN"
        assert result["detail_code"] == "XHS_PRIVATE_ENTITY_CARD_NOT_UNIQUE"


@pytest.mark.asyncio
@pytest.mark.parametrize("link_id", ["111111111111111111111111", "222222222222222222222222"])
async def test_private_manager_binds_actual_card_href_without_retaining_signature(link_id: str):
    note_id = "111111111111111111111111"
    signature = "synthetic-do-not-retain"
    view_url = f"https://www.xiaohongshu.com/explore/{link_id}?xsec_token={signature}"
    html = _manager_html(entity_id=note_id).replace(
        f'<span class="entity-id" data-note-id="{note_id}">{note_id}</span>',
        f'<a class="entity-id" href="{view_url}">查看</a>',
    )
    async with _local_page(html, url=CREATOR_NOTE_MANAGER) as page:
        platform = _bound_manager_platform(page)
        result = await _verify_manager(
            platform, page, entity_id=note_id, entity_attribute="href"
        )
        assert signature not in str(result)
        assert "xsec_token" not in str(result)
        if link_id == note_id:
            assert result["status"] == "submitted"
            assert result["entity_bound"] is True
        else:
            assert result["status"] == "RESULT_UNKNOWN"
            assert result["detail_code"] == "XHS_PRIVATE_ENTITY_ID_READBACK_MISMATCH"


@pytest.mark.asyncio
async def test_private_reader_link_does_not_replace_manager_evidence() -> None:
    note_id = "111111111111111111111111"
    view_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    async with _local_page(_manager_html(entity_id=note_id), url=view_url) as page:
        platform = _bound_manager_platform(page)
        result = await _verify_manager(platform, page, entity_id=note_id)
        assert result["status"] == "RESULT_UNKNOWN"
        assert result["detail_code"] == "XHS_PRIVATE_MANAGER_URL_UNVERIFIED"
