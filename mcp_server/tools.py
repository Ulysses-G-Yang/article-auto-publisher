"""Business-facing MCP tools backed by the existing Flask REST API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from account_sessions.platform_catalog import (
    ACCOUNT_ENABLED_PLATFORMS,
    DELIVERY_ENABLED_PLATFORMS,
)

from . import SERVER_ID
from .flask_client import FlaskClient, FlaskClientError, safe_error_message
from .task_store import TaskStore

Platform = Literal["zol", "xiaoheihe"]
InternalPlatform = Literal[
    "xiaoheihe",
    "zol",
    "zhihu",
    "weibo",
    "smzdm",
    "toutiao",
    "baijiahao",
    "xiaohongshu",
    "douyin",
]
DraftDeliveryPlatform = Literal[
    "xiaoheihe",
    "zol",
    "zhihu",
    "weibo",
    "smzdm",
    "baijiahao",
]
PositiveTaskId = Annotated[int, Field(gt=0)]
AccountId = Annotated[str, Field(min_length=1, max_length=128)]
ActivityLimit = Annotated[int, Field(ge=1, le=200)]
TaskToken = Annotated[str, Field(min_length=1, max_length=128)]
SourceDownloadURL = Annotated[str, Field(min_length=1, max_length=2048)]
ClientRequestId = Annotated[
    str,
    Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]
OptionalShortText = Annotated[str | None, Field(max_length=200)]
DEFAULT_FILE_SERVICE_HOSTS = {"dev.sccsai.com"}
SUPPORTED_PLATFORMS = {"zol", "xiaoheihe"}
SUPPORTED_ACCOUNT_PLATFORMS = set(ACCOUNT_ENABLED_PLATFORMS)
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
LEGACY_MCP_MUTATIONS_DISABLED = "LEGACY_MCP_MUTATIONS_DISABLED"
CURRENT_DRAFT_PLATFORMS = set(DELIVERY_ENABLED_PLATFORMS)


class DraftDeliveryTarget(BaseModel):
    """CS_Admin 可选择的一个平台草稿目标。"""

    model_config = ConfigDict(extra="forbid")

    platform: DraftDeliveryPlatform
    account_id: Annotated[str, Field(min_length=36, max_length=36)]
    persist_login: bool | None = None


class ToolFailure(RuntimeError):
    """Safe error intended for the MCP response boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _with_trace_fields(payload: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
    """Add the stable CS_Admin tracing fields to every tool result."""

    return {
        "server_id": SERVER_ID,
        "request_id": request_id or uuid.uuid4().hex,
        **payload,
    }


def error_result(code: str, message: str) -> dict[str, Any]:
    return _with_trace_fields({"error": {"code": code, "message": message[:500]}})


def _safe_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return safe_error_message(str(value), fallback=default)[:500]


def _safe_filename(value: Any) -> str:
    text = _safe_text(value)
    return text.replace("\\", "/").rsplit("/", 1)[-1]


def _safe_platform(value: Any) -> str:
    platform = str(value or "").strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        raise ToolFailure("INVALID_ARGUMENT", "platform 必须是 zol 或 xiaoheihe")
    return platform


def _safe_internal_platform(value: Any) -> str:
    platform = str(value or "").strip().lower()
    if platform not in SUPPORTED_ACCOUNT_PLATFORMS:
        raise ToolFailure("INVALID_ARGUMENT", "platform 不是已启用的账号平台")
    return platform


def _safe_account_id(value: Any) -> str:
    account_id = str(value or "").strip()
    if not account_id or len(account_id) > 128:
        raise ToolFailure("INVALID_ARGUMENT", "account_id 无效")
    return account_id


def _require_legacy_mutations(enabled: bool) -> None:
    if not enabled:
        raise ToolFailure(
            LEGACY_MCP_MUTATIONS_DISABLED,
            "旧版 MCP 变更工具默认关闭；仅允许通过显式紧急兼容开关启用",
        )


def _safe_task_id(value: Any) -> int:
    try:
        task_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolFailure("INVALID_ARGUMENT", "task_id 必须是正整数") from exc
    if task_id <= 0:
        raise ToolFailure("INVALID_ARGUMENT", "task_id 必须是正整数")
    return task_id


def _optional_text(value: Any, field: str, max_length: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_length:
        raise ToolFailure("INVALID_ARGUMENT", f"{field} 长度不能超过 {max_length} 个字符")
    return text


def _safe_account(account: dict[str, Any]) -> dict[str, Any]:
    platform = str(account.get("platform", ""))
    if platform not in SUPPORTED_PLATFORMS:
        return {}
    status = account.get("status") or "logged_out"
    if status not in {"logged_in", "logged_out", "logging_in", "login_failed"}:
        status = "logged_out"
    return {
        "platform": platform,
        "status": status,
        "last_login_time": account.get("last_login_time"),
    }


def _safe_internal_account(
    account: dict[str, Any],
    *,
    platform: str,
) -> dict[str, Any]:
    """Re-project the already public account response at the MCP boundary."""

    return {
        "account_id": _safe_text(account.get("account_id"))[:128],
        "display_name": _safe_text(account.get("display_name")),
        "masked_platform_user_id": _safe_text(
            account.get("masked_platform_user_id")
        ),
        "status": _safe_text(account.get("status")),
        "session_status": _safe_text(account.get("session_status")),
        "persist_login": bool(account.get("persist_login", False)),
        "heartbeat_enabled": bool(account.get("heartbeat_enabled", False)),
        "next_heartbeat_at": account.get("next_heartbeat_at"),
        "last_heartbeat_at": account.get("last_heartbeat_at"),
        "heartbeat_failures": int(account.get("heartbeat_failures", 0) or 0),
        "last_heartbeat_error_code": _safe_text(
            account.get("last_heartbeat_error_code"),
            default="",
        )
        or None,
        "last_verified_at": account.get("last_verified_at"),
        "platform": platform,
    }


def _safe_internal_activity(item: dict[str, Any]) -> dict[str, Any]:
    """Keep activity output aligned with AccountSessionService.list_activity."""

    safe_message = _safe_text(item.get("message"))
    safe_message = re.sub(
        r"(?i)\b(cookie|token|secret|password|api[_ -]?key)\s*=\s*\[redacted\]",
        "[redacted]",
        safe_message,
    )
    return {
        "id": item.get("id"),
        "operation_id": _safe_text(item.get("operation_id"), default="") or None,
        "platform": _safe_text(item.get("platform")),
        "display_name": _safe_text(item.get("display_name")),
        "source": _safe_text(item.get("source")),
        "action": _safe_text(item.get("action")),
        "level": _safe_text(item.get("level")),
        "message": safe_message,
        "created_at": item.get("created_at"),
    }


def _safe_task(task: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "task_id": task.get("id"),
        "platform": task.get("platform"),
        "status": task.get("status"),
        "article_title": _safe_text(task.get("article_title")),
        "article_filename": _safe_filename(task.get("article_filename")),
        "title_used": _safe_text(task.get("title_used")),
        "topic_used": _safe_text(task.get("topic_used")),
        "community_used": _safe_text(task.get("community_used")),
        "selection_status": task.get("selection_status"),
        "media_status": task.get("media_status"),
        "expected_images": task.get("expected_images", 0),
        "uploaded_images": task.get("uploaded_images", 0),
        "retry_count": task.get("retry_count", 0),
        "error_code": _safe_text(task.get("error_code")),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
        "created_at": task.get("created_at"),
    }
    error_message = _safe_text(task.get("error_message"))
    if error_message:
        result["error_message"] = error_message
    draft_url = task.get("draft_url")
    if isinstance(draft_url, str) and draft_url.startswith(("http://", "https://")):
        result["draft_url"] = draft_url[:1000]
    return result


def _safe_article(article: dict[str, Any]) -> dict[str, Any]:
    tasks = article.get("tasks") or []
    return {
        "article_id": article.get("id"),
        "filename": _safe_filename(article.get("filename")),
        "title": _safe_text(article.get("title")),
        "keywords": _safe_text(article.get("keywords")),
        "topic_zol": _safe_text(article.get("topic_zol")),
        "topic_xiaoheihe": _safe_text(article.get("topic_xiaoheihe")),
        "image_count": article.get("image_count", 0),
        "char_count": article.get("char_count", 0),
        "status": article.get("status"),
        "created_at": article.get("created_at"),
        "tasks": [_safe_task(task) for task in tasks if isinstance(task, dict)],
    }


def _new_task_id(prefix: str, suffix: str = "") -> str:
    timestamp = int(time.time())
    unique = uuid.uuid4().hex[:8]
    parts = [prefix]
    if suffix:
        parts.append(suffix)
    parts.extend([str(timestamp), unique])
    return "-".join(parts)


def _safe_client_request_id(value: Any) -> str:
    request_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", request_id):
        raise ToolFailure(
            "INVALID_ARGUMENT",
            "client_request_id 必须为 8 到 128 位字母、数字、点、下划线、冒号或连字符",
        )
    return request_id


def _draft_task_id(client_request_id: str) -> str:
    digest = hashlib.sha256(client_request_id.encode("utf-8")).hexdigest()[:24]
    return f"article-draft-{digest}"


def _draft_request_fingerprint(
    source_download_url: str,
    targets: list[dict[str, Any]],
) -> str:
    canonical = json.dumps(
        {"source_download_url": source_download_url, "targets": targets},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_SAFE_DRAFT_DEGRADED_VALUES = frozenset({"draft_list_confirmed"})
_SAFE_EVIDENCE_BOOL_FIELDS = frozenset(
    {
        "save_response_2xx",
        "draft_list_title_unique",
        "reopen_title_match",
        "reopen_dom_blocks_match",
        "draft_entity_bound",
        "draft_entity_id_match",
        "unknown",
    }
)
_SAFE_EVIDENCE_INT_FIELDS = frozenset(
    {"save_http_status", "draft_list_match_count"}
)
_SAFE_EVIDENCE_TEXT_FIELDS = frozenset(
    {"save_platform_code", "draft_entity_source", "summary"}
)
_SAFE_EVIDENCE_ENTITY_SOURCES = frozenset(
    {
        "save_response_id",
        "existing_draft_id",
        "baseline_new_id",
        "title_match_without_baseline",
        "unknown",
    }
)
_SAFE_EVIDENCE_SECRET_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "cookie",
        "password",
        "secret",
        "token",
    }
)


def _safe_evidence_url(value: Any) -> str | None:
    """Keep evidence links public and reject credentials/query secrets."""

    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return None
    try:
        parsed = urlsplit(value)
        query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        fragment_keys = {
            key.lower() for key, _ in parse_qsl(parsed.fragment, keep_blank_values=True)
        }
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or (query_keys | fragment_keys) & _SAFE_EVIDENCE_SECRET_QUERY_KEYS
    ):
        return None
    return value[:1000]


