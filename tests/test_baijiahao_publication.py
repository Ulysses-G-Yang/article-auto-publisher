"""Baijiahao publication receipts: isolated browser pages and databases only."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from playwright.async_api import async_playwright

from account_sessions.account_service import AccountSessionService
from account_sessions.contracts import DeliveryRequest
from account_sessions.database import AccountDatabase
from account_sessions.delivery_service import DeliveryService
from account_sessions.errors import AccountUnavailableError, ConfirmationRequiredError
from account_sessions.models import DeliveryOperation
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from platforms.baijiahao import BaijiahaoPlatform
from platforms.base import PublishResultUnknownError, SelectorError
from tests.test_account_sessions import insert_account, make_profile, sqlite_database_url
from tests.test_delivery_degraded import _FakePlatform, _run_publish_mode


def receipt():
    return {
        "status": "SUBMITTED", "platform_article_id": "123456",
        "verification_evidence": {
            "submit_acknowledged": True,
            "submission_source": "baijiahao_publish_response",
            "submission_scope": "PUBLIC", "submission_article_id": "123456",
        },
    }


@pytest.mark.parametrize("case", ["accepted", "missing_id", "wrong_source", "id_conflict"])
def test_base_preserves_baijiahao_receipt_and_article_id(case):
    platform = _FakePlatform()
    platform.platform_name = "baijiahao"
    platform.save_draft = AsyncMock(return_value="https://example.invalid/draft/123")
    response = receipt()
    if case == "missing_id":
        response.pop("platform_article_id")
    elif case == "wrong_source":
        response["verification_evidence"]["submission_source"] = "zol_publish_response"
    elif case == "id_conflict":
        response["verification_evidence"]["submission_article_id"] = "other"
    platform.publish_now = AsyncMock(return_value=response)
    result = _run_publish_mode(platform, "PUBLISH")
    if case == "accepted":
        assert result["success"] is True
        assert result["status"] == "SUBMITTED"
        assert result["post_url"] == ""
        assert result["platform_article_id"] == "123456"
    else:
        assert result["success"] is False
        assert result["error_code"] == "PUBLISH_RESULT_UNKNOWN"


@pytest.mark.parametrize("case", [
    "accepted", "missing_id", "bad_id", "id_conflict", "missing_ack",
    "wrong_source", "wrong_scope", "partial_images", "bad_receipt_with_url",
])
def test_baijiahao_submission_is_terminal_and_does_not_fabricate_publication(tmp_path, case):
    async def check():
        db = AccountDatabase(sqlite_database_url(tmp_path))
        result = {"success": True, "media_status": "completed", **receipt()}
        if case == "missing_id":
            result.pop("platform_article_id")
        elif case == "bad_id":
            result["platform_article_id"] = True
        elif case == "id_conflict":
            result["verification_evidence"]["submission_article_id"] = "9999"
        elif case == "missing_ack":
            result["verification_evidence"]["submit_acknowledged"] = 1
        elif case in {"wrong_source", "bad_receipt_with_url"}:
            result["verification_evidence"]["submission_source"] = "zol_publish_response"
            if case == "bad_receipt_with_url":
                result["post_url"] = "https://example.invalid/article/123456"
        elif case == "wrong_scope":
            result["verification_evidence"]["submission_scope"] = "SELF_ONLY"
        elif case == "partial_images":
            result.update(media_status="partial", draft_url="https://example.invalid/draft/123")
        platform = SimpleNamespace(
            platform_name="baijiahao", context=None, initialize=AsyncMock(), cleanup=AsyncMock(),
            publish=AsyncMock(return_value=result),
        )
        accounts = AccountSessionService(
            db, seed_legacy_profiles=False, platform_factory=lambda _: platform,
            allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
        )
        accounts.assert_delivery_identity = AsyncMock()
        await accounts.initialize()
        try:
            account = await insert_account(
                db, make_profile(tmp_path, "baijiahao", "test"), platform="baijiahao",
            )
            sink = AsyncMock()
            service = DeliveryService(
                accounts, public_publish_enabled=True, delivery_event_sink=sink,
            )
            req = DeliveryRequest.model_validate({
                "platform": "baijiahao", "account_id": account.account_id, "mode": "PUBLISH",
                "article": {"title": "测试文章标题", "body": "完整测试正文"},
            })
            with pytest.raises(ConfirmationRequiredError) as raised:
                await service.request_delivery(req, LOCAL_WEB_CONTEXT)
            req.confirmation_token = raised.value.token
            op = await service.request_delivery(req, LOCAL_WEB_CONTEXT)
            if case == "accepted":
                out = await service.execute_operation(op["operation_id"], LOCAL_WEB_CONTEXT)
                assert out["status"] == "SUBMITTED"
                assert out["platform_url"] is None
                assert out["platform_article_id"] == "123456"
                assert out["article_mapping_status"] == "NOT_PENDING"
            else:
                with pytest.raises(AccountUnavailableError) as error:
                    await service.execute_operation(op["operation_id"], LOCAL_WEB_CONTEXT)
                assert error.value.error_code == "PUBLISH_RESULT_UNKNOWN"
                async with db.session() as session:
                    stored = await session.get(DeliveryOperation, op["operation_id"])
                    assert stored.status == "RESULT_UNKNOWN"
            await service.execute_operation(op["operation_id"], LOCAL_WEB_CONTEXT)
            assert platform.publish.await_count == 1
            sink.assert_not_awaited()
        finally:
            await db.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("case", [
    "accepted", "async_checkbox", "wrong_draft", "bad_response", "bool_errno", "missing_nid",
    "old_notice", "security_verification", "preview_required", "preview_unknown", "duplicate_wire",
    "wrong_title", "changed_body", "missing_cover", "missing_image", "duplicate_button",
    "duplicate_title", "podcast_locked",
])
def test_native_submission_verifies_current_document_and_never_clicks_twice(case):
    async def check():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                title = "本地完整标题"
                fields = {
                    "type": "news", "article_id": "999" if case == "wrong_draft" else "123",
                    "title": title, "activity_list[3][id]": "ai_tts",
                    "activity_list[3][is_checked]": "0",
                }
                title_html = (
                    '<div class="FeEditorApp-test" contenteditable="true">'
                    + ("其他标题" if case == "wrong_title" else title) + '</div>'
                )
                cover_html = '' if case == "missing_cover" else (
                    '<div class="FeEditorApp-test-coverWrapper">'
                    '<img class="FeEditorApp-test-coverImg" src="/cover.svg"></div>'
                )
                publish_button = '<button onclick="submitArticle()">发布</button>'
                html = (
                    title_html * (2 if case == "duplicate_title" else 1)
                    + '<iframe src="/body" style="height:200px;width:600px"></iframe>'
                    + cover_html
                    + '<label><input type="checkbox" checked '
                    + ('disabled ' if case == "podcast_locked" else '')
                    + ('onclick="event.preventDefault();setTimeout(()=>this.checked=false,300)" '
                       if case == "async_checkbox" else '')
                    + '>自动生成播客</label>'
                    + publish_button * (2 if case == "duplicate_button" else 1)
                    + '<p>发布成功</p><script>window.clicks=0;async function submitArticle(){'
                    'window.clicks++;window.podcast=document.querySelector("input").checked;'
                    + ('' if case == "old_notice" else (
                        "await fetch('/pcui/article/publish?type=news', {method:'POST',body:"
                        'new URLSearchParams(' + json.dumps(fields) + ')});'
                    ))
                    + ("window.secondAttempt=true;await fetch('/pcui/article/publish?type=news', "
                       "{method:'POST',body:"
                       'new URLSearchParams(' + json.dumps(fields) + ')});'
                       if case == "duplicate_wire" else '')
                    + '}</script>'
                )
                body = (
                    '<body class="view news-editor-pc" contenteditable="true"><p>'
                    + ('错误正文' if case == "changed_body" else '完整正文')
                    + '</p><h2>第二节</h2><p><img src="https://baijiahao.baidu.com'
                    + ('/missing.svg' if case == "missing_image" else '/body.svg')
                    + '"></p><p>结尾</p></body>'
                )
                posts = []

                async def serve(route):
                    path = route.request.url.split('baijiahao.baidu.com', 1)[-1]
                    if route.request.method == "POST":
                        posts.append(route.request)
                        await route.fulfill(json={
                            "errno": False if case == "bool_errno" else (
                                10000015 if case == "security_verification"
                                else 9 if case == "bad_response" else 0
                            ),
                            "ret": {} if case == "missing_nid" else {"nid": "123456"},
                        })
                    elif path == "/missing.svg":
                        await route.fulfill(status=404)
                    elif path.endswith('.svg'):
                        await route.fulfill(
                            body='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>',
                            content_type="image/svg+xml",
                        )
                    else:
                        await route.fulfill(
                            body=body if path == "/body" else html,
                            content_type="text/html; charset=utf-8",
                        )

                await page.route("**/*", serve)
                await page.goto("https://baijiahao.baidu.com/builder/rc/edit?type=news&article_id=123")
                p = BaijiahaoPlatform()
                p.page = page
                p._bound_draft_id = "123"
                p._publication_preview_required = (
                    None if case == "preview_unknown" else case == "preview_required"
                )
                p._publication_preview_draft_id = "123"
                p._expected_persisted_blocks = [
                    {"type": "text", "text": "完整正文"},
                    {"type": "heading", "level": 2, "text": "第二节"},
                    {"type": "image"}, {"type": "text", "text": "结尾"},
                ]
                p.PUBLICATION_TIMEOUT_MS = 300 if case == "old_notice" else 2500
                before_click = case in {
                    "wrong_title", "changed_body", "missing_cover", "missing_image",
                    "duplicate_button", "duplicate_title", "podcast_locked",
                    "preview_required", "preview_unknown",
                }
                if case in {"accepted", "async_checkbox", "duplicate_wire"}:
                    actual = await p.publish_now(title)
                    assert actual["status"] == "SUBMITTED"
                    assert actual["platform_article_id"] == "123456"
                else:
                    error = SelectorError if before_click else PublishResultUnknownError
                    with pytest.raises(error):
                        await p.publish_now(title)
                assert await page.evaluate("window.clicks") == int(not before_click)
                if case == "duplicate_wire":
                    await page.wait_for_function("window.secondAttempt === true")
                sent = not before_click and case not in {"old_notice", "wrong_draft"}
                assert len(posts) == int(sent)
                if not before_click:
                    assert await page.evaluate("window.podcast") is False
                    with pytest.raises(PublishResultUnknownError):
                        await p.publish_now(title)
                    assert await page.evaluate("window.clicks") == 1
            finally:
                await browser.close()

    asyncio.run(check())


@pytest.mark.parametrize("case", [
    "accepted", "podcast_on", "missing_podcast", "duplicate_podcast", "duplicate_field",
    "wrong_type", "timer_time", "online_modify", "only_modify_goods", "replace_publish",
    "is_pay", "is_pay_training_camp", "is_pay_subscribe", "is_pay_mvp", "pay_read_type",
    "wrong_encoding",
])
def test_request_binding_rejects_extra_publication_modes_and_podcast(case):
    fields = {
        "type": "news", "article_id": "123", "title": "测试标题",
        "activity_list[4][id]": "ai_tts", "activity_list[4][is_checked]": "0",
    }
    if case == "podcast_on":
        fields["activity_list[4][is_checked]"] = "1"
    elif case == "missing_podcast":
        fields.pop("activity_list[4][id]")
    elif case == "duplicate_podcast":
        fields["activity_list[7][id]"] = "ai_tts"
        fields["activity_list[7][is_checked]"] = "0"
    elif case == "wrong_type":
        fields["type"] = "video"
    elif case in BaijiahaoPlatform.PUBLICATION_EXTRA_MODES:
        fields[case] = "1"
    raw = urlencode(fields)
    if case == "duplicate_field":
        raw += "&article_id=123"
    request = SimpleNamespace(post_data=raw, headers={
        "content-type": "application/json" if case == "wrong_encoding"
        else "application/x-www-form-urlencoded; charset=UTF-8",
    })
    assert BaijiahaoPlatform._publication_request_matches(
        request, "123", "测试标题",
    ) is (case == "accepted")


@pytest.mark.parametrize("case", [
    "direct", "preview", "missing", "bool_value", "bool_errno", "wrong_host", "duplicate_id",
])
def test_direct_publication_capability_requires_current_native_editor_response(case):
    platform = BaijiahaoPlatform()
    payload = {"errno": False if case == "bool_errno" else 0, "data": {"ability": {
        "is_preview_gray": True if case == "bool_value" else int(case == "preview"),
    }}}
    if case == "missing":
        payload["data"]["ability"] = {}
    host = "other.invalid" if case == "wrong_host" else "baijiahao.baidu.com"
    response = SimpleNamespace(
        url=f"https://{host}/pcui/article/edit?type=news&article_id=123" + (
            "&article_id=123" if case == "duplicate_id" else ""
        ),
        status=200, request=SimpleNamespace(method="GET"), json=AsyncMock(return_value=payload),
    )
    asyncio.run(platform._capture_publication_capability(response))
    if case in {"direct", "preview"}:
        assert platform._publication_preview_draft_id == "123"
        assert platform._publication_preview_required is (case == "preview")
    else:
        assert platform._publication_preview_required is None
        assert platform._publication_preview_draft_id == ""
