"""ZOL public submission checks use local HTML and isolated databases only."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from account_sessions.account_service import AccountSessionService
from account_sessions.contracts import DeliveryRequest
from account_sessions.database import AccountDatabase
from account_sessions.delivery_service import DeliveryService
from account_sessions.errors import AccountUnavailableError, ConfirmationRequiredError
from account_sessions.models import DeliveryOperation
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from platforms.base import SelectorError
from platforms.zol import ZOLPlatform, ZOLPublishUnknownError
from tests.test_account_sessions import insert_account, make_profile, sqlite_database_url
from tests.test_delivery_degraded import _FakePlatform, _run_publish_mode


def receipt():
    return {
        "status": "SUBMITTED",
        "verification_evidence": {
            "submit_acknowledged": True, "submission_source": "zol_publish_response",
            "submission_scope": "PUBLIC",
        },
    }


@pytest.mark.parametrize("case", [
    "same", "changed", "missing", "swapped", "old_unchanged", "old_reordered", "partially_replaced",
])
def test_native_cover_confirmation_and_cropped_variants_survive_reopen(tmp_path, case):
    async def check():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                html = '''<div class="ant-form-item"><label title="导读图">导读图</label>
                  <input type="file" accept="image/*"
                    onchange="document.querySelector('.ant-modal-wrap').style.display='block'">
                  <div id="previews" role="uploader"></div></div>
                  <div class="ant-modal-wrap" style="display:none">导读图裁剪
                  <button onclick="window.confirmed=(window.confirmed||0)+1;
                    document.getElementById('previews').innerHTML=
                    [1,2].map(i=>'&lt;span class=uploader__pic-item&gt;'
                    +'&lt;img class=flex-pic__img src=/cover-'+i+'.svg&gt;&lt;/span&gt;').join('');
                    this.parentElement.style.display='none'">确 定</button></div>'''

                def preview_items(order):
                    return ''.join(
                        f'<span class="uploader__pic-item"><img class="flex-pic__img" '
                        f'src="/cover-{i}.svg"></span>' for i in order
                    )

                previews = preview_items([1, 2])
                old_cases = {"old_unchanged", "old_reordered", "partially_replaced"}
                state = {"previews": previews if case in old_cases else ""}
                if case in old_cases:
                    new_order = {
                        "old_unchanged": [1, 2], "old_reordered": [2, 1],
                        "partially_replaced": [3, 2],
                    }[case]
                    html = html.replace('[1,2].map', json.dumps(new_order) + '.map')

                async def serve(route):
                    if "/cover-" in route.request.url:
                        await route.fulfill(
                            body=('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                                  + f'<desc>{route.request.url.rsplit("/", 1)[-1]}</desc></svg>'),
                            content_type="image/svg+xml",
                        )
                    else:
                        await route.fulfill(
                            body=html.replace(
                                '<div id="previews" role="uploader"></div>',
                                '<div id="previews" role="uploader">'
                                + state["previews"] + '</div>',
                            ),
                            content_type="text/html; charset=utf-8",
                        )

                await page.route("**/*", serve)
                await page.goto(ZOLPlatform._draft_editor_url("123"))
                source = tmp_path / "cover.png"
                source.write_bytes(b"local-upload-fixture")
                p = ZOLPlatform()
                async def fetch_crop(url, **_kwargs):
                    return SimpleNamespace(
                        ok=True, body=AsyncMock(return_value=url.rsplit("/", 1)[-1].encode()),
                    )

                p.page = page
                # APIRequestContext is not intercepted by page.route; keep its reads local too.
                p.context = SimpleNamespace(request=SimpleNamespace(get=fetch_crop))
                result = await p.apply_cover({
                    "strategy": "FIRST_BODY_IMAGE", "local_path": str(source),
                })
                if case in old_cases:
                    assert result["success"] is False
                    assert p._pending_cover_digests == ()
                    return
                assert result["success"] is True, result
                assert await page.evaluate("window.confirmed") == 1
                assert len(p._pending_cover_digests) == 2
                order = {"same": [1, 2], "changed": [1, 3],
                         "missing": [1], "swapped": [2, 1]}[case]
                state["previews"] = preview_items(order)
                await page.goto(ZOLPlatform._draft_editor_url("123"))
                await page.wait_for_load_state("load")
                verified = await p.verify_persisted_cover(
                    title="测试文章标题", draft_url=p.page.url, cover=None, apply_result=result,
                )
                assert verified["success"] is (case == "same")
                assert verified["cover_status"] == ("completed" if case == "same" else "failed")
            finally:
                await browser.close()

    asyncio.run(check())


@pytest.mark.parametrize("case", [
    "success", "wrong_draft", "rejected", "bool_code", "missing_cover", "duplicate", "old_message",
    "multipart", "multipart_wrong_draft", "multipart_duplicate",
])
def test_zol_submission_requires_current_request_and_clicks_once(case):
    async def check():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                image = '' if case == "missing_cover" else (
                    '<img class="flex-pic__img" src="data:image/svg+xml,%3Csvg '
                    'xmlns=\'http://www.w3.org/2000/svg\' '
                    'width=\'10\' height=\'10\'%3E%3C/svg%3E">'
                )
                button = '<div class="foot-item" onclick="submit()">发布</div>'
                cover_items = ('<span class="uploader__pic-item">' + image + '</span>') * 2
                draft = "other" if case in {"wrong_draft", "multipart_wrong_draft"} else "123"
                fields = json.dumps({
                    "draftUpdateId": draft, "title": "测试文章标题", "saveType": "1",
                })
                payload = "new URLSearchParams(" + fields + ")"
                if case.startswith("multipart"):
                    payload = (
                        "(()=>{const data=new FormData();Object.entries(" + fields
                        + ").forEach(([k,v])=>data.append(k,v));"
                        + ("data.append('draftUpdateId','other');"
                           if case == "multipart_duplicate" else "")
                        + "return data;})()"
                    )
                html = (
                    '<input placeholder="请输入文章标题" value="测试文章标题">'
                    '<span class="ant-tag">电脑外设</span><div class="ant-form-item">'
                    '<label title="导读图">导读图</label><div role="uploader">'
                    + cover_items + '</div></div>'
                    + button * (2 if case == "duplicate" else 1)
                    + '<div>提交成功</div><script>window.clicks=0;async function submit(){'
                    'window.clicks++;'
                    + ('' if case == "old_message" else (
                        "await fetch('https://open-api.zol.com.cn"
                        + ZOLPlatform.PUBLICATION_RESPONSE_PATH
                        + "',{method:'POST',body:" + payload + "});"
                    )) + '}</script>'
                )
                post_count = 0

                async def serve(route):
                    nonlocal post_count
                    if route.request.method == "OPTIONS":
                        await route.fulfill(headers={"Access-Control-Allow-Origin": "*"})
                    elif route.request.method == "POST":
                        post_count += 1
                        code = 500 if case == "rejected" else False if case == "bool_code" else 0
                        await route.fulfill(
                            json={"errcode": code, "data": {}},
                            headers={"Access-Control-Allow-Origin": "*"},
                        )
                    else:
                        await route.fulfill(body=html, content_type="text/html; charset=utf-8")

                await page.route("**/*", serve)
                await page.goto(ZOLPlatform._draft_editor_url("123"))
                p = ZOLPlatform()
                p.page = page
                p._bound_draft_id = "123"
                p._publication_topic = "电脑外设"
                p._expected_persisted_blocks = [{"type": "text", "text": "正文"}]
                p._resolve_content_editor = AsyncMock(return_value=(None, "fixture"))
                p._read_editor_dom_tokens = AsyncMock(return_value=p._expected_content_tokens(
                    p._expected_persisted_blocks, [],
                ))
                p.PUBLICATION_TIMEOUT_MS = 200 if case == "old_message" else 2000
                if case in {"success", "multipart"}:
                    assert await p.publish_now("测试文章标题") == receipt()
                else:
                    error = SelectorError if case in {"missing_cover", "duplicate"} else (
                        ZOLPublishUnknownError
                    )
                    with pytest.raises(error):
                        await p.publish_now("测试文章标题")
                clicked = case not in {"missing_cover", "duplicate"}
                assert await page.evaluate("window.clicks") == int(clicked)
                assert post_count == int(clicked and case != "old_message")
                if clicked:
                    with pytest.raises(ZOLPublishUnknownError):
                        await p.publish_now("测试文章标题")
                    assert await page.evaluate("window.clicks") == 1
            finally:
                await browser.close()

    asyncio.run(check())


@pytest.mark.parametrize("case", [
    "missing_boundary", "duplicate", "file", "update", "invalid_utf8",
    "encoded_field", "duplicate_disposition",
])
def test_multipart_submission_rejects_unverifiable_fields(case):
    fields = [("draftUpdateId", "123"), ("title", "测试文章标题"), ("saveType", "1")]
    if case == "duplicate":
        fields.append(("draftUpdateId", "123"))
    if case == "update":
        fields.append(("isUpdate", "1"))
    parts = [
        f'--boundary\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        for key, value in fields
    ]
    if case == "file":
        parts.append(
            '--boundary\r\nContent-Disposition: form-data; name="file"; filename="a.png"'
            '\r\n\r\nfile data\r\n'
        )
    if case == "invalid_utf8":
        parts.append(
            '--boundary\r\nContent-Disposition: form-data; name="extra"'
            '\r\nContent-Transfer-Encoding: base64\r\n\r\n/w==\r\n'
        )
    if case == "encoded_field":
        parts[0] = (
            '--boundary\r\nContent-Disposition: form-data; name="draftUpdateId"'
            '\r\nContent-Transfer-Encoding: base64\r\n\r\nMTIz\r\n'
        )
    if case == "duplicate_disposition":
        parts[0] = (
            '--boundary\r\nContent-Disposition: form-data; name="draftUpdateId"'
            '\r\nContent-Disposition: form-data; name="other"\r\n\r\n123\r\n'
        )
    request = SimpleNamespace(
        post_data="".join(parts) + "--boundary--\r\n",
        headers={"content-type": "multipart/form-data" + (
            "" if case == "missing_boundary" else "; boundary=boundary"
        )},
    )
    assert ZOLPlatform._publication_request_matches(request, "123", "测试文章标题") is False


def test_base_preserves_public_submission_without_fabricating_url():
    platform = _FakePlatform()
    platform.platform_name = "zol"
    platform.save_draft = AsyncMock(return_value="https://example.invalid/draft/123")
    platform.publish_now = AsyncMock(return_value=receipt())
    result = _run_publish_mode(platform, "PUBLISH")
    assert result["success"] is True
    assert result["status"] == "SUBMITTED"
    assert result["post_url"] == ""
    assert result["verification_evidence"]["submit_acknowledged"] is True


@pytest.mark.parametrize("case", ["accepted", "missing_ack", "wrong_source", "partial_images"])
def test_zol_public_submission_is_terminal_without_url(tmp_path, case):
    async def check():
        db = AccountDatabase(sqlite_database_url(tmp_path))
        result = {"success": True, "media_status": "completed", **receipt()}
        if case == "missing_ack":
            result["verification_evidence"]["submit_acknowledged"] = False
        elif case == "wrong_source":
            result["verification_evidence"]["submission_source"] = "old_message"
        elif case == "partial_images":
            result["media_status"] = "partial"
            result["draft_url"] = "https://example.invalid/draft/123"
        platform = SimpleNamespace(
            platform_name="zol", context=None, initialize=AsyncMock(), cleanup=AsyncMock(),
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
                db, make_profile(tmp_path, "zol", "test"), platform="zol",
            )
            sink = AsyncMock()
            service = DeliveryService(
                accounts, public_publish_enabled=True, delivery_event_sink=sink,
            )
            req = DeliveryRequest.model_validate({
                "platform": "zol", "account_id": account.account_id, "mode": "PUBLISH",
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
                assert out["article_mapping_status"] == "NOT_PENDING"
                await service.execute_operation(op["operation_id"], LOCAL_WEB_CONTEXT)
                assert platform.publish.await_count == 1
                sink.assert_not_awaited()
            else:
                with pytest.raises(AccountUnavailableError) as error:
                    await service.execute_operation(op["operation_id"], LOCAL_WEB_CONTEXT)
                assert error.value.error_code == "PUBLISH_RESULT_UNKNOWN"
                async with db.session() as session:
                    stored = await session.get(DeliveryOperation, op["operation_id"])
                    assert stored.status == "RESULT_UNKNOWN"
        finally:
            await db.dispose()

    asyncio.run(check())
