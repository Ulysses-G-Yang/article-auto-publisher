"""HTTP adapter for the existing Flask REST API."""

from __future__ import annotations

import re
from typing import Any

import httpx


class FlaskClientError(RuntimeError):
    """A safe, public-facing error from the Flask dependency."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


_SENSITIVE_MESSAGE_RE = re.compile(
    r"(?i)(cookie|token|secret|password|api[_ -]?key)\s*[:=]\s*[^\s,;]+"
)


def safe_error_message(value: Any, fallback: str = "Flask 服务返回错误") -> str:
    """Keep dependency errors short and free of common secret/path values."""

    if not isinstance(value, str) or not value.strip():
        return fallback
    message = _SENSITIVE_MESSAGE_RE.sub(r"\1=[redacted]", value.strip())
    # Flask may include local traceback paths in an exception string.  Do not
    # forward those implementation details through the MCP boundary.
    message = re.sub(r"(?i)\b[A-Z]:\\[^\r\n ]+", "[redacted path]", message)
    message = re.sub(r"(?i)(?:^|\s)/(?:[^\s/]+/)+[^\s]+", " [redacted path]", message)
    return message[:500]


class FlaskClient:
    """Async, bounded client for the known Flask endpoints only."""

    def __init__(self, base_url: str, timeout_seconds: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0))
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _error_code(status_code: int) -> str:
        if status_code == 404:
            return "NOT_FOUND"
        if status_code in (400, 409, 422):
            return "INVALID_ARGUMENT"
        if status_code in (408, 504):
            return "TIMEOUT"
        if status_code >= 500:
            return "UNAVAILABLE"
        return "INTERNAL_ERROR"

    @staticmethod
    def _http_error_message(status_code: int, payload: Any) -> str:
        if isinstance(payload, dict):
            detail = payload.get("message") or payload.get("error")
            if isinstance(detail, str) and detail.strip():
                return safe_error_message(detail)
        return {
            400: "Flask 服务拒绝了请求参数",
            404: "Flask 资源不存在",
            409: "Flask 当前状态不允许该操作",
        }.get(status_code, f"Flask 服务请求失败（HTTP {status_code}）")

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        client = await self._get_client()
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise FlaskClientError("TIMEOUT", "Flask 服务请求超时") from exc
        except httpx.RequestError as exc:
            raise FlaskClientError("UNAVAILABLE", "无法连接 Flask 服务") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code >= 400:
            raise FlaskClientError(
                self._error_code(response.status_code),
                self._http_error_message(response.status_code, payload),
                status_code=response.status_code,
            )
        return payload

    async def get_accounts(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/api/accounts")
        return payload if isinstance(payload, list) else []

    async def get_articles(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/api/articles")
        return payload if isinstance(payload, list) else []

    async def get_tasks(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/api/tasks")
        return payload if isinstance(payload, list) else []

    async def get_task_logs(self, task_id: int) -> list[dict[str, Any]]:
        payload = await self._request("GET", f"/api/task/{task_id}/logs")
        return payload if isinstance(payload, list) else []

    async def get_queue_status(self) -> dict[str, Any]:
        payload = await self._request("GET", "/api/status")
        return payload if isinstance(payload, dict) else {}

    async def resume_task(self, task_id: int, payload: dict[str, str]) -> dict[str, Any]:
        result = await self._request("POST", f"/api/tasks/{task_id}/resume", json=payload)
        return result if isinstance(result, dict) else {}

    async def upload_docx(self, file_path: str, platforms: list[str]) -> dict[str, Any]:
        client = await self._get_client()
        try:
            with open(file_path, "rb") as source:
                files = {
                    "files": (
                        "article.docx",
                        source,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                }
                form_data = [("platforms", platform) for platform in platforms]
                response = await client.post("/api/upload", files=files, data=form_data)
        except httpx.TimeoutException as exc:
            raise FlaskClientError("TIMEOUT", "Flask 文件上传超时") from exc
        except httpx.RequestError as exc:
            raise FlaskClientError("UNAVAILABLE", "无法连接 Flask 服务") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            raise FlaskClientError(
                self._error_code(response.status_code),
                self._http_error_message(response.status_code, payload),
                status_code=response.status_code,
            )
        return payload if isinstance(payload, dict) else {}

    async def start_login(self, platform: str) -> dict[str, Any]:
        try:
            payload = await self._request("POST", f"/api/accounts/{platform}/login")
        except FlaskClientError as exc:
            # Flask uses 409 to report an already-running login.  It is still
            # safe for MCP to create a polling task for that existing flow.
            if exc.status_code == 409:
                return {"status": "already_logging_in", "message": exc.message}
            raise
        return payload if isinstance(payload, dict) else {}

    async def clear_cookies(self, platform: str) -> dict[str, Any]:
        payload = await self._request("POST", f"/api/accounts/{platform}/clear-cookies")
        return payload if isinstance(payload, dict) else {}

    async def logout(self, platform: str) -> dict[str, Any]:
        payload = await self._request("POST", f"/api/accounts/{platform}/logout")
        return payload if isinstance(payload, dict) else {}

    async def cleanup_locks(self) -> dict[str, Any]:
        payload = await self._request("POST", "/api/cleanup")
        return payload if isinstance(payload, dict) else {}
