"""Native SMZDM publication fixtures; no real accounts or remote network."""

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from playwright.async_api import async_playwright

from account_sessions.account_service import AccountSessionService
from account_sessions.contracts import DeliveryRequest
from account_sessions.database import AccountDatabase
from account_sessions.delivery_service import DeliveryService, _is_verified_public_submission
from account_sessions.errors import ConfirmationRequiredError
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from platforms.base import DraftVerificationEvidence, PublishResultUnknownError, SelectorError
from platforms.smzdm import SmzdmPlatform
from platforms.smzdm_publication import (
    CROP_URL,
    LIST_URL,
    SUBMIT_URL,
    cover_url,
    image_key,
    native_payload,
)
from tests.test_account_sessions import insert_account, make_profile, sqlite_database_url
from tests.test_delivery_degraded import _FakePlatform, _run_publish_mode

ARTICLE_ID = "atest123"
EDIT_URL = f"https://post.smzdm.com/edit/{ARTICLE_ID}"
TITLE = "独立发布测试文章"
SOURCE = "https://am.zdmimg.com/202609/11/aaaaaaaa.png"
ORIGINAL = "https://tmpf.smzdm.com/202609/11/bbbbbbbb.png"
COVERS = ["https://am.zdmimg.com/202609/11/long.png", "https://am.zdmimg.com/202609/11/square.png"]
CONTENT = f'<p>第一段。<br>同段第二行。</p><h3>二级标题</h3><p>最后一段。</p><img src="{SOURCE}">'
EDITOR = CONTENT.replace("<br>", "</p><p>").replace(
    f'<img src="{SOURCE}">',
    f'<div class="image-view__body"><img src="{SOURCE}"></div>',
)
BLOCKS = [
    {"type": "text", "text": "第一段。\n同段第二行。"},
    {"type": "heading", "level": 2, "text": "二级标题"},
    {"type": "text", "text": "最后一段。"},
    {"type": "image"},
]
SETTINGS = {
    "anonymous": 0,
    "first_publish": 0,
    "create_state_type": 3,
    "ai_state_type": 3,
    "series_id": 0,
    "series_title": "",
    "series_order_id": 0,
    "remark": "",
    "group_id": "",
}


def receipt():
    return {
        "status": "SUBMITTED",
        "platform_article_id": ARTICLE_ID,
        "verification_evidence": {
            "submit_acknowledged": True,
            "submission_source": "smzdm_publish_response",
            "submission_scope": "PUBLIC",
            "submission_article_id": ARTICLE_ID,
        },
    }


def test_service_persists_pending_review_and_will_not_resubmit(tmp_path):
    async def scenario():
        db = AccountDatabase(sqlite_database_url(tmp_path))
        platform = SimpleNamespace(
            platform_name="smzdm", context=None, initialize=AsyncMock(), cleanup=AsyncMock(),
            publish=AsyncMock(return_value={
                "success": True, "media_status": "completed", **receipt(),
            }),
        )
        accounts = AccountSessionService(
            db, seed_legacy_profiles=False, platform_factory=lambda _: platform,
            allowed_profile_roots=(tmp_path / "runtime" / "profiles",),
        )
        accounts.assert_delivery_identity = AsyncMock()
        await accounts.initialize()
        try:
            account = await insert_account(
                db, make_profile(tmp_path, "smzdm", "test"), platform="smzdm",
            )
            service = DeliveryService(accounts, public_publish_enabled=True)
            request = DeliveryRequest.model_validate({
                "platform": "smzdm", "account_id": account.account_id, "mode": "PUBLISH",
                "article": {"title": TITLE, "body": "独立的测试正文"},
            })
            with pytest.raises(ConfirmationRequiredError) as error:
                await service.request_delivery(request, LOCAL_WEB_CONTEXT)
            request.confirmation_token = error.value.token
            operation = await service.request_delivery(request, LOCAL_WEB_CONTEXT)
            for _ in range(2):
                result = await service.execute_operation(
                    operation["operation_id"], LOCAL_WEB_CONTEXT,
                )
                assert result["status"] == "SUBMITTED"
                assert result["platform_article_id"] == ARTICLE_ID
                assert result["platform_url"] is None
            assert platform.publish.await_count == 1
        finally:
            await db.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case", ["accepted", "id_conflict", "missing_id", "wrong_scope", "bool_ack"]
)
def test_pending_review_receipt_survives_base_and_service_without_public_url(case):
    result = receipt()
    if case == "id_conflict":
        result["verification_evidence"]["submission_article_id"] = "other"
    elif case == "missing_id":
        result.pop("platform_article_id")
    elif case == "wrong_scope":
        result["verification_evidence"]["submission_scope"] = "PRIVATE"
    elif case == "bool_ack":
        result["verification_evidence"]["submit_acknowledged"] = 1
    assert _is_verified_public_submission("smzdm", result) is (case == "accepted")
    platform = _FakePlatform()
    platform.platform_name = "smzdm"
    platform.save_draft = AsyncMock(return_value=EDIT_URL)
    platform.publish_now = AsyncMock(return_value=result)
    actual = _run_publish_mode(platform, "PUBLISH")
    assert actual["success"] is (case == "accepted")
    if case == "accepted":
        assert actual["status"] == "SUBMITTED"
        assert actual["platform_article_id"] == ARTICLE_ID
        assert actual["post_url"] == ""


