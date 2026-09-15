"""Publish a verified Toutiao original draft through its native two-step form."""

from __future__ import annotations

import json
import re
from collections import deque
from urllib.parse import parse_qsl, urlsplit
from weakref import WeakSet

from loguru import logger

from platforms.base import DraftVerificationEvidence, PublishResultUnknownError, SelectorError
from platforms.toutiao_request_policy import publication_request_kind

SUBMIT_PATH = "/mp/agw/article/publish"
EDIT_PATH = "/mp/agw/article/edit"
TITLE = "textarea[placeholder*='文章标题']"
BODY = "div.ProseMirror[contenteditable='true']"


def native_payload(request) -> dict[str, str]:
    """Read native form data without constructing or replaying a platform request."""
    pairs = parse_qsl(
        request.post_data or "",
        keep_blank_values=True,
        strict_parsing=True,
        max_num_fields=100,
    )
    if len(dict(pairs)) != len(pairs):
        raise ValueError("duplicate native fields")
    return dict(pairs)


def native_receipt_id(result: dict) -> str | None:
    """Raw responses use snake case; the site's client also exposes camel case."""
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    values = [data[key] for key in ("pgc_id", "pgcId") if key in data]
    if not values or any(
        isinstance(v, bool)
        or not isinstance(v, (str, int))
        or not re.fullmatch(r"[0-9]{6,30}", str(v))
        for v in values
    ):
        return None
    ids = {str(v) for v in values}
    return ids.pop() if len(ids) == 1 else None


