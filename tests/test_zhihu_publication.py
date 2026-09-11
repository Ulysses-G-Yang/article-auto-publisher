"""Native save -> publish -> public page; all network traffic stays in the fixture."""

import asyncio
import io
import json

import pytest
from PIL import Image
from playwright.async_api import async_playwright

from platforms.base import DraftVerificationEvidence, PublishResultUnknownError, SelectorError
from platforms.zhihu import ZhihuPlatform, _publication_image_key

ARTICLE_ID = "123456789"
EDIT_URL = f"https://zhuanlan.zhihu.com/p/{ARTICLE_ID}/edit"
TITLE = "独立测试文章"
SOURCE = "https://pic1.zhimg.com/v2-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_1440w.png"
CONTENT = (
    '<p>首段完整文字。<br>同段第二行。</p><h3>二级标题</h3><p>第二段。</p>'
    f'<figure><img src="{SOURCE}"></figure>'
)
BLOCKS = [
    {"type": "text", "text": "首段完整文字。\n同段第二行。"},
    {"type": "heading", "level": 2, "text": "二级标题"},
    {"type": "text", "text": "第二段。"},
    {"type": "image"},
]


def native_payload():
    return {"action": "article", "data": {
        "draft": {"disabled": 1, "id": ARTICLE_ID, "isPublished": False},
        "title": {"title": TITLE},
        "hybrid": {"html": CONTENT},
        "commentsPermission": {"comment_permission": "anyone"},
        "creationStatement": {"disclaimer_type": "none", "disclaimer_status": "close"},
        "appreciate": {"can_reward": False, "tagline": ""},
        "commercialReportInfo": {"isReport": 0},
    }}


