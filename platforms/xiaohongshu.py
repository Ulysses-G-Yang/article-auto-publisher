"""小红书创作服务平台账号会话适配器。

⚠️ 风控警告：小红书风控严格，所有涉及小红的任务必须先读
``docs/XIAOHONGSHU_RISK_CONTROL.md``，并遵守其中的硬性纪律。

登录态与身份验证链路（真实扫码 → 会话 cookie → 身份捕获），
文字草稿链路已验收；图片上传待真实验收，未获得正文控件证据时必须
fail closed；公开发布始终关闭。

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
VERIFIED_BODY_IMAGE_ICON_FINGERPRINT = (
    "75d8717d8ac60eb26ac4490c408d1ee766029f5cf293b8adc1dc36d89bce11f2"
)
VERIFIED_H2_ICON_FINGERPRINT = (
    "716e4ef689591d5c7c193e7ece200b8066058db8142d4bf7b182e44af2f8cab0"
)
VERIFIED_BODY_IMAGE_ACCEPT = frozenset({
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
})


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


class XiaohongshuPlatform(BasePlatform):
    """小红书账号会话适配器；图片控件证据不足时保持 fail closed。"""

    platform_name = "xiaohongshu"
    # 2026-08 真实验证：小红书已不再下发 web_session，现行会话 cookie 为
    # customer-sso-sid / customerClientId / access-token-creator.* /
    # x-user-id-creator.* / galaxy_creator_session_id；web_session 保留兼容。
    SESSION_COOKIE_NAMES = frozenset({
        "web_session",
        "customer-sso-sid",
        "customerClientId",
        "access-token-creator.xiaohongshu.com",
        "x-user-id-creator.xiaohongshu.com",
        "galaxy_creator_session_id",
        "galaxy.creator.beaker.session.id",
    })
    LOGIN_POLL_ATTEMPTS = 40
    LOGIN_POLL_INTERVAL_SECONDS = 3

    def __init__(self, *, resume_existing_title: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None
        self._expected_persisted_blocks: list[dict] | None = None
        self._preflight_title = ""
        self._resume_existing_title = " ".join(
            str(resume_existing_title or "").split()
        )
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
            cookies = await self.context.cookies([
                "https://creator.xiaohongshu.com/",
                "https://www.xiaohongshu.com/",
            ])
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

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
        if not (
            isinstance(self._identity_payload, dict) and self._identity_payload.get("ok")
        ):
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
                if (
                    isinstance(dom, dict)
                    and dom.get("user_id")
                    and dom.get("display_name")
                ):
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

    # ==================== 投递链路（尚未接入） ====================

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
        """保存前证明不存在同名长文草稿，避免结果未知后产生重复副作用。"""

        self._require_page_alive("小红书草稿基线检查")
        expected_title = " ".join(str(title or "").split())
        if not expected_title:
            raise DraftBaselineError("DRAFT_BASELINE_FAILED: 小红书标题不能为空")
        try:
            await self.page.goto(
                "https://creator.xiaohongshu.com/publish/publish",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            self._draft_box_count_before = await self._draft_box_count()
            await self._open_long_draft_drawer()
            matches = await self._matching_long_draft_cards(expected_title)
        except DraftBaselineError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书草稿基线检查时页面已关闭"
                ) from exc
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 小红书无法确认同名长文草稿基线"
            ) from exc
        self._editing_existing_draft = False
        if matches and (
            len(matches) != 1 or expected_title != self._resume_existing_title
        ):
            raise DraftBaselineError(
                "DRAFT_BASELINE_FAILED: 小红书已存在同名草稿，禁止自动重复创建"
            )
        self._preflight_title = expected_title
        if matches:
            actions = matches[0].locator(".draft-actions .btn").filter(
                has_text=re.compile(r"^编辑$")
            )
            if await actions.count() != 1:
                raise DraftBaselineError(
                    "DRAFT_BASELINE_FAILED: 小红书待恢复草稿编辑入口不唯一"
                )
            await actions.click(timeout=15000)
            await self.page.wait_for_selector(
                XHS_EDITOR_SELECTOR,
                state="visible",
                timeout=20000,
            )
            self._editing_existing_draft = True

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
        await visible_tabs[0].click(timeout=15000)
        await self.page.wait_for_selector(
            ".draft-drawer .draft-list",
            state="visible",
            timeout=15000,
        )

    async def _matching_long_draft_cards(self, title: str) -> list:
        """返回标题行精确匹配的草稿卡；不读取正文或资源地址。"""

        cards = self.page.locator(".draft-drawer .draft-list .draft-item")
        matches = []
        for index in range(await cards.count()):
            card = cards.nth(index)
            lines = [line.strip() for line in (await card.inner_text()).splitlines()]
            if title in lines:
                matches.append(card)
        return matches

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
            if await title_field.count() == 0 or not await title_field.is_visible():
                raise RuntimeError("标题输入框不可见")
            await title_field.click()
            await title_field.fill(str(title or "").strip())
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

        expected_images = sum(
            1 for block in content_blocks if block.get("type") == "image"
        )
        uploaded_images = 0
        failed_images: list[dict[str, str]] = []
        wrote_any = False
        previous_was_image = False

        for block in content_blocks:
            if not isinstance(block, dict):
                raise ContentValidationError(
                    "XHS_CONTENT_CONTRACT_INVALID: 正文块无效"
                )
            btype = block.get("type")
            if btype in ("text", "heading") and block.get("text"):
                text = str(block["text"]).strip()
                if not text:
                    continue
                if btype == "heading" and (
                    block.get("level") != 2 or "\n" in text or "\r" in text
                ):
                    raise ContentValidationError(
                        "XHS_HEADING_UNSUPPORTED: 仅支持单行二级标题"
                    )
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
                    failed_images.append(
                        {"filename": "", "error": "文章图片块没有对应本地文件"}
                    )
                    break
            elif btype not in ("text", "heading"):
                raise ContentValidationError(
                    "XHS_CONTENT_CONTRACT_INVALID: 未知正文块类型"
                )

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
            raise ContentValidationError(
                "XHS_EDITOR_CARET_FAILED: 无法安全定位正文末尾"
            )

    async def _apply_h2_to_current_block(self) -> None:
        """用真实 SVG 指纹定位 H2，并验证非空 h2 节点增加。"""

        button = await self._toolbar_button_by_fingerprint(VERIFIED_H2_ICON_FINGERPRINT)
        if button is None:
            raise ContentValidationError(
                "XHS_H2_BUTTON_UNVERIFIED: 二级标题按钮不符合已验证指纹"
            )
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
        raise ContentValidationError(
            "XHS_H2_APPLY_FAILED: 当前正文块未变为二级标题"
        )

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
            raise ContentValidationError(
                "XHS_BODY_ORDER_INVALID: 正文图文块数量与冻结版本不一致"
            )
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

        return await self._toolbar_button_by_fingerprint(
            VERIFIED_BODY_IMAGE_ICON_FINGERPRINT
        )

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
        """点击「暂存离开」保存草稿，以「草稿箱计数 +1 + 保存接口 2xx」验证。

        2026-08 实测：小红书 Web 端草稿的唯一可见入口是发布页侧栏的
        「草稿箱(N)」计数（写长文子视图/笔记管理均无草稿卡片列表），
        因此验证语义 = 保存前计数 N → 点击暂存离开（捕获全部 POST/PUT，
        任一 2xx 视为保存信号）→ 重新加载后计数 N+1。两者都满足才返回
        发布页 URL；否则如实返回空串，绝不以当前页 URL 冒充成功。
        """
        self._require_page_alive("小红书保存草稿")
        before = getattr(self, "_draft_box_count_before", None)
        if not isinstance(before, int):
            # 未在发布页记录基准计数（如直接进入编辑器），读不到则如实失败
            before = await self._draft_box_count()
        captured: dict = {}

        async def _on_response(response) -> None:
            try:
                if response.request.method in ("POST", "PUT", "PATCH"):
                    captured["status"] = response.status
                    if "draft" in response.url.lower():
                        captured["url"] = response.url[:160]
                        try:
                            body = await response.json()
                            if isinstance(body, dict):
                                captured["ok"] = (
                                    body.get("code") in (0, None)
                                    or body.get("success") is True
                                )
                        except Exception:
                            captured["ok"] = response.status < 300
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", _on_response)
            await self.page.evaluate(
                """() => {
                    const nodes = Array.from(
                        document.querySelectorAll('button, [role=button]')
                    );
                    const target = nodes.find((el) =>
                        (el.innerText || '').replace(/\\s+/g, '').includes('暂存离开'));
                    if (target) target.click();
                }"""
            )
            # 风控敏感：点击后放慢节奏，给平台保存请求留足时间
            await self.simulator.random_delay(5, 8)
            try:
                confirm = self.page.locator(
                    "button:has-text('确定'), button:has-text('暂存')"
                ).first
                if await confirm.count() > 0:
                    await confirm.click(timeout=3000)
                    await self.simulator.random_delay(4, 6)
            except Exception:
                pass
            for _ in range(15):
                if captured.get("status"):
                    break
                await asyncio.sleep(1)
            await self.simulator.random_delay(2, 3)
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if not captured.get("status"):
            logger.error("小红书暂存离开未产生任何保存请求")
            return ""
        if not captured.get("ok", captured.get("status", 0) < 300):
            logger.error("小红书草稿 API 未确认成功: {}", captured)
            return ""

        # 新草稿要求计数 +1；显式恢复同一唯一草稿则要求计数保持不变。
        try:
            await self.page.goto(
                "https://creator.xiaohongshu.com/publish/publish",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            after = None
            expected_after = before if self._editing_existing_draft else (
                before + 1 if before is not None else None
            )
            for _ in range(3):
                await self.simulator.random_delay(5, 8)
                after = await self._draft_box_count()
                if after is not None and expected_after is not None and after == expected_after:
                    break
            if after is None or before is None:
                logger.error("小红书草稿箱计数读取失败: before={}, after={}", before, after)
                return ""
            if after != expected_after:
                logger.error(
                    "小红书草稿箱计数不符合预期: before={}, after={}, resume={}",
                    before,
                    after,
                    self._editing_existing_draft,
                )
                return ""
            logger.info(
                "小红书草稿验证成功: 草稿箱计数 {} -> {}，保存接口 {}",
                before,
                after,
                captured.get("status"),
            )
            await self._verify_saved_long_draft(title)
            return "https://creator.xiaohongshu.com/publish/publish"
        except DraftResultUnknownError:
            raise
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 小红书验证草稿时页面已关闭"
                ) from exc
            logger.error("小红书草稿验证失败: {}", exc)
            return ""

    async def _verify_saved_long_draft(self, title: str) -> None:
        """重开唯一同名草稿，验证标题、文字与正文图片数量。"""

        expected_title = " ".join(str(title or "").split())
        if expected_title != getattr(self, "_preflight_title", ""):
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小红书缺少与本次一致的标题基线"
            )
        blocks = self._expected_persisted_blocks
        if blocks is None:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小红书缺少冻结内容核验快照"
            )
        await self._open_long_draft_drawer()
        matches = await self._matching_long_draft_cards(expected_title)
        if len(matches) != 1:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小红书未找到唯一同名长文草稿"
            )
        actions = matches[0].locator(".draft-actions .btn").filter(
            has_text=re.compile(r"^编辑$")
        )
        if await actions.count() != 1:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小红书草稿编辑入口不唯一"
            )
        await actions.click(timeout=15000)
        await self.page.wait_for_selector(
            XHS_EDITOR_SELECTOR,
            state="visible",
            timeout=20000,
        )
        persisted_title = await self.page.locator("textarea.d-text").first.input_value()
        if " ".join(persisted_title.split()) != expected_title:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小红书草稿重开后标题不一致"
            )
        editor = self.page.locator(XHS_EDITOR_SELECTOR).first
        ensure_valid_content(
            blocks,
            await editor.inner_text(),
            platform="小红书",
            phase="草稿重开后",
        )
        expected_images = sum(1 for block in blocks if block.get("type") == "image")
        actual_images = await self._editor_image_count()
        if actual_images != expected_images:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小红书草稿重开后正文图片数量不一致"
            )
        try:
            await self._assert_editor_body_tokens(blocks)
        except ContentValidationError as exc:
            raise DraftResultUnknownError(
                "DRAFT_RESULT_UNKNOWN: 小红书草稿重开后图文顺序或标题样式不一致"
            ) from exc

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
