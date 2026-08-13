"""知乎账号会话适配器。

当前只实现账号登录态与身份验证。所有内容投递动作均明确拒绝，避免把未经
真实平台验证的编辑器逻辑伪装成可用能力。
"""

from __future__ import annotations

import asyncio

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    LoginRequiredError,
    PlatformAutomationError,
)


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class ZhihuPlatform(BasePlatform):
    """知乎账号会话适配器；阶段一不提供投递能力。"""

    platform_name = "zhihu"
    SESSION_COOKIE_NAMES = frozenset({"z_c0", "d_c0", "q_c1"})
    LOGIN_POLL_ATTEMPTS = 60
    LOGIN_POLL_INTERVAL_SECONDS = 2

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """通过同源身份 API 返回最小、脱敏后的账号身份。"""

        self._require_page_alive("知乎身份 API 验证")
        payload = await self.page.evaluate(
            """async (identityPath) => {
                try {
                    const response = await fetch(identityPath, {
                        credentials: 'include',
                        headers: { Accept: 'application/json' },
                    });
                    const body = await response.json().catch(() => ({}));
                    const data = body && body.data ? body.data : body;
                    const id = data && data.id ? String(data.id) : '';
                    const urlToken = data && data.url_token
                        ? String(data.url_token)
                        : '';
                    const displayName = data && data.name
                        ? String(data.name)
                        : '';
                    return {
                        ok: Boolean(response.ok && (id || urlToken) && displayName),
                        status: response.status,
                        user_id: id || urlToken,
                        display_name: displayName,
                    };
                } catch (_) {
                    return {
                        ok: false,
                        status: 0,
                        user_id: '',
                        display_name: '',
                    };
                }
            }""",
            self.platform_cfg.get("identity_api_path", "/api/v4/me"),
        )
        if not isinstance(payload, dict):
            payload = {}
        sanitized: dict[str, str | int | bool] = {
            "ok": bool(payload.get("ok")),
            "status": int(payload.get("status") or 0),
            "user_id": _text(payload.get("user_id")),
            "display_name": _text(payload.get("display_name")),
        }
        sanitized["ok"] = bool(
            sanitized["ok"]
            and sanitized["user_id"]
            and sanitized["display_name"]
        )
        self._identity_payload = sanitized if sanitized["ok"] else None
        return sanitized

    async def _has_session_cookie_signal(self) -> bool:
        """Cookie 只作为弱信号；不返回、不记录 Cookie 名和值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies(["https://www.zhihu.com/"])
        except TypeError:
            cookies = await self.context.cookies()
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def check_login(self) -> bool:
        """只读验证现有 Profile；身份 API 成功才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("知乎登录态检测")
            await self.page.goto(
                self.platform_cfg.get("home_url", "https://www.zhihu.com/"),
                wait_until="domcontentloaded",
                timeout=15000,
            )
            identity = await self.fetch_identity_payload()
            if identity["ok"]:
                return True
            has_weak_signal = await self._has_session_cookie_signal()
            self.last_login_error = (
                "ZHIHU_SESSION_INVALID: 知乎身份 API 未确认当前会话"
                if has_weak_signal
                else "LOGIN_REQUIRED: 知乎账号需要登录"
            )
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 知乎登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "ZHIHU_LOGIN_CHECK_ERROR: 知乎登录态验证失败"
            return False

    async def login(self):
        """打开知乎官方登录页，等待用户在隔离 Profile 中交互登录。"""

        self._require_page_alive("知乎打开登录页")
        await self.page.goto(
            self.platform_cfg.get("login_url", "https://www.zhihu.com/signin"),
            wait_until="domcontentloaded",
            timeout=15000,
        )
        for attempt in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("知乎等待登录")
            identity = await self.fetch_identity_payload()
            if identity["ok"]:
                self.last_login_error = ""
                return
            if attempt + 1 < self.LOGIN_POLL_ATTEMPTS:
                await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: 知乎登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    @staticmethod
    def _not_implemented(operation: str):
        raise PlatformNotImplementedError(
            f"PLATFORM_NOT_IMPLEMENTED: 知乎{operation}能力尚未接入"
        )

    async def publish(self, *args, **kwargs) -> dict:
        self._not_implemented("内容投递")

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


def _text(value: object) -> str:
    return " ".join(str(value or "").split())[:255]
