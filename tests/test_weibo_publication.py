"""Isolated native Weibo save/submit stages; no real account or network."""

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
from account_sessions.delivery_service import DeliveryService, _is_verified_public_submission
from account_sessions.errors import AccountUnavailableError, ConfirmationRequiredError
from account_sessions.models import DeliveryOperation
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from platforms.base import PublishResultUnknownError, SelectorError
from platforms.content_validation import ContentValidationError
from platforms.weibo import WeiboPlatform
from tests.test_account_sessions import insert_account, make_profile, sqlite_database_url
from tests.test_delivery_degraded import _FakePlatform, _run_publish_mode


@pytest.mark.parametrize(
    "case",
    [
        "accepted",
        "wrong_draft",
        "wrong_title",
        "missing_cover",
        "missing_image",
        "restricted",
        "save_error",
        "captcha",
        "publish_error",
        "duplicate_wire",
        "wrong_wire_id",
        "wrong_wire_scope",
        "follow_official",
        "schedule",
        "old_notice",
    ],
)
def test_native_weibo_saves_same_draft_and_submits_at_most_once(case):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                counts = {"save": 0, "publish": 0}
                title = "本地原稿标题"
                save = {
                    "id": "123",
                    "title": title,
                    "action": "2",
                    "content": "<p>完整正文</p>",
                    "free_content": "",
                    "cover": "/cover.svg",
                    "follow_to_read": "0",
                }
                publish = {
                    "id": "999" if case == "wrong_wire_id" else "123",
                    "text": title,
                    "rank": "10" if case == "wrong_wire_scope" else "0",
                    "follow_to_read": "0",
                    "follow_official": "1" if case == "follow_official" else "0",
                    "sync_wb": "0",
                    "is_original": "0",
                    "mpkey": "0",
                    "time": "tomorrow" if case == "schedule" else "",
                    "timestamp": "",
                }
                cover = (
                    ""
                    if case == "missing_cover"
                    else '<div class="cover-preview"><img class="cover-img" src="/cover.svg"></div>'
                )
                picture = (
                    ""
                    if case == "missing_image"
                    else '<figure class="wb-node-image">'
                    '<img class="image-view__body__image" src="/cover.svg"></figure>'
                )
                check = "true" if case == "restricted" else "false"
                html = (
                    '<meta charset="utf-8">'
                    '<textarea placeholder="请输入标题">'
                    + ("别的标题" if case == "wrong_title" else title)
                    + "</textarea>"
                    f'<div class="tiptap ProseMirror"><p>完整正文</p>{picture}</div>{cover}'
                    f'<div><div role="checkbox" aria-checked="{check}"></div>'
                    "<span>仅粉丝阅读全文</span></div>"
                    '<button onclick="next()">下一步</button><div>发布成功</div>'
                    '<div class="publish-modal" style="display:none"><span>公开</span>'
                    f'<textarea>{title}</textarea><button onclick="publish()">发布</button></div>'
                    "<script>"
                    'const send = (path,fields)=>fetch(path,{method:"POST",headers:'
                    '{"Content-Type":"application/x-www-form-urlencoded"},'
                    "body:new URLSearchParams(fields)});"
                    "async function next(){await send("
                    f'"{WeiboPlatform.PUBLICATION_SAVE_PATH}",{json.dumps(save)});'
                    'document.querySelector(".publish-modal").style.display="block";}'
                    f'function publish(){{send("{WeiboPlatform.PUBLICATION_PATH}",'
                    f"{json.dumps(publish)});"
                    + (
                        f'send("{WeiboPlatform.PUBLICATION_PATH}",{json.dumps(publish)});'
                        if case == "duplicate_wire"
                        else ""
                    )
                    + "}</script>"
                )

                async def route_handler(route):
                    request = route.request
                    if request.url.endswith("/cover.svg"):
                        await route.fulfill(
                            content_type="image/svg+xml",
                            body='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
                            '<rect width="20" height="20" fill="red"/></svg>',
                        )
                    elif request.method == "POST":
                        stage = "save" if request.url.endswith("/save") else "publish"
                        counts[stage] += 1
                        code = (
                            100001
                            if case == stage + "_error"
                            or case == "old_notice"
                            and stage == "publish"
                            else 100000
                        )
                        data = (
                            {"geetest": {"verifyUrl": "https://example.invalid"}}
                            if case == "captcha"
                            else {}
                        )
                        await route.fulfill(json={"code": code, "data": data})
                    else:
                        await route.fulfill(content_type="text/html", body=html)

                await page.route("**/*", route_handler)
                await page.goto(
                    "https://card.weibo.com/article/v5/editor#/draft/"
                    + ("999" if case == "wrong_draft" else "123")
                )
                await page.wait_for_load_state("load")
                p = WeiboPlatform()
                p.page = page
                p._active_draft_id = "123"
                p._expected_persisted_blocks = [
                    {"type": "text", "text": "完整正文"},
                    {"type": "image"},
                ]
                p.PUBLICATION_TIMEOUT_MS = 350
                if case in {
                    "wrong_draft",
                    "wrong_title",
                    "missing_cover",
                    "missing_image",
                    "restricted",
                }:
                    with pytest.raises(
                        (SelectorError, ContentValidationError, PublishResultUnknownError)
                    ):
                        await p.publish_now(title)
                    assert counts == {"save": 0, "publish": 0}
                elif case in {"accepted", "duplicate_wire"}:
                    result = await p.publish_now(title)
                    assert _is_verified_public_submission("weibo", result)
                    assert counts == {"save": 1, "publish": 1}
                else:
                    with pytest.raises(PublishResultUnknownError):
                        await p.publish_now(title)
                    assert counts["save"] == 1
                    assert counts["publish"] == (
                        1 if case in {"publish_error", "old_notice"} else 0
                    )
                before = dict(counts)
                with pytest.raises(
                    (SelectorError, ContentValidationError, PublishResultUnknownError)
                ):
                    await p.publish_now(title)
                assert counts == before
            finally:
                await browser.close()

    asyncio.run(run())


