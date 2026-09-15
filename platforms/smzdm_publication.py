"""SMZDM original-draft publication through native controls and bound receipts."""

from __future__ import annotations

import json
import math
import re
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qsl, urlsplit

from platforms.base import DraftVerificationEvidence, PublishResultUnknownError, SelectorError
from platforms.content_validation import extract_expected_paragraphs, normalize_for_comparison

SUBMIT_URL = "https://post.smzdm.com/api/editor/article/submit"
CROP_URL = "https://post.smzdm.com/api/image/crop"
LIST_URL = "https://zhiyou.smzdm.com/user/article/"
COVER_SELECTOR = "#publish-setting .my-cover .image-url > img"


def native_payload(request) -> dict:
    """Decode the site's qs form serialization; never reconstruct or send a request."""
    raw = request.post_data or ""
    if raw.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("SMZDM payload is not an object")
        return data
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=2000)
    data, images = {}, {}
    for key, value in pairs:
        image = re.fullmatch(r"image_list\[([0-9]+)\]\[([a-z_]+)\]", key)
        if image:
            row = images.setdefault(int(image[1]), {})
            if image[2] in row:
                raise ValueError("SMZDM duplicate image field")
            row[image[2]] = value
        else:
            if key in data:
                raise ValueError("SMZDM duplicate form field")
            data[key] = value
    if images:
        if "image_list" in data or set(images) != set(range(len(images))):
            raise ValueError("SMZDM image indices are not contiguous")
        data["image_list"] = [images[i] for i in range(len(images))]
    for key in (
        "anonymous", "first_publish", "create_state_type", "ai_state_type",
        "series_id", "series_order_id",
    ):
        if key in data and re.fullmatch(r"0|[1-9][0-9]*", data[key]):
            data[key] = int(data[key])
    return data


def image_key(source: str) -> str:
    parsed = urlsplit("https:" + source if source.startswith("//") else source)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not (
        parsed.netloc.endswith(".zdmimg.com") or parsed.netloc == "tmpf.smzdm.com"
    ):
        raise ValueError("SMZDM image host is not verified")
    match = re.fullmatch(r"(.+?\.(?:png|jpg|jpeg|gif|webp))(?:_[^/]*)?", parsed.path, re.I)
    if not match:
        raise ValueError("SMZDM image path is not verified")
    return parsed.netloc + match.group(1)


def cover_url(source: str) -> str:
    """Retain the exact cover variant while resolving native CDN URL schemes."""
    image_key(source)
    parsed = urlsplit("https:" + source if source.startswith("//") else source)
    return parsed._replace(scheme="https").geturl()


def crop_fields(request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data;"):
        return {}
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + request.post_data_buffer,
    )
    result = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name or name in result or part.get_filename():
            return {}
        result[name] = part.get_content()
    return result


