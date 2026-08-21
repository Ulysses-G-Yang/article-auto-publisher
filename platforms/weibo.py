"""微博账号会话与头条文章草稿适配器。

登录态通过持久 Profile 验证；草稿链路只允许 ``DRAFT``，按冻结图文块顺序
写入 TipTap，保存后必须重开同一 draft ID 并复核标题、H2 与正文图片。

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
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    DraftBaselineError,
    DraftResultUnknownError,
    LoginRequiredError,
    PlatformAutomationError,
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
        self._draft_title_baseline_count: int | None = None
        self._expected_persisted_blocks: list[dict] | None = None
        self._expected_persisted_image_count = 0
        self._media_progress_state: dict | None = None

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None
        self._editing_existing_draft = False
        self._preflight_title = ""
        self._active_draft_id = ""
        self._draft_title_baseline_count = None
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
            await self.page.goto(
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
            self.last_login_error = "LOGIN_REQUIRED: 微博账号需要登录"
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
        await self.page.goto(
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

    async def preflight_delivery(self, title: str) -> None:
        """冻结标题，并证明新建或显式恢复的唯一草稿基线。"""

        self._require_page_alive("微博草稿基线检查")
        expected_title = str(title or "").strip()
        if not expected_title:
            raise DraftBaselineError("DRAFT_BASELINE_FAILED: 微博标题不能为空")
        if len(expected_title) > 32:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博头条文章标题不能超过 32 个字符"
            )
        try:
            await self.page.goto(
                "https://card.weibo.com/article/v5/editor#/draft",
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
            matches = int(
                await self.page.evaluate(
                    r"""(title) => {
                        const visible = (node) => {
                            const rect = node.getBoundingClientRect();
                            const style = getComputedStyle(node);
                            return rect.width > 0 && rect.height > 0
                                && style.display !== 'none'
                                && style.visibility !== 'hidden';
                        };
                        return Array.from(
                            document.querySelectorAll('.list-item')
                        ).filter((card) => {
                            if (!visible(card)) return false;
                            const firstLine = (card.innerText || '')
                                .split(/\r?\n/, 1)[0].trim();
                            return firstLine === title;
                        }).length;
                    }""",
                    expected_title,
                )
            )
        except DraftBaselineError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博草稿基线检查时页面已关闭"
                ) from exc
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博无法确认同名草稿基线"
            ) from exc
        self._editing_existing_draft = False
        if self._resume_existing_title:
            if expected_title != self._resume_existing_title or matches != 1:
                raise DraftBaselineError(
                    "DRAFT_BASELINE_FAILED: 微博待恢复草稿标题不唯一或与冻结标题不一致"
                )
            await self._open_resumed_draft(expected_title)
            self._preflight_title = expected_title
            self._draft_title_baseline_count = 1
            self._editing_existing_draft = True
            return
        if matches:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博已存在同名草稿，禁止自动重复创建"
            )
        self._preflight_title = expected_title
        self._draft_title_baseline_count = 0

    async def _open_resumed_draft(self, expected_title: str) -> None:
        """只打开唯一精确标题草稿，并把当前编辑器绑定到预期 draft ID。"""

        clicked = await self.page.evaluate(
            r"""(title) => {
                const visible = (node) => {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const matches = Array.from(
                    document.querySelectorAll('.list-item')
                ).filter((card) => visible(card)
                    && (card.innerText || '').split(/\r?\n/, 1)[0].trim() === title);
                if (matches.length !== 1) return false;
                matches[0].click();
                return true;
            }""",
            expected_title,
        )
        if not clicked:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博待恢复草稿无法唯一打开"
            )
        await self.page.wait_for_function(
            r"""() => /^#\/draft\/\d+$/.test(location.hash)
                && !location.hash.endsWith('/0')""",
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
        draft_id = str(
            await self.page.evaluate(
                r"""() => {
                    const match = location.hash.match(/^#\/draft\/(\d+)$/);
                    return match ? match[1] : '';
                }"""
            )
            or ""
        )
        if not draft_id.isdigit() or int(draft_id) <= 0:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博待恢复草稿 ID 无效"
            )
        if (
            self._resume_existing_draft_id
            and draft_id != self._resume_existing_draft_id
        ):
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
        if not self._preflight_title or self._draft_title_baseline_count != 0:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博缺少唯一标题草稿基线"
            )
        create_started = False
        try:
            await self.page.goto(
                "https://card.weibo.com/article/v5/editor#/draft",
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
                        const match = location.hash.match(/^#\/draft\/(\d+)$/);
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
                timeout=20000,
            ) as response_info:
                create_started = True
                await write_buttons.click(timeout=10000)
            create_response = await response_info.value
            if not 200 <= int(create_response.status) < 300:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博新草稿创建请求未返回成功状态，禁止重试"
                )

            # 草稿页会默认打开最近一篇旧稿；只有创建接口成功且 hash 切换到
            # 一个不同的正整数 ID，才能证明当前编辑器属于本次新草稿。
            await self.page.wait_for_function(
                r"""(beforeId) => {
                    const match = location.hash.match(/^#\/draft\/(\d+)$/);
                    return Boolean(match && match[1] !== '0' && match[1] !== beforeId);
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
            draft_id = str(
                await self.page.evaluate(
                    r"""() => {
                        const match = location.hash.match(/^#\/draft\/(\d+)$/);
                        return match ? match[1] : '';
                    }"""
                )
                or ""
            )
            if (
                not draft_id.isdigit()
                or int(draft_id) <= 0
                or draft_id == before_draft_id
            ):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博创建响应已返回但无法绑定新的草稿 ID"
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
        if (
            not self._preflight_title
            or self._draft_title_baseline_count != 1
            or not expected_id.isdigit()
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博现有草稿恢复基线不完整"
            )
        current_id = str(
            await self.page.evaluate(
                r"""() => {
                    const match = location.hash.match(/^#\/draft\/(\d+)$/);
                    return match ? match[1] : '';
                }"""
            )
            or ""
        )
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
            await title_field.fill(expected_title)
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
                await editor.click(timeout=5000)
            except Exception:
                await editor.evaluate("(el) => el.focus()")
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
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
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
                        await self.page.keyboard.press("Enter")
                await self._place_body_caret_at_end()
                lines = text.splitlines() or [text]
                for line_index, line in enumerate(lines):
                    if line.strip():
                        await self.page.keyboard.insert_text(line.strip())
                    if line_index < len(lines) - 1:
                        await self.page.keyboard.press("Enter")
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
                    await self.page.keyboard.press("Enter")
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
            await editor.press("Control+End")
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
            await trigger_matches[0].click(timeout=5000)
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
            await option_matches[0].click(timeout=5000)
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
            await matches[0].hover(timeout=5000)
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
            await editor.press("Control+End")
            await self.page.keyboard.press("ArrowDown")
            await self.page.keyboard.press("ArrowRight")
            await self.page.keyboard.press("Enter")
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
                    const tokens = [];
                    for (const node of root.children) {
                        const images = node.matches('img')
                            ? [node] : Array.from(node.querySelectorAll('img'));
                        if (images.length) {
                            for (const _image of images) tokens.push({kind: 'image'});
                            continue;
                        }
                        const text = node.innerText || node.textContent || '';
                        if (!text.trim()) continue;
                        const tag = node.tagName.toLowerCase();
                        if (/^h[1-6]$/.test(tag)) {
                            tokens.push({
                                kind: 'heading',
                                level: Number(tag.slice(1)),
                                text,
                            });
                        } else {
                            tokens.push({kind: 'text', text});
                        }
                    }
                    return tokens;
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
        normalized: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ContentValidationError(
                    "WEIBO_CONTENT_DOM_VERIFY_FAILED: DOM token 无效"
                )
            kind = item.get("kind")
            if kind == "image":
                normalized.append({"kind": "image"})
                continue
            text = normalize_for_comparison(item.get("text"))
            if not text:
                continue
            if kind == "heading":
                normalized.append(
                    {
                        "kind": "heading",
                        "level": int(item.get("level") or 0),
                        "text": text,
                    }
                )
            elif kind == "text":
                for paragraph in extract_expected_paragraphs(
                    [{"type": "text", "text": text}]
                ):
                    normalized.append(
                        {"kind": "text", "text": paragraph.comparison_text}
                    )
            else:
                raise ContentValidationError(
                    "WEIBO_CONTENT_DOM_VERIFY_FAILED: DOM token 类型无效"
                )
        return normalized

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
            trigger = await self._find_body_image_trigger()
            before = await self.page.evaluate(
                """() => document.querySelectorAll(
                    '.tiptap img, .ProseMirror img'
                ).length"""
            )
            await trigger.click(timeout=5000)
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
            await body_input.set_input_files(str(image_path), timeout=15000)

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
            await uploaded_item.click(timeout=5000)
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
            await insert_button.click(timeout=5000)
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
                observed = await self.page.evaluate(
                    """() => document.querySelectorAll(
                        '.tiptap img, .ProseMirror img'
                    ).length"""
                )
                if observed <= before:
                    continue
                await asyncio.sleep(1)
                stable = await self.page.evaluate(
                    """() => document.querySelectorAll(
                        '.tiptap img, .ProseMirror img'
                    ).length"""
                )
                if stable == observed == before + 1:
                    after = stable
                    break
            if after != before + 1:
                return {
                    "success": False,
                    "error_code": "WEIBO_EDITOR_IMAGE_COUNT_UNCHANGED",
                    "error": "上传后正文编辑器图片数量没有稳定且只增加一张",
                }
            return {
                "success": True,
                "error": "",
                "observed_image_count": after,
            }
        except ContentValidationError as exc:
            return {
                "success": False,
                "error_code": "WEIBO_BODY_IMAGE_TRIGGER_NOT_UNIQUE",
                "error": safe_media_error(
                    exc,
                    fallback="微博正文插图入口不存在或不唯一",
                ),
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
        """从正文图片中选择第一张设为文章封面（微博封面必须来自正文图）。

        2026-08-17 用户实测：**微博正文插图可自动化上传**（input[type=file]
        控件存在，图片随文章保存/发布成功）。封面弹窗从正文图缩略图中选
        第一张；正文无图则弹窗无图可选，set_cover 如实失败、绝不假成功。
        用户手动粘贴/拖拽插图后可手动设置封面。
        """

        self._require_page_alive("微博设置封面")
        clicked = await self.page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('*'));
                const target = nodes.find(el => {
                    const t = (el.innerText || '').trim();
                    return t === '设置文章封面' && el.children.length === 0;
                });
                if (!target) return 'not-found';
                const clickable = target.closest(
                    'button, [role="button"], [class*="btn" i], [class*="click" i], label, a'
                );
                if (clickable) { clickable.click(); return 'clicked'; }
                target.click();
                return 'clicked';
            }"""
        )
        if clicked != "clicked":
            return {"success": False, "error": "微博「设置文章封面」按钮未找到"}
        await self.simulator.random_delay(1, 2)

        # 弹窗中选择第一张正文图片（缩略图/图片容器）
        picked = await self.page.evaluate(
            """() => {
                const dialog = document.querySelector('.n-dialog');
                if (!dialog) return 'no-dialog';
                const imgs = Array.from(dialog.querySelectorAll('img'));
                if (imgs.length === 0) return 'no-images';
                const container = imgs[0].closest(
                    '[class*="item" i], [class*="pic" i], label'
                );
                const target = container || imgs[0];
                target.click();
                return 'picked';
            }"""
        )
        await self.simulator.random_delay(1, 2)
        if picked != "picked":
            return {"success": False, "error": "微博封面弹窗未找到正文图片"}

        # 点「下一步」（有图片时弹窗按钮应为「下一步」）
        next_clicked = await self.page.evaluate(
            """() => {
                const dialog = document.querySelector('.n-dialog');
                if (!dialog) return 'no-dialog';
                const buttons = Array.from(dialog.querySelectorAll('button'));
                const target = buttons.find(b =>
                    ['下一步', '确定', '完成', '使用该图'].includes((b.innerText || '').trim()));
                if (target) { target.click(); return 'clicked'; }
                return 'no-next-btn';
            }"""
        )
        await self.simulator.random_delay(2, 3)

        # 成功判据：弹窗关闭
        dialog_open = await self.page.evaluate(
            "() => !!document.querySelector('.n-dialog')"
        )
        if dialog_open:
            return {
                "success": False,
                "error": f"微博封面设置弹窗未关闭（pick={picked} next={next_clicked}）",
            }
        logger.info("微博封面已从正文首图设置完成")
        return {"success": True, "error": ""}

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
        expected_title = str(title or "").strip()
        expected_baseline_count = 1 if self._editing_existing_draft else 0
        if (
            not expected_title
            or expected_title != self._preflight_title
            or self._draft_title_baseline_count != expected_baseline_count
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博缺少与本次一致的唯一标题基线"
            )
        if self._expected_persisted_blocks is None:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博缺少冻结内容核验快照"
            )
        draft_id = self._active_draft_id
        if not draft_id.isdigit():
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 微博缺少当前草稿 ID"
            )

        try:
            current_id = str(
                await self.page.evaluate(
                    r"""() => {
                        const match = location.hash.match(/^#\/draft\/(\d+)$/);
                        return match ? match[1] : '';
                    }"""
                )
                or ""
            )
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
                if "draft/save" in url and method in {"POST", "PUT"}:
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
            click_result = await self.page.evaluate(
                """() => {
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
                }"""
            )
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
                    "DRAFT_RESULT_UNKNOWN: 微博保存期间出现公开发布请求，已拦截且禁止重试"
                )
            status = captured.get("status")
            if not isinstance(status, int) or not 200 <= status < 300:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博保存动作已触发但未观察到 2xx 保存响应"
                )

            await self.page.goto(
                "https://card.weibo.com/article/v5/editor#/draft",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            opened = False
            for _ in range(4):
                await self.simulator.random_delay(2, 3)
                card_result = await self.page.evaluate(
                    r"""(title) => {
                        const visible = (node) => {
                            const rect = node.getBoundingClientRect();
                            const style = getComputedStyle(node);
                            return rect.width > 0 && rect.height > 0
                                && style.display !== 'none'
                                && style.visibility !== 'hidden';
                        };
                        const matches = Array.from(
                            document.querySelectorAll('.list-item')
                        ).filter((card) => visible(card)
                            && (card.innerText || '').split(/\r?\n/, 1)[0].trim()
                                === title);
                        if (matches.length !== 1) {
                            return {clicked: false, count: matches.length};
                        }
                        matches[0].click();
                        return {clicked: true, count: 1};
                    }""",
                    expected_title,
                )
                if not isinstance(card_result, dict) or not card_result.get("clicked"):
                    continue
                for _ in range(10):
                    observed_id = str(
                        await self.page.evaluate(
                            r"""() => {
                                const match = location.hash.match(/^#\/draft\/(\d+)$/);
                                return match ? match[1] : '';
                            }"""
                        )
                        or ""
                    )
                    if observed_id == draft_id:
                        opened = True
                        break
                    await asyncio.sleep(0.5)
                if opened:
                    break
            if not opened:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博未找到标题精确匹配且绑定本次 ID 的唯一草稿"
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
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博草稿重开后标题不一致"
                )
            await self._validate_dom_exact(
                self._expected_persisted_blocks,
                phase="草稿重开后",
            )
            logger.info(
                "微博草稿验证成功: draft ID={}，接口 status={} code={}",
                draft_id,
                captured.get("status"),
                captured.get("code"),
            )
            return f"https://card.weibo.com/article/v5/editor#/draft/{draft_id}"
        except DraftBaselineError:
            raise
        except DraftResultUnknownError:
            raise
        except Exception as exc:
            if save_triggered:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 微博保存动作已触发但持久化结果无法证明"
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

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