@pytest.mark.parametrize(
    "case",
    [
        "accepted",
        "pipeline",
        "wrong_id",
        "wrong_title",
        "wrong_images",
        "wrong_image_list",
        "truncated",
        "schedule",
        "wrong_cover",
        "changed_statement",
        "duplicate",
        "concurrent_duplicate",
        "http_error",
        "api_error",
        "bool_code",
        "already_published",
        "crop_bad_id",
        "crop_failed",
        "cover_not_loaded",
        "wrong_persisted",
    ],
)
def test_original_draft_cover_crop_and_single_native_submission(case):
    async def scenario():
        payload = {
            **SETTINGS,
            "article_id": ARTICLE_ID,
            "title": TITLE,
            "submit_type": "submit",
            "focus_image": COVERS[0],
            "square_pic_url": COVERS[1],
            "editorValue": CONTENT,
            "topic_list": [],
            "tag_list": [],
            "custom_topics": "",
            "image_list": [{"pic_url": SOURCE}],
        }
        if case == "wrong_id":
            payload["article_id"] = "other"
        elif case == "wrong_title":
            payload["title"] = "其他标题"
        elif case == "wrong_images":
            payload["editorValue"] = CONTENT.replace("aaaaaaaa", "cccccccc")
        elif case == "wrong_image_list":
            payload["image_list"] = [{"pic_url": SOURCE.replace("aaaaaaaa", "cccccccc")}]
        elif case == "truncated":
            payload["editorValue"] = "<p>第一段。</p>"
        elif case == "schedule":
            payload["schedule_time"] = 1234
        elif case == "wrong_cover":
            payload["focus_image"] = COVERS[1]
        elif case == "changed_statement":
            payload["ai_state_type"] = 2
        writes = []
        pixel = io.BytesIO()
        Image.new("RGB", (1600, 900), "blue").save(pixel, format="PNG")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()

                async def fixture(route, request):
                    if request.url == CROP_URL:
                        writes.append("crop")
                        await route.fulfill(
                            json={
                                "error_code": 1 if case == "crop_failed" else 0,
                                "data": [{"pic_url": COVERS[0], "square_pic_url": COVERS[1]}],
                            }
                        )
                    elif request.url == SUBMIT_URL:
                        writes.append(native_payload(request)["submit_type"])
                        await route.fulfill(
                            status=500 if case == "http_error" else 200,
                            json={
                                "error_code": False
                                if case == "bool_code"
                                else (1 if case == "api_error" else 0),
                            },
                        )
                    elif request.url.endswith("/api/image/original"):
                        await route.fulfill(
                            json={"error_code": 0, "data": {"original_url": ORIGINAL}}
                        )
                    elif request.resource_type == "image":
                        if case == "cover_not_loaded" and request.url in COVERS:
                            await route.abort()
                        else:
                            await route.fulfill(body=pixel.getvalue(), content_type="image/png")
                    elif request.url.endswith(f"/api/draft/{ARTICLE_ID}"):
                        await route.fulfill(
                            json={
                                "error_code": 0,
                                "data": {
                                    **SETTINGS,
                                    "article_id": ARTICLE_ID,
                                    "article_title": TITLE,
                                    "article_content": "<p>其他内容</p>"
                                    if case == "wrong_persisted"
                                    else CONTENT,
                                    **({"article_image": {"pic_url": COVERS[0]},
                                        "square_pic_url": COVERS[1]} if case == "pipeline" else {}),
                                },
                            }
                        )
                    elif request.url == LIST_URL:
                        status = "已发布" if case == "already_published" else "草稿"
                        await route.fulfill(
                            body=f'''<meta charset="utf-8">
                        <div class="pandect-content-common"><div class="p-pandect-content-title">
                        <a href="{EDIT_URL}">{TITLE}</a><em>{status}</em></div>
                        <a class="isEdit_" href="{EDIT_URL}">编辑</a></div>''',
                            content_type="text/html",
                        )
                    elif request.url == EDIT_URL:
                        cover_html = "".join(
                            f'<div class="image-url"><img src="{s}"></div>' for s in COVERS
                        )
                        crop_id = "other" if case == "crop_bad_id" else ARTICLE_ID
                        await route.fulfill(
                            body=f'''<meta charset="utf-8">
                        <textarea class="article-title">{TITLE}</textarea>
                        <div class="ProseMirror">{EDITOR}</div>
                        <div id="publish-setting"><div class="my-cover" id="cover">
                        {cover_html if case == "pipeline" else ""}</div>
                        <label class="el-radio is-checked">暂不表态</label>
                        <label class="el-radio is-checked">暂不表态</label></div>
                        <button onclick="gallery.hidden=false">添加长图</button>
                        <div class="pic-box" id="gallery" hidden>
                        <button onclick="fetch('/api/images/{ARTICLE_ID}?two_list=1',
                        {{method:'POST',body:'withCredentials=true'}})">已上传图片</button>
                        <div class="pic-item"><img class="thumb-imgs" src="{SOURCE}_e1080.jpg"
                        onclick="original()"></div></div>
                        <div role="dialog" aria-label="封面图-长图编辑" id="crop" hidden>
                        <img class="big-image" src="{ORIGINAL}">
                        <button class="ok-btn" onclick="cropImage()">确认</button></div>
                        <div class="page-footer teleport">
                        <button onclick="publish()">发布</button></div>
                        <div role="dialog" aria-label="提交成功" id="success" hidden>提交成功</div>
                        <script>
                        fetch('/api/draft/{ARTICLE_ID}',{{method:'POST'}});
                        const target='{SUBMIT_URL}', payload={json.dumps(payload)};
                        function encode(d){{const pairs=new URLSearchParams();
                          for(const [k,v] of Object.entries(d)){{
                            if(k==='series_order_id' && d.series_id===0)continue;
                            if(k==='image_list')v.forEach((row,i)=>{{
                              for(const [n,s] of Object.entries(row))
                                pairs.append(`image_list[${{i}}][${{n}}]`,s);
                            }}); else if(!Array.isArray(v))pairs.append(k,String(v));
                          }}return pairs;
                        }}
                        const save=()=>fetch(target,{{method:'POST',body:encode(
                          {{...payload,submit_type:'auto_save'}})}}).catch(()=>{{}});
                        setTimeout(save,100);
                        async function original(){{
                          await fetch('/api/image/original',{{method:'POST',body:encode(
                            {{article_id:'{ARTICLE_ID}',pic_url:'{SOURCE}'}})}});
                          crop.hidden=false;
                        }}
                        async function cropImage(){{
                          const form=new FormData(); const vals={{src_x:'0',src_y:'0',src_w:'1600',
                          src_h:'677',size_w:'420',size_h:'178',original_pic_height:'900',
                          original_pic_width:'1600',article_id:'{crop_id}',
                          cropperData:'{{}}',cutUrl:'{ORIGINAL}',is_head:'1'}};
                          for(const [k,v] of Object.entries(vals))
                            form.append(`cut_pic_list[0][${{k}}]`,v);
                          const r=await fetch('{CROP_URL}',{{method:'POST',body:form}});
                          const d=await r.json();if(d.error_code===0){{
                            cover.innerHTML={json.dumps(cover_html)};
                            crop.hidden=true;gallery.hidden=true;save();
                          }}
                        }}
                        async function publish(){{
                          const opts={{method:'POST',body:encode(payload)}};
                          try{{const pending=fetch(target,opts);
                            if({json.dumps(case == "concurrent_duplicate")})
                              fetch(target,opts).catch(()=>{{}});
                            const r=await pending;const d=await r.json();
                            if({json.dumps(case == "duplicate")})
                              await fetch(target,opts).catch(()=>{{}});
                            if(d.error_code===0)success.hidden=false;else save();
                          }}catch(e){{save()}}
                        }}
                        </script>''',
                            content_type="text/html",
                        )
                    else:
                        await route.fulfill(json={})

                await context.route("**/*", fixture)
                p = SmzdmPlatform()
                p.page, p.context = page, context
                p._identity_payload = {"ok": True, "user_id": "bound"}
                p._expected_persisted_blocks = BLOCKS
                p.PUBLICATION_TIMEOUT_MS = 500
                if case == "pipeline":
                    evidence = DraftVerificationEvidence()
                    evidence.mark_entity_binding(
                        bound=True, source="baseline_new_id", id_match=True,
                    )
                    evidence.mark_draft_list(1)
                    evidence.mark_reopen(title_match=True, dom_blocks_match=True)
                    evidence.set_draft_url(EDIT_URL)
                    p._last_draft_evidence = evidence.finalize()
                    assert await p.publish_now(TITLE) == receipt()
                    assert writes == ["submit"]
                    with pytest.raises(PublishResultUnknownError):
                        await p.publish_now(TITLE)
                    assert writes == ["submit"]
                    return
                if case in {"already_published", "wrong_persisted"}:
                    with pytest.raises(SelectorError):
                        await p.prepare_existing_publication(TITLE, EDIT_URL)
                    assert writes == []
                    return
                await p.prepare_existing_publication(TITLE, EDIT_URL)
                assert writes == []
                if case in {"crop_bad_id", "crop_failed", "cover_not_loaded"}:
                    with pytest.raises((SelectorError, PublishResultUnknownError)):
                        await p.prepare_first_image_cover()
                    assert writes == ([] if case == "crop_bad_id" else ["crop"])
                    return
                await p.prepare_first_image_cover()
                assert writes == ["crop"]
                if case in {"accepted", "duplicate", "concurrent_duplicate"}:
                    assert await p.publish_now(TITLE) == receipt()
                    assert writes == ["crop", "submit"]
                else:
                    with pytest.raises((SelectorError, PublishResultUnknownError)):
                        await p.publish_now(TITLE)
                    assert writes == (
                        ["crop", "submit"]
                        if case
                        in {
                            "http_error",
                            "api_error",
                            "bool_code",
                        }
                        else ["crop"]
                    )
                before = list(writes)
                with pytest.raises(PublishResultUnknownError):
                    await p.publish_now(TITLE)
                assert writes == before
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_native_cdn_variants_preserve_cover_shape_and_image_identity():
    assert image_key(SOURCE + "_e1080.jpg") == image_key(SOURCE)
    assert cover_url(COVERS[0].replace("https:", "")) == COVERS[0]
    assert cover_url(COVERS[0].replace("https:", "http:")) == COVERS[0]
    assert cover_url(SOURCE + "_fo742.jpg") != cover_url(SOURCE + "_a320.jpg")
    with pytest.raises(ValueError):
        cover_url("https://untrusted.invalid/cover.png")
    with pytest.raises(ValueError):
        cover_url("https://user@am.zdmimg.com/cover.png")
