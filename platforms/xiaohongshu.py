"""小红书创作服务平台账号会话适配器。

⚠️ 风控警告：小红书风控严格，所有涉及小红的任务必须先读
``docs/XIAOHONGSHU_RISK_CONTROL.md``，并遵守其中的硬性纪律。

登录态与身份验证链路（真实扫码 → 会话 cookie → 身份捕获）已真实验收。
正文图文写入和长文排版可以在隔离 Profile 内完成，但 2026-08-21 复核证明
网页端“暂存离开/草稿箱”只保存在该浏览器 Profile，本账号在无 LocalStorage/
IndexedDB 的新上下文中看不到这些卡片；最终页也只有“发布笔记”，没有云端
“保存草稿”入口。因此 DRAFT 投递当前明确关闭，绝不能把本地卡片冒充云端
草稿成功；公开发布同样始终关闭。

真实登录页（https://creator.xiaohongshu.com/login）：
- 「APP扫一扫登录」为默认 Tab，二维码为约 160x160 的 base64 PNG。
- 登录成功信号：.xiaohongshu.com 出现 web_session cookie。
- 平台接口带重型反爬（as.xiaohongshu.com 签名/验证码），裸 fetch 会被拒；
  因此身份提取采用「捕获页面自身响应」模式，与小黑盒 restore_login 一致。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

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

CREATOR_HOME = "https://creator.xiaohongshu.com/"
CREATOR_MAIN = "https://creator.xiaohongshu.com/new/home"
LOGIN_URL = "https://creator.xiaohongshu.com/login"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name"}
IDENTITY_UID_KEYS = {"user_id", "sec_uid", "uid"}

# 2026-08-20 真实只读探测：点击长文 TipTap 工具栏中唯一 SVG 指纹按钮后，
# 页面才动态创建正文图片 FileChooser；不存在可安全复用的静态 file input。
XHS_EDITOR_SELECTOR = "div.tiptap.ProseMirror"
XHS_TOOLBAR_BUTTON_SELECTOR = ".edit-page.new-ui button.menu-item"
XHS_LAYOUT_ROOT_SELECTOR = ".editor-cards-container"
XHS_LAYOUT_CARD_SELECTOR = ".editor-cards-wrapper .card-outer-container"
XHS_LAYOUT_IMAGE_SELECTOR = f"{XHS_LAYOUT_ROOT_SELECTOR} img.resizable-image"
VERIFIED_BODY_IMAGE_ICON_FINGERPRINT = (
    "75d8717d8ac60eb26ac4490c408d1ee766029f5cf293b8adc1dc36d89bce11f2"
)
VERIFIED_H2_ICON_FINGERPRINT = "716e4ef689591d5c7c193e7ece200b8066058db8142d4bf7b182e44af2f8cab0"
VERIFIED_BODY_IMAGE_ACCEPT = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }
)


def choose_verified_body_image_index(
    inputs: Sequence[Mapping[str, object]],
) -> int | None:
    """Choose the unique body image input from sanitized probe evidence."""

    candidates: list[int] = []
    for index, item in enumerate(inputs):
        if str(item.get("type") or "").lower() != "file":
            continue
        if not bool(item.get("in_body_editor")):
            continue
        if "image" not in str(item.get("accept") or "").lower():
            continue
        labels = item.get("neighbor_labels", [])
        if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes)):
            label_text = " ".join(str(label) for label in labels)
            if re.search(r"(?:\u5c01\u9762|\bcover\b)", label_text, flags=re.IGNORECASE):
                continue
        candidates.append(index)
    return candidates[0] if len(candidates) == 1 else None


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class XiaohongshuCloudDraftUnavailableError(DraftBaselineError):
    """小红书网页长文只有 Profile 本地草稿，不能形成云端 DRAFT 实体。"""

    error_code = "XHS_CLOUD_DRAFT_UNAVAILABLE"


class XiaohongshuPlatform(BasePlatform):
    """小红书账号会话与长文草稿适配器；证据不足时保持 fail closed。"""

    platform_name = "xiaohongshu"
    use_native_viewport = True
    # 2026-08 真实验证：小红书已不再下发 web_session，现行会话 cookie 为
    # customer-sso-sid / customerClientId / access-token-creator.* /
    # x-user-id-creator.* / galaxy_creator_session_id；web_session 保留兼容。
    SESSION_COOKIE_NAMES = frozenset(
        {
            "web_session",
            "customer-sso-sid",
            "customerClientId",
            "access-token-creator.xiaohongshu.com",
            "x-user-id-creator.xiaohongshu.com",
            "galaxy_creator_session_id",
            "galaxy.creator.beaker.session.id",
        }
    )
    LOGIN_POLL_ATTEMPTS = 40
    LOGIN_POLL_INTERVAL_SECONDS = 3

    def __init__(self, *, resume_existing_title: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None
        self._expected_persisted_blocks: list[dict] | None = None
        self._expected_persisted_cover = False
        self._layout_finalized = False
        self._layout_expected_image_count = 0
        self._preflight_title = ""
        self._preflight_matching_draft_count = 0
        self._resume_existing_title = " ".join(str(resume_existing_title or "").split())
        self._editing_existing_draft = False

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None

    # ==================== 登录态与身份 ====================

    async def _has_session_cookie_signal(self) -> bool:
        """会话 cookie 作为登录成功信号；不返回、不记录 cookie 名和值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies(
                [
                    "https://creator.xiaohongshu.com/",
                    "https://www.xiaohongshu.com/",
                ]
            )
        except Exception:
            return False
        return any(str(item.get("name") or "") in self.SESSION_COOKIE_NAMES for item in cookies)

    async def check_login(self) -> bool:
        """只读验证现有 Profile；会话 cookie 出现才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("小红书登录态检测")
            await self.page.goto(
                CREATOR_HOME,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(4)
            if await self._has_session_cookie_signal():
                return True
            self.last_login_error = "LOGIN_REQUIRED: 小红书账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "XHS_LOGIN_CHECK_ERROR: 小红书登录态验证失败"
            return False

    async def login(self):
        """打开小红书创作服务登录页，等待用户在隔离 Profile 中扫码登录。"""

        self._require_page_alive("小红书打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=15000,
        )
        # 等待二维码出现：约 160x160 的 base64 PNG
        try:
            await self.page.wait_for_function(
                """() => {
                    const imgs = Array.from(
                        document.querySelectorAll('img[src^="data:image/png;base64"]')
                    );
                    return imgs.some(
                        (i) => i.naturalWidth > 130 && i.naturalWidth < 220
                    );
                }""",
                timeout=15000,
            )
        except Exception:
            pass
        await self._show_scan_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("小红书等待登录")
            if await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: 小红书登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_scan_hint(self):
        """页面顶部显示扫码提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'xhs-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#ff2442;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请用小红书 App 扫码登录'
                        + '<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """返回创作者首页同源确认的最小平台身份。

        策略：优先捕获页面自身的用户接口响应（resource_type 同时覆盖
        xhr 与 fetch）；捕获失败时回退到首页 DOM 提取（侧栏 ``user-info``
        昵称 + 「小红书账号:」正则）。缺失时如实返回未确认，绝不伪造。
        """

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:

            async def _on_response(response) -> None:
                try:
                    if (
                        response.request.resource_type in ("xhr", "fetch")
                        and "xiaohongshu.com" in response.url
                        and any(
                            key in response.url.lower()
                            for key in ("user", "profile", "account", "selfinfo")
                        )
                    ):
                        payload = await response.json()
                        found = self._extract_identity_from_json(payload)
                        if found and self._identity_payload is None:
                            self._identity_payload = {
                                "ok": True,
                                "user_id": found[0],
                                "display_name": found[1],
                            }
                except Exception:  # noqa: BLE001
                    pass

            self.page.on("response", _on_response)
            try:
                await self.page.goto(
                    CREATOR_MAIN,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                for _ in range(8):
                    if self._identity_payload is not None:
                        break
                    await asyncio.sleep(1)
            finally:
                try:
                    self.page.remove_listener("response", _on_response)
                except Exception:  # noqa: BLE001
                    pass
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书身份捕获时页面已关闭"
                ) from exc
            logger.warning("小红书身份捕获失败: {}", exc)

        # DOM 兜底：侧栏 user-info 昵称 + 「小红书账号:」行
        if not (isinstance(self._identity_payload, dict) and self._identity_payload.get("ok")):
            try:
                dom = await self.page.evaluate(
                    """() => {
                        const body = document.body.innerText || '';
                        const idMatch = body.match(/小红书账号[:：]\\s*(\\d{5,})/);
                        const userInfo = document.querySelector('[class*="user-info"]');
                        const nickname = userInfo
                            ? (userInfo.innerText || '').trim()
                            : '';
                        return {
                            user_id: idMatch ? idMatch[1] : '',
                            display_name: nickname,
                        };
                    }"""
                )
                if isinstance(dom, dict) and dom.get("user_id") and dom.get("display_name"):
                    self._identity_payload = {
                        "ok": True,
                        "user_id": str(dom["user_id"]),
                        "display_name": str(dom["display_name"]),
                    }
            except Exception:  # noqa: BLE001
                pass

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        return {"ok": False, "user_id": "", "display_name": ""}

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

    # ==================== 长文草稿投递链路 ====================

    @staticmethod
    def _not_implemented(operation: str):
        raise PlatformNotImplementedError(
            f"PLATFORM_NOT_IMPLEMENTED: 小红书{operation}能力尚未接入"
        )

    async def navigate_to_editor(self):
        """打开小红书长文编辑器：发布页 → 写长文 → 新的创作。

        真实结构（2026-08 探测）：标题 ``textarea.d-text``（0/64），
        正文 ``div.tiptap.ProseMirror``（TipTap contenteditable）。
        进入编辑器前先记录发布页侧栏的草稿箱计数，供 save_draft 验证。
        """
        self._require_page_alive("小红书打开编辑器")
        if self._editing_existing_draft:
            editor = self.page.locator(XHS_EDITOR_SELECTOR).first
            if await editor.count() == 1 and await editor.is_visible():
                return
            raise SelectorError("小红书待恢复草稿编辑器已失效")
        try:
            await self.page.goto(
                "https://creator.xiaohongshu.com/publish/publish",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            self._draft_box_count_before = await self._draft_box_count()
            await self._click_sidebar_text("写长文")
            await self._click_sidebar_text("新的创作")
            await self.page.wait_for_selector(
                "textarea.d-text",
                state="visible",
                timeout=20000,
            )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书打开编辑器时页面已关闭"
                ) from exc
            raise SelectorError("小红书长文编辑器未找到标题输入框") from exc

    async def preflight_delivery(self, title: str) -> None:
        """在任何编辑器副作用前拒绝不可跨浏览器验证的本地草稿。"""

        expected_title = " ".join(str(title or "").split())
        if not expected_title:
            raise DraftBaselineError("DRAFT_BASELINE_FAILED: 小红书标题不能为空")
        raise XiaohongshuCloudDraftUnavailableError(
            "XHS_CLOUD_DRAFT_UNAVAILABLE: 小红书网页长文仅保存到隔离浏览器本地，"
            "当前没有可跨浏览器回读的云端草稿入口"
        )

    async def _open_long_draft_drawer(self) -> None:
        """使用真实鼠标事件打开草稿抽屉并切换到长文笔记。"""

        entries = self.page.locator(".draft-title-box")
        await entries.first.wait_for(state="visible", timeout=15000)
        visible_entries = []
        for index in range(await entries.count()):
            entry = entries.nth(index)
            if await entry.is_visible():
                visible_entries.append(entry)
        if len(visible_entries) != 1:
            raise DraftBaselineError("DRAFT_BASELINE_FAILED: 小红书草稿箱入口不唯一")
        await visible_entries[0].click(timeout=15000)
        tabs = self.page.get_by_text(re.compile(r"^长文笔记\(\d+\)$"))
        visible_tabs = []
        for index in range(await tabs.count()):
            tab = tabs.nth(index)
            if await tab.is_visible():
                visible_tabs.append(tab)
        if len(visible_tabs) != 1:
            raise DraftBaselineError("DRAFT_BASELINE_FAILED: 小红书长文草稿分类不唯一")
        tab_label = " ".join((await visible_tabs[0].inner_text()).split())
        count_match = re.fullmatch(r"长文笔记\((\d+)\)", tab_label)
        if count_match is None:
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 小红书长文草稿数量标签无法识别"
            )
        expected_items = int(count_match.group(1))
        await visible_tabs[0].click(timeout=15000)
        await self.page.wait_for_selector(
            ".draft-drawer .draft-list",
            state="visible",
            timeout=15000,
        )
        cards = self.page.locator(".draft-drawer .draft-list .draft-item")
        stable_samples = 0
        for _ in range(60):
            visible_count = 0
            for index in range(await cards.count()):
                if await cards.nth(index).is_visible():
                    visible_count += 1
            if visible_count == expected_items:
                stable_samples += 1
                if stable_samples >= 2:
                    return
            else:
                stable_samples = 0
            await asyncio.sleep(0.25)
        raise DraftBaselineError(
            "DRAFT_BASELINE_FAILED: 小红书长文草稿列表未完整稳定加载"
        )

    async def _matching_long_draft_cards(self, title: str) -> list:
        """返回标题行精确匹配的草稿卡；不读取正文或资源地址。"""

        expected_key = self._draft_title_key(title)
        cards = self.page.locator(".draft-drawer .draft-list .draft-item")
        matches = []
        for index in range(await cards.count()):
            card = cards.nth(index)
            title_field = card.locator(".draft-title-text").first
            if await title_field.count() != 1:
                continue
            card_title = await title_field.inner_text()
            if expected_key and self._draft_title_key(card_title) == expected_key:
                matches.append(card)
        return matches

    @staticmethod
    def _draft_title_key(value: str) -> str:
        """消除排版页为换行自动插入的空白，但不做模糊包含匹配。"""

        return "".join(normalize_for_comparison(value).split())

    async def _draft_box_count(self) -> int | None:
        """读取发布页侧栏「草稿箱(N)」计数。

        页面先渲染占位「草稿箱(0)」，真实计数稍后到达；因此要求两次
        连续读数一致才算稳定，避免把占位 0 当基准。读不到返回 None。
        """

        last: int | None = None
        for _ in range(6):
            try:
                value = await self.page.evaluate(
                    """() => {
                        const nodes = Array.from(document.querySelectorAll('*'));
                        const el = nodes.find((n) => {
                            const t = (n.innerText || '').trim();
                            return t.startsWith('草稿箱') && t.length < 20;
                        });
                        if (!el) return null;
                        const m = (el.innerText || '').match(/草稿箱\\s*\\((\\d+)\\)/);
                        return m ? parseInt(m[1], 10) : null;
                    }"""
                )
                if isinstance(value, int):
                    if last is not None and value == last:
                        return value
                    last = value
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(3)
        return last

    async def _click_sidebar_text(self, text: str) -> None:
        """点击侧栏中文本精确匹配的元素（写长文/新的创作）。

        先等待目标文本渲染完成（SPA 侧栏异步加载），再点击。
        """

        try:
            await self.page.wait_for_function(
                """(t) => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    return nodes.some((el) => {
                        const txt = (el.innerText || '').trim();
                        return txt === t && el.children.length <= 3;
                    });
                }""",
                arg=text,
                timeout=15000,
            )
        except Exception:
            pass
        clicked = await self.page.evaluate(
            """(t) => {
                const nodes = Array.from(document.querySelectorAll('*'));
                const target = nodes.find((el) => {
                    const txt = (el.innerText || '').trim();
                    return txt === t && el.children.length <= 3;
                });
                if (target) { target.click(); return true; }
                return false;
            }""",
            text,
        )
        if not clicked:
            raise SelectorError(f"小红书侧栏未找到「{text}」入口")
        await self.simulator.random_delay(1, 2)

    async def fill_title(self, title: str):
        """填写小红书长文标题（textarea.d-text，placeholder「输入标题」）。"""

        self._require_page_alive("小红书填写标题")
        title_field = self.page.locator("textarea.d-text").first
        try:
            if self._editing_existing_draft:
                if await title_field.count() == 1 and await title_field.is_visible():
                    persisted = await title_field.input_value()
                    if normalize_for_comparison(persisted) != normalize_for_comparison(title):
                        raise ContentValidationError(
                            "XHS_RESUME_TITLE_MISMATCH: 待恢复草稿标题与冻结版本不一致"
                        )
                    return

                snapshot = await self._layout_snapshot()
                if snapshot.get("root_visible") is True:
                    observed_cover_title = str(snapshot.get("cover_title") or "")
                    if observed_cover_title and self._draft_title_key(
                        title
                    ) == self._draft_title_key(observed_cover_title):
                        return
                    if (
                        not self._expected_persisted_cover
                        and normalize_for_comparison(title)
                        == normalize_for_comparison(self._preflight_title)
                    ):
                        return
                raise ContentValidationError(
                    "XHS_RESUME_TITLE_MISMATCH: 待恢复排版草稿标题与冻结版本不一致"
                )
            if await title_field.count() == 0 or not await title_field.is_visible():
                raise RuntimeError("标题输入框不可见")
            await title_field.click()
            await title_field.fill(str(title or "").strip())
        except ContentValidationError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书填写标题时页面已关闭"
                ) from exc
            logger.error("小红书标题填写失败: {}", exc)
            raise SelectorError("小红书标题输入框未找到或填写失败") from exc

    async def fill_content(self, content_blocks: list, images: list):
        """按冻结图文块顺序写入 TipTap；图片失败后停止且不自动重试。"""

        self._require_page_alive("小红书填写正文")
        expected_images = sum(1 for block in content_blocks if block.get("type") == "image")
        if self._editing_existing_draft:
            return await self._verify_resumed_content(
                content_blocks,
                expected_images=expected_images,
            )

        editor = self.page.locator("div.tiptap.ProseMirror").first
        try:
            if await editor.count() == 0 or not await editor.is_visible():
                raise RuntimeError("正文编辑器不可见")
            await editor.click()
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("小红书正文编辑器未找到") from exc

        try:
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
        except Exception:
            pass
        await self.simulator.random_delay(0.3, 0.8)

        uploaded_images = 0
        failed_images: list[dict[str, str]] = []
        wrote_any = False
        previous_was_image = False

        for block in content_blocks:
            if not isinstance(block, dict):
                raise ContentValidationError("XHS_CONTENT_CONTRACT_INVALID: 正文块无效")
            btype = block.get("type")
            if btype in ("text", "heading") and block.get("text"):
                text = str(block["text"]).strip()
                if not text:
                    continue
                if btype == "heading" and (block.get("level") != 2 or "\n" in text or "\r" in text):
                    raise ContentValidationError("XHS_HEADING_UNSUPPORTED: 仅支持单行二级标题")
                await self._place_body_caret_at_end()
                if wrote_any and not previous_was_image:
                    await self.page.keyboard.press("Enter")
                    # 分段间留足节奏，降低风控敏感度
                    await self.simulator.random_delay(0.8, 1.5)
                lines = text.splitlines() or [text]
                for i, line in enumerate(lines):
                    if line.strip():
                        await self.page.keyboard.insert_text(line.strip())
                        await self.simulator.random_delay(0.1, 0.3)
                    if i < len(lines) - 1:
                        await self.page.keyboard.press("Enter")
                        await self.simulator.random_delay(0.8, 1.5)
                if btype == "heading":
                    await self._apply_h2_to_current_block()
                wrote_any = True
                previous_was_image = False
            elif btype == "image":
                await self._place_body_caret_at_end()
                img_path = block.get("local_path")
                if not img_path and images:
                    matches = [
                        img.get("local_path")
                        for img in images
                        if img.get("position_index") == block.get("position")
                        and img.get("local_path")
                    ]
                    if len(matches) == 1:
                        img_path = matches[0]
                if img_path:
                    if wrote_any and not previous_was_image:
                        await self.page.keyboard.press("Enter")
                        await self.simulator.random_delay(0.8, 1.5)
                    upload_result = await self._upload_image(img_path) or {}
                    if upload_result.get("success"):
                        uploaded_images += 1
                        wrote_any = True
                        previous_was_image = True
                        # 图片节点后创建下一段，确保后续文字不会落到图片前面。
                        await self._place_body_caret_at_end()
                        await self.page.keyboard.press("Enter")
                    else:
                        failed_images.append(
                            {
                                "filename": Path(str(img_path)).name,
                                "error": safe_media_error(
                                    upload_result.get("error"),
                                    fallback="图片上传失败",
                                ),
                            }
                        )
                        break
                    # 每张图片之间放慢节奏，避免触发平台风控
                    await self.simulator.random_delay(2.5, 4.5)
                else:
                    failed_images.append({"filename": "", "error": "文章图片块没有对应本地文件"})
                    break
            elif btype not in ("text", "heading"):
                raise ContentValidationError("XHS_CONTENT_CONTRACT_INVALID: 未知正文块类型")

        editor = self.page.locator(XHS_EDITOR_SELECTOR).first
        actual_text = await editor.inner_text()
        expected_count = ensure_valid_content(
            content_blocks,
            actual_text,
            platform="小红书",
            phase="图文写入后",
        )
        logger.info("小红书正文输入并最终验证成功: {} 个文本段落", expected_count)
        await self._assert_editor_body_tokens(content_blocks)
        self._expected_persisted_blocks = copy.deepcopy(content_blocks)
        self._layout_expected_image_count = expected_images
        self._layout_finalized = False
        self._expected_persisted_cover = False

        if expected_images == 0:
            media_status = "not_required"
            media_error = None
        elif uploaded_images == expected_images:
            media_status = "completed"
            media_error = None
        elif uploaded_images == 0:
            media_status = "failed"
            media_error = f"{expected_images} 张图片全部上传失败"
        else:
            media_status = "partial"
            media_error = f"{expected_images - uploaded_images} 张图片上传失败"

        if failed_images:
            logger.warning(
                "小红书图片处理结果: expected={}, uploaded={}, failed={}",
                expected_images,
                uploaded_images,
                len(failed_images),
            )
        return {
            "text_ok": True,
            "expected_images": expected_images,
            "uploaded_images": uploaded_images,
            "failed_images": failed_images,
            "media_status": media_status,
            "media_error": media_error,
        }

    async def _verify_resumed_content(
        self,
        content_blocks: list,
        *,
        expected_images: int,
    ) -> dict:
        """只读核验同名草稿；一致时接管，绝不清空、重写或重复上传。"""

        snapshot = await self._layout_snapshot()
        if snapshot.get("root_visible") is True:
            if (
                int(snapshot.get("image_count") or 0) != expected_images
                or int(snapshot.get("loaded_image_count") or 0) != expected_images
            ):
                raise ContentValidationError(
                    "XHS_RESUME_IMAGE_INVALID: 待恢复排版草稿图片未完整加载"
                )
            await self._assert_layout_body_tokens(content_blocks)
            self._expected_persisted_blocks = copy.deepcopy(content_blocks)
            self._layout_expected_image_count = expected_images
            self._layout_finalized = True
            self._expected_persisted_cover = expected_images > 0
            return self._completed_resume_result(expected_images)

        editor = self.page.locator(XHS_EDITOR_SELECTOR).first
        if await editor.count() != 1 or not await editor.is_visible():
            raise ContentValidationError(
                "XHS_RESUME_EDITOR_INVALID: 待恢复草稿正文编辑器不可唯一确认"
            )
        try:
            ensure_valid_content(
                content_blocks,
                await editor.inner_text(),
                platform="小红书",
                phase="接管原始草稿前",
            )
            await self._assert_editor_body_tokens(content_blocks)
        except ContentValidationError:
            repaired = await self._repair_strict_trailing_text_only(content_blocks)
            if not repaired:
                raise
            await self._reopen_and_verify_resumed_raw_draft(
                content_blocks,
                expected_images=expected_images,
            )
        image_state = await self._raw_editor_image_state()
        if (
            image_state["count"] != expected_images
            or image_state["loaded_count"] != expected_images
        ):
            raise ContentValidationError("XHS_RESUME_IMAGE_INVALID: 待恢复原始草稿图片未完整加载")

        self._expected_persisted_blocks = copy.deepcopy(content_blocks)
        self._layout_expected_image_count = expected_images
        self._layout_finalized = False
        self._expected_persisted_cover = False
        return self._completed_resume_result(expected_images)

    async def _repair_strict_trailing_text_only(self, content_blocks: list) -> bool:
        """只补严格前缀之后的普通文本尾段；中间缺失、标题或图片一律拒绝。"""

        expected = self._expected_body_tokens(content_blocks)
        actual = await self._editor_body_tokens()
        if not actual or len(actual) >= len(expected):
            return False
        for wanted, observed in zip(expected[: len(actual)], actual, strict=True):
            if wanted["kind"] != observed["kind"]:
                return False
            if wanted["kind"] == "text" and wanted["text"] != observed["text"]:
                return False
            if wanted["tag"] == "h2" and observed["tag"] != "h2":
                return False

        missing = expected[len(actual) :]
        if not missing or any(
            item.get("kind") != "text" or item.get("tag") != "text" for item in missing
        ):
            return False

        await self._place_body_caret_at_end()
        for item in missing:
            await self.page.keyboard.press("Enter")
            await self.simulator.random_delay(0.8, 1.5)
            await self.page.keyboard.insert_text(str(item["text"]))
            await self.simulator.random_delay(0.8, 1.5)

        # 给平台的原始草稿自动保存留出时间；下一步还会离开并重开验证，
        # 因此这里只能修复同一草稿，不能靠当前 DOM 自证成功。
        await self.simulator.random_delay(6, 8)
        ensure_valid_content(
            content_blocks,
            await self.page.locator(XHS_EDITOR_SELECTOR).first.inner_text(),
            platform="小红书",
            phase="尾段修复后",
        )
        await self._assert_editor_body_tokens(content_blocks)
        return True

    async def _reopen_and_verify_resumed_raw_draft(
        self,
        content_blocks: list,
        *,
        expected_images: int,
    ) -> None:
        """重开同一唯一草稿，证明尾段和图片已由平台持久化。"""

        expected_title = self._preflight_title
        await self.page.goto(
            "https://creator.xiaohongshu.com/publish/publish",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await self._open_long_draft_drawer()
        matches = await self._matching_long_draft_cards(expected_title)
        if len(matches) != 1:
            raise ContentValidationError("XHS_RESUME_REOPEN_FAILED: 尾段修复后无法唯一重开同名草稿")
        actions = matches[0].locator(".draft-actions .btn").filter(has_text=re.compile(r"^编辑$"))
        if await actions.count() != 1:
            raise ContentValidationError("XHS_RESUME_REOPEN_FAILED: 尾段修复后编辑入口不唯一")
        await actions.click(timeout=15000)
        await self.page.wait_for_selector(
            XHS_EDITOR_SELECTOR,
            state="visible",
            timeout=20000,
        )
        editor = self.page.locator(XHS_EDITOR_SELECTOR).first
        ensure_valid_content(
            content_blocks,
            await editor.inner_text(),
            platform="小红书",
            phase="尾段修复重开后",
        )
        await self._assert_editor_body_tokens(content_blocks)
        image_state = await self._raw_editor_image_state()
        if (
            image_state["count"] != expected_images
            or image_state["loaded_count"] != expected_images
        ):
            raise ContentValidationError("XHS_RESUME_IMAGE_INVALID: 尾段修复重开后图片未完整加载")

    @staticmethod
    def _completed_resume_result(expected_images: int) -> dict:
        return {
            "text_ok": True,
            "expected_images": expected_images,
            "uploaded_images": expected_images,
            "failed_images": [],
            "media_status": "completed" if expected_images else "not_required",
            "media_error": None,
        }

    async def apply_cover(self, cover: dict | None = None) -> dict:
        """完成小红书长文排版，并把第一页作为平台封面。

        真实页面没有独立封面文件输入；点击「一键排版」才会把正文中的临时
        ``blob:`` 图片上传为可重开的远程资源，同时生成长文第一页。平台把
        该第一页作为封面，因此封面策略只能是不设置，或使用正文首图。
        """

        strategy = str((cover or {}).get("strategy") or "NONE").upper()
        if strategy not in {"NONE", "FIRST_BODY_IMAGE", "EXPLICIT"}:
            return self._layout_failure(
                "XHS_COVER_STRATEGY_UNSUPPORTED",
                "小红书不支持该封面策略",
            )

        expected_images = self._layout_expected_image_count
        wants_cover = strategy != "NONE"

        async def with_next_step_ui(result: dict) -> dict:
            """只附加展示提示；失败不得改变已完成的排版结果。"""

            try:
                ui_result = await self.prepare_next_step_visibility()
            except Exception:  # noqa: BLE001
                ui_result = {
                    "success": False,
                    "next_step_ui": "check_failed",
                    "error_code": "XHS_NEXT_STEP_VIEWPORT_CHECK_FAILED",
                    "error": "小红书下一步按钮展示提示失败",
                }
            return {**result, "next_step_ui": ui_result}

        if wants_cover:
            first_image = self._first_body_image_path()
            requested = str((cover or {}).get("local_path") or "")
            try:
                requested_path = Path(requested).resolve(strict=True)
                first_path = Path(first_image).resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                return self._layout_failure(
                    "XHS_COVER_ASSET_UNAVAILABLE",
                    "小红书封面素材不可用",
                )
            if expected_images < 1 or requested_path != first_path:
                return self._layout_failure(
                    "XHS_COVER_MUST_BE_FIRST_BODY_IMAGE",
                    "小红书长文封面必须使用正文首图",
                )

        if self._editing_existing_draft and self._layout_finalized:
            snapshot = await self._layout_snapshot()
            ready = (
                snapshot.get("root_visible") is True
                and int(snapshot.get("card_count") or 0) >= 1
                and snapshot.get("save_visible") is True
                and snapshot.get("next_visible") is True
                and int(snapshot.get("image_count") or 0) == expected_images
                and int(snapshot.get("loaded_image_count") or 0) == expected_images
            )
            if not ready:
                return self._layout_failure(
                    "XHS_RESUMED_LAYOUT_INVALID",
                    "小红书待恢复排版草稿未保持稳定",
                )
            try:
                await self._assert_layout_body_tokens(self._expected_persisted_blocks or [])
            except ContentValidationError:
                return self._layout_failure(
                    "XHS_RESUMED_LAYOUT_INVALID",
                    "小红书待恢复排版草稿图文或标题样式不一致",
                )
            self._expected_persisted_cover = wants_cover
            if wants_cover:
                return await with_next_step_ui(
                    {
                        "success": True,
                        "cover_status": "pending_verification",
                        "safe_to_continue": True,
                        "cover_mode": "PLATFORM_GENERATED_LONGFORM",
                    }
                )
            return await with_next_step_ui(
                {
                    "success": True,
                    "cover_status": "not_required",
                    "safe_to_continue": True,
                }
            )

        finalized = await self._finalize_long_text_layout(
            expected_images=expected_images,
            require_first_page_image=False,
        )
        if not finalized.get("success"):
            return finalized

        self._layout_finalized = True
        self._expected_persisted_cover = wants_cover
        if wants_cover:
            return await with_next_step_ui(
                {
                    "success": True,
                    "cover_status": "pending_verification",
                    "safe_to_continue": True,
                    "cover_mode": "PLATFORM_GENERATED_LONGFORM",
                }
            )
        return await with_next_step_ui(
            {
                "success": True,
                "cover_status": "not_required",
                "safe_to_continue": True,
            }
        )

    def _first_body_image_path(self) -> str:
        for block in self._expected_persisted_blocks or []:
            if block.get("type") == "image" and block.get("local_path"):
                return str(block["local_path"])
        return ""

    async def verify_persisted_cover(
        self,
        *,
        title: str,
        draft_url: str,
        cover: dict | None,
        apply_result: dict,
    ) -> dict:
        """进入发布预览页，验证平台生成封面存在且质量检查通过。

        小红书长文没有独立封面素材选择器；其真实封面是冻结长文排版生成的
        第一张 1440×2400 页面图。这里明确报告平台原生模式，不冒充精确
        ``FIRST_BODY_IMAGE`` 上传，也绝不点击公开发布按钮。
        """

        del title, draft_url, cover
        action = await self._unique_visible_button("下一步")
        if action is None:
            return self._cover_verification_failure(
                "XHS_COVER_PREVIEW_ENTRY_MISSING",
                "小红书封面预览入口不存在或候选不唯一",
            )
        await action.click(timeout=10000)
        try:
            await self.page.get_by_text("封面预览", exact=True).wait_for(
                state="visible", timeout=15000
            )
        except Exception:
            return self._cover_verification_failure(
                "XHS_COVER_PREVIEW_NOT_READY",
                "小红书平台生成封面预览未就绪",
            )
        state = await self.page.evaluate(
            """() => {
                const loaded = (selector) => Array.from(
                    document.querySelectorAll(selector)
                ).filter((image) => image.complete && image.naturalWidth > 0).length;
                return {
                    pagePreviews: loaded('img.img.preview'),
                    phonePreviews: loaded('img.preivew-image'),
                };
            }"""
        )
        if (
            not isinstance(state, dict)
            or int(state.get("pagePreviews") or 0) < 1
            or int(state.get("phonePreviews") or 0) < 1
        ):
            return self._cover_verification_failure(
                "XHS_COVER_PREVIEW_IMAGE_MISSING",
                "小红书平台生成封面图片未完整加载",
            )
        assessment = self.page.get_by_text("获取封面建议", exact=True)
        visible_assessments = [
            assessment.nth(index)
            for index in range(await assessment.count())
            if await assessment.nth(index).is_visible()
        ]
        if len(visible_assessments) == 1:
            await visible_assessments[0].click(timeout=5000)
        passed = False
        for _ in range(20):
            await asyncio.sleep(0.5)
            passed = await self.page.get_by_text(
                "封面效果评估通过，未发现封面质量问题", exact=True
            ).count() > 0
            if passed:
                break
        if not passed:
            return self._cover_verification_failure(
                "XHS_COVER_QUALITY_UNVERIFIED",
                "小红书平台生成封面未通过质量评估",
            )
        return {
            **apply_result,
            "success": True,
            "cover_status": "completed",
            "cover_mode": "PLATFORM_GENERATED_LONGFORM",
        }

    @staticmethod
    def _cover_verification_failure(error_code: str, error: str) -> dict:
        return {
            "success": False,
            "cover_status": "failed",
            "safe_to_continue": True,
            "error_code": error_code,
            "error": error,
        }

    @staticmethod
    def _layout_failure(error_code: str, error: str) -> dict:
        return {
            "success": False,
            "cover_status": "unverified",
            "safe_to_continue": False,
            "error_code": error_code,
            "error": error,
        }

    async def _unique_visible_button(self, label: str):
        candidates = self.page.get_by_text(label, exact=True)
        visible = []
        for index in range(await candidates.count()):
            item = candidates.nth(index)
            if await item.is_visible():
                visible.append(item)
        return visible[0] if len(visible) == 1 else None

    async def _unique_visible_exact_button(self, label: str):
        """仅按 button 角色确认唯一精确按钮，不改变旧调用方语义。"""

        candidates = self.page.get_by_role("button", name=label, exact=True)
        visible = []
        for index in range(await candidates.count()):
            item = candidates.nth(index)
            if await item.is_visible():
                visible.append(item)
        return visible[0] if len(visible) == 1 else None

    async def prepare_next_step_visibility(self) -> dict:
        """仅调整排版后的「下一步」按钮展示；不点击、不导航、不保存。

        该方法只提供 UI 可见性提示，不能改变排版或草稿成功状态。调用方应在
        排版成功后最多调用一次；若按钮不存在、不唯一或滚动后仍未完整落入真实
        视口，返回失败提示但不推翻已经完成的排版结果。
        """

        self._require_page_alive("小红书下一步视口准备")
        try:
            action = await self._unique_visible_exact_button("下一步")
        except Exception as exc:  # noqa: BLE001
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书下一步视口准备时页面已关闭"
                ) from exc
            return {
                "success": False,
                "next_step_ui": "check_failed",
                "error_code": "XHS_NEXT_STEP_VIEWPORT_CHECK_FAILED",
                "error": "小红书下一步按钮唯一性检查失败",
            }
        if action is None:
            return {
                "success": False,
                "next_step_ui": "not_unique",
                "error_code": "XHS_NEXT_STEP_NOT_UNIQUE",
                "error": "小红书下一步按钮不可见或候选不唯一",
            }
        try:
            await action.scroll_into_view_if_needed(timeout=10000)
            snapshot = await self._layout_snapshot()
        except BrowserLifecycleError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书下一步视口准备时页面已关闭"
                ) from exc
            return {
                "success": False,
                "next_step_ui": "check_failed",
                "error_code": "XHS_NEXT_STEP_VIEWPORT_CHECK_FAILED",
                "error": "小红书下一步按钮视口复核失败",
            }

        result = {
            "success": False,
            "next_step_ui": "not_ready",
            "next_visible": snapshot.get("next_visible") is True,
            "next_in_viewport": snapshot.get("next_in_viewport") is True,
            "next_center_uncovered": snapshot.get("next_center_uncovered"),
            "next_rect": snapshot.get("next_rect"),
            "viewport_width": snapshot.get("viewport_width"),
            "viewport_height": snapshot.get("viewport_height"),
        }
        if not result["next_in_viewport"]:
            result["next_step_ui"] = "out_of_viewport"
            result["error"] = "小红书下一步按钮未完整落入真实视口"
        elif result["next_center_uncovered"] is not True:
            result["next_step_ui"] = "obscured"
            result["error"] = "小红书下一步按钮中心被遮挡或无法确认"
        if (
            result["next_visible"]
            and result["next_in_viewport"]
            and result["next_center_uncovered"] is True
        ):
            result["success"] = True
            result["next_step_ui"] = "ready"
        return result

    async def _finalize_long_text_layout(
        self,
        *,
        expected_images: int,
        require_first_page_image: bool,
    ) -> dict:
        """单次点击排版并等待远程图片、模板和封面页稳定。"""

        self._require_page_alive("小红书长文排版")
        action = await self._unique_visible_button("一键排版")
        if action is None:
            return self._layout_failure(
                "XHS_LAYOUT_ACTION_NOT_UNIQUE",
                "小红书一键排版入口未唯一确认",
            )

        signals = {"preview": False, "cover": False}

        async def _on_response(response) -> None:
            try:
                if not 200 <= response.status < 300:
                    return
                path = response.url.split("?", 1)[0]
                if path.endswith("/long_text/template/preview"):
                    signals["preview"] = True
                elif path.endswith("/long_text/cover/images"):
                    signals["cover"] = True
            except Exception:  # noqa: BLE001
                return

        try:
            self.page.on("response", _on_response)
            await action.click(timeout=10000)
            ready = await self._wait_for_layout_ready(
                expected_images=expected_images,
                require_first_page_image=require_first_page_image,
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书排版时页面已关闭"
                ) from exc
            return self._layout_failure(
                "XHS_LAYOUT_FAILED",
                safe_media_error(exc, fallback="小红书长文排版失败"),
            )
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if not ready:
            return self._layout_failure(
                "XHS_LAYOUT_NOT_PERSISTABLE",
                "小红书排版后的图文或封面页未稳定就绪",
            )
        if not signals["preview"] or (require_first_page_image and not signals["cover"]):
            return self._layout_failure(
                "XHS_LAYOUT_API_UNVERIFIED",
                "小红书排版接口未返回完整成功证据",
            )
        return {"success": True}

    async def _wait_for_layout_ready(
        self,
        *,
        expected_images: int,
        require_first_page_image: bool,
    ) -> bool:
        stable = 0
        for _ in range(45):
            self._require_page_alive("小红书等待排版完成")
            snapshot = await self._layout_snapshot()
            ready = (
                snapshot.get("root_visible") is True
                and int(snapshot.get("card_count") or 0) >= 1
                and snapshot.get("save_visible") is True
                and snapshot.get("next_visible") is True
                and int(snapshot.get("image_count") or 0) == expected_images
                and int(snapshot.get("loaded_image_count") or 0) == expected_images
                and (
                    not require_first_page_image
                    or int(snapshot.get("first_card_loaded_images") or 0) >= 1
                )
            )
            if ready:
                try:
                    await self._assert_layout_body_tokens(self._expected_persisted_blocks or [])
                except ContentValidationError:
                    ready = False
            stable = stable + 1 if ready else 0
            if stable >= 2:
                return True
            await asyncio.sleep(2)
        return False

    async def _layout_snapshot(self) -> dict:
        return await self.page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const roots = Array.from(document.querySelectorAll(
                    '.editor-cards-container .tiptap.ProseMirror'
                ));
                const images = Array.from(document.querySelectorAll(
                    '.editor-cards-container img.resizable-image'
                ));
                const buttons = Array.from(document.querySelectorAll('button'));
                const exactVisible = (label) => buttons.some((button) =>
                    visible(button) && (button.innerText || '').trim() === label
                );
                const inViewport = (rect) => rect.left >= 0 && rect.top >= 0 &&
                    rect.right <= window.innerWidth && rect.bottom <= window.innerHeight;
                const rectNumbers = (rect) => ({
                    left: rect.left,
                    top: rect.top,
                    right: rect.right,
                    bottom: rect.bottom,
                });
                const centerUncovered = (button, rect) => {
                    if (!inViewport(rect)) return false;
                    const target = document.elementFromPoint(
                        (rect.left + rect.right) / 2,
                        (rect.top + rect.bottom) / 2,
                    );
                    return Boolean(target && (target === button || button.contains(target)));
                };
                const buttonDetails = (label) => {
                    const matches = buttons.filter((button) =>
                        visible(button) && (button.innerText || '').trim() === label
                    );
                    if (matches.length !== 1) {
                        return {
                            rect: null,
                            in_viewport: false,
                            center_uncovered: false,
                        };
                    }
                    const button = matches[0];
                    const rect = button.getBoundingClientRect();
                    return {
                        rect: rectNumbers(rect),
                        in_viewport: inViewport(rect),
                        center_uncovered: centerUncovered(button, rect),
                    };
                };
                const nextButton = buttonDetails('下一步');
                const saveButton = buttonDetails('暂存离开');
                const first = document.querySelector(
                    '.editor-cards-wrapper .card-outer-container'
                );
                const coverTitle = document.querySelector(
                    '.cover-outer-container .title.title-container'
                );
                return {
                    root_visible: visible(document.querySelector(
                        '.editor-cards-container'
                    )),
                    card_count: document.querySelectorAll(
                        '.editor-cards-wrapper .card-outer-container'
                    ).length,
                    save_visible: exactVisible('暂存离开'),
                    next_visible: exactVisible('下一步'),
                    viewport_width: window.innerWidth,
                    viewport_height: window.innerHeight,
                    save_rect: saveButton.rect,
                    save_in_viewport: saveButton.in_viewport,
                    save_center_uncovered: saveButton.center_uncovered,
                    next_rect: nextButton.rect,
                    next_in_viewport: nextButton.in_viewport,
                    next_center_uncovered: nextButton.center_uncovered,
                    image_count: images.length,
                    loaded_image_count: images.filter((image) =>
                        image.complete && image.naturalWidth > 0
                    ).length,
                    first_card_loaded_images: first ? Array.from(
                        first.querySelectorAll('img.resizable-image')
                    ).filter((image) => image.complete && image.naturalWidth > 0).length : 0,
                    first_card_text: first ? (first.innerText || '') : '',
                    cover_title: coverTitle ? (coverTitle.innerText || '') : '',
                    text: roots.map((root) => root.innerText || '').join('\\n'),
                };
            }"""
        )

    async def _layout_body_tokens(self) -> list[dict[str, str]]:
        raw = await self.page.locator(
            f"{XHS_LAYOUT_ROOT_SELECTOR} {XHS_EDITOR_SELECTOR}"
        ).evaluate_all(
            """(roots) => {
                const output = [];
                const flush = (buffer, tag) => {
                    const text = buffer.join('').trim();
                    if (text) output.push({kind: 'text', text, tag});
                    buffer.length = 0;
                };
                for (const root of roots) {
                    for (const child of Array.from(root.children)) {
                        const buffer = [];
                        const walker = document.createTreeWalker(
                            child,
                            NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
                        );
                        let node = walker.currentNode;
                        while (node) {
                            if (node.nodeType === Node.ELEMENT_NODE &&
                                    node.tagName === 'IMG') {
                                const heading = child.matches('.heading-level-2') ||
                                    Boolean(child.querySelector('.heading-level-2'));
                                flush(buffer, heading ? 'h2' : child.tagName.toLowerCase());
                                output.push({kind: 'image', text: '', tag: 'img'});
                            } else if (node.nodeType === Node.TEXT_NODE) {
                                buffer.push(node.textContent || '');
                            }
                            node = walker.nextNode();
                        }
                        const heading = child.matches('.heading-level-2') ||
                            Boolean(child.querySelector('.heading-level-2'));
                        flush(buffer, heading ? 'h2' : child.tagName.toLowerCase());
                    }
                }
                return output;
            }"""
        )
        return [
            {
                "kind": str(item.get("kind") or ""),
                "text": normalize_for_comparison(str(item.get("text") or "")),
                "tag": str(item.get("tag") or "").lower(),
            }
            for item in raw
            if isinstance(item, dict)
        ]

    async def _assert_layout_body_tokens(self, content_blocks: list) -> None:
        expected = self._expected_body_tokens(content_blocks)
        actual = await self._layout_body_tokens()
        cursor = 0
        for index, wanted in enumerate(expected):
            matched = False
            while cursor < len(actual):
                observed = actual[cursor]
                cursor += 1
                if wanted["kind"] != observed["kind"]:
                    continue
                if wanted["kind"] == "image":
                    matched = True
                    break
                if wanted["tag"] == "h2" and observed["tag"] != "h2":
                    continue
                if wanted["tag"] != "h2" and observed["tag"] == "h2":
                    continue
                expected_text = "".join(wanted["text"].split())
                combined = "".join(observed["text"].split())
                if not combined:
                    continue
                if combined == expected_text:
                    matched = True
                    break
                if not expected_text.startswith(combined):
                    continue
                while cursor < len(actual) and len(combined) < len(expected_text):
                    continuation = actual[cursor]
                    if continuation["kind"] != "text":
                        break
                    if wanted["tag"] == "h2" and continuation["tag"] != "h2":
                        break
                    if wanted["tag"] != "h2" and continuation["tag"] == "h2":
                        break
                    candidate = combined + "".join(continuation["text"].split())
                    if not expected_text.startswith(candidate):
                        break
                    combined = candidate
                    cursor += 1
                if combined == expected_text:
                    matched = True
                    break
            if not matched:
                raise ContentValidationError(
                    f"XHS_LAYOUT_ORDER_INVALID: 第 {index + 1} 个图文块未按序保留"
                )

    async def _place_body_caret_at_end(self) -> None:
        """把选区放到 TipTap 正文末尾，不依赖工具栏点击后的焦点状态。"""

        placed = await self.page.evaluate(
            """(selector) => {
                const root = document.querySelector(selector);
                if (!root || !root.isContentEditable) return false;
                root.focus();
                const range = document.createRange();
                range.selectNodeContents(root);
                range.collapse(false);
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
                return true;
            }""",
            XHS_EDITOR_SELECTOR,
        )
        if not placed:
            raise ContentValidationError("XHS_EDITOR_CARET_FAILED: 无法安全定位正文末尾")

    async def _apply_h2_to_current_block(self) -> None:
        """用真实 SVG 指纹定位 H2，并验证非空 h2 节点增加。"""

        button = await self._toolbar_button_by_fingerprint(VERIFIED_H2_ICON_FINGERPRINT)
        if button is None:
            raise ContentValidationError("XHS_H2_BUTTON_UNVERIFIED: 二级标题按钮不符合已验证指纹")
        headings = self.page.locator(f"{XHS_EDITOR_SELECTOR} > h2")
        before = await headings.count()
        await button.click(timeout=5000)
        for _ in range(10):
            headings = self.page.locator(f"{XHS_EDITOR_SELECTOR} > h2")
            if await headings.count() == before + 1 and normalize_for_comparison(
                await headings.last.inner_text()
            ):
                return
            await asyncio.sleep(0.2)
        raise ContentValidationError("XHS_H2_APPLY_FAILED: 当前正文块未变为二级标题")

    @staticmethod
    def _expected_body_tokens(content_blocks: list) -> list[dict[str, str]]:
        tokens: list[dict[str, str]] = []
        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "image":
                tokens.append({"kind": "image", "text": "", "tag": "img"})
                continue
            if block_type not in {"text", "heading"}:
                continue
            tag = "h2" if block_type == "heading" else "text"
            for paragraph in extract_expected_paragraphs([block]):
                tokens.append(
                    {
                        "kind": "text",
                        "text": paragraph.comparison_text,
                        "tag": tag,
                    }
                )
        return tokens

    async def _editor_body_tokens(self) -> list[dict[str, str]]:
        raw = await self.page.locator(XHS_EDITOR_SELECTOR).first.evaluate(
            """(root) => {
                const output = [];
                const flush = (buffer, tag) => {
                    const text = buffer.join('').trim();
                    if (text) output.push({kind: 'text', text, tag});
                    buffer.length = 0;
                };
                for (const child of Array.from(root.children)) {
                    const buffer = [];
                    const walker = document.createTreeWalker(
                        child,
                        NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
                    );
                    let node = walker.currentNode;
                    while (node) {
                        if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'IMG') {
                            flush(buffer, child.tagName.toLowerCase());
                            output.push({kind: 'image', text: '', tag: 'img'});
                        } else if (node.nodeType === Node.TEXT_NODE) {
                            buffer.push(node.textContent || '');
                        }
                        node = walker.nextNode();
                    }
                    flush(buffer, child.tagName.toLowerCase());
                }
                return output;
            }"""
        )
        return [
            {
                "kind": str(item.get("kind") or ""),
                "text": normalize_for_comparison(str(item.get("text") or "")),
                "tag": str(item.get("tag") or "").lower(),
            }
            for item in raw
            if isinstance(item, dict)
        ]

    async def _assert_editor_body_tokens(self, content_blocks: list) -> None:
        expected = self._expected_body_tokens(content_blocks)
        actual = await self._editor_body_tokens()
        if len(actual) != len(expected):
            raise ContentValidationError("XHS_BODY_ORDER_INVALID: 正文图文块数量与冻结版本不一致")
        for index, (wanted, observed) in enumerate(zip(expected, actual, strict=True)):
            if wanted["kind"] != observed["kind"]:
                raise ContentValidationError(
                    f"XHS_BODY_ORDER_INVALID: 第 {index + 1} 个图文块类型不一致"
                )
            if wanted["kind"] == "text" and wanted["text"] != observed["text"]:
                raise ContentValidationError(
                    f"XHS_BODY_ORDER_INVALID: 第 {index + 1} 个文本块内容不一致"
                )
            if wanted["tag"] == "h2" and observed["tag"] != "h2":
                raise ContentValidationError(
                    f"XHS_BODY_ORDER_INVALID: 第 {index + 1} 个二级标题样式不一致"
                )

    async def _upload_image(self, image_path: str) -> dict:
        """通过已验证工具栏按钮捕获临时 FileChooser，单次上传正文图片。"""

        self._require_page_alive("小红书上传图片")
        image_button = await self._get_verified_body_image_button()
        if image_button is None:
            return {
                "success": False,
                "error_code": "XHS_BODY_IMAGE_BUTTON_UNVERIFIED",
                "error": "小红书正文图片按钮不符合已验证指纹，已安全停止",
            }
        try:
            before = await self._editor_image_count()
            async with self.page.expect_file_chooser(timeout=5000) as pending:
                await image_button.click(timeout=5000)
            chooser = await pending.value
            metadata = await chooser.element.evaluate(
                """(el) => ({
                    type: String(el.type || '').toLowerCase(),
                    accept: String(el.accept || '').toLowerCase(),
                    multiple: Boolean(el.multiple),
                    cover: Boolean(el.closest('[class*=cover], [data-cover]')),
                })"""
            )
            accepted = {
                item.strip()
                for item in str(metadata.get("accept") or "").split(",")
                if item.strip()
            }
            if (
                metadata.get("type") != "file"
                or metadata.get("multiple") is True
                or metadata.get("cover") is True
                or not accepted
                or not accepted.issubset(VERIFIED_BODY_IMAGE_ACCEPT)
            ):
                return {
                    "success": False,
                    "error_code": "XHS_BODY_FILE_CHOOSER_UNVERIFIED",
                    "error": "小红书文件选择器属性与正文图片证据不一致",
                }
            await chooser.set_files(str(Path(image_path).resolve()), timeout=20000)
            observed = before
            for _ in range(15):
                await asyncio.sleep(1)
                observed = await self._editor_image_count()
                if observed <= before:
                    continue
                await asyncio.sleep(1)
                stable = await self._editor_image_count()
                if stable >= observed:
                    return {"success": True, "error": ""}
            return {
                "success": False,
                "error_code": "XHS_EDITOR_IMAGE_COUNT_UNCHANGED",
                "error": "上传后正文编辑器图片数量未稳定增加",
            }
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书上传图片时页面已关闭"
                ) from exc
            return {
                "success": False,
                "error_code": "XHS_IMAGE_UPLOAD_FAILED",
                "error": safe_media_error(exc, fallback="小红书图片上传失败"),
            }

    async def _get_verified_body_image_button(self):
        """返回 SVG path 指纹唯一匹配的可见正文图片按钮。"""

        return await self._toolbar_button_by_fingerprint(VERIFIED_BODY_IMAGE_ICON_FINGERPRINT)

    async def _toolbar_button_by_fingerprint(self, fingerprint: str):
        """按完整 SVG path SHA-256 返回唯一可见工具栏按钮。"""

        try:
            editor = self.page.locator(XHS_EDITOR_SELECTOR).first
            if await editor.count() != 1 or not await editor.is_visible():
                return None
            buttons = self.page.locator(XHS_TOOLBAR_BUTTON_SELECTOR)
            matches = []
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                if not await button.is_visible():
                    continue
                paths = await button.locator("svg path").evaluate_all(
                    "(nodes) => nodes.map((node) => node.getAttribute('d') || '')"
                )
                digest = hashlib.sha256("|".join(paths).encode("utf-8")).hexdigest()
                if digest == fingerprint:
                    matches.append(button)
            return matches[0] if len(matches) == 1 else None
        except Exception:
            return None

    async def _editor_image_count(self) -> int:
        """Count images inside TipTap body only; cover images do not qualify."""

        return int(
            await self.page.evaluate(
                """() => document.querySelectorAll(
                    'div.tiptap.ProseMirror img'
                ).length"""
            )
        )

    async def _raw_editor_image_state(self) -> dict[str, int]:
        """返回原始编辑器图片节点与已加载数量，不暴露资源 URL。"""

        state = await self.page.evaluate(
            """() => {
                const images = Array.from(document.querySelectorAll(
                    'div.tiptap.ProseMirror img'
                ));
                return {
                    count: images.length,
                    loaded_count: images.filter((image) =>
                        image.complete && image.naturalWidth > 0
                    ).length,
                };
            }"""
        )
        return {
            "count": int((state or {}).get("count") or 0),
            "loaded_count": int((state or {}).get("loaded_count") or 0),
        }

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """小红书保存草稿不需要话题；公开话题选择尚未接入，如实报告。"""

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
            "error": "小红书话题选择尚未接入（保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """拒绝把“暂存离开”的 Profile 本地卡片冒充云端草稿。"""

        del title
        raise XiaohongshuCloudDraftUnavailableError(
            "XHS_CLOUD_DRAFT_UNAVAILABLE: 小红书网页长文没有可验证的云端草稿保存入口"
        )

    async def _verify_saved_long_draft(self, title: str) -> None:
        """重开唯一同名排版草稿，验证标题、图文顺序、图片加载与封面。"""

        expected_title = " ".join(str(title or "").split())
        if expected_title != getattr(self, "_preflight_title", ""):
            raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 小红书缺少与本次一致的标题基线")
        blocks = self._expected_persisted_blocks
        if blocks is None:
            raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 小红书缺少冻结内容核验快照")
        try:
            await self._open_long_draft_drawer()
        except DraftBaselineError as exc:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小红书保存后草稿列表未完整稳定加载"
            ) from exc
        matches = await self._matching_long_draft_cards(expected_title)
        baseline_match_count = getattr(self, "_preflight_matching_draft_count", None)
        if not isinstance(baseline_match_count, int):
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小红书缺少保存前同名草稿数量基线"
            )
        expected_match_count = (
            baseline_match_count
            if self._editing_existing_draft
            else baseline_match_count + 1
        )
        if len(matches) != expected_match_count:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小红书同名草稿数量未按预期变化"
            )
        # 小红书草稿抽屉按最近更新时间倒序；结合总数和同名数均只增加一条，
        # 第一张同名卡片就是本次新实体。后续仍会重开并逐项核验冻结内容，
        # 因此排序变化只会得到 RESULT_UNKNOWN，不会把旧草稿误报为成功。
        actions = matches[0].locator(".draft-actions .btn").filter(has_text=re.compile(r"^编辑$"))
        if await actions.count() != 1:
            raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 小红书草稿编辑入口不唯一")
        await actions.click(timeout=15000)
        await self.page.wait_for_selector(
            XHS_EDITOR_SELECTOR,
            state="visible",
            timeout=20000,
        )
        if self._layout_finalized:
            snapshot = await self._layout_snapshot()
            # 无封面模式不会生成 ``.cover-outer-container`` 标题节点；此时
            # 上面的草稿抽屉唯一精确标题匹配已经完成标题证明，不能再拿空的
            # 封面标题误判真实草稿为 RESULT_UNKNOWN。只有平台生成封面时才
            # 要求封面页标题与冻结标题一致。
            title_key = self._draft_title_key(
                str(snapshot.get("cover_title") or "")
            )
            if (
                self._expected_persisted_cover
                and self._draft_title_key(expected_title) != title_key
            ):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 小红书排版草稿重开后标题不一致"
                )
            expected_images = sum(1 for block in blocks if block.get("type") == "image")
            if (
                int(snapshot.get("image_count") or 0) != expected_images
                or int(snapshot.get("loaded_image_count") or 0) != expected_images
            ):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 小红书草稿重开后图片未完整加载"
                )
            if (
                self._expected_persisted_cover
                and int(snapshot.get("first_card_loaded_images") or 0) < 1
            ):
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 小红书草稿重开后封面首图未持久化"
                )
            try:
                await self._assert_layout_body_tokens(blocks)
            except ContentValidationError as exc:
                raise DraftResultUnknownError(
                    "DRAFT_RESULT_UNKNOWN: 小红书草稿重开后图文顺序或标题样式不一致"
                ) from exc
            return

        persisted_title = await self.page.locator("textarea.d-text").first.input_value()
        if " ".join(persisted_title.split()) != expected_title:
            raise DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 小红书草稿重开后标题不一致")
        editor = self.page.locator(XHS_EDITOR_SELECTOR).first
        ensure_valid_content(
            blocks,
            await editor.inner_text(),
            platform="小红书",
            phase="草稿重开后",
        )

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
