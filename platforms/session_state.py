"""Conservative interpretation of existing platform login checks.

A false boolean means verification failed, not necessarily that authentication
expired. Only explicit stable codes may request another login.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LoginFailure:
    code: str
    message: str
    requires_login: bool = False


def login_check_failure(platform) -> LoginFailure:
    raw = getattr(platform, "last_login_error", "")
    code = raw.split(":", 1)[0].strip().upper() if isinstance(raw, str) else ""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
        code = "SESSION_CHECK_FAILED"
    if code in {"LOGIN_REQUIRED", "SESSION_EXPIRED"} or code.endswith(
        ("_LOGIN_REQUIRED", "_SESSION_EXPIRED")
    ):
        return LoginFailure("LOGIN_REQUIRED", "平台明确要求重新登录，请手动完成登录。", True)
    if code in {"CHALLENGE", "CAPTCHA_REQUIRED", "VERIFICATION_REQUIRED"} or code.endswith(
        ("_SECURITY_CHALLENGE", "_CHALLENGE", "_CAPTCHA_REQUIRED")
    ):
        return LoginFailure("CHALLENGE", "平台要求本人完成安全验证，本次操作已停止。")
    if code in {"RATE_LIMITED", "TOO_MANY_REQUESTS", "RATE_LIMIT"}:
        return LoginFailure("RATE_LIMITED", "平台限制当前操作频率，本次操作已停止。")
    return LoginFailure(code, "本次登录状态检查未完成，不能据此判断账号已退出；本次操作已停止。")
