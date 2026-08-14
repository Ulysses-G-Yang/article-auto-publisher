"""小红书创作服务平台账号会话适配器。

⚠️ 风控警告：小红书风控严格，所有涉及小红的任务必须先读
``docs/XIAOHONGSHU_RISK_CONTROL.md``，并遵守其中的硬性纪律。

登录态与身份验证链路（真实扫码 → 会话 cookie → 身份捕获），
内容投递能力尚未接入：所有投递方法显式拒绝，避免把未经真实编辑器
验证的逻辑伪装成可用能力。

真实登录页（https://creator.xiaohongshu.com/login）：
- 「APP扫一扫登录」为默认 Tab，二维码为约 160x160 的 base64 PNG。
- 登录成功信号：.xiaohongshu.com 出现 web_session cookie。
- 平台接口带重型反爬（as.xiaohongshu.com 签名/验证码），裸 fetch 会被拒；
  因此身份提取采用「捕获页面自身响应」模式，与小黑盒 restore_login 一致。
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

CREATOR_HOME = "https://creator.xiaohongshu.com/"
CREATOR_MAIN = "https://creator.xiaohongshu.com/new/home"
LOGIN_URL = "https://creator.xiaohongshu.com/login"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name"}
IDENTITY_UID_KEYS = {"user_id", "sec_uid", "uid"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class XiaohongshuPlatform(BasePlatform):
    """小红书创作服务平台账号会话适配器；内容投递能力保持关闭。"""

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

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None

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
        """捕获创作服务平台自身的身份响应，返回最小平台身份。

        缺失时如实返回未确认，绝不伪造。
        """

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        response.request.resource_type == "xhr"
                        and (
                            "edith.xiaohongshu.com" in response.url
                            or "creator.xiaohongshu.com" in response.url
                        )
                        and any(
                            key in response.url.lower()
                            for key in ("user", "profile", "account")
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