class ToutiaoPublicationMixin:
    PUBLICATION_TIMEOUT_MS = 30000

    async def _publication_html_tokens(self, html: str) -> list[dict]:
        return await self.page.evaluate(
            r"""html => {
                const root = new DOMParser().parseFromString(html, 'text/html').body;
                if (root.querySelector('script,iframe,video,audio,a,input,button')) return [];
                const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                const result = [];
                for (const child of root.children) {
                    const kind = child.matches('h1,h2') ? 'H2' : 'P';
                    let text = '';
                    const flush = () => {
                        if (norm(text)) result.push({kind, text:norm(text)});
                        text = '';
                    };
                    const visit = node => {
                        if (node.nodeType === Node.TEXT_NODE) {text += node.nodeValue; return;}
                        if (node.nodeType !== Node.ELEMENT_NODE) return;
                        if (node.tagName === 'BR') {text += ' '; return;}
                        if (node.tagName === 'IMG') {
                            flush();
                            const u = new URL(node.getAttribute('src'), location.href);
                            result.push({kind:'I',text:'',fingerprint:
                                ['http:','https:'].includes(u.protocol)
                                && !u.username && !u.password
                                ? u.hostname + u.pathname : ''});
                            return;
                        }
                        for (const c of node.childNodes) visit(c);
                    };
                    visit(child); flush();
                }
                return result;
            }""",
            html,
        )

    async def prepare_existing_publication(self, title: str, edit_url: str) -> None:
        if getattr(self, "_publication_guard_installed", False):
            raise SelectorError("头条原稿发布已准备，不重置请求计数")
        parsed = urlsplit(edit_url)
        pairs = dict(parse_qsl(parsed.query))
        article_id = pairs.get("pgc_id", "")
        if (
            not re.fullmatch(r"[0-9]{1,30}", article_id)
            or edit_url != self._edit_url(article_id)
            or not (self._identity_payload or {}).get("ok")
            or not self._expected_persisted_tokens
            or any(
                t["kind"] == "I" and not t.get("fingerprint")
                for t in self._expected_persisted_tokens
            )
        ):
            raise SelectorError("头条发布缺少已验证账号、原稿或冻结图文")
        self._publication_id = article_id
        self._publication_title = self._normalize_platform_title(title)
        self._publication_edit_url = edit_url
        self._publication_stage = "blocked"
        self._publication_preview_sent = 0
        self._publication_post_sent = 0
        self._publication_blocked = []
        self._publication_network_events = deque(maxlen=200)
        self._publication_local_aborts = WeakSet()
        self._publication_guard_error = None
        self._publication_guard_installed = True

        async def guard(route, request):
            kind = publication_request_kind(request.method, request.url)
            allowed = kind not in {
                "ARTICLE_SUBMISSION",
                "FORBIDDEN_MUTATION",
                "IMAGE_UPLOAD_REQUIRED",
                "UNRECOGNIZED_WRITE",
            }
            if kind == "ARTICLE_SUBMISSION":
                stage = self._publication_stage
                counter = (
                    "_publication_preview_sent" if stage == "preview" else "_publication_post_sent"
                )
                try:
                    data = native_payload(request)
                except Exception:
                    data = {}
                candidate = (
                    stage == "preview"
                    and data.get("save") == "0"
                    and data.get("is_app_preview") == "1"
                    or stage == "submit"
                    and data.get("save") == "1"
                )
                if candidate and getattr(self, counter) == 0:
                    # Reserve before any await: concurrent duplicates cannot both pass.
                    self._publication_stage = "validating"
                    try:
                        allowed = await self._publication_payload_matches(
                            data,
                            stage=stage,
                        )
                    except Exception:
                        allowed = False
                    if allowed:
                        setattr(self, counter, 1)
            if allowed:
                await route.fallback()
            else:
                # A local cancellation is not an Internet failure. Keep only
                # bounded category-level evidence, never URLs, tokens or bodies.
                self._publication_local_aborts.add(request)
                self._publication_blocked.append(kind)
                del self._publication_blocked[:-200]
                self._record_publication_network("LOCAL_REQUEST_BLOCKED", kind)
                if kind != "ARTICLE_SUBMISSION":
                    self._publication_guard_error = kind
                await route.abort()

        self.context.on("requestfailed", self._publication_request_failed)
        self.context.on("response", self._publication_response_observed)
        await self.context.route("**/*", guard)
        probe = await self.verify_draft_readonly(title)
        if probe.get("match_count") != 1 or probe.get("draft_url") != edit_url:
            raise SelectorError("头条原稿未在草稿箱唯一绑定，停止发布")
        async with self.page.expect_response(
            lambda r: (
                r.request.method == "GET"
                and urlsplit(r.url).netloc == "mp.toutiao.com"
                and urlsplit(r.url).path == EDIT_PATH
                and dict(parse_qsl(urlsplit(r.url).query)).get("pgc_id") == article_id
            ),
            timeout=self.actions.event_timeout(30000),
        ) as pending:
            matches = await self._verify_persisted_draft(self._publication_title, edit_url)
        response = await pending.value
        data = await response.json()
        account_id = str(self._identity_payload.get("user_id") or "")
        if (
            not response.ok
            or matches != (True, True)
            or data.get("is_draft") is not True
            or data.get("is_passed") is not False
            or data.get("pgc_id") != article_id
            or data.get("title") != self._publication_title
            or not account_id
            or str((data.get("article_pgc") or {}).get("creator_id")) != account_id
            or str((data.get("media") or {}).get("creator_id")) != account_id
            or data.get("timer_status") != 0
            or await self._publication_html_tokens(data.get("content", ""))
            != self._expected_persisted_tokens
        ):
            raise SelectorError("头条云端原稿、作者或完整图文未核验通过")
        self._publication_ad_type = str(data.get("article_ad_type"))
        if self._publication_ad_type not in {"0", "1", "2"}:
            raise SelectorError("头条原稿广告设置无法确认")
        await self._assert_publication_content()

    def _record_publication_network(self, code: str, kind: str, **details) -> None:
        event = {"code": code, "request_kind": kind, **details}
        self._publication_network_events.append(event)
        logger.info("Toutiao request outcome: {}", event)

    def _publication_request_failed(self, request) -> None:
        if request not in self._publication_local_aborts:
            self._record_publication_network(
                "BROWSER_REQUEST_FAILED", publication_request_kind(request.method, request.url)
            )

    def _publication_response_observed(self, response) -> None:
        if response.status >= 400:
            self._record_publication_network(
                "PLATFORM_HTTP_ERROR",
                publication_request_kind(response.request.method, response.url),
                http_status=response.status,
            )

    async def _assert_publication_content(self) -> None:
        if getattr(self, "_publication_guard_error", None):
            raise SelectorError(
                "TOUTIAO_LOCAL_REQUEST_BLOCKED: 原稿需要未获准的写操作；"
                "本地拦截后停止，不能据此判断断网或掉登录"
            )
        if (
            self.page.url != self._publication_edit_url
            or (await self.page.locator(TITLE).input_value()).strip() != self._publication_title
            or await self._read_editor_tokens() != self._expected_persisted_tokens
        ):
            raise SelectorError("头条发布前原稿标题或完整图文发生变化")
        await self.page.wait_for_function(
            "s => [...document.querySelectorAll(s+' img')]"
            ".every(e=>e.complete && e.naturalWidth>0)",
            arg=BODY,
            timeout=15000,
        )

    async def _prepare_publication_settings(self) -> None:
        """The currently verified publication contract is ordinary, immediate, no cover."""
        await self.actions.perform(self.page.bring_to_front)
        cover = self.page.locator("label.byte-radio").filter(has_text="无封面")
        if await cover.count() != 1:
            raise SelectorError("头条无封面控件不唯一")
        if not await cover.locator(".byte-radio-inner.checked").count():
            await self.actions.perform(cover.click, timeout=8000)
        transfer = self.page.locator("label.byte-checkbox").filter(has_text="发布得更多收益")
        if await transfer.count() == 1 and await transfer.locator("input").is_checked():
            await self.actions.perform(transfer.click, timeout=8000)
        await self._assert_publication_settings()

    async def _assert_publication_settings(self) -> None:
        selected = await self.page.locator(
            "label.byte-radio:has(.byte-radio-inner.checked)"
        ).all_text_contents()
        checks = await self.page.locator("label.byte-checkbox input:checked").count()
        if [s.strip() for s in selected] != ["无封面"] or checks:
            raise SelectorError("头条发布设置必须为无封面、无首发声明、无额外同步")

    async def _publication_payload_matches(self, data: dict, *, stage: str) -> bool:
        required = {
            "article_type": "0",
            "pgc_id": self._publication_id,
            "source": "29",
            "title": self._publication_title,
            "title_id": "",
            "is_refute_rumor": "0",
            "save": "0" if stage == "preview" else "1",
            "entrance": "" if stage == "preview" else "main",
            "timer_status": "0",
            "educluecard": "",
            "article_ad_type": self._publication_ad_type,
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
        }
        if stage == "preview":
            required["is_app_preview"] = "1"
        allowed = set(required) | {
            "extra",
            "content",
            "search_creation_info",
            "mp_editor_stat",
            "timer_time",
            "draft_form_data",
            "pgc_feed_covers",
            "ic_uri_list",
            "appid_list",
            "stock_ids",
            "concern_list",
        }
        if (
            set(data) - allowed
            or any(data.get(k) != v for k, v in required.items())
            or any(data.get(k) for k in ("ic_uri_list", "appid_list", "stock_ids", "concern_list"))
            or json.loads(data.get("draft_form_data", "null")) != {"coverType": 1}
            or json.loads(data.get("pgc_feed_covers", "null")) != []
        ):
            return False
        search = json.loads(data.get("search_creation_info", "null"))
        if search != {"searchTopOne": 0, "abstract": "", "clue_id": ""}:
            return False
        extra = json.loads(data.get("extra", "null"))
        if (
            not isinstance(extra, dict)
            or set(extra)
            - {
                "content_source",
                "content_word_cnt",
                "is_multi_title",
                "sub_titles",
                "gd_ext",
                "tuwen_wtt_trans_flag",
                "info_source",
            }
            or extra.get("content_source") != 100000000402
            or type(extra.get("content_word_cnt")) is not int
            or extra["content_word_cnt"] <= 0
            or extra.get("is_multi_title") != 0
            or extra.get("sub_titles") != []
            or extra.get("tuwen_wtt_trans_flag") != "0"
            or stage == "submit"
            and extra.get("info_source") != {"source_type": -1}
            or stage == "preview"
            and extra.get("info_source") not in (None, {"source_type": -1})
        ):
            return False
        await self._assert_publication_content()
        await self._assert_publication_settings()
        return (
            await self._publication_html_tokens(data.get("content", ""))
            == self._expected_persisted_tokens
        )

    async def _native_publication_step(self, name: str, stage: str) -> None:
        button = self.page.get_by_role("button", name=name, exact=True)
        if await button.count() != 1 or not await button.is_enabled():
            raise SelectorError("头条原生发布按钮不唯一或不可用")
        self._publication_stage = stage
        try:
            async with self.page.expect_response(
                lambda r: (
                    r.request.method == "POST"
                    and urlsplit(r.url).netloc == "mp.toutiao.com"
                    and urlsplit(r.url).path == SUBMIT_PATH
                    and native_payload(r.request).get("save")
                    == ("0" if stage == "preview" else "1")
                    and (
                        stage != "preview" or native_payload(r.request).get("is_app_preview") == "1"
                    )
                ),
                timeout=self.actions.event_timeout(self.PUBLICATION_TIMEOUT_MS),
            ) as pending:
                await self.actions.perform(button.click, timeout=8000)
            response = await pending.value
            result = await response.json()
            self._record_publication_network(
                "PLATFORM_ACK"
                if type(result.get("code")) is int and result["code"] == 0
                else "PLATFORM_BUSINESS_ERROR",
                "ARTICLE_SUBMISSION",
                http_status=response.status,
                # Unrecognized response values must not leak into diagnostic logs.
                platform_code=result.get("code") if type(result.get("code")) is int else None,
            )
            self._publication_last_reply = {
                "stage": stage,
                "http_status": response.status,
                "code": result.get("code"),
                "article_id_matched": native_receipt_id(result) == self._publication_id,
            }
            if (
                not response.ok
                or type(result.get("code")) is not int
                or result["code"] != 0
                or native_receipt_id(result) != self._publication_id
            ):
                raise ValueError("native acknowledgement not verified")
        finally:
            self._publication_stage = "blocked"

    async def publish_now(self, title: str = "") -> dict:
        if getattr(self, "_public_publish_attempted", False):
            raise PublishResultUnknownError("头条已尝试发布，不重复操作")
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
                raise SelectorError("头条发布缺少完整原草稿证据")
            await self.prepare_existing_publication(title, evidence.draft_url)
        if self._normalize_platform_title(title) != self._publication_title:
            raise SelectorError("头条发布标题与原稿不一致")
        await self._assert_publication_content()
        await self._prepare_publication_settings()
        self._public_publish_attempted = True
        try:
            await self._native_publication_step("预览并发布", "preview")
            await self.page.get_by_role("button", name="确认发布", exact=True).wait_for(
                state="visible",
                timeout=15000,
            )
            await self._assert_publication_content()
            await self._assert_publication_settings()
            await self._native_publication_step("确认发布", "submit")
            if self._publication_preview_sent != 1 or self._publication_post_sent != 1:
                raise ValueError("native submission count not verified")
            return {
                "status": "SUBMITTED",
                "platform_article_id": self._publication_id,
                "verification_evidence": {
                    "submit_acknowledged": True,
                    "submission_scope": "PUBLIC",
                    "submission_source": "toutiao_publish_response",
                    "submission_article_id": self._publication_id,
                },
            }
        except Exception as exc:
            if not (self._publication_preview_sent or self._publication_post_sent):
                raise SelectorError("头条保存和发布请求均未发送，本地检查已停止") from exc
            raise PublishResultUnknownError(
                "头条已发送保存或发布但后续未确认，停止且不重试"
            ) from exc
        finally:
            self._publication_stage = "blocked"