class SmzdmPublicationMixin:
    PUBLICATION_TIMEOUT_MS = 30000

    async def _publication_content_matches(self, html: object) -> bool:
        if not isinstance(html, str) or not html:
            return False
        raw = await self.page.evaluate(
            """html => {
                const doc = new DOMParser().parseFromString(html, 'text/html');
                doc.querySelectorAll('br').forEach(e => e.replaceWith(doc.createTextNode('\\n')));
                return {
                    tokens: Array.from(doc.querySelectorAll('p,h2,h3,img')).map(e => {
                        if (e.tagName === 'IMG') return {kind:'image'};
                        if (e.querySelector('img') || !e.textContent.trim()) return null;
                        return e.tagName === 'P' ? {kind:'text',text:e.textContent} :
                            {kind:'heading',level:e.tagName === 'H3' ? 2 : 1,text:e.textContent};
                    }).filter(Boolean),
                    images: Array.from(doc.querySelectorAll('img')).map(e => e.getAttribute('src'))
                };
            }""",
            html,
        )
        tokens = []
        for token in raw["tokens"]:
            if token["kind"] == "text":
                tokens.extend(
                    {"kind": "text", "text": p.comparison_text}
                    for p in extract_expected_paragraphs([{"type": "text", **token}])
                )
            elif token["kind"] == "heading":
                tokens.append({**token, "text": normalize_for_comparison(token["text"])})
            else:
                tokens.append(token)
        return (
            tokens == self._expected_content_tokens(self._expected_persisted_blocks)
            and [image_key(src) for src in raw["images"]] == self._publication_images
        )

    async def prepare_existing_publication(self, title: str, edit_url: str) -> None:
        if getattr(self, "_publication_guard_installed", False):
            raise SelectorError("什么值得买原稿发布已准备，不重置提交计数")
        draft_id, edit_url = self._validated_draft_entity(edit_url)
        if not (self._identity_payload or {}).get("ok") or not self._expected_persisted_blocks:
            raise SelectorError("什么值得买发布缺少已验证账号或冻结正文")
        self._publication_id = draft_id
        self._publication_title = title
        self._publication_stage = "blocked"
        self._publication_post_sent = 0
        self._publication_crop_sent = 0
        self._publication_blocked = []
        self._publication_images = []
        self._publication_covers = []
        self._publication_crop_source = ""
        self._publication_guard_installed = True

        async def guard(route, request):
            parsed = urlsplit(request.url)
            allowed = request.method in {"GET", "HEAD", "OPTIONS"}
            if "ajax_create_caogao" in parsed.path:
                allowed = False
            if request.method == "POST" and parsed.netloc == "post.smzdm.com":
                try:
                    if request.url == CROP_URL:
                        allowed = self._crop_payload_matches(crop_fields(request))
                        if allowed:
                            self._publication_crop_sent += 1
                    else:
                        # Native draft reads can carry an empty body rather than serialized {}.
                        data = native_payload(request)
                        if parsed.path == f"/api/draft/{draft_id}":
                            allowed = data == {}
                        elif parsed.path == f"/api/images/{draft_id}":
                            allowed = data in (
                                {}, {"withCredentials": True}, {"withCredentials": "true"},
                            )
                        elif parsed.path == "/api/image/original":
                            allowed = (
                                data.get("article_id") == draft_id
                                and self._publication_images
                                and image_key(data.get("pic_url", ""))
                                == self._publication_images[0]
                            )
                        elif request.url == SUBMIT_URL:
                            if (
                                self._publication_stage == "submit"
                                and self._publication_post_sent == 0
                            ):
                                # Reserve before the DOM await to reject concurrent duplicates.
                                self._publication_stage = "validating"
                                allowed = await self._publication_payload_matches(data)
                            if allowed:
                                self._publication_post_sent += 1
                except Exception:
                    allowed = False
            if allowed:
                await route.fallback()
            else:
                if parsed.netloc == "post.smzdm.com":
                    self._publication_blocked.append(f"{request.method} {parsed.path}")
                await route.abort()

        await self.context.route("**/*", guard)
        probe = await self.verify_draft_readonly(title)
        if probe.get("draft_url") != edit_url:
            raise SelectorError("什么值得买原稿不是当前账号唯一同名草稿")
        all_titles = await self.page.locator(".p-pandect-content-title a").all_text_contents()
        if sum(value.strip() == title for value in all_titles) != 1:
            raise SelectorError("什么值得买内容列表存在同名文章，停止以免重复发布")
        async with self.page.expect_response(
            lambda r: (
                r.request.method == "POST"
                and r.url == f"https://post.smzdm.com/api/draft/{draft_id}"
            ),
            timeout=self.actions.event_timeout(30000),
        ) as pending:
            await self._verify_persisted_draft(title, edit_url)
        response = await pending.value
        envelope = await response.json()
        data = envelope.get("data", {})
        if (
            not response.ok
            or type(envelope.get("error_code")) is not int
            or envelope["error_code"] != 0
            or data.get("article_id") != draft_id
            or data.get("article_title") != title
        ):
            raise SelectorError("什么值得买原稿读取回执不匹配")
        self._publication_settings = {
            key: data.get(key)
            for key in (
                "anonymous",
                "first_publish",
                "create_state_type",
                "ai_state_type",
                "series_id",
                "series_title",
                "series_order_id",
                "remark",
                "group_id",
            )
        }
        if (
            any(
                data.get(key) != value
                for key, value in {
                    "anonymous": 0,
                    "first_publish": 0,
                    "create_state_type": 3,
                    "ai_state_type": 3,
                    "series_id": 0,
                    "series_title": "",
                    "group_id": "",
                }.items()
            )
            or data.get("topic_list")
            or data.get("tag_list")
            or data.get("remark")
        ):
            raise SelectorError("什么值得买原稿发布设置与普通文章不符")
        await self._assert_publication_content(title)
        if not await self._publication_content_matches(data.get("article_content")):
            raise SelectorError("什么值得买云端正文或图片源与冻结原稿不匹配")
        long_cover = (data.get("article_image") or {}).get("pic_url")
        square_cover = data.get("square_pic_url")
        if long_cover and square_cover:
            for source in (long_cover, square_cover):
                image_key(source)
            self._publication_covers = [cover_url(long_cover), cover_url(square_cover)]
            await self._assert_publication_covers()

    async def _assert_publication_content(self, title: str) -> None:
        if (
            self.page.url != f"https://post.smzdm.com/edit/{self._publication_id}"
            or title != self._publication_title
            or await self.page.locator("textarea.article-title").input_value() != title
        ):
            raise SelectorError("什么值得买发布未绑定原稿与标题")
        await self._validate_dom_exact(self._expected_persisted_blocks, phase="发布前")
        images = self.page.locator("div.ProseMirror img")
        for index in range(await images.count()):
            await images.nth(index).scroll_into_view_if_needed(timeout=5000)
        await self.page.wait_for_function(
            "() => Array.from(document.querySelectorAll('div.ProseMirror img'))"
            ".every(e => e.complete && e.naturalWidth > 0)",
            timeout=15000,
        )
        sources = [image_key(s) for s in await images.evaluate_all("es => es.map(e => e.src)")]
        if not sources or self._publication_images and sources != self._publication_images:
            raise SelectorError("什么值得买发布图片源发生变化")
        self._publication_images = sources

    def _crop_payload_matches(self, data: dict) -> bool:
        prefix = "cut_pic_list[0]"
        fields = {
            key.removeprefix(prefix + "[").removesuffix("]"): value
            for key, value in data.items()
            if key.startswith(prefix + "[")
        }
        numbers = {
            "src_x",
            "src_y",
            "src_w",
            "src_h",
            "size_w",
            "size_h",
            "original_pic_height",
            "original_pic_width",
        }
        if (
            self._publication_stage != "crop"
            or self._publication_crop_sent != 0
            or len(data) != len(fields)
            or set(fields) != numbers | {"article_id", "cropperData", "cutUrl", "is_head"}
            or fields["article_id"] != self._publication_id
            or fields["is_head"] != "1"
            or not self._publication_crop_source
            or fields["cutUrl"] != self._publication_crop_source
        ):
            return False
        return all(math.isfinite(float(fields[k])) and float(fields[k]) >= 0 for k in numbers)

    async def prepare_first_image_cover(self) -> None:
        """Select the existing first image; native crop produces both cover variants once."""
        await self._assert_publication_content(self._publication_title)
        if await self.page.locator(COVER_SELECTOR).count():
            raise SelectorError("什么值得买已有封面，不自动覆盖")
        await self.actions.perform(
            self.page.get_by_text("添加长图", exact=True).click, timeout=5000,
        )
        await self.actions.perform(
            self.page.get_by_text("已上传图片", exact=True).click, timeout=15000,
        )
        thumbs = self.page.locator(".pic-box .pic-item img.thumb-imgs")
        await thumbs.first.wait_for(state="visible", timeout=15000)
        sources = await thumbs.evaluate_all("es => es.map(e => e.src)")
        matches = [
            i for i, src in enumerate(sources) if image_key(src) == self._publication_images[0]
        ]
        if len(matches) != 1:
            raise SelectorError("什么值得买图片库无法唯一绑定正文首图")
        async with self.page.expect_response(
            lambda r: (
                r.url == "https://post.smzdm.com/api/image/original" and r.request.method == "POST"
            ),
            timeout=self.actions.event_timeout(20000),
        ) as pending:
            await self.actions.perform(thumbs.nth(matches[0]).click, timeout=5000)
        response = await pending.value
        payload = await response.json()
        original = (payload.get("data") or {}).get("original_url", "")
        if not response.ok or type(payload.get("error_code")) is not int or payload["error_code"]:
            raise SelectorError("什么值得买首图原图读取未确认")
        image_key(original)
        self._publication_crop_source = original
        dialog = self.page.get_by_role("dialog", name="封面图-长图编辑", exact=True)
        await dialog.wait_for(state="visible", timeout=15000)
        big = dialog.locator("img.big-image")
        if await big.get_attribute("src") != original:
            raise SelectorError("什么值得买裁剪器不是绑定首图")
        button = dialog.get_by_role("button", name="确认", exact=True)
        await self.page.wait_for_function(
            "() => Array.from(document.querySelectorAll('[role=dialog] .ok-btn'))"
            ".some(e => !e.disabled)",
            timeout=20000,
        )
        self._publication_stage = "crop"
        try:
            async with self.page.expect_response(
                lambda r: r.url == CROP_URL and r.request.method == "POST",
                timeout=self.actions.event_timeout(30000),
            ) as pending:
                await self.actions.perform(button.click, timeout=5000)
            response = await pending.value
            result = await response.json()
            rows = result.get("data")
            if (
                not response.ok
                or type(result.get("error_code")) is not int
                or result["error_code"]
                or self._publication_crop_sent != 1
                or not isinstance(rows, list)
                or len(rows) != 1
            ):
                raise ValueError("crop receipt invalid")
            self._publication_covers = [
                cover_url(rows[0]["pic_url"]), cover_url(rows[0]["square_pic_url"]),
            ]
            for source in self._publication_covers:
                image_key(source)
            await dialog.wait_for(state="hidden", timeout=10000)
            await self._assert_publication_covers()
        except Exception as exc:
            if not self._publication_crop_sent:
                raise SelectorError("什么值得买裁剪请求未发送，停止") from exc
            raise PublishResultUnknownError("什么值得买封面裁剪已发送但未确认，不重复裁剪") from exc
        finally:
            self._publication_stage = "blocked"

    async def _assert_publication_covers(self) -> None:
        await self.page.locator("#publish-setting").scroll_into_view_if_needed(timeout=5000)
        await self.page.wait_for_function(
            "s => {const es=Array.from(document.querySelectorAll(s));"
            "return es.length===2 && es.every(e => e.complete && e.naturalWidth>0)}",
            arg=COVER_SELECTOR,
            timeout=15000,
        )
        sources = await self.page.locator(COVER_SELECTOR).evaluate_all("es => es.map(e => e.src)")
        if (
            not self._publication_covers
            or [cover_url(s) for s in sources] != self._publication_covers
        ):
            raise SelectorError("什么值得买长图和方图与裁剪回执不匹配")

    async def _publication_payload_matches(self, data: dict) -> bool:
        allowed_keys = {
            "article_id",
            "submit_type",
            "ai_title",
            "title",
            "series_title",
            "focus_image",
            "series_order_id",
            "series_id",
            "anonymous",
            "first_publish",
            "remark",
            "editorValue",
            "create_state_type",
            "ai_state_type",
            "topic_list",
            "tag_list",
            "square_pic_url",
            "cover_image_rectangle",
            "cover_image_square",
            "custom_topics",
            "group_id",
            "awne",
            "wne",
            "image_list",
            "ai_outline_log_ids",
        }
        if (
            set(data) - allowed_keys
            or data.get("article_id") != self._publication_id
            or data.get("submit_type") != "submit"
            or data.get("title") != self._publication_title
            or any(
                data.get(
                    k, 0 if k == "series_order_id" and data.get("series_id") == 0 else None,
                ) != v
                for k, v in self._publication_settings.items()
            )
            or data.get("topic_list")
            or data.get("tag_list")
            or data.get("custom_topics")
            or data.get("ai_title")
            or data.get("ai_outline_log_ids")
            or data.get("cover_image_rectangle")
            or data.get("cover_image_square")
            or [cover_url(data.get("focus_image", "")), cover_url(data.get("square_pic_url", ""))]
            != self._publication_covers
        ):
            return False
        images = data.get("image_list")
        if not isinstance(images, list) or [
            image_key(row.get("pic_url", "")) for row in images
        ] != self._publication_images:
            return False
        return await self._publication_content_matches(data.get("editorValue"))

    async def publish_now(self, title: str = "") -> dict:
        if getattr(self, "_public_publish_attempted", False):
            raise PublishResultUnknownError("什么值得买已尝试发布，不重复提交")
        if not getattr(self, "_publication_guard_installed", False):
            evidence = getattr(self, "_last_draft_evidence", None)
            if not (
                isinstance(evidence, DraftVerificationEvidence)
                and not evidence.unknown
                and evidence.draft_entity_bound
                and evidence.draft_list_title_unique
                and evidence.reopen_title_match
                and evidence.reopen_dom_blocks_match
                and evidence.draft_url
            ):
                raise SelectorError("什么值得买发布缺少完整原草稿证据")
            await self.prepare_existing_publication(title, evidence.draft_url)
        await self._assert_publication_content(title)
        await self._assert_publication_covers()
        declarations = await self.page.locator(
            "#publish-setting .el-radio.is-checked"
        ).all_text_contents()
        if [value.strip() for value in declarations] != ["暂不表态", "暂不表态"]:
            raise SelectorError("什么值得买创作声明与原稿不一致")
        button = self.page.locator(".page-footer.teleport").get_by_role(
            "button", name="发布", exact=True
        )
        if await button.count() != 1 or not await button.is_enabled():
            raise SelectorError("什么值得买原生发布按钮不唯一或不可用")
        self._public_publish_attempted = True
        self._publication_stage = "submit"
        try:
            async with self.page.expect_response(
                lambda r: r.url == SUBMIT_URL and r.request.method == "POST",
                timeout=self.actions.event_timeout(self.PUBLICATION_TIMEOUT_MS),
            ) as pending:
                await self.actions.perform(button.click, timeout=8000)
            response = await pending.value
            result = await response.json()
            if (
                self._publication_post_sent != 1
                or not response.ok
                or type(result.get("error_code")) is not int
                or result["error_code"] != 0
            ):
                raise ValueError("publication receipt invalid")
            # The native success modal explicitly says review status is in the account center.
            await self.page.get_by_role("dialog", name="提交成功", exact=True).wait_for(
                state="visible",
                timeout=10000,
            )
            return {
                "status": "SUBMITTED",
                "platform_article_id": self._publication_id,
                "verification_evidence": {
                    "submit_acknowledged": True,
                    "submission_source": "smzdm_publish_response",
                    "submission_scope": "PUBLIC",
                    "submission_article_id": self._publication_id,
                },
            }
        except Exception as exc:
            if not self._publication_post_sent:
                raise SelectorError("什么值得买发布请求未发送，本地检查已停止") from exc
            raise PublishResultUnknownError(
                "什么值得买已尝试提交但回执未确认，停止且不重发"
            ) from exc
        finally:
            self._publication_stage = "blocked"
