"""Isolated native Toutiao draft/preview/publication contracts; no real accounts."""

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from playwright.async_api import async_playwright

from account_sessions.delivery_service import _is_verified_public_submission
from platforms.base import DraftVerificationEvidence, PublishResultUnknownError, SelectorError
from platforms.toutiao import ToutiaoPlatform
from platforms.toutiao_publication import EDIT_PATH, SUBMIT_PATH, native_payload, native_receipt_id
from tests.test_delivery_degraded import _FakePlatform, _run_publish_mode

ARTICLE_ID = "7000000000000000001"
USER_ID = "2000000000000000001"
EDIT_URL = ToutiaoPlatform._edit_url(ARTICLE_ID)
IMAGE_URL = "https://image-tt-private.toutiao.com/example/image.png?signature=fixture"
CONTENT = (
    '<p>第一段。<br>同段第二行。</p><h1 class="pgc-h-forward-slash">正文标题</h1>'
    f'<div class="pgc-img"><img src="{IMAGE_URL}"><p class="pgc-img-caption"></p></div>'
    "<p>最后一段。</p>"
)
TOKENS = [
    {"kind": "P", "text": "第一段。 同段第二行。"},
    {"kind": "H2", "text": "正文标题"},
    {"kind": "I", "text": "", "fingerprint": "image-tt-private.toutiao.com/example/image.png"},
    {"kind": "P", "text": "最后一段。"},
]
TITLE = "独立头条发布测试"


def payload(*, preview=False):
    extra = {
        "content_source": 100000000402,
        "content_word_cnt": 40,
        "is_multi_title": 0,
        "sub_titles": [],
        "gd_ext": {},
        "tuwen_wtt_trans_flag": "0",
    }
    if not preview:
        extra["info_source"] = {"source_type": -1}
    return {
        "article_type": "0",
        "pgc_id": ARTICLE_ID,
        "source": "29",
        "title": TITLE,
        "content": CONTENT,
        "extra": json.dumps(extra),
        "search_creation_info": json.dumps({"searchTopOne": 0, "abstract": "", "clue_id": ""}),
        "title_id": "",
        "mp_editor_stat": "{}",
        "timer_time": "2026-09-15 22:00",
        "is_refute_rumor": "0",
        "save": "0" if preview else "1",
        "entrance": "" if preview else "main",
        **({"is_app_preview": "1"} if preview else {}),
        "timer_status": "0",
        "educluecard": "",
        "draft_form_data": '{"coverType":1}',
        "pgc_feed_covers": "[]",
        "article_ad_type": "2",
        "is_fans_article": "0",
        "govern_forward": "0",
        "praise": "0",
        "disable_praise": "0",
        "tree_plan_article": "0",
        "star_order_id": "",
        "star_order_name": "",
        "activity_tag": "0",
        "trends_writing_tag": "0",
        "claim_exclusive": "0",
        "ic_uri_list": "",
        "appid_list": "",
        "stock_ids": "",
        "concern_list": "",
    }


def receipt():
    return {
        "status": "SUBMITTED",
        "platform_article_id": ARTICLE_ID,
        "verification_evidence": {
            "submit_acknowledged": True,
            "submission_scope": "PUBLIC",
            "submission_source": "toutiao_publish_response",
            "submission_article_id": ARTICLE_ID,
        },
    }


@pytest.mark.parametrize("case", ["valid", "wrong_id", "missing_id", "bool_ack", "wrong_scope"])
def test_bound_receipt_survives_base_and_service_without_invented_public_url(case):
    result = receipt()
    if case == "wrong_id":
        result["verification_evidence"]["submission_article_id"] = "other"
    elif case == "missing_id":
        result.pop("platform_article_id")
    elif case == "bool_ack":
        result["verification_evidence"]["submit_acknowledged"] = 1
    elif case == "wrong_scope":
        result["verification_evidence"]["submission_scope"] = "PRIVATE"
    assert _is_verified_public_submission("toutiao", result) is (case == "valid")
    platform = _FakePlatform()
    platform.platform_name = "toutiao"
    platform.save_draft = AsyncMock(return_value=EDIT_URL)
    platform.publish_now = AsyncMock(return_value=result)
    actual = _run_publish_mode(platform, "PUBLISH")
    assert actual["success"] is (case == "valid")
    if case == "valid":
        assert actual["status"] == "SUBMITTED"
        assert actual["platform_article_id"] == ARTICLE_ID
        assert actual["post_url"] == ""