def test_weibo_receipt_requires_exact_source_scope_and_ack():
    response = {
        "status": "SUBMITTED",
        "verification_evidence": {
            "submit_acknowledged": True,
            "submission_source": "weibo_publish_response",
            "submission_scope": "PUBLIC",
            "submission_draft_id": "123",
        },
    }
    platform = _FakePlatform()
    platform.platform_name = "weibo"
    platform.save_draft = AsyncMock(return_value="https://example.invalid/draft/123")
    platform.publish_now = AsyncMock(return_value=response)
    result = _run_publish_mode(platform, "PUBLISH")
    assert result["success"] is True
    assert result["status"] == "SUBMITTED"
    assert result["post_url"] == ""
    assert _is_verified_public_submission("weibo", result)
    for key, wrong in [
        ("submit_acknowledged", 1),
        ("submission_source", "zol_publish_response"),
        ("submission_scope", "SELF_ONLY"),
    ]:
        bad = {
            **response,
            "verification_evidence": {**response["verification_evidence"], key: wrong},
        }
        assert not _is_verified_public_submission("weibo", bad)


def test_weibo_native_form_rejects_duplicates_and_other_encodings():
    req = SimpleNamespace(
        headers={"content-type": "application/x-www-form-urlencoded"},
        post_data=urlencode([("id", "1"), ("id", "2")]),
    )
    with pytest.raises(ValueError):
        WeiboPlatform._native_form(req)
    req.headers["content-type"] = "application/json"
    with pytest.raises(ValueError):
        WeiboPlatform._native_form(req)


