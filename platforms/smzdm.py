"""什么值得买（smzdm）账号会话适配器。

登录方式说明（2026-08 真实页面探测）：
- smzdm Web 登录页（https://zhiyou.smzdm.com/user/login）只有
  手机号/邮箱 + 密码 + 「60 天内免登录」表单，**没有扫码登录**
  （页面上 120x120 二维码是 App 下载码，不是登录码）。
- 因此本适配器采用「人工凭据登录」：打开可见浏览器窗口，由用户在
  窗口内输入账号密码（凭据不进入代码、不存储、不记录），适配器
  轮询会话 cookie 确认登录成功。

登录成功信号：.smzdm.com 出现 sess 会话 cookie（登录后才下发）。
身份提取采用「捕获页面自身响应」模式 + 首页 DOM 兜底。
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

LOGIN_URL = "https://zhiyou.smzdm.com/user/login"
HOME_URL = "https://zhiyou.smzdm.com/"
USERNAME_SELECTOR = "input#username.form-input"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name", "nick"}
IDENTITY_UID_KEYS = {"uid", "user_id", "id"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class SmzdmPlatform(BasePlatform):
    """什么值得买账号会话适配器；内容投递能力保持关闭。"""

    platform_name = "smzdm"
    SESSION_COOKIE_NAMES = frozenset({"sess"})
    LOGIN_POLL_ATTEMPTS = 120
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
        """sess 会话 cookie 作为登录成功信号；不返回、不记录 cookie 值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies([
                "https://www.smzdm.com/",
                "https://zhiyou.smzdm.com/",
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
            self._require_page_alive("smzdm 登录态检测")
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
                self.last_login_error = "SMZDM_IDENTITY_MISSING: 会话存在但身份未确认"
                return False
            self.last_login_error = "LOGIN_REQUIRED: smzdm 账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "SMZDM_LOGIN_CHECK_ERROR: smzdm 登录态验证失败"
            return False

    async def login(self):
        """打开 smzdm 登录页，由用户在可见窗口中输入凭据完成登录。"""

        self._require_page_alive("smzdm 打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        try:
            await self.page.wait_for_selector(
                USERNAME_SELECTOR,
                state="visible",
                timeout=20000,
            )
        except Exception:
            pass
        await self._show_login_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("smzdm 等待登录")
            if await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: smzdm 登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_login_hint(self):
        """页面顶部显示登录提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'smzdm-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#fe6d01;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请在下方表单输入手机号/邮箱和密码登录'
                        + '（可勾选 60 天内免登录）<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """返回 smzdm 同源确认的最小平台身份（捕获 + DOM 兜底）。"""

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        response.request.resource_type in ("xhr", "fetch")
                        and "smzdm.com" in response.url
                        and any(
                            key in response.url.lower()
                            for key in ("user", "profile", "account", "info")
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
                    "BROWSER_CONTEXT_CLOSED: smzdm 身份捕获时页面已关闭"
                ) from exc
            logger.warning("smzdm 身份捕获失败: {}", exc)

        # DOM 兜底：顶部用户区昵称 + 个人中心 ID
        if not (
            isinstance(self._identity_payload, dict) and self._identity_payload.get("ok")
        ):
            try:
                dom = await self.page.evaluate(
                    """() => {
                        const body = document.body.innerText || '';
                        const nickEl = document.querySelector(
                            '[class*="user-name"], [class*="nickname"],'
                            + ' [class*="userinfo"] [class*="name"]'
                        );
                        let nickname = nickEl
                            ? (nickEl.innerText || '').trim()
                            : '';
                        if (!nickname) {
                            const m = body.match(
                                /(?:欢迎|Hi)\\s*[，,~]?\\s*([\\u4e00-\\u9fa5A-Za-z0-9_]{2,20})/
                            );
                            nickname = m ? m[1] : '';
                        }
                        return { user_id: '', display_name: nickname };
                    }"""
                )
                if isinstance(dom, dict) and dom.get("display_name"):
                    # 仅拿到昵称不足以确认稳定 ID，如实标记未完成，等待捕获兜底
                    pass
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
            f"PLATFORM_NOT_IMPLEMENTED: smzdm{operation}能力尚未接入"
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
