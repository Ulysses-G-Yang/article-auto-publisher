"""外部 AI 建议的稳定、脱敏错误。"""

from __future__ import annotations


class PublicationAIError(RuntimeError):
    """只携带稳定错误码和固定安全消息，不保存正文、密钥或原始响应。"""

    _MESSAGES = {
        "AI_GUIDANCE_DISABLED": "AI 发布建议功能未启用",
        "AI_CONFIGURATION_ERROR": "AI 发布建议配置不可用",
        "AI_AUTH_FAILED": "AI 服务认证失败",
        "AI_RATE_LIMITED": "AI 服务请求过于频繁，请稍后再试",
        "AI_TIMEOUT": "AI 服务响应超时",
        "AI_UPSTREAM_ERROR": "AI 服务暂时不可用",
        "AI_RESPONSE_INVALID": "AI 服务返回内容不符合建议契约",
        "AI_INPUT_TOO_LARGE": "文章正文超过 AI 建议允许的长度",
    }
    _STATUSES = {
        "AI_GUIDANCE_DISABLED": 409,
        "AI_CONFIGURATION_ERROR": 503,
        "AI_AUTH_FAILED": 502,
        "AI_RATE_LIMITED": 429,
        "AI_TIMEOUT": 504,
        "AI_UPSTREAM_ERROR": 502,
        "AI_RESPONSE_INVALID": 502,
        "AI_INPUT_TOO_LARGE": 413,
    }

    def __init__(self, error_code: str, *, message: str | None = None) -> None:
        if error_code not in self._MESSAGES:
            error_code = "AI_UPSTREAM_ERROR"
        self.error_code = error_code
        self.http_status = self._STATUSES[error_code]
        self.safe_message = message or self._MESSAGES[error_code]
        # RuntimeError 的 args 只写固定信息，禁止把第三方异常作为 args 保存。
        super().__init__(self.safe_message)

    def __repr__(self) -> str:
        return f"PublicationAIError(error_code={self.error_code!r})"


__all__ = ["PublicationAIError"]