def test_duplicate_native_fields_rejected():
    with pytest.raises(ValueError):
        native_payload(SimpleNamespace(post_data="save=1&save=0"))


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"pgc_id": ARTICLE_ID}, ARTICLE_ID),
        ({"pgcId": ARTICLE_ID}, ARTICLE_ID),
        ({"pgc_id": int(ARTICLE_ID), "pgcId": ARTICLE_ID}, ARTICLE_ID),
        ({"pgc_id": ARTICLE_ID, "pgcId": "7000000000000000002"}, None),
        ({"pgc_id": True}, None),
        ({"pgc_id": 1.2}, None),
        ({}, None),
    ],
)
def test_raw_and_client_id_aliases_must_agree(data, expected):
    assert native_receipt_id({"data": data}) == expected


@pytest.mark.parametrize(
    "case",
    [
        "accepted",
        "pipeline",
        "wrong_id",
        "wrong_title",
        "wrong_image",
        "missing_text",
        "schedule",
        "cover",
        "exclusive",
        "sync",
        "unexpected",
        "preview_failure",
        "api_failure",
        "bool_code",
        "wrong_receipt",
        "already_published",
        "wrong_author",
        "wrong_cloud_content",
        "concurrent_duplicate",
        "background_autosave",
        "repeated_call",
        "editor_changed",
    ],
)
def test_verified_original_preview_and_publication_exactly_once(case, tmp_path):
    async def scenario():
        public = payload()
        if case == "wrong_id":
            public["pgc_id"] = "7000000000000000002"
        elif case == "wrong_title":
            public["title"] = "另一篇文章"
        elif case == "wrong_image":
            public["content"] = CONTENT.replace("image.png", "other.png")
        elif case == "missing_text":
            public["content"] = CONTENT.replace("最后一段。", "")
        elif case == "schedule":
            public["timer_status"] = "1"
        elif case == "cover":
            public["draft_form_data"] = '{"coverType":3}'
        elif case == "exclusive":
            public["claim_exclusive"] = "1"
        elif case == "sync":
            extra = json.loads(public["extra"])
            extra["tuwen_wtt_trans_flag"] = "2"
            public["extra"] = json.dumps(extra)
        elif case == "unexpected":
            public["pay_price"] = "10"
        cloud = {
            "is_draft": case != "already_published",
            "is_passed": case == "already_published",
            "pgc_id": ARTICLE_ID,
            "title": TITLE,
            "content": CONTENT,
            "article_pgc": {"creator_id": USER_ID},
            "media": {"creator_id": USER_ID},
            "timer_status": 0,
            "article_ad_type": 2,
        }
        if case == "wrong_author":
            cloud["article_pgc"]["creator_id"] = "other"
        elif case == "wrong_cloud_content":
            cloud["content"] = CONTENT.replace("image.png", "other.png")
        pixel = io.BytesIO()
        Image.new("RGB", (80, 60), "blue").save(pixel, format="PNG")
        writes = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()

                async def fixture(route, request):
                    if EDIT_PATH in request.url:
                        await route.fulfill(json=cloud)
                    elif SUBMIT_PATH in request.url:
                        body = native_payload(request)
                        writes.append(body)
                        preview = body["save"] == "0"
                        code = (
                            7050
                            if (case == "preview_failure" and preview)
                            or (case == "api_failure" and not preview)
                            else False
                            if case == "bool_code" and not preview
                            else 0
                        )
                        await route.fulfill(
                            json={
                                "code": code,
                                "data": {
                                    "pgc_id": "other"
                                    if (case == "wrong_receipt" and not preview)
                                    else ARTICLE_ID
                                },
                            }
                        )
                    elif request.url.startswith(IMAGE_URL.split("?")[0]):
                        await route.fulfill(body=pixel.getvalue(), content_type="image/png")
                    elif request.url == EDIT_URL:
                        html = f"""<meta charset="utf-8">
                        <textarea placeholder="请输入文章标题">{TITLE}</textarea>
                        <div class="ProseMirror" contenteditable="true">{CONTENT}</div>
                        <label class="byte-radio">
                            <span class="byte-radio-inner checked"></span>三图</label>
                        <label class="byte-radio" id="none">
                            <span class="byte-radio-inner"></span>无封面</label>
                        <label class="byte-checkbox">
                            <input type="checkbox" checked>发布得更多收益</label>
                        <label class="byte-checkbox"><input type="checkbox">头条首发</label>
                        <button id="preview">预览并发布</button>
                        <button id="confirm" hidden>确认发布</button>
                        <script>
                        const previewData={json.dumps(payload(preview=True))};
                        const publicData={json.dumps(public)};
                        fetch('{EDIT_PATH}?pgc_id={ARTICLE_ID}&format=json');
                        document.getElementById('none').onclick=()=>{{
                            document.querySelectorAll('.byte-radio-inner')
                                .forEach(e=>e.classList.remove('checked'));
                            document.querySelector('#none .byte-radio-inner')
                                .classList.add('checked');
                        }};
                        const send=d=>fetch('{SUBMIT_PATH}?source=mp&type=article&aid=1231',{{
                            method:'POST',body:new URLSearchParams(d)}}).then(r=>r.json());
                        document.getElementById('preview').onclick=async()=>{{
                            if ({json.dumps(case == "background_autosave")})
                                send({{...previewData,is_app_preview:''}}).catch(()=>{{}});
                            const r=await send(previewData);
                            if(r.code===0) document.getElementById('confirm').hidden=false;
                        }};
                        document.getElementById('confirm').onclick=async()=>{{
                            send(publicData).catch(()=>{{}});
                            if ({json.dumps(case == "concurrent_duplicate")})
                                send(publicData).catch(()=>{{}});
                        }};
                        </script>"""
                        await route.fulfill(body=html, content_type="text/html")
                    else:
                        await route.fulfill(body="")

                await context.route("**/*", fixture)
                platform = ToutiaoPlatform(profile_dir=tmp_path / "profile")
                platform.context, platform.page = context, page
                platform._identity_payload = {"ok": True, "user_id": USER_ID}
                platform._expected_persisted_tokens = TOKENS
                platform.PUBLICATION_TIMEOUT_MS = 700
                platform.verify_draft_readonly = AsyncMock(
                    return_value={
                        "match_count": 1,
                        "draft_url": EDIT_URL,
                    }
                )
                cloud_cases = {"already_published", "wrong_author", "wrong_cloud_content"}
                if case in cloud_cases:
                    with pytest.raises(SelectorError):
                        await platform.prepare_existing_publication(TITLE, EDIT_URL)
                    assert not writes
                    return
                if case == "pipeline":
                    evidence = DraftVerificationEvidence()
                    evidence.mark_entity_binding(
                        bound=True, source="baseline_new_id", id_match=True
                    )
                    evidence.mark_draft_list(1)
                    evidence.mark_reopen(title_match=True, dom_blocks_match=True)
                    evidence.set_draft_url(EDIT_URL)
                    platform._last_draft_evidence = evidence.finalize()
                else:
                    await platform.prepare_existing_publication(TITLE, EDIT_URL)
                if case == "editor_changed":
                    await page.locator("textarea").fill("changed")
                    with pytest.raises(SelectorError):
                        await platform.publish_now(TITLE)
                    assert not writes
                    return
                good = {
                    "accepted",
                    "pipeline",
                    "concurrent_duplicate",
                    "background_autosave",
                    "repeated_call",
                }
                if case in good:
                    assert await platform.publish_now(TITLE) == receipt()
                    if case == "repeated_call":
                        with pytest.raises(PublishResultUnknownError):
                            await platform.publish_now(TITLE)
                    assert [d["save"] for d in writes] == ["0", "1"]
                else:
                    with pytest.raises(PublishResultUnknownError):
                        await platform.publish_now(TITLE)
                    assert len(writes) == (
                        2 if case in {"api_failure", "bool_code", "wrong_receipt"} else 1
                    )
                assert platform._publication_stage == "blocked"
                # Neither account-profile writes nor unbound article creation may escape the guard.
                for path in ("/mp/agw/article/new", "/mp/agw/article/delete"):
                    await page.evaluate("p=>fetch(p).catch(()=>null)", path)
                assert all(d["pgc_id"] == ARTICLE_ID for d in writes)
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_publish_without_original_evidence_stops():
    with pytest.raises(SelectorError, match="完整原草稿证据"):
        asyncio.run(ToutiaoPlatform().publish_now(TITLE))