@pytest.mark.parametrize("case", [
    "accepted", "wrong_first_image", "wrong_count", "empty_candidates",
    "missing_entry", "no_body_images",
])
def test_cover_uses_first_body_image_and_crops_once(case):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                item = (
                    '<div class="image-item" onclick="this.classList.add(\'is-selected\')">'
                    '<img src="/body.svg"></div>'
                )
                if case == "wrong_first_image":
                    item = item.replace("/body.svg", "/other.svg")
                if case == "wrong_count":
                    item *= 2
                if case == "empty_candidates":
                    item = ""
                html = """<meta charset="utf-8">
                <div class="tiptap ProseMirror"><figure class="wb-node-image">
                <img class="image-view__body__image" src="/body.svg"></figure></div>
                <div class="cover-empty" onclick="select.style.display='block'">设置文章封面</div>
                <div class="n-dialog" id="select" style="display:none"><span>正文图片</span>
                <div class="image-list">ITEMS</div>
                <button onclick="select.style.display='none';crop.style.display='block'">
                下一步</button></div>
                <div class="n-dialog" id="crop" style="display:none">
                <cropper-selection style="display:block;width:200px;height:100px">
                </cropper-selection>
                <button onclick="confirmed++;crop.style.display='none';
                previews.innerHTML='<img class=cover-img src=/new.svg>'">确定</button></div>
                <div id="previews" class="cover-preview"></div>
                <script>window.confirmed=0;const sel=document.querySelector('cropper-selection');
                sel.width=200;sel.height=100;</script>""".replace("ITEMS", item)

                if case == "missing_entry":
                    html = html.replace('class="cover-empty"', 'class="unavailable-entry"')
                if case == "no_body_images":
                    html = html.replace('class="image-view__body__image"', 'class="other-image"')

                async def serve(route):
                    if route.request.url.endswith(".svg"):
                        await route.fulfill(
                            content_type="image/svg+xml",
                            body='<svg xmlns="http://www.w3.org/2000/svg" '
                            'width="20" height="20"></svg>',
                        )
                    else:
                        await route.fulfill(content_type="text/html; charset=utf-8", body=html)

                await page.route("**/*", serve)
                await page.goto("https://card.weibo.com/article/v5/editor#/draft/123")
                platform = WeiboPlatform()
                platform.page = page
                result = await platform.set_cover()
                assert result["success"] is (case == "accepted"), result
                assert await page.evaluate("confirmed") == (1 if case == "accepted" else 0)
                repeated = await platform.set_cover()
                assert repeated["success"] is False
                assert await page.evaluate("confirmed") == (1 if case == "accepted" else 0)
            finally:
                await browser.close()

    asyncio.run(run())


def weibo_receipt():
    return {
        "status": "SUBMITTED",
        "verification_evidence": {
            "submit_acknowledged": True,
            "submission_source": "weibo_publish_response",
            "submission_scope": "PUBLIC",
        },
    }


@pytest.mark.parametrize("case", ["accepted", "missing_ack", "wrong_source", "partial_images"])
def test_weibo_public_submission_is_terminal_without_url(tmp_path, case):
    async def check():
        db = AccountDatabase(sqlite_database_url(tmp_path))
        result = {"success": True, "media_status": "completed", **weibo_receipt()}
        if case == "missing_ack":
            result["verification_evidence"]["submit_acknowledged"] = False
        elif case == "wrong_source":
            result["verification_evidence"]["submission_source"] = "old_message"
        elif case == "partial_images":
            result["media_status"] = "partial"
            result["draft_url"] = "https://example.invalid/draft/123"
        platform = SimpleNamespace(
            platform_name="weibo",
            context=None,
            initialize=AsyncMock(),
            cleanup=AsyncMock(),
            publish=AsyncMock(return_value=result),
        )
        accounts = AccountSessionService(
            db,
            seed_legacy_profiles=False,
            platform_factory=lambda _: platform,
            allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
        )
        accounts.assert_delivery_identity = AsyncMock()
        await accounts.initialize()
        try:
            account = await insert_account(
                db,
                make_profile(tmp_path, "weibo", "test"),
                platform="weibo",
            )
            sink = AsyncMock()
            service = DeliveryService(
                accounts,
                public_publish_enabled=True,
                delivery_event_sink=sink,
            )
            req = DeliveryRequest.model_validate(
                {
                    "platform": "weibo",
                    "account_id": account.account_id,
                    "mode": "PUBLISH",
                    "article": {"title": "测试文章标题", "body": "完整测试正文"},
                }
            )
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