def _safe_evidence_summary(value: Any) -> str | None:
    """Project the backend's short summary without exposing paths or secrets."""

    if value is None:
        return None
    summary = _safe_text(value, default="")
    if not summary:
        return None
    # safe_error_message covers the common ``token=`` form. Keep this second
    # guard for profile/cookie labels and variants embedded in arbitrary text.
    summary = re.sub(
        r"(?i)\b(profile|cookie|token|secret|password|api[_ -]?key)\b\s*[:=]\s*"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^,;\r\n]*)",
        r"\1=[redacted]",
        summary,
    )
    return summary[:500]


def _safe_verification_evidence(value: Any) -> dict[str, Any] | None:
    """Allow only stable, scalar evidence fields at the MCP boundary."""

    if not isinstance(value, dict):
        return None
    evidence: dict[str, Any] = {}
    for field in _SAFE_EVIDENCE_BOOL_FIELDS:
        if field not in value:
            continue
        raw = value[field]
        if raw is None or isinstance(raw, bool):
            evidence[field] = raw
    for field in _SAFE_EVIDENCE_INT_FIELDS:
        if field not in value:
            continue
        raw = value[field]
        if raw is None:
            evidence[field] = None
        elif isinstance(raw, int) and not isinstance(raw, bool):
            if field == "save_http_status" and not 100 <= raw <= 599:
                continue
            if field == "draft_list_match_count" and raw < 0:
                continue
            evidence[field] = raw
    for field in _SAFE_EVIDENCE_TEXT_FIELDS:
        if field not in value:
            continue
        raw = value[field]
        if raw is None:
            evidence[field] = None
        elif field == "summary":
            evidence[field] = _safe_evidence_summary(raw)
        elif field == "draft_entity_source":
            source = str(raw)
            evidence[field] = source if source in _SAFE_EVIDENCE_ENTITY_SOURCES else "unknown"
        else:
            text = _safe_text(raw, default="")
            evidence[field] = text[:128] or None
    if "draft_url" in value:
        evidence["draft_url"] = _safe_evidence_url(value.get("draft_url"))
    return evidence or None


