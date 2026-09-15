"""微博账号会话与头条文章草稿适配器。

登录态通过持久 Profile 验证；按冻结图文块顺序写入 TipTap，保存后重开
同一 draft ID 复核图文。公开发布另核对封面和阅读范围，单次保存/提交。

真实登录页（https://passport.weibo.com/sso/signin?entry=miniblog...）：
- 「扫描二维码登录」为默认 Tab，二维码为约 140x140 的 img（v2.qr.weibo.cn）。
- 登录成功信号：扫码确认后页面从 passport 跳转回 weibo.com。
- 身份接口带签名/cookie 约束，裸 fetch 不可靠；只读取登录配置或顶部导航
  的唯一当前用户，禁止从内容流用户卡片猜测账号。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    DraftBaselineError,
    DraftResultUnknownError,
    DraftVerificationEvidence,
    LoginRequiredError,
    PlatformAutomationError,
    PublishResultUnknownError,
    SelectorError,
)
from platforms.content_validation import (
    ContentValidationError,
    ensure_valid_content,
    extract_expected_paragraphs,
    normalize_for_comparison,
    safe_media_error,
)
from platforms.media_progress import safe_media_progress

LOGIN_URL = (
    "https://passport.weibo.com/sso/signin?entry=miniblog"
    "&source=miniblog&disp=popup&url=https%3A%2F%2Fweibo.com%2F"
)
HOME_URL = "https://weibo.com/"
DRAFTS_URL = "https://card.weibo.com/article/v5/editor#/draft"
DRAFT_HASH_PATTERN = re.compile(r"^/draft/(?P<draft_id>[1-9][0-9]*)$")
BODY_SELECTOR = "div.tiptap.ProseMirror:visible"
BODY_IMAGE_ICON_FINGERPRINT = (
    "81fecffe5f54bf65524a7465730d595b95fbf6be95d1bb792f1682118b03d00a"
)
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name", "nick"}
IDENTITY_UID_KEYS = {"uid", "user_id"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class WeiboPlatform(BasePlatform):
    """微博账号会话与 fail-closed 草稿投递适配器。"""

    platform_name = "weibo"
    # 游客也有 SUB/SUBP/WBPSESS；SCF/ALF/SSOLoginState 仅登录后存在。
    SESSION_COOKIE_NAMES = frozenset({"SCF", "ALF", "SSOLoginState"})
    LOGIN_POLL_ATTEMPTS = 40
    LOGIN_POLL_INTERVAL_SECONDS = 3

    def __init__(
        self,
        *,
        resume_existing_title: str | None = None,
        resume_existing_draft_id: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None
        self._resume_existing_title = str(resume_existing_title or "").strip()
        self._resume_existing_draft_id = str(resume_existing_draft_id or "").strip()
        self._editing_existing_draft = False
        self._preflight_title = ""
        self._active_draft_id = ""
        self._preflight_draft_ids_by_title: dict[str, frozenset[str]] | None = None
        self._preflight_all_draft_ids: frozenset[str] | None = None
        self._expected_persisted_blocks: list[dict] | None = None
        self._expected_persisted_image_count = 0
        self._media_progress_state: dict | None = None

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None
        self._editing_existing_draft = False
        self._preflight_title = ""
        self._active_draft_id = ""
        self._preflight_draft_ids_by_title = None
        self._preflight_all_draft_ids = None
        self._expected_persisted_blocks = None
        self._expected_persisted_image_count = 0
        self._media_progress_state = None

    # ==================== 登录态与身份 ====================

    async def _has_session_cookie_signal(self) -> bool:
        """登录会话 cookie（SCF/ALF/SSOLoginState）作为登录成功信号。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies([
                "https://weibo.com/",
                "https://www.weibo.com/",
                "https://weibo.cn/",
            ])
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def _on_home(self) -> bool:
        """是否已离开 passport 并处于微博首页（登录成功信号）。"""

        try:
            url = self.page.url or ""
            return "passport.weibo.com" not in url and "weibo.com" in url
        except Exception:
            return False

    async def check_login(self) -> bool:
        """只读验证现有 Profile；首页身份确认成功才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("微博登录态检测")
            await self.actions.perform(
                self.page.goto,
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(4)
            if await self._has_session_cookie_signal():
                identity = await self.fetch_identity_payload()
                if identity.get("ok"):
                    return True
                self.last_login_error = (
                    "WEIBO_IDENTITY_MISSING: 微博会话存在但身份未确认"
                )
                return False
            self.last_login_error = await self.login_obstacle_code()
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "WEIBO_LOGIN_CHECK_ERROR: 微博登录态验证失败"
            return False

    async def login(self):
        """打开微博扫码登录页，等待用户在隔离 Profile 中扫码登录。"""

        self._require_page_alive("微博打开登录页")
        await self.actions.perform(
            self.page.goto,
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        try:
            await self.page.wait_for_function(
                """() => {
                    const imgs = Array.from(
                        document.querySelectorAll('img')
                    );
                    return imgs.some((i) => {
                        const src = i.src || '';
                        return src.includes('qr.weibo.cn')
                            && i.naturalWidth > 100 && i.naturalWidth < 260;
                    });
                }""",
                timeout=20000,
            )
        except Exception:
            pass
        await self._show_scan_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("微博等待登录")
            if await self._on_home() or await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: 微博登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_scan_hint(self):
        """页面顶部显示扫码提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'wb-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#ff8200;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请用微博 App 扫码登录'
                        + '<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """返回微博导航区同源确认的当前账号，绝不扫描内容流用户。"""

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            identity = await self.page.evaluate(
                r"""() => {
                    const pairs = [];
                    const add = (value) => {
                        if (!value || typeof value !== 'object') return;
                        const uid = String(
                            value.uid || value.user_id || value.id || ''
                        ).trim();
                        const displayName = String(
                            value.screen_name || value.nickname || value.nick
                            || value.name || ''
                        ).trim();
                        if (/^\d+$/.test(uid) && displayName) {
                            pairs.push({user_id: uid, display_name: displayName});
                        }
                    };
                    for (const root of [
                        window.$CONFIG,
                        window.__INITIAL_STATE__,
                        window.__WB_STATE__,
                    ]) {
                        add(root);
                        add(root && root.user);
                        add(root && root.loginUser);
                        add(root && root.account);
                    }
                    const unique = Array.from(new Map(
                        pairs.map((item) => [
                            item.user_id + '\u0000' + item.display_name,
                            item,
                        ])
                    ).values());
                    if (unique.length === 1) return unique[0];
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                            && rect.y >= -10 && rect.y < 140
                            && style.display !== 'none'
                            && style.visibility !== 'hidden';
                    };
                    const nav = Array.from(
                        document.querySelectorAll('a[href*="/u/"]')
                    ).filter(visible).map((node) => {
                        const match = (node.getAttribute('href') || '')
                            .match(/\/u\/(\d+)/);
                        return {
                            user_id: match ? match[1] : '',
                            display_name: (
                                node.getAttribute('title')
                                || node.getAttribute('aria-label')
                                || node.innerText || ''
                            ).trim(),
                        };
                    }).filter((item) => item.user_id && item.display_name);
                    const uniqueNav = Array.from(new Map(
                        nav.map((item) => [
                            item.user_id + '\u0000' + item.display_name,
                            item,
                        ])
                    ).values());
                    return uniqueNav.length === 1
                        ? uniqueNav[0]
                        : {user_id: '', display_name: ''};
                }"""
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博身份确认时页面已关闭"
                ) from exc
            return {"ok": False, "user_id": "", "display_name": ""}
        if not isinstance(identity, dict):
            return {"ok": False, "user_id": "", "display_name": ""}
        user_id = str(identity.get("user_id") or "").strip()
        display_name = str(identity.get("display_name") or "").strip()
        if not user_id or not display_name:
            return {"ok": False, "user_id": "", "display_name": ""}
        self._identity_payload = {
            "ok": True,
            "user_id": user_id,
            "display_name": display_name,
        }
        return dict(self._identity_payload)

    @staticmethod
    def _extract_identity_from_json(payload) -> tuple[str, str] | None:
        """递归扫描响应 JSON，寻找同时含昵称与 ID 的最小身份对象。"""

        if not isinstance(payload, dict):
            return None
        found: list[tuple[str, str]] = []

        def walk(node) -> None:
            if isinstance(node, dict):
                name = next(
                    (str(node[key]) for key in IDENTITY_NAME_KEYS if node.get(key)),
                    "",
                )
                uid = next(
                    (str(node[key]) for key in IDENTITY_UID_KEYS if node.get(key)),
                    "",
                )
                if name and uid:
                    found.append((uid, name))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(payload)
        return found[0] if found else None

    # ==================== 草稿投递链路 ====================

    @staticmethod
    def _not_implemented(operation: str):
        raise PlatformNotImplementedError(
            f"PLATFORM_NOT_IMPLEMENTED: 微博{operation}能力尚未接入"
        )

    @staticmethod
    def _normalize_draft_title(title: object) -> str:
        return str(title or "").strip()

    @classmethod
    def _draft_id_from_editor_url(cls, url: object) -> str:
        """只接受微博头条文章的规范 HTTPS 草稿编辑地址。"""

        parts = urlsplit(str(url or "").strip())
        if (
            parts.scheme != "https"
            or parts.netloc.lower() != "card.weibo.com"
            or parts.path.rstrip("/") != "/article/v5/editor"
            or parts.query
        ):
            return ""
        match = DRAFT_HASH_PATTERN.fullmatch(parts.fragment)
        return match.group("draft_id") if match else ""

    @classmethod
    def _draft_editor_url(cls, draft_id: object) -> str:
        safe_id = str(draft_id or "").strip()
        if not re.fullmatch(r"[1-9][0-9]*", safe_id):
            return ""
        return f"https://card.weibo.com/article/v5/editor#/draft/{safe_id}"

    async def _current_draft_id(self) -> str:
        draft_id = str(
            await self.page.evaluate(
                r"""() => {
                    const match = location.hash.match(/^#\/draft\/([1-9]\d*)$/);
                    return match ? match[1] : '';
                }"""
            )
            or ""
        )
        return draft_id if re.fullmatch(r"[1-9][0-9]*", draft_id) else ""

    async def _open_draft_list(self) -> None:
        await self.actions.perform(
            self.page.goto,
            DRAFTS_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await self.page.wait_for_function(
            """() => Array.from(document.querySelectorAll('*')).some((node) =>
                (node.innerText || '').trim() === '写文章'
                && node.children.length <= 3
            )""",
            timeout=15000,
        )

    async def _read_visible_draft_cards(self) -> list[dict[str, object]]:
        """读取可见草稿卡的标题和受信任编辑 URL，不读取正文摘要。"""

        raw_cards = await self.page.evaluate(
            r"""() => {
                const visible = (node) => {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none'
                        && style.visibility !== 'hidden';
                };
                const exactDraftUrl = (href) => {
                    try {
                        const url = new URL(href, location.href);
                        return url.origin === 'https://card.weibo.com'
                            && url.pathname.replace(/\/$/, '') === '/article/v5/editor'
                            && !url.search
                            && /^#\/draft\/[1-9]\d*$/.test(url.hash)
                            ? url.href
                            : '';
                    } catch (_error) {
                        return '';
                    }
                };
                return Array.from(document.querySelectorAll('.list-item'))
                    .filter(visible)
                    .map((card, cardIndex) => {
                        const title = (card.innerText || '')
                            .split(/\r?\n/, 1)[0].trim();
                        const linkNodes = [
                            ...(card.matches('a[href]') ? [card] : []),
                            ...card.querySelectorAll('a[href]'),
                        ];
                        const editUrls = Array.from(new Set(
                            linkNodes.map((node) => exactDraftUrl(
                                node.getAttribute('href') || ''
                            )).filter(Boolean)
                        ));
                        return {card_index: cardIndex, title, edit_urls: editUrls};
                    });
            }"""
        )
        if not isinstance(raw_cards, list):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博草稿卡结构无法读取"
            )

        cards: list[dict[str, object]] = []
        title_occurrences: dict[str, int] = {}
        for raw in raw_cards:
            if not isinstance(raw, dict):
                raise DraftBaselineError(
                    "DRAFT_BASELINE_FAILED: 微博草稿卡结构无效"
                )
            title = self._normalize_draft_title(raw.get("title"))
            urls = raw.get("edit_urls")
            if not isinstance(urls, list):
                raise DraftBaselineError(
                    "DRAFT_BASELINE_FAILED: 微博草稿卡编辑地址结构无效"
                )
            draft_ids = {
                self._draft_id_from_editor_url(url)
                for url in urls
                if self._draft_id_from_editor_url(url)
            }
            if len(draft_ids) > 1:
                raise DraftBaselineError(
                    "DRAFT_BASELINE_FAILED: 微博单个草稿卡包含冲突 ID"
                )
            occurrence = title_occurrences.get(title, 0)
            title_occurrences[title] = occurrence + 1
            cards.append(
                {
                    "title": title,
                    "title_occurrence": occurrence,
                    "draft_id": next(iter(draft_ids), ""),
                }
            )
        return cards

    async def _open_draft_card_for_id(
        self,
        *,
        title: str,
        title_occurrence: int,
    ) -> str:
        clicked = await self.actions.perform(self.page.evaluate, r"""(target) => {
                const visible = (node) => {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none'
                        && style.visibility !== 'hidden';
                };
                const matches = Array.from(document.querySelectorAll('.list-item'))
                    .filter((card) => visible(card)
                        && (card.innerText || '').split(/\r?\n/, 1)[0].trim()
                            === target.title);
                const card = matches[target.occurrence];
                if (!card) return false;
                card.click();
                return true;
            }""", {"title": title, "occurrence": title_occurrence})
        if not clicked:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博草稿卡无法按标题序号打开"
            )
        await self.page.wait_for_function(
            r"""() => /^#\/draft\/[1-9]\d*$/.test(location.hash)""",
            timeout=20000,
        )
        await self.page.wait_for_selector(
            "textarea[placeholder='请输入标题']",
            state="visible",
            timeout=20000,
        )
        draft_id = await self._current_draft_id()
        title_field = self.page.locator(
            "textarea[placeholder='请输入标题']"
        ).first
        if (
            not draft_id
            or self._normalize_draft_title(await title_field.input_value()) != title
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博草稿卡打开后的 ID 或标题不一致"
            )
        return draft_id

    async def _collect_draft_ids_by_title(
        self,
        *,
        only_title: str | None = None,
        guard_mutations: bool = False,
    ) -> dict[str, frozenset[str]]:
        """将草稿标题映射到稳定 ID；同名实体保留为 ID 集合。"""

        route_registered = False

        async def _readonly_guard(route, request) -> None:
            method = str(request.method or "").upper()
            if method not in {"GET", "HEAD", "OPTIONS"}:
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        try:
            if guard_mutations:
                await self.page.route("**/*", _readonly_guard)
                route_registered = True
            await self._open_draft_list()
            initial_cards = await self._read_visible_draft_cards()
            if only_title is not None:
                normalized_only_title = self._normalize_draft_title(only_title)
                initial_cards = [
                    card
                    for card in initial_cards
                    if card.get("title") == normalized_only_title
                ]

            by_title: dict[str, set[str]] = {}
            seen_ids: set[str] = set()
            navigated = False
            for card in initial_cards:
                title = str(card.get("title") or "")
                draft_id = str(card.get("draft_id") or "")
                if not draft_id:
                    await self._open_draft_list()
                    current_cards = await self._read_visible_draft_cards()
                    matching_cards = [
                        current
                        for current in current_cards
                        if current.get("title") == title
                    ]
                    occurrence = int(card.get("title_occurrence") or 0)
                    if occurrence >= len(matching_cards):
                        raise DraftBaselineError(
                            "DRAFT_BASELINE_FAILED: 微博草稿列表读取期间结构发生变化"
                        )
                    current = matching_cards[occurrence]
                    current_id = str(current.get("draft_id") or "")
                    draft_id = current_id or await self._open_draft_card_for_id(
                        title=title,
                        title_occurrence=occurrence,
                    )
                    navigated = navigated or not current_id
                if not re.fullmatch(r"[1-9][0-9]*", draft_id):
                    raise DraftBaselineError(
                        "DRAFT_BASELINE_FAILED: 微博草稿卡缺少稳定数字 ID"
                    )
                if draft_id in seen_ids:
                    raise DraftBaselineError(
                        "DRAFT_BASELINE_FAILED: 微博多个草稿卡映射到同一 ID"
                    )
                seen_ids.add(draft_id)
                by_title.setdefault(title, set()).add(draft_id)
            if navigated:
                await self._open_draft_list()
            return {
                title: frozenset(draft_ids)
                for title, draft_ids in by_title.items()
            }
        finally:
            if route_registered:
                try:
                    await self.page.unroute("**/*", _readonly_guard)
                except Exception:  # noqa: BLE001
                    pass

    async def preflight_delivery(self, title: str) -> None:
        """冻结标题和 title→IDs 基线；同名草稿不再阻止新建或恢复。"""

        self._require_page_alive("微博草稿基线检查")
        expected_title = self._normalize_draft_title(title)
        if not expected_title:
            raise DraftBaselineError("DRAFT_BASELINE_FAILED: 微博标题不能为空")
        if len(expected_title) > 32:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博头条文章标题不能超过 32 个字符"
            )
        try:
            baseline = await self._collect_draft_ids_by_title(
                only_title=expected_title,
                guard_mutations=True,
            )
        except DraftBaselineError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博草稿基线检查时页面已关闭"
                ) from exc
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博无法确认草稿 ID 基线"
            ) from exc

        self._preflight_title = expected_title
        self._preflight_draft_ids_by_title = baseline
        self._preflight_all_draft_ids = frozenset(
            draft_id
            for draft_ids in baseline.values()
            for draft_id in draft_ids
        )
        self._editing_existing_draft = False
        wants_resume = bool(
            self._resume_existing_title or self._resume_existing_draft_id
        )
        if wants_resume:
            resume_title = self._normalize_draft_title(self._resume_existing_title)
            resume_id = str(self._resume_existing_draft_id or "").strip()
            if (
                expected_title != resume_title
                or not re.fullmatch(r"[1-9][0-9]*", resume_id)
                or resume_id not in baseline.get(expected_title, frozenset())
            ):
                raise DraftBaselineError(
                    "DRAFT_BASELINE_FAILED: 微博待恢复草稿 ID 不在冻结标题基线中"
                )
            await self._open_resumed_draft(expected_title, resume_id)
            self._editing_existing_draft = True
            return

    async def _open_resumed_draft(
        self,
        expected_title: str,
        expected_draft_id: str,
    ) -> None:
        """按冻结 draft ID 直接恢复，不要求标题在列表中唯一。"""

        editor_url = self._draft_editor_url(expected_draft_id)
        if not editor_url:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博待恢复草稿 ID 无效"
            )
        await self.actions.perform(
            self.page.goto,
            editor_url,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await self.page.wait_for_function(
            r"""(draftId) => location.hash === `#/draft/${draftId}`""",
            arg=expected_draft_id,
            timeout=20000,
        )
        await self.page.wait_for_selector(
            "textarea[placeholder='请输入标题']",
            state="visible",
            timeout=20000,
        )
        await self.page.wait_for_selector(
            BODY_SELECTOR,
            state="visible",
            timeout=20000,
        )
        draft_id = await self._current_draft_id()
        if draft_id != expected_draft_id:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博待恢复草稿 ID 与验收门不一致"
            )
        title_field = self.page.locator(
            "textarea[placeholder='请输入标题']"
        ).first
        if str(await title_field.input_value()).strip() != expected_title:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博待恢复草稿标题回读不一致"
            )
        self._active_draft_id = draft_id

    async def navigate_to_editor(self):
        """打开微博头条文章编辑器：草稿箱视图 → 真实点击「写文章」。

        2026-08 实测：必须用真实鼠标点击「写文章」才会创建草稿并切换到
        ``#/draft/{id}`` 视图（JS 模拟点击不会触发导航，导致保存草稿时
        id 为空、服务端返回参数错误）。创建后标题/正文字段即可填写。
        """
        self._require_page_alive("微博打开编辑器")
        if self._editing_existing_draft:
            await self._verify_current_resumed_draft()
            return
        if (
            not self._preflight_title
            or self._preflight_draft_ids_by_title is None
            or self._preflight_all_draft_ids is None
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博缺少草稿 ID 基线"
            )
        create_started = False
        try:
            await self.actions.perform(
                self.page.goto,
                DRAFTS_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.page.wait_for_function(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    return nodes.some((el) => {
                        const txt = (el.innerText || '').trim();
                        return txt === '写文章' && el.children.length <= 3;
                    });
                }""",
                timeout=15000,
            )
            before_draft_id = str(
                await self.page.evaluate(
                    r"""() => {
                        const match = location.hash.match(/^#\/draft\/([1-9]\d*)$/);
                        return match ? match[1] : '';
                    }"""
                )
                or ""
            )
            write_buttons = self.page.get_by_role(
                "button",
                name="写文章",
                exact=True,
            )
            if await write_buttons.count() != 1 or not await write_buttons.is_visible():
                raise DraftBaselineError(
                    "DRAFT_BASELINE_FAILED: 微博写文章按钮不唯一或不可见"
                )

            async with self.page.expect_response(
                lambda response: (
                    str(response.request.method or "").upper() == "POST"
                    and urlsplit(str(response.url or "")).path
                    == "/article/v5/aj/editor/draft/create"
                ),
                timeout=self.actions.event_timeout(20000),
            ) as response_info:
                create_started = True
                await self.actions.perform(write_buttons.click, timeout=10000)
            create_response = await response_info.value
            if not 200 <= int(create_response.status) < 300:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博新草稿创建请求未返回成功状态，禁止重试"
                )

            # 草稿页会默认打开最近一篇旧稿；只有创建接口成功且 hash 切换到
            # 一个不同的正整数 ID，才能证明当前编辑器属于本次新草稿。
            await self.page.wait_for_function(
                r"""(beforeId) => {
                    const match = location.hash.match(/^#\/draft\/([1-9]\d*)$/);
                    return Boolean(match && match[1] !== beforeId);
                }""",
                arg=before_draft_id,
                timeout=20000,
            )
            await self.page.wait_for_selector(
                "textarea[placeholder='请输入标题']",
                state="visible",
                timeout=20000,
            )
            try:
                await self.page.wait_for_selector(
                    ".wb-editor-spin, .n-spin-body",
                    state="detached",
                    timeout=20000,
                )
            except Exception:
                pass
            draft_id = await self._current_draft_id()
            if (
                not draft_id
                or draft_id == before_draft_id
                or draft_id in self._preflight_all_draft_ids
            ):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博创建响应未绑定基线之外的新草稿 ID"
                )
            self._active_draft_id = draft_id
            editor_state = await self.page.evaluate(
                """(selector) => {
                    const title = document.querySelector(
                        "textarea[placeholder='请输入标题']"
                    );
                    const body = document.querySelector(selector);
                    return {
                        title: title ? title.value : null,
                        body: body ? (body.innerText || '') : null,
                    };
                }""",
                BODY_SELECTOR.removesuffix(":visible"),
            )
            if not isinstance(editor_state, dict):
                raise DraftBaselineError(
                    "DRAFT_BASELINE_FAILED: 微博新草稿编辑器状态不可读"
                )
            if normalize_for_comparison(editor_state.get("title")) or (
                normalize_for_comparison(editor_state.get("body"))
            ):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博新草稿已创建但编辑器不是空白状态，禁止重试"
                )
        except (DraftBaselineError, DraftResultUnknownError):
            raise
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                if create_started:
                    raise DraftResultUnknownError(
                        "DRAFT_RESULT_UNKNOWN: 微博创建草稿后页面关闭，禁止重试"
                    ) from exc
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博打开编辑器时页面已关闭"
                ) from exc
            if create_started:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博创建草稿后无法证明新编辑器状态，禁止重试"
                ) from exc
            raise SelectorError("微博头条文章编辑器未找到标题输入框") from exc

    async def _verify_current_resumed_draft(self) -> None:
        """任何覆盖写入前再次证明页面仍是验收门绑定的那一篇草稿。"""

        expected_id = self._active_draft_id
        baseline = self._preflight_draft_ids_by_title
        if (
            not self._preflight_title
            or baseline is None
            or expected_id not in baseline.get(
                self._preflight_title,
                frozenset(),
            )
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博现有草稿恢复基线不完整"
            )
        current_id = await self._current_draft_id()
        title_field = self.page.locator(
            "textarea[placeholder='请输入标题']"
        ).first
        editor = self.page.locator(BODY_SELECTOR).first
        if (
            current_id != expected_id
            or await title_field.count() != 1
            or not await title_field.is_visible()
            or str(await title_field.input_value()).strip() != self._preflight_title
            or await editor.count() != 1
            or not await editor.is_visible()
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博当前页面不是验收门绑定的现有草稿"
            )

    async def fill_title(self, title: str):
        """填写微博头条文章标题（textarea，placeholder「请输入标题」，0/32）。"""

        self._require_page_alive("微博填写标题")
        expected_title = str(title or "").strip()
        if expected_title != self._preflight_title:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博标题与预检冻结标题不一致"
            )
        if self._editing_existing_draft:
            await self._verify_current_resumed_draft()
            return
        title_field = self.page.locator("textarea[placeholder='请输入标题']").first
        try:
            if await title_field.count() == 0 or not await title_field.is_visible():
                raise RuntimeError("标题输入框不可见")
            # 直接 fill（不依赖 click，避免加载遮罩拦截命中）
            await self.actions.fill(title_field, expected_title)
            if str(await title_field.input_value()).strip() != expected_title:
                raise RuntimeError("标题回读不一致")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                if self._active_draft_id:
                    raise DraftResultUnknownError(
                        "DRAFT_RESULT_UNKNOWN: 微博新草稿已创建但填写标题时页面关闭，禁止重试"
                    ) from exc
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博填写标题时页面已关闭"
                ) from exc
            if self._active_draft_id:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博新草稿已创建但标题未能完整验证，禁止重试"
                ) from exc
            logger.error("微博标题填写失败: {}", exc)
            raise SelectorError("微博标题输入框未找到或填写失败") from exc

    async def fill_content(self, content_blocks: list, images: list):
        """把创建后所有正文异常统一升级为结果未知，禁止重复建稿。"""

        try:
            return await self._fill_content_impl(content_blocks, images)
        except DraftResultUnknownError:
            raise
        except Exception as exc:
            if not self._active_draft_id:
                raise
            media_error_code = str(
                getattr(exc, "media_error_code", "WEIBO_CONTENT_WRITE_FAILED")
            )
            error = DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 微博当前草稿正文未完整验证，"
                "平台可能已自动保存部分内容，禁止重试；"
                f"stage={media_error_code}"
            )
            error.media_error_code = media_error_code
            self._attach_media_progress(error)
            raise error from exc

    async def _fill_content_impl(self, content_blocks: list, images: list):
        """按冻结 ContentVersion 的原始顺序写入微博 TipTap 图文。"""

        self._require_page_alive("微博填写正文")
        expected_images = self._validate_delivery_blocks(content_blocks, images)
        if self._editing_existing_draft:
            await self._verify_current_resumed_draft()
        self._media_progress_state = {
            "expected_images": expected_images,
            "uploaded_images": 0,
            "failed_image_count": 0,
            "media_status": "not_required" if expected_images == 0 else "in_progress",
        }
        editor = await self._current_body_editor()
        try:
            try:
                await self.actions.perform(editor.click, timeout=5000)
            except Exception:
                await self.actions.perform(editor.evaluate, "(el) => el.focus()")
            focused = await self.page.evaluate(
                """() => {
                    const el = document.activeElement;
                    return el ? el.isContentEditable : false;
                }"""
            )
            if not focused:
                raise RuntimeError("正文编辑器未能获得焦点")
            # 标题填充可能触发编辑器重渲染，等它稳定再输入
            await self.simulator.random_delay(1, 2)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("微博正文编辑器未找到或无法聚焦") from exc

        try:
            await self.actions.perform(self.page.keyboard.press, "Control+A")
            await self.actions.perform(self.page.keyboard.press, "Backspace")
        except Exception:
            pass
        await self.simulator.random_delay(0.3, 0.8)

        uploaded_images = 0
        failed_images: list[dict[str, str]] = []
        content_started = False
        paragraph_ready_after_image = False
        for block_index, block in enumerate(content_blocks):
            if not isinstance(block, dict):
                raise ContentValidationError(
                    "WEIBO_CONTENT_CONTRACT_INVALID: 正文块无效"
                )
            block_type = block.get("type")
            if block_type in {"text", "heading"}:
                text = str(block.get("text") or "").strip()
                if not text:
                    continue
                if content_started:
                    await self._place_body_caret_at_end()
                    if paragraph_ready_after_image:
                        paragraph_ready_after_image = False
                    else:
                        await self.actions.perform(self.page.keyboard.press, "Enter")
                await self._place_body_caret_at_end()
                lines = text.splitlines() or [text]
                for line_index, line in enumerate(lines):
                    if line.strip():
                        await self.actions.insert_text(self.page.keyboard, line.strip())
                    if line_index < len(lines) - 1:
                        await self.actions.perform(self.page.keyboard.press, "Enter")
                if block_type == "heading":
                    await self._apply_h2_to_current_block()
                content_started = True
                continue

            if block_type != "image":
                raise ContentValidationError(
                    "WEIBO_CONTENT_CONTRACT_INVALID: 未知正文块类型"
                )
            if content_started:
                await self._place_body_caret_at_end()
                if paragraph_ready_after_image:
                    paragraph_ready_after_image = False
                else:
                    await self.actions.perform(self.page.keyboard.press, "Enter")
            await self._place_body_caret_at_end()
            image_path = self._image_path_for_block(block, images)
            if not image_path:
                raise ContentValidationError(
                    "WEIBO_CONTENT_CONTRACT_INVALID: 图片块没有唯一受控文件"
                )
            upload_result = await self._upload_image(image_path) or {}
            if not upload_result.get("success"):
                failed_images.append(
                    {
                        "filename": Path(image_path).name,
                        "error_code": str(
                            upload_result.get("error_code")
                            or "PLATFORM_MEDIA_INCOMPLETE"
                        ),
                        "error": safe_media_error(
                            upload_result.get("error"),
                            fallback="图片上传失败",
                        ),
                    }
                )
                self._update_media_progress(
                    expected_images,
                    uploaded_images,
                    len(failed_images),
                )
                media_error_code = str(failed_images[-1]["error_code"])
                error = ContentValidationError(
                    "WEIBO_MEDIA_INCOMPLETE: 微博正文图片未完整写入，已停止且禁止自动重试"
                )
                error.media_error_code = media_error_code
                self._attach_media_progress(error)
                raise error
            uploaded_images += 1
            self._update_media_progress(
                expected_images,
                uploaded_images,
                len(failed_images),
            )
            await self._create_paragraph_after_image()
            paragraph_ready_after_image = True
            try:
                await self._validate_dom_prefix(
                    content_blocks[: block_index + 1],
                    phase=f"图片处理后第{block_index + 1}块",
                )
            except ContentValidationError as exc:
                self._attach_media_progress(exc)
                raise
            await self.simulator.random_delay(2.5, 4.5)
            content_started = True

        editor = await self._current_body_editor()
        actual_text = await editor.inner_text()
        expected_count = ensure_valid_content(
            content_blocks,
            actual_text,
            platform="微博",
            phase="图文处理后",
        )
        try:
            await self._validate_dom_exact(content_blocks, phase="正文最终")
        except ContentValidationError as exc:
            self._attach_media_progress(exc)
            raise
        logger.info("微博正文输入并最终验证成功: {} 个文本段落", expected_count)

        if expected_images == 0:
            media_status = "not_required"
        else:
            media_status = "completed"
        self._expected_persisted_blocks = copy.deepcopy(content_blocks)
        self._expected_persisted_image_count = expected_images

        return {
            "text_ok": True,
            "expected_images": expected_images,
            "uploaded_images": uploaded_images,
            "failed_images": failed_images,
            "media_status": media_status,
            "media_error": None,
            "media_error_code": None,
        }

    def _validate_delivery_blocks(self, blocks: list, images: list[dict]) -> int:
        if not isinstance(blocks, list) or not blocks:
            raise ContentValidationError(
                "WEIBO_CONTENT_CONTRACT_INVALID: 正文块不能为空"
            )
        expected_images = 0
        for block in blocks:
            if not isinstance(block, dict):
                raise ContentValidationError(
                    "WEIBO_CONTENT_CONTRACT_INVALID: 正文块无效"
                )
            block_type = block.get("type")
            if block_type == "image":
                expected_images += 1
                if not self._image_path_for_block(block, images):
                    raise ContentValidationError(
                        "WEIBO_CONTENT_CONTRACT_INVALID: 图片块没有唯一受控文件"
                    )
                continue
            if block_type not in {"text", "heading"}:
                raise ContentValidationError(
                    "WEIBO_CONTENT_CONTRACT_INVALID: 未知正文块类型"
                )
            text = str(block.get("text") or "").strip()
            if not text:
                raise ContentValidationError(
                    "WEIBO_CONTENT_CONTRACT_INVALID: 文字块不能为空"
                )
            if block_type == "heading" and (
                block.get("level") != 2 or "\n" in text or "\r" in text
            ):
                raise ContentValidationError(
                    "WEIBO_HEADING_UNSUPPORTED: 仅支持单行二级标题"
                )
        return expected_images

    @staticmethod
    def _media_status(expected: int, uploaded: int, failed: int) -> str:
        if expected == 0:
            return "not_required"
        if uploaded == expected:
            return "completed"
        if uploaded == 0 and failed:
            return "failed"
        if uploaded + failed == expected:
            return "partial"
        return "in_progress"

    def _update_media_progress(self, expected: int, uploaded: int, failed: int) -> None:
        self._media_progress_state = {
            "expected_images": expected,
            "uploaded_images": uploaded,
            "failed_image_count": failed,
            "media_status": self._media_status(expected, uploaded, failed),
        }

    def _attach_media_progress(self, exc: Exception) -> None:
        progress = safe_media_progress(self._media_progress_state)
        if progress is not None:
            exc.media_progress = progress

    async def _current_body_editor(self):
        """返回当前可见正文编辑器；图片重渲染后必须重新定位。"""

        self._require_page_alive("微博定位当前正文编辑器")
        editor = self.page.locator(BODY_SELECTOR).first
        try:
            if await editor.count() == 0 or not await editor.is_visible():
                raise SelectorError("微博正文编辑器未找到或当前不可见")
            return editor
        except SelectorError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("微博正文编辑器未找到或当前不可见") from exc

    async def _place_body_caret_at_end(self) -> None:
        editor = await self._current_body_editor()
        try:
            await self.actions.perform(editor.press, "Control+End")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博移动正文光标时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "WEIBO_CARET_POSITION_FAILED: 正文末尾光标定位失败"
            ) from exc

    async def _apply_h2_to_current_block(self) -> None:
        """通过真实“标题 2”菜单设置当前块，并立即验证末尾为 H2。"""

        try:
            triggers = self.page.locator(
                ".main-editor-toolbar .wb-cursor-pointer"
            )
            trigger_matches = []
            for index in range(await triggers.count()):
                candidate = triggers.nth(index)
                if not await candidate.is_visible():
                    continue
                label = " ".join((await candidate.inner_text()).split())
                if label in {"正文", *(f"标题 {level}" for level in range(1, 7))}:
                    trigger_matches.append(candidate)
            if len(trigger_matches) != 1:
                raise RuntimeError("标题格式入口不唯一")
            await self.actions.perform(trigger_matches[0].click, timeout=5000)
            await asyncio.sleep(0.25)

            options = self.page.locator(".n-popover:visible .card")
            option_matches = []
            for index in range(await options.count()):
                candidate = options.nth(index)
                if await candidate.is_visible() and (
                    " ".join((await candidate.inner_text()).split()) == "标题 2"
                ):
                    option_matches.append(candidate)
            if len(option_matches) != 1:
                raise RuntimeError("标题 2 选项不唯一")
            await self.actions.perform(option_matches[0].click, timeout=5000)
            await asyncio.sleep(0.25)
            editor = await self._current_body_editor()
            tail_tag = str(
                await editor.evaluate(
                    """root => root.lastElementChild
                        ? root.lastElementChild.tagName.toLowerCase() : ''"""
                )
                or ""
            )
            if tail_tag != "h2":
                raise RuntimeError("标题 2 没有落为 H2")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博设置二级标题时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "WEIBO_HEADING_APPLY_FAILED: 二级标题样式未能应用"
            ) from exc

    async def _find_body_image_trigger(self):
        """以真实 SVG 指纹定位，再用悬浮文案二次确认“插入图片”。"""

        try:
            candidates = self.page.locator(
                ".main-editor-toolbar .svg-icon-wrapper"
            )
            matches = []
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if not await candidate.is_visible():
                    continue
                paths = await candidate.locator("svg path").evaluate_all(
                    "nodes => nodes.map((node) => node.getAttribute('d') || '')"
                )
                if not isinstance(paths, list):
                    continue
                fingerprint = hashlib.sha256(
                    "|".join(str(path) for path in paths).encode("utf-8")
                ).hexdigest()
                if fingerprint == BODY_IMAGE_ICON_FINGERPRINT:
                    matches.append(candidate)
            if len(matches) != 1:
                raise ContentValidationError(
                    "WEIBO_BODY_IMAGE_TRIGGER_NOT_UNIQUE: 正文插图入口不存在或不唯一"
                )
            await self.actions.perform(matches[0].hover, timeout=5000)
            await asyncio.sleep(0.45)
            labels = await self.page.evaluate(
                    """() => {
                        const visible = (node) => {
                            const rect = node.getBoundingClientRect();
                            const style = getComputedStyle(node);
                            return rect.width > 0 && rect.height > 0
                                && style.display !== 'none'
                                && style.visibility !== 'hidden';
                        };
                        return Array.from(document.querySelectorAll(
                            '.n-popover, [role=tooltip]'
                        )).filter(visible).map((node) =>
                            (node.innerText || '').trim()
                        ).filter(Boolean);
                    }"""
                )
            if not isinstance(labels, list) or "插入图片" not in labels:
                raise ContentValidationError(
                    "WEIBO_BODY_IMAGE_TRIGGER_NOT_UNIQUE: 正文插图入口语义未确认"
                )
            return matches[0]
        except ContentValidationError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博定位正文插图入口时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "WEIBO_BODY_IMAGE_TRIGGER_NOT_UNIQUE: 正文插图入口不存在或不唯一"
            ) from exc

    async def _create_paragraph_after_image(self) -> None:
        """越过图片原子块，创建后续文字可安全写入的段落。"""

        editor = await self._current_body_editor()
        try:
            await self.actions.perform(editor.press, "Control+End")
            await self.actions.perform(self.page.keyboard.press, "ArrowDown")
            await self.actions.perform(self.page.keyboard.press, "ArrowRight")
            await self.actions.perform(self.page.keyboard.press, "Enter")
            tail_ready = bool(
                await editor.evaluate(
                    """root => {
                        const tail = root.lastElementChild;
                        return !!tail && tail.tagName.toLowerCase() === 'p'
                            && tail.querySelectorAll('img').length === 0;
                    }"""
                )
            )
            if not tail_ready:
                raise RuntimeError("图片后正文段落未建立")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博图片后创建正文段落时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "WEIBO_POST_IMAGE_PARAGRAPH_FAILED: 图片后无法建立正文插入点"
            ) from exc

    @staticmethod
    def _image_path_for_block(block: dict, images: list[dict]) -> str | None:
        direct = block.get("local_path") if isinstance(block, dict) else None
        if direct:
            return str(direct)
        position = block.get("position") if isinstance(block, dict) else None
        matches = [
            image
            for image in images or []
            if isinstance(image, dict) and image.get("position_index") == position
        ]
        if len(matches) != 1:
            return None
        local_path = matches[0].get("local_path")
        return str(local_path) if local_path else None

    @staticmethod
    def _expected_content_tokens(blocks: list[dict]) -> list[dict]:
        tokens: list[dict] = []
        for block in blocks:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type == "image":
                tokens.append({"kind": "image"})
                continue
            for paragraph in extract_expected_paragraphs([block]):
                if block_type == "heading":
                    tokens.append(
                        {
                            "kind": "heading",
                            "level": int(block.get("level") or 0),
                            "text": paragraph.comparison_text,
                        }
                    )
                else:
                    tokens.append(
                        {"kind": "text", "text": paragraph.comparison_text}
                    )
        return tokens

    async def _read_editor_dom_tokens(self) -> list[dict]:
        editor = await self._current_body_editor()
        try:
            raw = await editor.evaluate(
                """root => {
                    return Array.from(root.children).map((node) => ({
                        tag: node.tagName.toLowerCase(),
                        class_name: String(node.className || ''),
                        text: node.innerText || node.textContent || '',
                        images: Array.from(
                            node.matches('img') ? [node] : node.querySelectorAll('img')
                        ).map((image) => ({
                            is_separator: image.classList.contains(
                                'ProseMirror-separator'
                            ),
                            is_body_image: image.classList.contains(
                                'image-view__body__image'
                            ),
                        })),
                    }));
                }"""
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博读取正文 DOM 时页面已关闭"
                ) from exc
            raise ContentValidationError(
                "WEIBO_CONTENT_DOM_VERIFY_FAILED: 正文 DOM 回读失败"
            ) from exc
        if not isinstance(raw, list):
            raise ContentValidationError(
                "WEIBO_CONTENT_DOM_VERIFY_FAILED: DOM 序列无效"
            )
        return self._normalize_editor_dom_snapshot(raw)

    @staticmethod
    def _normalize_editor_dom_snapshot(raw: list[dict]) -> list[dict]:
        """将微博 ProseMirror DOM 归一成图文 token。

        微博会在普通文字段落内插入不可见的 ``ProseMirror-separator``
        ``img``。它是光标占位节点，不是用户正文图片；正文图片只接受当前
        官方结构 ``figure.wb-node-image`` 内唯一的 body image。未知形状一律
        fail closed，避免把占位图算成正文图或吞掉同段文字。
        """

        normalized: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ContentValidationError(
                    "WEIBO_CONTENT_DOM_VERIFY_FAILED: DOM 节点无效"
                )
            tag = str(item.get("tag") or "").lower()
            class_tokens = set(str(item.get("class_name") or "").split())
            images = item.get("images")
            if not isinstance(images, list) or any(
                not isinstance(image, dict) for image in images
            ):
                raise ContentValidationError(
                    "WEIBO_CONTENT_DOM_VERIFY_FAILED: 图片节点无效"
                )
            content_images = [
                image for image in images if not image.get("is_separator")
            ]
            if content_images:
                if (
                    tag != "figure"
                    or "wb-node-image" not in class_tokens
                    or len(content_images) != 1
                    or not content_images[0].get("is_body_image")
                    or normalize_for_comparison(item.get("text"))
                ):
                    raise ContentValidationError(
                        "WEIBO_CONTENT_DOM_VERIFY_FAILED: 正文图片结构无法确认"
                    )
                normalized.append({"kind": "image"})
                continue
            text = normalize_for_comparison(item.get("text"))
            if not text:
                continue
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                normalized.append(
                    {
                        "kind": "heading",
                        "level": int(tag[1]),
                        "text": text,
                    }
                )
            else:
                for paragraph in extract_expected_paragraphs(
                    [{"type": "text", "text": text}]
                ):
                    normalized.append(
                        {"kind": "text", "text": paragraph.comparison_text}
                    )
        return normalized

    async def _semantic_editor_image_count(self) -> int:
        """只统计官方正文图片 figure，忽略 ProseMirror 光标占位图。"""

        editor = await self._current_body_editor()
        state = await editor.evaluate(
            """root => {
                const contentImages = Array.from(root.querySelectorAll('img')).filter(
                    (image) => !image.classList.contains('ProseMirror-separator')
                );
                const validFigures = Array.from(
                    root.querySelectorAll(':scope > figure.wb-node-image')
                ).filter((figure) => {
                    const images = Array.from(figure.querySelectorAll('img')).filter(
                        (image) => !image.classList.contains('ProseMirror-separator')
                    );
                    return images.length === 1
                        && images[0].classList.contains('image-view__body__image');
                });
                const recognizedImages = new Set(
                    validFigures.map((figure) => Array.from(
                        figure.querySelectorAll('img')
                    ).find((image) => !image.classList.contains(
                        'ProseMirror-separator'
                    )))
                );
                return {
                    semantic_count: validFigures.length,
                    unsupported_count: contentImages.filter(
                        (image) => !recognizedImages.has(image)
                    ).length,
                };
            }"""
        )
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("semantic_count"), int)
            or int(state.get("unsupported_count") or 0) != 0
        ):
            raise ContentValidationError(
                "WEIBO_CONTENT_DOM_VERIFY_FAILED: 正文图片结构无法确认"
            )
        return int(state["semantic_count"])

    @staticmethod
    def _token_shape(tokens: list[dict]) -> str:
        shape = [
            "I" if token.get("kind") == "image"
            else (
                f"H{token.get('level')}:{len(token.get('text') or '')}"
                if token.get("kind") == "heading"
                else f"T:{len(token.get('text') or '')}"
            )
            for token in tokens[:40]
        ]
        return ",".join(shape) or "EMPTY"

    async def _validate_dom_prefix(self, blocks: list[dict], *, phase: str) -> None:
        expected = self._expected_content_tokens(blocks)
        actual = await self._read_editor_dom_tokens()
        if actual[: len(expected)] != expected:
            raise ContentValidationError(
                f"CONTENT_VALIDATION_ERROR: 微博{phase}图文顺序不完整; "
                f"expected={self._token_shape(expected)}; "
                f"actual={self._token_shape(actual)}"
            )

    async def _validate_dom_exact(self, blocks: list[dict], *, phase: str) -> None:
        expected = self._expected_content_tokens(blocks)
        actual = await self._read_editor_dom_tokens()
        if actual != expected:
            raise ContentValidationError(
                f"CONTENT_VALIDATION_ERROR: 微博{phase}图文顺序不完整; "
                f"expected={self._token_shape(expected)}; "
                f"actual={self._token_shape(actual)}"
            )

    async def _upload_image(self, image_path: str) -> dict:
        """点击语义唯一的“插入图片”，一次选择一张正文图并核验数量。"""

        self._require_page_alive("微博上传图片")
        try:
            try:
                trigger = await self._find_body_image_trigger()
            except ContentValidationError as exc:
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_TRIGGER_NOT_UNIQUE",
                    "error": safe_media_error(
                        exc,
                        fallback="微博正文插图入口不存在或不唯一",
                    ),
                }
            try:
                before = await self._semantic_editor_image_count()
            except ContentValidationError as exc:
                return {
                    "success": False,
                    "error_code": "WEIBO_EDITOR_IMAGE_SHAPE_UNSUPPORTED",
                    "error": safe_media_error(
                        exc,
                        fallback="微博正文已有图片结构无法确认",
                    ),
                }
            await self.actions.perform(trigger.click, timeout=5000)
            await asyncio.sleep(0.5)
            dialogs = self.page.locator(".n-dialog:visible")
            dialog_matches = []
            for index in range(await dialogs.count()):
                candidate = dialogs.nth(index)
                if not await candidate.is_visible():
                    continue
                label = " ".join((await candidate.inner_text()).split())
                if all(token in label for token in ("图片库", "上传", "插入")):
                    dialog_matches.append(candidate)
            if len(dialog_matches) != 1:
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_DIALOG_NOT_UNIQUE",
                    "error": "微博正文图片弹窗不存在或不唯一",
                }
            dialog = dialog_matches[0]
            try:
                await dialog.locator(".n-spin-body").first.wait_for(
                    state="hidden",
                    timeout=15000,
                )
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_LIBRARY_UNSTABLE",
                    "error": "微博图片库初始加载未完成",
                }
            inputs = dialog.locator("input[type=file]")
            if await inputs.count() != 1:
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_INPUT_NOT_UNIQUE",
                    "error": "微博正文图片弹窗内文件控件不存在或不唯一",
                }
            body_input = inputs.first
            accept = str(await body_input.get_attribute("accept") or "").lower()
            allowed_suffixes = {".jpg", ".jpeg", ".bmp", ".gif", ".png", ".heic"}
            observed_suffixes = {
                value.strip() for value in accept.split(",") if value.strip()
            }
            if (
                not observed_suffixes
                or not observed_suffixes.issubset(allowed_suffixes)
                or not observed_suffixes.intersection({".jpg", ".jpeg", ".png"})
            ):
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_INPUT_INVALID",
                    "error": "微博正文图片文件控件类型无法确认",
                }

            # 微博当前 AlbumList 会异步加载账号图片库。必须先得到稳定基线，
            # 再以“恰好多出一个新 item”证明后续点击的是本次上传，而不是旧图。
            image_items = dialog.locator(".image-list .image-item")
            baseline_count = None
            previous_count = None
            stable_reads = 0
            for _ in range(20):
                observed_count = await image_items.count()
                if observed_count == previous_count:
                    stable_reads += 1
                else:
                    previous_count = observed_count
                    stable_reads = 1
                if stable_reads >= 3:
                    baseline_count = observed_count
                    break
                await asyncio.sleep(0.25)
            if baseline_count is None:
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_LIBRARY_UNSTABLE",
                    "error": "微博图片库基线无法稳定确认",
                }
            await self.actions.perform(body_input.set_input_files, str(image_path), timeout=15000)

            # 2026-08-21 当前官方编辑器逻辑：上传成功只会新增 image-item，
            # 还必须点中该项（class=is-selected）才会启用“插入”。官方上传
            # 请求自身允许 120 秒；这里只等待同一次上传完成，绝不重试选文件。
            uploaded_item = None
            for _ in range(240):
                observed_count = await image_items.count()
                if observed_count > baseline_count + 1:
                    return {
                        "success": False,
                        "error_code": "WEIBO_BODY_IMAGE_ITEM_COUNT_AMBIGUOUS",
                        "error": "微博图片库出现多个无法归属本次上传的新条目",
                    }
                if observed_count == baseline_count + 1:
                    candidate = image_items.first
                    upload_state = await candidate.evaluate(
                        """item => {
                            const image = item.querySelector('img');
                            const source = image
                                ? (image.currentSrc || image.getAttribute('src') || '')
                                : '';
                            const failed = (item.innerText || '').includes('上传失败');
                            const spinner = item.querySelector(
                                '.n-spin, .n-spin-body, .n-spin-content, [class*=loading]'
                            );
                            return {
                                failed,
                                ready: Boolean(
                                    image && !failed && !spinner
                                    && !source.startsWith('blob:')
                                    && /^https?:/i.test(source)
                                    && image.complete && image.naturalWidth > 0
                                ),
                            };
                        }"""
                    )
                    if isinstance(upload_state, dict) and upload_state.get("failed"):
                        return {
                            "success": False,
                            "error_code": "WEIBO_BODY_IMAGE_UPLOAD_REJECTED",
                            "error": "微博图片库明确报告本次图片上传失败",
                        }
                    if isinstance(upload_state, dict) and upload_state.get("ready"):
                        uploaded_item = candidate
                        break
                await asyncio.sleep(0.5)
            if uploaded_item is None:
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_UPLOAD_TIMEOUT",
                    "error": "微博图片在单次上传等待窗口内未完成",
                }

            selected_items = dialog.locator(".image-list .image-item.is-selected")
            if await selected_items.count() != 0:
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_SELECTION_DIRTY",
                    "error": "微博图片弹窗在选择前存在无法归属的旧选中项",
                }
            await self.actions.perform(uploaded_item.click, timeout=5000)
            selection_confirmed = False
            for _ in range(20):
                if await selected_items.count() == 1:
                    selected_class = str(
                        await uploaded_item.get_attribute("class") or ""
                    )
                    if "is-selected" in selected_class.split():
                        selection_confirmed = True
                        break
                await asyncio.sleep(0.25)
            if not selection_confirmed:
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_SELECTION_FAILED",
                    "error": "微博本次新上传图片未形成唯一选中状态",
                }

            insert_button = None
            for _ in range(30):
                buttons = dialog.locator("button")
                enabled_matches = []
                for index in range(await buttons.count()):
                    candidate = buttons.nth(index)
                    if (
                        await candidate.is_visible()
                        and await candidate.is_enabled()
                        and " ".join((await candidate.inner_text()).split()) == "插入"
                    ):
                        enabled_matches.append(candidate)
                if len(enabled_matches) == 1:
                    insert_button = enabled_matches[0]
                    break
                if len(enabled_matches) > 1:
                    break
                await asyncio.sleep(0.5)
            if insert_button is None:
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_INSERT_NOT_READY",
                    "error": "微博正文图片上传后唯一插入按钮未就绪",
                }
            await self.actions.perform(insert_button.click, timeout=5000)
            dialog_closed = False
            for _ in range(20):
                if await dialogs.count() == 0:
                    dialog_closed = True
                    break
                await asyncio.sleep(0.25)
            if not dialog_closed:
                return {
                    "success": False,
                    "error_code": "WEIBO_BODY_IMAGE_DIALOG_DID_NOT_CLOSE",
                    "error": "微博插入图片后弹窗未关闭",
                }
            after = before
            for _ in range(10):
                await asyncio.sleep(1)
                try:
                    observed = await self._semantic_editor_image_count()
                except ContentValidationError:
                    return {
                        "success": False,
                        "error_code": "WEIBO_EDITOR_IMAGE_SHAPE_UNSUPPORTED",
                        "error": "微博插图后正文图片结构无法确认",
                    }
                if observed <= before:
                    continue
                if observed > before + 1:
                    return {
                        "success": False,
                        "error_code": "WEIBO_EDITOR_IMAGE_COUNT_AMBIGUOUS",
                        "error": "微博插图后正文图片块增加数量不唯一",
                    }
                await asyncio.sleep(1)
                try:
                    stable = await self._semantic_editor_image_count()
                except ContentValidationError:
                    return {
                        "success": False,
                        "error_code": "WEIBO_EDITOR_IMAGE_SHAPE_UNSUPPORTED",
                        "error": "微博插图后正文图片结构无法确认",
                    }
                if stable == observed == before + 1:
                    after = stable
                    break
            if after != before + 1:
                return {
                    "success": False,
                    "error_code": "WEIBO_EDITOR_SEMANTIC_IMAGE_COUNT_UNCHANGED",
                    "error": "上传后正文编辑器真实图片块没有稳定且只增加一张",
                }
            return {
                "success": True,
                "error": "",
                "observed_image_count": after,
            }
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博上传图片时页面已关闭"
                ) from exc
            return {
                "success": False,
                "error_code": "WEIBO_IMAGE_UPLOAD_FAILED",
                "error": safe_media_error(exc, fallback="微博图片上传失败"),
            }

    async def set_cover(self) -> dict:
        """原生选择正文首图、裁剪一次，要求唯一新封面实际加载。"""
        self._require_page_alive("微博设置封面")
        if getattr(self, "_cover_attempted", False):
            return {"success": False, "error": "微博封面已尝试，不会自动重复上传"}
        editor = await self._current_body_editor()
        body_images = editor.locator("figure.wb-node-image img.image-view__body__image")
        if not await body_images.count():
            return {"success": False, "error": "微博正文无图片可用作封面"}
        first_source = await body_images.first.get_attribute("src")
        trigger = self.page.locator(".cover-empty:visible")
        if await trigger.count() != 1:
            return {"success": False, "error": "微博空封面入口不唯一，现有封面不自动替换"}
        self._cover_attempted = True
        try:
            await self.actions.perform(trigger.click, timeout=5000)
            dialog = self.page.locator(".n-dialog:visible")
            await dialog.wait_for(timeout=10000)
            if await dialog.get_by_text("正文图片", exact=True).count() != 1:
                raise SelectorError("微博封面弹窗不是正文图片列表")
            items = dialog.locator(".image-list .image-item")
            if await items.count() != await body_images.count():
                raise SelectorError("微博封面候选与正文图片数量不一致")
            candidate = items.first.locator("img")
            await candidate.wait_for(timeout=10000)
            if await candidate.count() != 1 or await candidate.get_attribute("src") != first_source:
                raise SelectorError("微博封面首候选与正文首图不匹配")
            await self.actions.perform(items.first.click, timeout=5000)
            if await dialog.locator(".image-item.is-selected").count() != 1:
                raise SelectorError("微博封面未唯一选中首图")
            await self.actions.perform(
                dialog.get_by_role("button", name="下一步", exact=True).click,
                timeout=5000,
            )
            crop = self.page.locator(".n-dialog:visible").filter(
                has=self.page.locator("cropper-selection"),
            )
            await crop.wait_for(timeout=10000)
            await crop.locator("cropper-selection").wait_for(state="visible", timeout=10000)
            # Native cropper settles its selection after image loading and resize.
            for _ in range(60):
                ready = await crop.locator("cropper-selection").evaluate(
                    "e => e.width > 0 && e.height > 0",
                )
                if ready:
                    break
                await asyncio.sleep(0.2)
            if not ready:
                raise SelectorError("微博原生裁剪区域尚未就绪")
            await asyncio.sleep(0.5)
            await self.actions.perform(
                crop.get_by_role("button", name="确定", exact=True).click,
                timeout=5000,
            )
            await crop.wait_for(state="hidden", timeout=45000)
            covers = self.page.locator(".cover-preview img.cover-img")
            await covers.wait_for(timeout=10000)
            for _ in range(50):
                if await covers.count() == 1 and await covers.evaluate(
                    "i => i.complete && i.naturalWidth > 0 && i.naturalHeight > 0",
                ):
                    return {"success": True, "error": ""}
                await asyncio.sleep(0.2)
            raise SelectorError("微博新封面尚未加载")
        except Exception as exc:
            return {"success": False, "error": safe_media_error(exc, fallback="微博封面设置未确认")}

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """微博保存草稿不需要话题；公开话题选择尚未接入，如实报告。"""

        if not (topic or community or selection_query):
            return {
                "success": True,
                "selection_status": "not_required",
                "selection": {},
            }
        return {
            "success": False,
            "needs_selection": True,
            "error_code": "TOPIC_SELECTION_NOT_IMPLEMENTED",
            "error": "微博话题选择尚未接入（保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """精确保存一次，并重开同一 draft ID 核验冻结图文。"""

        self._require_page_alive("微博保存草稿")
        evidence = DraftVerificationEvidence()
        self._last_draft_evidence = evidence
        expected_title = self._normalize_draft_title(title)
        baseline = self._preflight_draft_ids_by_title
        all_baseline_ids = self._preflight_all_draft_ids
        if (
            not expected_title
            or expected_title != self._preflight_title
            or baseline is None
            or all_baseline_ids is None
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博缺少与本次一致的草稿 ID 基线"
            )
        if self._expected_persisted_blocks is None:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博缺少冻结内容核验快照"
            )
        draft_id = self._active_draft_id
        baseline_title_ids = baseline.get(expected_title, frozenset())
        if (
            not re.fullmatch(r"[1-9][0-9]*", draft_id)
            or (
                self._editing_existing_draft
                and draft_id not in baseline_title_ids
            )
            or (
                not self._editing_existing_draft
                and draft_id in all_baseline_ids
            )
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博当前草稿 ID 与保存前基线冲突"
            )

        try:
            current_id = await self._current_draft_id()
            title_field = self.page.locator(
                "textarea[placeholder='请输入标题']"
            ).first
            if (
                current_id != draft_id
                or await title_field.count() != 1
                or not await title_field.is_visible()
                or str(await title_field.input_value()).strip() != expected_title
            ):
                raise DraftBaselineError(
                    "DRAFT_BASELINE_FAILED: 微博保存前草稿身份或标题不一致"
                )
            await self._validate_dom_exact(
                self._expected_persisted_blocks,
                phase="保存前",
            )
        except DraftBaselineError:
            raise
        except ContentValidationError as exc:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博保存前图文结构不完整"
            ) from exc
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博保存草稿前页面已关闭"
                ) from exc
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博保存前状态无法证明"
            ) from exc

        captured: dict = {}
        save_triggered = False

        async def _guard_public_publish(route, request) -> None:
            method = str(request.method or "").upper()
            path = self._safe_request_path(request.url)
            if self._is_public_publish_mutation(path, method):
                captured["blocked_publish"] = True
                captured["publish_path"] = path
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        async def _on_response(response) -> None:
            try:
                url = response.url
                method = str(response.request.method or "").upper()
                if (
                    "draft/save" in url
                    and method in {"POST", "PUT"}
                    and "status" not in captured
                ):
                    captured["status"] = response.status
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            captured["code"] = body.get("code")
                    except Exception:
                        pass
                path = self._safe_request_path(url)
                if self._is_public_publish_mutation(path, method):
                    captured["published"] = True
                    captured["publish_path"] = path
            except Exception:  # noqa: BLE001
                pass

        route_registered = False
        response_registered = False
        try:
            await self.page.route("**/*", _guard_public_publish)
            route_registered = True
            self.page.on("response", _on_response)
            response_registered = True
            click_result = await self.actions.perform(self.page.evaluate, """() => {
                    const nodes = Array.from(
                        document.querySelectorAll('button, [role=button]')
                    );
                    const candidates = nodes.filter((el) => {
                        const text = (el.innerText || '').replace(/\\s+/g, '');
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return text === '保存草稿'
                            && rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && !el.disabled
                            && el.getAttribute('aria-disabled') !== 'true';
                    });
                    if (candidates.length !== 1) {
                        return {clicked: false, count: candidates.length};
                    }
                    candidates[0].click();
                    return {clicked: true, count: 1};
                }""")
            if not isinstance(click_result, dict) or not click_result.get("clicked"):
                raise DraftBaselineError(
                    "DRAFT_BASELINE_FAILED: 微博精确保存草稿按钮不存在或不唯一"
                )
            save_triggered = True
            await self.simulator.random_delay(2, 4)
            for _ in range(10):
                if (
                    captured.get("status")
                    or captured.get("published")
                    or captured.get("blocked_publish")
                ):
                    break
                await asyncio.sleep(1)
            # 给发布/保存响应一个收敛窗口，避免 status 先到、publish 后到被漏检
            await self.simulator.random_delay(1, 2)
            if captured.get("published") or captured.get("blocked_publish"):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博保存期间出现公开发布请求，已拦截且禁止重试",
                    evidence=evidence,
                )
            status = captured.get("status")
            evidence.mark_save_response(status=status, code=captured.get("code"))
            if not isinstance(status, int) or not 200 <= status < 300:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博保存动作已触发但未观察到 2xx 保存响应",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                )

            try:
                current_by_title = await self._collect_draft_ids_by_title(
                    only_title=expected_title,
                )
            except Exception as exc:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博保存后无法读取草稿 ID 列表",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                ) from exc
            matching_ids = current_by_title.get(expected_title, frozenset())
            evidence.mark_draft_list(match_count=len(matching_ids))
            binding_source = (
                "existing_draft_id"
                if self._editing_existing_draft
                else "baseline_new_id"
            )
            draft_url = self._draft_editor_url(draft_id)
            evidence.set_draft_url(draft_url)
            if draft_id not in matching_ids:
                evidence.mark_entity_binding(
                    bound=False,
                    source=binding_source,
                    id_match=False,
                )
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博未找到标题精确匹配且绑定本次 ID 的草稿",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                )

            evidence.mark_entity_binding(
                bound=True,
                source=binding_source,
                id_match=True,
            )
            await self.actions.perform(
                self.page.goto,
                draft_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.page.wait_for_function(
                r"""(draftId) => location.hash === `#/draft/${draftId}`""",
                arg=draft_id,
                timeout=20000,
            )
            if await self._current_draft_id() != draft_id:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博草稿重开后 ID 不一致",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                )

            await self.page.wait_for_selector(
                "textarea[placeholder='请输入标题']",
                state="visible",
                timeout=20000,
            )
            reopened_title = str(
                await self.page.locator(
                    "textarea[placeholder='请输入标题']"
                ).first.input_value()
            ).strip()
            if reopened_title != expected_title:
                evidence.mark_reopen(title_match=False, dom_blocks_match=None)
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博草稿重开后标题不一致",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                )
            try:
                await self._validate_dom_exact(
                    self._expected_persisted_blocks,
                    phase="草稿重开后",
                )
                evidence.mark_reopen(title_match=True, dom_blocks_match=True)
            except Exception as exc:
                evidence.mark_reopen(title_match=True, dom_blocks_match=False)
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博草稿重开后图文结构不一致",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                ) from exc
            logger.info(
                "微博草稿验证成功: draft ID={}，接口 status={} code={}",
                draft_id,
                captured.get("status"),
                captured.get("code"),
            )
            evidence.finalize()
            return draft_url
        except DraftBaselineError:
            evidence.finalize(error_code="DRAFT_BASELINE_UNAVAILABLE")
            raise
        except DraftResultUnknownError:
            raise
        except Exception as exc:
            if save_triggered:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博保存动作已触发但持久化结果无法证明",
                    evidence=evidence.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
                ) from exc
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博保存前页面已关闭"
                ) from exc
            raise
        finally:
            if response_registered:
                try:
                    self.page.remove_listener("response", _on_response)
                except Exception:  # noqa: BLE001
                    pass
            if route_registered:
                try:
                    await self.page.unroute("**/*", _guard_public_publish)
                except Exception:  # noqa: BLE001
                    pass

    async def verify_draft_readonly(self, title: str) -> dict:
        """只读核验：返回标题实体的规范 ID URL；同名时仅接受已知目标 ID。

        必要的卡片导航会拦截全部非只读请求，不输入、不保存、不发布。
        """
        expected_title = self._normalize_draft_title(title)
        if not expected_title:
            return {"error_code": "PROBE_TITLE_MISSING", "error_message": "缺少可核验标题"}
        try:
            by_title = await self._collect_draft_ids_by_title(
                only_title=expected_title,
                guard_mutations=True,
            )
            matching_ids = by_title.get(expected_title, frozenset())
            match_count = len(matching_ids)
            preferred_ids = (
                self._active_draft_id,
                self._resume_existing_draft_id,
            )
            target_id = next(
                (
                    candidate
                    for candidate in preferred_ids
                    if candidate in matching_ids
                ),
                "",
            )
            if match_count == 1:
                target_id = next(iter(matching_ids))
            if target_id:
                return {
                    "title_matched": True,
                    "match_count": match_count,
                    "draft_url": self._draft_editor_url(target_id),
                    "structure": {
                        "source": "draft_list_id",
                        "id_targeted": match_count > 1,
                    },
                }
            if match_count > 1:
                return {
                    "error_code": "PROBE_TITLE_AMBIGUOUS",
                    "error_message": f"草稿箱存在 {match_count} 个同名草稿",
                }
            return {
                "error_code": "PROBE_NOT_FOUND",
                "error_message": "草稿箱未找到该标题草稿",
            }
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                return {
                    "error_code": "PROBE_RESULT_UNKNOWN",
                    "error_message": "核验期间浏览器已关闭",
                }
            return {
                "error_code": "PROBE_RESULT_UNKNOWN",
                "error_message": "草稿箱核验失败",
            }

    @staticmethod
    def _safe_request_path(url: str) -> str:
        try:
            return urlsplit(str(url or "")).path.lower()
        except Exception:
            return ""

    @staticmethod
    def _is_public_publish_mutation(path: str, method: str) -> bool:
        if str(method or "").upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
            return False
        normalized = str(path or "").lower()
        return "/publish" in normalized or "/article/publish" in normalized

    PUBLICATION_TIMEOUT_MS = 30000
    PUBLICATION_PATH = "/article/v5/aj/editor/draft/publish"
    PUBLICATION_SAVE_PATH = "/article/v5/aj/editor/draft/save"

    @staticmethod
    def _native_form(request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type.strip().lower() != "application/x-www-form-urlencoded":
            raise ValueError("unsupported native form")
        fields = {}
        for key, value in parse_qsl(request.post_data or "", keep_blank_values=True):
            if key in fields:
                raise ValueError("duplicate native field")
            fields[key] = value
        return fields

    @staticmethod
    def _native_request_at(request, path: str) -> bool:
        parsed = urlsplit(request.url)
        return (
            request.method == "POST" and parsed.scheme == "https"
            and parsed.netloc == "card.weibo.com" and parsed.path == path
        )

    async def prepare_publication_options(self) -> None:
        labels = self.page.locator("span:visible").filter(
            has_text=re.compile(r"^仅粉丝阅读全文$"),
        )
        if await labels.count() != 1:
            raise SelectorError("微博粉丝阅读设置无法唯一确认")
        checkbox = labels.locator("..").get_by_role("checkbox")
        if await checkbox.count() != 1:
            raise SelectorError("微博粉丝阅读复选框不唯一")
        state = await checkbox.get_attribute("aria-checked")
        if state == "true":
            await self.actions.perform(checkbox.click, timeout=5000)
        for _ in range(40):
            if await checkbox.get_attribute("aria-checked") == "false":
                return
            await asyncio.sleep(0.1)
        raise SelectorError("微博仅粉丝阅读全文尚未关闭")

    async def _assert_publication_ready(self, title: str) -> str:
        if not self._active_draft_id or self._expected_persisted_blocks is None:
            raise SelectorError("微博发布前缺少已核验草稿")
        current = urlsplit(self.page.url)
        if (
            current.scheme != "https" or current.netloc != "card.weibo.com"
            or current.path != "/article/v5/editor"
        ):
            raise SelectorError("微博发布前不是原生草稿编辑页")
        titles = self.page.get_by_placeholder("请输入标题", exact=True)
        if (
            await self._current_draft_id() != self._active_draft_id
            or await titles.count() != 1
            or self._normalize_draft_title(await titles.input_value())
            != self._normalize_draft_title(title)
        ):
            raise SelectorError("微博发布前草稿或标题不匹配")
        await self._validate_dom_exact(self._expected_persisted_blocks, phase="公开发布前")
        count = sum(b.get("type") == "image" for b in self._expected_persisted_blocks)
        editor = await self._current_body_editor()
        images = editor.locator("figure.wb-node-image img.image-view__body__image")
        if (
            await self._semantic_editor_image_count() != count
            or not await images.evaluate_all(
                "els => els.every(i => i.complete && i.naturalWidth > 0)",
            )
        ):
            raise SelectorError("微博发布前正文图片尚未完整加载")
        covers = self.page.locator(".cover-preview img.cover-img")
        if await covers.count() != 1 or not await covers.evaluate(
            "i => i.complete && i.naturalWidth > 0",
        ):
            raise SelectorError("微博发布前缺少唯一已加载封面")
        labels = self.page.locator("span").filter(has_text=re.compile(r"^仅粉丝阅读全文$"))
        if await labels.count() != 1:
            raise SelectorError("微博发布前阅读范围不明确")
        checks = labels.locator("..").get_by_role("checkbox")
        if await checks.count() != 1 or await checks.get_attribute("aria-checked") != "false":
            raise SelectorError("微博发布前仍限制粉丝阅读")
        return self._active_draft_id

    async def _install_publication_guard(self) -> None:
        if getattr(self, "_publication_guard_installed", False):
            return
        self._publication_stage = "blocked"
        self._publication_save_sent = False
        self._publication_post_sent = False

        async def guard(route, request):
            path = urlsplit(request.url).path
            if request.method in {"GET", "HEAD", "OPTIONS"} or not path.startswith(
                "/article/v5/aj/editor/draft/",
            ):
                await route.fallback()
                return
            allowed = False
            try:
                fields = self._native_form(request)
                bound = fields.get("id") == self._active_draft_id
                if self._publication_stage == "save" and not self._publication_save_sent:
                    allowed = (
                        bound and self._native_request_at(request, self.PUBLICATION_SAVE_PATH)
                        and fields.get("action") == "2"
                        and fields.get("title") == self._publication_title
                        and fields.get("cover") == self._publication_cover
                        and fields.get("follow_to_read") == "0"
                        and bool(fields.get("content"))
                        and not fields.get("free_content")
                    )
                    if allowed:
                        self._publication_save_sent = True
                elif self._publication_stage == "publish" and not self._publication_post_sent:
                    allowed = (
                        bound and self._native_request_at(request, self.PUBLICATION_PATH)
                        and fields.get("text") == self._publication_text
                        and all(fields.get(key) == "0" for key in (
                            "rank", "follow_to_read", "follow_official", "sync_wb",
                            "is_original", "mpkey",
                        ))
                        and fields.get("time") == ""
                        and fields.get("timestamp") == ""
                    )
                    if allowed:
                        self._publication_post_sent = True
            except (AttributeError, TypeError, ValueError):
                allowed = False
            if allowed:
                await route.fallback()
            else:
                await route.abort()

        await self.page.route("**/article/v5/aj/editor/draft/**", guard)
        self._publication_guard_installed = True

    @staticmethod
    async def _assert_native_ack(response) -> None:
        payload = await response.json()
        if (
            not 200 <= response.status < 300 or not isinstance(payload, dict)
            or type(payload.get("code")) not in {int, str}
            or str(payload["code"]) not in {"100000", "A00006"}
            or not isinstance(payload.get("data"), dict)
            or payload["data"].get("geetest")
        ):
            raise ValueError("微博未返回明确成功回执")

    async def open_publication_dialog(self, title: str) -> None:
        """原生下一步会保存一次 action=2；只放行当前稿，禁止自动保存重试。"""
        if getattr(self, "_publication_next_attempted", False):
            raise PublishResultUnknownError("微博下一步已尝试，不会重复保存")
        await self._assert_publication_ready(title)
        await self._install_publication_guard()
        buttons = self.page.get_by_role("button", name="下一步", exact=True)
        if await buttons.count() != 1 or not await buttons.is_enabled():
            raise SelectorError("微博下一步按钮不可用或不唯一")
        self._publication_title = self._normalize_draft_title(title)
        self._publication_cover = await self.page.locator(
            ".cover-preview img.cover-img",
        ).get_attribute("src")
        self._publication_next_attempted = True
        self._publication_stage = "save"
        try:
            async with self.page.expect_response(
                lambda response: self._native_request_at(
                    response.request, self.PUBLICATION_SAVE_PATH,
                ),
                timeout=self.actions.event_timeout(self.PUBLICATION_TIMEOUT_MS),
            ) as pending:
                await self.actions.perform(buttons.click, timeout=8000)
            await self._assert_native_ack(await pending.value)
            await self.page.locator(".publish-modal:visible").wait_for(timeout=15000)
            await self._assert_publication_ready(title)
            self._publication_save_confirmed = self._active_draft_id
        except Exception as exc:
            raise PublishResultUnknownError(
                "微博下一步保存或发布窗口未确认，已停止；不会自动重试",
            ) from exc
        finally:
            self._publication_stage = "blocked"

    async def publish_now(self, title: str = "") -> dict:
        if getattr(self, "_public_publish_attempted", False):
            raise PublishResultUnknownError("微博本次发布已尝试，不会自动重发")
        await self._assert_publication_ready(title)
        if getattr(self, "_publication_save_confirmed", None) != self._active_draft_id:
            await self.open_publication_dialog(title)
        dialog = self.page.locator(".publish-modal:visible")
        if await dialog.count() != 1:
            raise SelectorError("微博最终发布窗口不唯一")
        follow = dialog.get_by_role("checkbox").filter(has_text="关注@头条文章")
        if await follow.count() > 1:
            raise SelectorError("微博附加关注选项不唯一")
        if await follow.count() == 1:
            if await follow.get_attribute("aria-checked") == "true":
                await self.actions.perform(follow.click, timeout=5000)
            if await follow.get_attribute("aria-checked") != "false":
                raise SelectorError("微博附加关注选项未关闭")
        if await dialog.get_by_text("公开", exact=True).count() != 1:
            raise SelectorError("微博最终发布范围不是公开")
        text = dialog.locator("textarea")
        if await text.count() != 1:
            raise SelectorError("微博发布配文不唯一")
        self._publication_text = await text.input_value()
        if self._normalize_draft_title(title) not in self._publication_text:
            raise SelectorError("微博发布配文与本篇标题不匹配")
        buttons = dialog.get_by_role("button", name="发布", exact=True)
        if await buttons.count() != 1 or not await buttons.is_enabled():
            raise SelectorError("微博正式发布按钮不可用或不唯一")
        await self._assert_publication_ready(title)
        self._public_publish_attempted = True
        self._publication_stage = "publish"
        try:
            async with self.page.expect_response(
                lambda response: self._native_request_at(response.request, self.PUBLICATION_PATH),
                timeout=self.actions.event_timeout(self.PUBLICATION_TIMEOUT_MS),
            ) as pending:
                await self.actions.perform(buttons.click, timeout=8000)
            await self._assert_native_ack(await pending.value)
            return {
                "status": "SUBMITTED",
                "verification_evidence": {
                    "submit_acknowledged": True,
                    "submission_source": "weibo_publish_response",
                    "submission_scope": "PUBLIC",
                    "submission_draft_id": self._active_draft_id,
                },
            }
        except Exception as exc:
            raise PublishResultUnknownError(
                "微博已尝试发布，但未确认本篇接收回执；不会自动重发",
            ) from exc
        finally:
            self._publication_stage = "blocked"