@pytest.mark.parametrize("case", [
    "success", "wrong_id", "wrong_title", "wrong_images", "truncated",
    "schedule", "paid", "reward", "answer", "already_published", "wrong_author",
    "http_error", "public_mismatch", "duplicate", "unchanged", "pipeline",
])
def test_single_native_submission_and_independent_public_proof(case):
    async def scenario():
        payload = native_payload()
        if case == "wrong_id":
            payload["data"]["draft"]["id"] = "987654321"
        elif case == "wrong_title":
            payload["data"]["title"]["title"] = "另一篇文章"
        elif case == "wrong_images":
            payload["data"]["hybrid"]["html"] = CONTENT.replace("a" * 32, "b" * 32)
        elif case == "truncated":
            payload["data"]["hybrid"]["html"] = "<p>首段完整文字。</p>"
        elif case == "schedule":
            payload["data"]["schedule"] = {"publishAt": 1234}
        elif case == "paid":
            payload["data"]["pay_type_config"] = {"type": 1}
        elif case == "reward":
            payload["data"]["appreciate"]["can_reward"] = True
        elif case == "answer":
            payload["action"] = "answer"
        draft = {
            "id": ARTICLE_ID, "state": "published" if case == "already_published" else "draft",
            "title": TITLE, "content": CONTENT,
            "author": {"id": "other" if case == "wrong_author" else "bound", "url_token": "tester"},
        }
        pixel = io.BytesIO()
        Image.new("RGB", (10, 10), "blue").save(pixel, format="PNG")
        writes = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()

                async def fixture(route, request):
                    if request.resource_type == "image":
                        await route.fulfill(body=pixel.getvalue(), content_type="image/png")
                    elif request.method == "PATCH":
                        writes.append("save")
                        await route.fulfill(json=draft)
                    elif request.method == "POST":
                        writes.append("publish")
                        await route.fulfill(status=403 if case == "http_error" else 200, json={})
                    elif request.url.endswith("/draft"):
                        await route.fulfill(json=draft)
                    elif "/edit" in request.url:
                        editor = CONTENT.replace("<p>", '<div data-block="true">').replace(
                            "</p>", "</div>",
                        ).replace("<h3>", '<h3 data-block="true">').replace(
                            "<br>", '</div><div data-block="true">',
                        ).replace(
                            SOURCE, SOURCE.replace("pic1.", "picx.").replace("/v2-", "/80/v2-")
                            .replace("1440w", "720w"),
                        )
                        html = f'''<meta charset="utf-8">
                        <textarea class="Input" placeholder="请输入标题">{TITLE}</textarea>
                        <div class="notranslate public-DraftEditor-content">{editor}</div>
                        <input type="radio" id="PublishPanel-RewardSetting-1" checked>
                        <button role="combobox">未选择</button>
                        <button role="combobox">无声明</button>
                        <input role="combobox" aria-label="文章话题">
                        <button onclick="publish()">发布</button>
                        <script>
                        const savePath='/api/articles/{ARTICLE_ID}/draft';
                        fetch('/sc-profiler', {{method:'POST',body:'opaque-non-json'}})
                          .catch(()=>{{}});
                        setTimeout(()=>fetch(savePath), 100);
                        setTimeout(()=>fetch(savePath, {{method:'PATCH',body:'{{}}'}})
                          .catch(()=>{{}}),150);
                        async function publish() {{
                          try {{
                            if (!{json.dumps(case == "unchanged")})
                              await fetch(savePath, {{method:'PATCH',body:JSON.stringify(
                              {{content:{json.dumps(CONTENT)},can_reward:false}})}});
                            const options={{method:'POST',
                              body:JSON.stringify({json.dumps(payload)})}};
                            const target='https://www.zhihu.com/api/v4/content/publish';
                            const result=await fetch(target, options);
                            if ({json.dumps(case == "duplicate")})
                              await fetch(target,options).catch(()=>{{}});
                            if(result.ok)location.href='/p/{ARTICLE_ID}?just_published=1';
                          }} catch(e) {{}} 
                        }}
                        </script>'''
                        await route.fulfill(body=html, content_type="text/html")
                    else:
                        public_body = CONTENT if case != "public_mismatch" else "<p>错稿</p>"
                        await route.fulfill(body=(
                            f'<meta charset="utf-8"><h1 class="Post-Title">{TITLE}</h1>'
                            '<a class="AuthorInfo-name" href="https://www.zhihu.com/people/tester">测试</a>'
                            f'<div class="Post-RichTextContainer">{public_body}</div>'
                        ), content_type="text/html")

                await context.route("**/*", fixture)
                platform = ZhihuPlatform()
                platform.page, platform.context = page, context
                platform.PUBLICATION_TIMEOUT_MS = 700
                platform.PERSIST_VERIFY_ATTEMPTS = 1
                platform._identity_payload = {"ok": True, "user_id": "bound"}
                platform._expected_persisted_blocks = BLOCKS
                if case in {"already_published", "wrong_author"}:
                    with pytest.raises(SelectorError):
                        await platform.prepare_existing_publication(TITLE, EDIT_URL)
                    assert writes == []
                    return
                if case == "pipeline":
                    evidence = DraftVerificationEvidence()
                    evidence.mark_entity_binding(
                        bound=True, source="baseline_new_id", id_match=True,
                    )
                    evidence.mark_draft_list(1)
                    evidence.mark_reopen(title_match=True, dom_blocks_match=True)
                    evidence.set_draft_url(EDIT_URL)
                    platform._last_draft_evidence = evidence.finalize()
                else:
                    await platform.prepare_existing_publication(TITLE, EDIT_URL)
                assert writes == []
                if case in {"success", "duplicate", "unchanged", "pipeline"}:
                    assert await platform.publish_now(TITLE) == EDIT_URL.removesuffix("/edit")
                    assert writes == (["publish"] if case == "unchanged" else ["save", "publish"])
                else:
                    with pytest.raises(PublishResultUnknownError):
                        await platform.publish_now(TITLE)
                    assert writes == (
                        ["save", "publish"]
                        if case in {"http_error", "public_mismatch"} else ["save"]
                    )
                before = list(writes)
                with pytest.raises(PublishResultUnknownError):
                    await platform.publish_now(TITLE)
                assert writes == before
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_media_binding_ignores_only_native_cdn_variants_and_temporary_signatures():
    signed = (
        "https://pic-private.zhihu.com/v2-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        "~resize:1440:q75.png?auth_key=fixture-only&expiration=123"
    )
    assert _publication_image_key(signed) == _publication_image_key(SOURCE)
    assert _publication_image_key(SOURCE.replace("a" * 32, "b" * 32)) != (
        _publication_image_key(SOURCE)
    )
    with pytest.raises(ValueError):
        _publication_image_key(SOURCE.replace("pic1.zhimg.com", "untrusted.invalid"))