def _safe_plan_target(item: dict[str, Any]) -> dict[str, Any]:
    target = {
        "platform": _safe_text(item.get("platform")),
        "account_display_name": _safe_text(item.get("account_display_name")),
        "mode": "DRAFT",
        "status": _safe_text(item.get("status")),
        "operation_id": _safe_text(item.get("operation_id"), default="") or None,
        "article_mapping_status": _safe_text(
            item.get("article_mapping_status"), default=""
        )
        or None,
        "error_code": _safe_text(item.get("error_code"), default="") or None,
        "error_message": _safe_text(item.get("error_message"), default="") or None,
    }
    draft_url = item.get("draft_url")
    safe_draft_url = _safe_evidence_url(draft_url)
    if safe_draft_url:
        target["draft_url"] = safe_draft_url
    if "degraded" in item:
        degraded = item.get("degraded")
        target["degraded"] = (
            degraded
            if isinstance(degraded, str) and degraded in _SAFE_DRAFT_DEGRADED_VALUES
            else None
        )
    if "verification_evidence" in item:
        target["verification_evidence"] = _safe_verification_evidence(
            item.get("verification_evidence")
        )
    return target


def _draft_plan_result(plan: dict[str, Any]) -> dict[str, Any]:
    source_targets = plan.get("targets")
    targets = [
        _safe_plan_target(item)
        for item in source_targets or []
        if isinstance(item, dict)
    ]
    return {
        "plan_id": _safe_text(plan.get("plan_id"))[:128],
        "overall_status": _safe_text(plan.get("status")),
        "targets": targets,
    }


def _mcp_status_for_plan(plan_result: dict[str, Any]) -> tuple[str, str]:
    status = str(plan_result.get("overall_status") or "").upper()
    targets = plan_result.get("targets") or []
    target_statuses = {
        str(item.get("status") or "").upper()
        for item in targets
        if isinstance(item, dict)
    }
    all_incomplete = bool(targets) and target_statuses == {"DELIVERY_INCOMPLETE"}
    has_incomplete = "DELIVERY_INCOMPLETE" in target_statuses
    if all_incomplete:
        return "failed", "草稿投递未完成；平台未确认本次草稿实体，请人工核对后再决定下一步。"

    def needs_review(item: dict[str, Any]) -> bool:
        item_status = str(item.get("status") or "").upper()
        if item_status in {
            "DRAFT_SAVED_WITH_WARNINGS",
            "PUBLISHED_WITH_WARNINGS",
        } or item.get("degraded"):
            return True
        evidence = item.get("verification_evidence")
        return isinstance(evidence, dict) and (
            evidence.get("reopen_title_match") is False
            or evidence.get("reopen_dom_blocks_match") is False
        )

    review_required = any(
        needs_review(item) for item in targets if isinstance(item, dict)
    )
    if status == "SUCCESS":
        if has_incomplete:
            return "failed", "草稿投递包含未完成目标；请查看各平台目标状态并人工核对。"
        if review_required:
            return "completed", "草稿已保存，但部分目标需核对平台草稿箱的正文和图片完整性。"
        return "completed", "所有平台草稿均已保存并通过平台侧验证。"
    if status == "PARTIAL_FAIL":
        if review_required:
            return (
                "completed",
                "草稿投递已结束；已保存目标仍需核对正文和图片，其他目标请查看错误状态。",
            )
        return "completed", "草稿投递已结束，部分平台成功、部分平台失败或结果未知。"
    if status in {"FATAL", "FORMAT_REVIEW_REQUIRED", "DELIVERY_INCOMPLETE"}:
        return "failed", "草稿投递未成功，请查看各平台目标的错误状态。"
    if any(
        str(item.get("status") or "").upper()
        in {
            "FAILED",
            "BLOCKED",
            "RESULT_UNKNOWN",
            "LOGIN_REQUIRED",
            "DELIVERY_INCOMPLETE",
        }
        for item in targets
        if isinstance(item, dict)
    ) and all(
        str(item.get("status") or "").upper()
        not in {"CREATING", "QUEUED", "RUNNING", "READY"}
        for item in targets
        if isinstance(item, dict)
    ):
        return "failed", "所有草稿目标均已进入终态，但部分目标投递未完成或结果未知。"
    if status == "READY":
        return "pending", "投递计划已创建，等待执行单启动。"
    return "running", "平台草稿正在按目标账号执行，请继续轮询。"


def _stored_draft_task_response(record: dict[str, Any]) -> dict[str, Any]:
    response = {
        "task_id": record["task_id"],
        "status": record["status"],
        "message": _safe_text(record.get("message")),
    }
    if isinstance(record.get("result"), dict):
        response["result"] = record["result"]
    if record["status"] in {"pending", "running"}:
        response["async_task"] = _async_task_info(
            record["task_id"],
            "get_article_draft_delivery_result",
            10,
        )
    return response


