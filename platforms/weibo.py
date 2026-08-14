"""微博账号会话适配器。

登录态与身份验证链路（真实扫码 → 跳转回首页 → 身份捕获/DOM 确认），
内容投递能力尚未接入：所有投递方法显式拒绝。

真实登录页（https://passport.weibo.com/sso/signin?entry=miniblog...）：
- 「扫描二维码登录」为默认 Tab，二维码为约 140x140 的 img（v2.qr.weibo.cn）。
- 登录成功信号：扫码确认后页面从 passport 跳转回 weibo.com。
- 身份接口带签名/cookie 约束，裸 fetch 不可靠；采用「捕获页面自身响应」
  模式 + 首页 DOM 兜底，与小黑盒/小红书一致。
"""

from __future__ import annotations

import asyncio

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    LoginRequiredError,
    PlatformAutomationError,
)

LOGIN_URL = (
    "https://passport.weibo.com/sso/signin?entry=miniblog"
    "&source=miniblog&disp=popup&url=https%3A%2F%2Fweibo.com%2F"
)
HOME_URL = "https://weibo.com/"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name", "nick"}
IDENTITY_UID_KEYS = {"uid", "user_id"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class WeiboPlatform(BasePlatform):
    """微博账号会话适配器；内容投递能力保持关闭。"""

    platform_name = "weibo"
    LOGIN_POLL_ATTEMPTS = 40
    LOGIN_POLL_INTERVAL_SECONDS = 3

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None

    # ==================== 登录态与身份 ====================

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
            identity = await self.fetch_identity_payload()
            if identity.get("ok"):
                return True
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
            if await self._on_home():
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
        """返回微博首页同源确认的最小平台身份（捕获 + DOM 兜底）。"""

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        response.request.resource_type in ("xhr", "fetch")
                        and "weibo.com" in response.url
                        and any(
                            key in response.url.lower()
                            for key in ("user", "logininfo", "profile", "account")
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
                    HOME_URL,
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
                    "BROWSER_CONTEXT_CLOSED: 微博身份捕获时页面已关闭"
                ) from exc
            logger.warning("微博身份捕获失败: {}", exc)

        # DOM 兜底：头像链接 /u/<uid> + 顶部用户区昵称（登录后首页）
        if not (
            isinstance(self._identity_payload, dict) and self._identity_payload.get("ok")
        ):
            try:
                dom = await self.page.evaluate(
                    """() => {
                        const uidLink = document.querySelector('a[href*="/u/"]');
                        const uidMatch = uidLink
                            ? (uidLink.getAttribute('href') || '').match(/\\/u\\/(\\d+)/)
                            : null;
                        const nameEl = document.querySelector(
                            '[class*="woo-pop-avatar"] [class*="name"],'
                            + ' [class*="user-info"] [class*="name"]'
                        );
                        const nickname = nameEl
                            ? (nameEl.innerText || '').trim()
                            : '';
                        return {
                            user_id: uidMatch ? uidMatch[1] : '',
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
            f"PLATFORM_NOT_IMPLEMENTED: 微博{operation}能力尚未接入"
        )

    async def navigate_to_editor(self):
        self._not_implemented("编辑器导航")

    async def fill_title(self, title: str):
        self._not_implemented("标题填写")

    async def fill_content(self, content_blocks: list, images: list):
        self._not_implemented("正文填写")

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        self._not_implemented("话题选择")

    async def save_draft(self, title: str = "") -> str:
        self._not_implemented("草稿保存")

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