def _async_task_info(task_id: str, poll_tool: str, interval: int) -> dict[str, Any]:
    return {
        "protocol": "cs-admin-async-task/v1",
        "poll_tool": poll_tool,
        "poll_arguments": {"task_id": "$task_id"},
        "poll_interval_seconds": interval,
    }


def _argument_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """Audit only non-sensitive shape information, never URLs or file content."""
    summary: dict[str, Any] = {}
    for key, value in arguments.items():
        if key == "source_download_url":
            summary[key] = "provided" if value else "missing"
        elif isinstance(value, list):
            summary[key] = {"count": len(value)}
        elif isinstance(value, str):
            summary[key] = "provided" if value else "empty"
        else:
            summary[key] = value
    return summary


async def _execute(
    tool_name: str,
    arguments: dict[str, Any],
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    try:
        return _with_trace_fields(await operation(), request_id)
    except ToolFailure as exc:
        return _with_trace_fields(
            {"error": {"code": exc.code, "message": exc.message[:500]}},
            request_id,
        )
    except FlaskClientError as exc:
        return _with_trace_fields(
            {"error": {"code": exc.code, "message": exc.message[:500]}},
            request_id,
        )
    except Exception:
        logger.exception("MCP 工具执行异常: {}", tool_name)
        return _with_trace_fields(
            {"error": {"code": "INTERNAL_ERROR", "message": "MCP 工具内部错误"}},
            request_id,
        )
    finally:
        logger.info(
            "MCP 调用完成: server_id={} request_id={} tool={} args={} elapsed_ms={:.0f}",
            SERVER_ID,
            request_id,
            tool_name,
            _argument_summary(arguments),
            (time.monotonic() - started) * 1000,
        )


def _configured_file_service_hosts() -> set[str]:
    raw = os.getenv("MCP_FILE_SERVICE_ALLOWED_HOSTS")
    if raw is None:
        return set(DEFAULT_FILE_SERVICE_HOSTS)
    return {
        item.strip().lower().rstrip("/")
        for item in raw.split(",")
        if item.strip()
    }


def _host_allowed(url: str, allowed_hosts: set[str]) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    host_with_port = hostname
    if parsed.port is not None:
        host_with_port = f"{hostname}:{parsed.port}"
    return hostname in allowed_hosts or host_with_port in allowed_hosts


def _validate_source_url(source_download_url: str) -> str:
    value = str(source_download_url or "").strip()
    if not value or len(value) > 2048:
        raise ToolFailure("INVALID_ARGUMENT", "source_download_url 不能为空且长度不能超过 2048")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolFailure("RESOURCE_RESTRICTED", "文件地址必须是受控的 HTTP(S) 下载地址")
    if parsed.username or parsed.password:
        raise ToolFailure("RESOURCE_RESTRICTED", "文件地址不能包含账号或密码")
    allowed_hosts = _configured_file_service_hosts()
    if not allowed_hosts:
        raise ToolFailure("RESOURCE_RESTRICTED", "未配置受控文件服务白名单")
    if not _host_allowed(value, allowed_hosts):
        raise ToolFailure("RESOURCE_RESTRICTED", "文件地址主机不在受控文件服务白名单中")
    return value


def _validate_redirect_url(value: str, allowed_hosts: set[str]) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ToolFailure("RESOURCE_RESTRICTED", "文件下载重定向地址不受支持")
    if not _host_allowed(value, allowed_hosts):
        raise ToolFailure("RESOURCE_RESTRICTED", "文件下载重定向主机不在受控文件服务白名单中")


async def _download_docx(source_download_url: str, destination: Path) -> None:
    """Download only the CS_Admin-provided source file to a temporary path."""
    url = _validate_source_url(source_download_url)
    allowed_hosts = _configured_file_service_hosts()
    timeout = httpx.Timeout(30.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                final_url = str(response.url)
                _validate_redirect_url(final_url, allowed_hosts)
                if response.status_code == 404:
                    raise ToolFailure("NOT_FOUND", "源文件下载地址不存在或已过期")
                if response.status_code >= 400:
                    raise ToolFailure("UNAVAILABLE", "源文件服务暂时不可用")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                    raise ToolFailure("INVALID_ARGUMENT", "源文件不能超过 50 MB")

                total = 0
                with destination.open("wb") as target:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ToolFailure("INVALID_ARGUMENT", "源文件不能超过 50 MB")
                        target.write(chunk)
    except ToolFailure:
        raise
    except httpx.TimeoutException as exc:
        raise ToolFailure("TIMEOUT", "源文件下载超时") from exc
    except httpx.RequestError as exc:
        raise ToolFailure("UNAVAILABLE", "无法连接源文件服务") from exc
    except (OSError, ValueError) as exc:
        raise ToolFailure("INTERNAL_ERROR", "源文件下载失败") from exc


def register_tools(
    server: Any,
    client: FlaskClient,
    store: TaskStore,
    *,
    legacy_mutations_enabled: bool = False,
) -> dict[str, Callable[..., Any]]:
    """Register the fixed, business-scoped tool set and return handlers for tests."""
    handlers: dict[str, Callable[..., Any]] = {}

    @server.tool(
        name="list_platform_accounts",
        description=(
            "按平台读取 MCP 白名单内的账号公开投影；仅查询，不登录、验证、退出、"
            "清 Cookie 或发布。"
        ),
        structured_output=True,
    )
    async def list_platform_accounts(
        platform: InternalPlatform,
        usable: bool = False,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            checked_platform = _safe_internal_platform(platform)
            payload = await client.get_platform_accounts(
                checked_platform,
                usable=bool(usable),
            )
            source_accounts = payload.get("accounts")
            if not isinstance(source_accounts, list):
                raise ToolFailure("INTERNAL_ERROR", "Flask 未返回账号公开投影")
            items = [
                _safe_internal_account(item, platform=checked_platform)
                for item in source_accounts
                if isinstance(item, dict)
            ]
            return {
                "platform": checked_platform,
                "usable": bool(usable),
                "count": len(items),
                "empty": not items,
                "items": items,
            }

        return await _execute(
            "list_platform_accounts",
            {"platform": platform, "usable": usable},
            operation,
        )

    handlers["list_platform_accounts"] = list_platform_accounts

    @server.tool(
        name="get_account_activity",
        description=(
            "读取 MCP 白名单内指定账号的脱敏活动日志；仅查询，不触发任何账号、"
            "会话或投递动作。"
        ),
        structured_output=True,
    )
    async def get_account_activity(
        account_id: AccountId,
        limit: ActivityLimit = 100,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            checked_account_id = _safe_account_id(account_id)
            bounded_limit = min(max(int(limit), 1), 200)
            payload = await client.get_account_activity(
                checked_account_id,
                limit=bounded_limit,
            )
            source_rows = payload.get("activities")
            if not isinstance(source_rows, list):
                raise ToolFailure("INTERNAL_ERROR", "Flask 未返回账号活动日志")
            items = [
                _safe_internal_activity(item)
                for item in source_rows
                if isinstance(item, dict)
            ]
            return {
                "account_id": checked_account_id,
                "limit": bounded_limit,
                "count": len(items),
                "empty": not items,
                "activities": items,
            }

        return await _execute(
            "get_account_activity",
            {"account_id": account_id, "limit": limit},
            operation,
        )

    handlers["get_account_activity"] = get_account_activity

    @server.tool(
        name="start_article_draft_delivery",
        description=(
            "将 CS_Admin 提供的受控 DOCX 按目标账号保存为平台草稿。只允许当前已验证的"
            "草稿平台和 MCP 账号白名单；不接受本机路径、不公开发布。返回异步任务。"
        ),
        structured_output=True,
    )
    async def start_article_draft_delivery(
        source_download_url: SourceDownloadURL,
        targets: Annotated[list[DraftDeliveryTarget], Field(min_length=1, max_length=50)],
        client_request_id: ClientRequestId,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            checked_request_id = _safe_client_request_id(client_request_id)
            source_url = _validate_source_url(source_download_url)
            normalized_targets: list[dict[str, Any]] = []
            seen_accounts: set[str] = set()
            for raw_target in targets:
                try:
                    target = (
                        raw_target
                        if isinstance(raw_target, DraftDeliveryTarget)
                        else DraftDeliveryTarget.model_validate(raw_target)
                    )
                except ValidationError as exc:
                    raise ToolFailure(
                        "INVALID_ARGUMENT",
                        "targets 不符合平台草稿目标契约",
                    ) from exc
                if target.platform not in CURRENT_DRAFT_PLATFORMS:
                    raise ToolFailure(
                        "INVALID_ARGUMENT",
                        f"平台 {target.platform} 尚未开放 Content Studio 草稿投递",
                    )
                if target.account_id in seen_accounts:
                    raise ToolFailure("INVALID_ARGUMENT", "同一账号不能重复添加为投递目标")
                seen_accounts.add(target.account_id)
                normalized_targets.append(
                    {
                        "platform": target.platform,
                        "account_id": target.account_id,
                        "persist_login": target.persist_login,
                    }
                )

            task_id = _draft_task_id(checked_request_id)
            fingerprint = _draft_request_fingerprint(source_url, normalized_targets)
            existing = await store.get(task_id)
            if existing is not None:
                existing_fingerprint = (existing.get("metadata") or {}).get(
                    "request_fingerprint"
                )
                if existing_fingerprint != fingerprint:
                    raise ToolFailure(
                        "REQUEST_KEY_CONFLICT",
                        "client_request_id 已用于另一组内容或投递目标",
                    )
                return _stored_draft_task_response(existing)

            try:
                await store.create(
                    task_id,
                    "content_studio_draft_delivery",
                    "pending",
                    message="正在下载并冻结 DOCX，禁止重复提交相同 client_request_id。",
                    metadata={
                        "request_fingerprint": fingerprint,
                        "target_platforms": [
                            item["platform"] for item in normalized_targets
                        ],
                        "submission_deadline_epoch": int(time.time()) + 300,
                    },
                )
            except sqlite3.IntegrityError as exc:
                concurrent = await store.get(task_id)
                if concurrent is None:
                    raise ToolFailure(
                        "INTERNAL_ERROR", "MCP 幂等任务读取失败"
                    ) from exc
                concurrent_fingerprint = (concurrent.get("metadata") or {}).get(
                    "request_fingerprint"
                )
                if concurrent_fingerprint != fingerprint:
                    raise ToolFailure(
                        "REQUEST_KEY_CONFLICT",
                        "client_request_id 已用于另一组内容或投递目标",
                    ) from exc
                return _stored_draft_task_response(concurrent)
            try:
                with tempfile.TemporaryDirectory(prefix="articleops-mcp-draft-") as temp_dir:
                    source_path = Path(temp_dir) / "source.docx"
                    await _download_docx(source_url, source_path)
                    payload = await client.create_draft_delivery(
                        str(source_path),
                        normalized_targets,
                    )
                plan = payload.get("plan")
                if not isinstance(plan, dict) or not plan.get("plan_id"):
                    raise ToolFailure("INTERNAL_ERROR", "Content Studio 未返回投递计划")
                plan_result = _draft_plan_result(plan)
                metadata = {
                    "request_fingerprint": fingerprint,
                    "target_platforms": [item["platform"] for item in normalized_targets],
                    "draft_id": _safe_text(payload.get("draft_id"))[:128],
                    "plan_id": plan_result["plan_id"],
                }
                task_status, message = _mcp_status_for_plan(plan_result)
                await store.update(
                    task_id,
                    status=task_status,
                    message=message,
                    metadata=metadata,
                    result=plan_result if task_status not in {"pending", "running"} else None,
                )
            except (ToolFailure, FlaskClientError) as exc:
                result_unknown = exc.code in {"TIMEOUT", "UNAVAILABLE"}
                code = "SUBMISSION_RESULT_UNKNOWN" if result_unknown else exc.code
                message = (
                    "草稿提交结果未知；为避免重复平台草稿，禁止使用相同请求自动重试"
                    if result_unknown
                    else exc.message
                )
                await store.update(
                    task_id,
                    status="failed",
                    message=message,
                    result={"error": {"code": code, "message": message}},
                )
                raise ToolFailure(code, message) from exc

            record = await store.get(task_id)
            if record is None:
                raise ToolFailure("INTERNAL_ERROR", "MCP 异步任务未能持久化")
            return _stored_draft_task_response(record)

        return await _execute(
            "start_article_draft_delivery",
            {
                "source_download_url": source_download_url,
                "targets": targets,
                "client_request_id": client_request_id,
            },
            operation,
        )

    handlers["start_article_draft_delivery"] = start_article_draft_delivery

    @server.tool(
        name="get_article_draft_delivery_result",
        description=(
            "查询 start_article_draft_delivery 创建的持久化任务。该工具幂等，只读取并"
            "对账既有计划，不会再次创建草稿或重放平台操作。"
        ),
        structured_output=True,
    )
    async def get_article_draft_delivery_result(
        task_id: TaskToken,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            checked_task_id = str(task_id or "").strip()
            record = await store.get(checked_task_id)
            if not record or record.get("kind") != "content_studio_draft_delivery":
                raise ToolFailure("NOT_FOUND", "草稿投递任务不存在")
            metadata = record.get("metadata") or {}
            plan_id = str(metadata.get("plan_id") or "").strip()
            if not plan_id:
                deadline = int(metadata.get("submission_deadline_epoch") or 0)
                if deadline and int(time.time()) <= deadline:
                    return _stored_draft_task_response(record)
                message = "草稿提交进程中断且未取得计划 ID；结果未知，禁止自动重试"
                await store.update(
                    checked_task_id,
                    status="failed",
                    message=message,
                    result={
                        "error": {
                            "code": "SUBMISSION_RESULT_UNKNOWN",
                            "message": message,
                        }
                    },
                )
                updated = await store.get(checked_task_id)
                return _stored_draft_task_response(updated or record)

            plan = await client.get_draft_delivery_plan(plan_id)
            plan_result = _draft_plan_result(plan)
            task_status, message = _mcp_status_for_plan(plan_result)
            await store.update(
                checked_task_id,
                status=task_status,
                message=message,
                result=plan_result if task_status not in {"pending", "running"} else None,
            )
            updated = await store.get(checked_task_id)
            return _stored_draft_task_response(updated or record)

        return await _execute(
            "get_article_draft_delivery_result",
            {"task_id": task_id},
            operation,
        )

    handlers["get_article_draft_delivery_result"] = get_article_draft_delivery_result

    @server.tool(
        name="list_accounts",
        description="[LEGACY] 查询 ZOL 和小黑盒账号登录状态以及上次登录时间。不修改任何数据。",
        structured_output=True,
    )
    async def list_accounts() -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            accounts = [_safe_account(item) for item in await client.get_accounts()]
            accounts = [item for item in accounts if item]
            by_platform = {item["platform"]: item for item in accounts}
            ordered = [by_platform[platform] for platform in ("zol", "xiaoheihe") if platform in by_platform]
            return {
                "count": len(ordered),
                "empty": not ordered,
                "items": ordered,
                # Keep the original field as a backwards-compatible alias.
                "accounts": ordered,
            }

        return await _execute("list_accounts", {}, operation)

    handlers["list_accounts"] = list_accounts

    @server.tool(
        name="list_articles",
        description="[LEGACY] 查询已上传文章的标题、关键词、字数、图片数和关联发布任务。不修改任何数据。",
        structured_output=True,
    )
    async def list_articles() -> dict[str, Any]:
        return await _execute(
            "list_articles",
            {},
            lambda: _list_articles(client),
        )

    handlers["list_articles"] = list_articles

    @server.tool(
        name="list_tasks",
        description="[LEGACY] 查询所有发布任务及其状态、平台和关联文章标题。不修改任何数据。",
        structured_output=True,
    )
    async def list_tasks() -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            tasks = await client.get_tasks()
            items = [_safe_task(task) for task in tasks]
            return {
                "count": len(items),
                "empty": not items,
                "items": items,
                # Keep the original field as a backwards-compatible alias.
                "tasks": items,
            }

        return await _execute("list_tasks", {}, operation)

    handlers["list_tasks"] = list_tasks

    @server.tool(
        name="get_task_logs",
        description="[LEGACY] 查询指定发布任务的按时间排序执行日志。不修改任何数据。",
        structured_output=True,
    )
    async def get_task_logs(task_id: PositiveTaskId) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            checked_id = _safe_task_id(task_id)
            tasks = await client.get_tasks()
            if not any(int(item.get("id", 0)) == checked_id for item in tasks):
                raise ToolFailure("NOT_FOUND", f"任务 {checked_id} 不存在")
            logs = await client.get_task_logs(checked_id)
            return {
                "count": len(logs),
                "empty": not logs,
                "task_id": checked_id,
                "logs": [
                    {
                        "level": item.get("level", "INFO"),
                        "message": _safe_text(item.get("message")),
                        "created_at": item.get("created_at"),
                    }
                    for item in logs
                    if isinstance(item, dict)
                ],
            }

        return await _execute("get_task_logs", {"task_id": task_id}, operation)

    handlers["get_task_logs"] = get_task_logs

    @server.tool(
        name="get_queue_status",
        description="[LEGACY] 查询文章发布队列的队列大小、运行状态和待处理任务数。不修改任何数据。",
        structured_output=True,
    )
    async def get_queue_status() -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            status = await client.get_queue_status()
            return {
                "queue_size": int(status.get("queue_size", 0) or 0),
                "running": bool(status.get("running", False)),
                "pending": int(status.get("pending", 0) or 0),
            }

        return await _execute("get_queue_status", {}, operation)

    handlers["get_queue_status"] = get_queue_status

    @server.tool(
        name="start_login",
        description="[LEGACY MUTATION] 默认关闭（需 MCP_LEGACY_MUTATIONS_ENABLED=true）；发起 ZOL 或小黑盒登录。",
        structured_output=True,
    )
    async def start_login(platform: Platform, force: bool = False) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            _require_legacy_mutations(legacy_mutations_enabled)
            checked_platform = _safe_platform(platform)
            if force:
                await client.clear_cookies(checked_platform)
            login_payload = await client.start_login(checked_platform)
            if login_payload.get("status") not in {"login_started", "already_logging_in"}:
                raise ToolFailure("INTERNAL_ERROR", "Flask 未能启动登录流程")

            task_id = _new_task_id("login", checked_platform)
            message = (
                f"已在本机打开 {checked_platform} 登录页面，请完成扫码或账号登录。"
            )
            if login_payload.get("status") == "already_logging_in":
                message = f"{checked_platform} 登录流程已在进行中，请完成浏览器中的登录操作。"
            await store.create(
                task_id,
                "login",
                "awaiting_user_action",
                platform=checked_platform,
                message=message,
            )
            return {
                "task_id": task_id,
                "status": "awaiting_user_action",
                "message": message,
                "async_task": _async_task_info(task_id, "get_login_result", 3),
            }

        return await _execute("start_login", {"platform": platform, "force": force}, operation)

    handlers["start_login"] = start_login

    @server.tool(
        name="get_login_result",
        description="[LEGACY] 轮询 start_login 创建的登录任务，返回等待扫码、登录成功或登录失败状态。",
        structured_output=True,
    )
    async def get_login_result(task_id: TaskToken) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            record = await store.get(str(task_id or "").strip())
            if not record or record.get("kind") != "login":
                raise ToolFailure("NOT_FOUND", "登录任务不存在")
            platform = _safe_platform(record.get("platform"))
            accounts = await client.get_accounts()
            account = next((item for item in accounts if item.get("platform") == platform), None)
            status = account.get("status") if account else "logging_in"
            if status == "logged_in":
                final_status, message = "completed", f"{platform} 登录成功"
                result = {"platform": platform, "status": "logged_in"}
            elif status == "logging_in":
                final_status, message = "awaiting_user_action", f"等待 {platform} 完成扫码或登录"
                result = {"platform": platform, "status": "logging_in"}
            elif status == "login_failed":
                final_status, message = "failed", f"{platform} 登录失败"
                result = {"platform": platform, "status": "login_failed"}
            else:
                final_status, message = "failed", f"{platform} 未完成登录"
                result = {"platform": platform, "status": status or "logged_out"}

            await store.update(record["task_id"], status=final_status, message=message, result=result)
            response: dict[str, Any] = {
                "task_id": record["task_id"],
                "status": final_status,
                "message": message,
                "result": result,
            }
            if final_status == "awaiting_user_action":
                response["async_task"] = _async_task_info(record["task_id"], "get_login_result", 3)
            return response

        return await _execute("get_login_result", {"task_id": task_id}, operation)

    handlers["get_login_result"] = get_login_result

    @server.tool(
        name="publish_article",
        description="[LEGACY MUTATION] 默认关闭（需 MCP_LEGACY_MUTATIONS_ENABLED=true）；下载 DOCX 并创建旧发布任务。",
        structured_output=True,
    )
    async def publish_article(
        source_download_url: SourceDownloadURL,
        platforms: Annotated[list[Platform] | None, Field(min_length=1, max_length=2)] = None,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            _require_legacy_mutations(legacy_mutations_enabled)
            source_url = _validate_source_url(source_download_url)
            selected = list(platforms or ["zol", "xiaoheihe"])
            if not selected or len(selected) > 2 or len(set(selected)) != len(selected):
                raise ToolFailure("INVALID_ARGUMENT", "platforms 必须是不重复的平台列表")
            selected = [_safe_platform(item) for item in selected]

            with tempfile.TemporaryDirectory(prefix="article-publisher-mcp-") as temp_dir:
                source_path = Path(temp_dir) / "source.docx"
                await _download_docx(source_url, source_path)
                upload_payload = await client.upload_docx(str(source_path), selected)

            results = upload_payload.get("results")
            if not isinstance(results, list):
                raise ToolFailure("INTERNAL_ERROR", "Flask 未返回文章任务结果")
            internal_task_ids: list[int] = []
            article_ids: list[int] = []
            errors: list[str] = []
            for item in results:
                if not isinstance(item, dict):
                    continue
                if item.get("status") == "error":
                    errors.append("文章解析或任务创建失败")
                    continue
                if isinstance(item.get("id"), int):
                    article_ids.append(item["id"])
                for task in item.get("tasks") or []:
                    if isinstance(task, dict) and isinstance(task.get("task_id"), int):
                        internal_task_ids.append(task["task_id"])
            if errors or not internal_task_ids:
                raise ToolFailure("INTERNAL_ERROR", errors[0] if errors else "Flask 未创建发布任务")

            task_id = _new_task_id("publish")
            message = "文章已解析并创建发布任务，正在排队执行。"
            await store.create(
                task_id,
                "publish",
                "pending",
                internal_task_ids=internal_task_ids,
                metadata={"platforms": selected, "article_ids": article_ids},
                message=message,
            )
            return {
                "task_id": task_id,
                "status": "pending",
                "message": message,
                "async_task": _async_task_info(task_id, "get_publish_result", 10),
            }

        return await _execute(
            "publish_article",
            {"source_download_url": source_download_url, "platforms": platforms or []},
            operation,
        )

    handlers["publish_article"] = publish_article

    @server.tool(
        name="get_publish_result",
        description="[LEGACY] 轮询 publish_article 创建的发布任务，返回排队、发布中、需人工选择、完成或失败状态。",
        structured_output=True,
    )
    async def get_publish_result(task_id: TaskToken) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            record = await store.get(str(task_id or "").strip())
            if not record or record.get("kind") != "publish":
                raise ToolFailure("NOT_FOUND", "发布任务不存在")
            internal_ids = [int(value) for value in record.get("internal_task_ids", [])]
            tasks = await client.get_tasks()
            by_id = {int(item.get("id")): item for item in tasks if isinstance(item, dict) and item.get("id") is not None}
            missing = [value for value in internal_ids if value not in by_id]
            if missing:
                raise ToolFailure("NOT_FOUND", "Flask 发布任务不存在")

            views = [_safe_task(by_id[value]) for value in internal_ids]
            statuses = [str(by_id[value].get("status") or "") for value in internal_ids]
            if any(status == "needs_selection" for status in statuses):
                current_status = "awaiting_user_action"
                needs = []
                for view in views:
                    if view.get("status") != "needs_selection":
                        continue
                    if view.get("platform") == "xiaoheihe":
                        if not view.get("community_used"):
                            needs.append("community")
                        if not view.get("topic_used"):
                            needs.append("topic")
                    elif view.get("platform") == "zol" and not view.get("topic_used"):
                        needs.append("topic")
                needs = list(dict.fromkeys(needs)) or ["topic"]
                message = "请补充平台所需的社区/话题选择后使用 resume_task 恢复。"
                result: dict[str, Any] = {"tasks": views, "needs": needs}
            elif any(status in {"failed", "cancelled"} for status in statuses):
                current_status = "failed"
                message = "至少一个平台的发布任务失败。"
                result = {"tasks": views}
            elif statuses and all(status in {"completed", "completed_with_warnings"} for status in statuses):
                if any(status == "completed_with_warnings" for status in statuses):
                    current_status = "completed_with_warnings"
                    message = "基础草稿已保存，但部分社区、话题或图片需要后续处理。"
                else:
                    current_status = "completed"
                    message = "文章发布任务已完成。"
                result = {"tasks": views}
            elif any(status == "processing" for status in statuses):
                current_status = "running"
                message = "文章正在发布处理中。"
                result = {"tasks": views}
            else:
                current_status = "pending"
                message = "文章发布任务正在排队或等待恢复。"
                result = {"tasks": views}

            await store.update(record["task_id"], status=current_status, message=message, result=result)
            response = {
                "task_id": record["task_id"],
                "status": current_status,
                "message": message,
                "result": result,
            }
            if current_status in {"pending", "running", "awaiting_user_action"}:
                response["async_task"] = _async_task_info(record["task_id"], "get_publish_result", 10)
            return response

        return await _execute("get_publish_result", {"task_id": task_id}, operation)

    handlers["get_publish_result"] = get_publish_result

    @server.tool(
        name="resume_task",
        description="[LEGACY MUTATION] 默认关闭（需 MCP_LEGACY_MUTATIONS_ENABLED=true）；恢复旧发布任务。",
        structured_output=True,
    )
    async def resume_task(
        task_id: PositiveTaskId,
        community: OptionalShortText = None,
        topic: OptionalShortText = None,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            _require_legacy_mutations(legacy_mutations_enabled)
            checked_id = _safe_task_id(task_id)
            community_value = _optional_text(community, "community")
            topic_value = _optional_text(topic, "topic")
            payload = {
                key: value
                for key, value in {
                    "community": community_value,
                    "topic": topic_value,
                }.items()
                if value is not None
            }
            response = await client.resume_task(checked_id, payload)
            return {
                "task_id": checked_id,
                "status": "pending",
                "message": "任务已恢复，等待队列执行。",
                "selection_status": response.get("selection_status"),
            }

        return await _execute(
            "resume_task",
            {"task_id": task_id, "community": community, "topic": topic},
            operation,
        )

    handlers["resume_task"] = resume_task

    @server.tool(
        name="logout_account",
        description="[LEGACY MUTATION] 默认关闭（需 MCP_LEGACY_MUTATIONS_ENABLED=true）；退出旧平台账号。",
        structured_output=True,
    )
    async def logout_account(platform: Platform) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            _require_legacy_mutations(legacy_mutations_enabled)
            checked_platform = _safe_platform(platform)
            payload = await client.logout(checked_platform)
            return {
                "status": "ok",
                "platform": checked_platform,
                "cookies_cleared": bool(payload.get("cookies_cleared", False)),
            }

        return await _execute("logout_account", {"platform": platform}, operation)

    handlers["logout_account"] = logout_account

    @server.tool(
        name="cleanup_locks",
        description="[LEGACY MUTATION] 默认关闭（需 MCP_LEGACY_MUTATIONS_ENABLED=true）；清理旧 Profile 锁文件。",
        structured_output=True,
    )
    async def cleanup_locks() -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            _require_legacy_mutations(legacy_mutations_enabled)
            payload = await client.cleanup_locks()
            return {
                "status": "ok",
                "cleaned_lock_files": int(payload.get("killed", 0) or 0),
            }

        return await _execute("cleanup_locks", {}, operation)

    handlers["cleanup_locks"] = cleanup_locks

    # The SDK intentionally leaves Pydantic's default unspecified in the
    # generated object schema.  CS_Admin requires closed input objects, so set
    # this explicitly on every registered tool before the server is exposed.
    tool_manager = getattr(server, "_tool_manager", None)
    if tool_manager is not None:
        for tool in tool_manager.list_tools():
            parameters = getattr(tool, "parameters", None)
            if isinstance(parameters, dict) and parameters.get("type") == "object":
                parameters["additionalProperties"] = False
    return handlers


async def _list_articles(client: FlaskClient) -> dict[str, Any]:
    articles = await client.get_articles()
    items = [_safe_article(article) for article in articles]
    return {
        "count": len(items),
        "empty": not items,
        "items": items,
        # Keep the original field as a backwards-compatible alias.
        "articles": items,
    }
