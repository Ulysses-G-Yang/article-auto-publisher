"""Business-facing MCP tools backed by the existing Flask REST API."""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal
from urllib.parse import urlsplit

import httpx
from loguru import logger
from pydantic import Field

from . import SERVER_ID
from .flask_client import FlaskClient, FlaskClientError, safe_error_message
from .task_store import TaskStore

Platform = Literal["zol", "xiaoheihe"]
PositiveTaskId = Annotated[int, Field(gt=0)]
TaskToken = Annotated[str, Field(min_length=1, max_length=128)]
SourceDownloadURL = Annotated[str, Field(min_length=1, max_length=2048)]
OptionalShortText = Annotated[str | None, Field(max_length=200)]
DEFAULT_FILE_SERVICE_HOSTS = {"dev.sccsai.com"}
SUPPORTED_PLATFORMS = {"zol", "xiaoheihe"}
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


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


def register_tools(server: Any, client: FlaskClient, store: TaskStore) -> dict[str, Callable[..., Any]]:
    """Register the fixed, business-scoped tool set and return handlers for tests."""
    handlers: dict[str, Callable[..., Any]] = {}

    @server.tool(
        name="list_accounts",
        description="查询 ZOL 和小黑盒两个平台的账号登录状态以及上次登录时间。不修改任何数据。",
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
        description="查询已上传文章的标题、关键词、字数、图片数和关联发布任务。不修改任何数据。",
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
        description="查询所有发布任务及其状态、平台和关联文章标题。不修改任何数据。",
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
        description="查询指定发布任务的按时间排序执行日志。不修改任何数据。",
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
        description="查询文章发布队列的队列大小、运行状态和待处理任务数。不修改任何数据。",
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
        description="发起 ZOL 或小黑盒登录，在本机打开浏览器等待人工扫码。立即返回异步任务，使用 get_login_result 轮询。",
        structured_output=True,
    )
    async def start_login(platform: Platform, force: bool = False) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
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
        description="轮询 start_login 创建的登录任务，返回等待扫码、登录成功或登录失败状态。",
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
        description="下载 CS_Admin 注入的 docx 文件并通过 Flask 创建 ZOL/小黑盒发布任务。返回异步任务，使用 get_publish_result 轮询。",
        structured_output=True,
    )
    async def publish_article(
        source_download_url: SourceDownloadURL,
        platforms: Annotated[list[Platform] | None, Field(min_length=1, max_length=2)] = None,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
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
        description="轮询 publish_article 创建的发布任务，返回排队、发布中、需人工选择、完成或失败状态。",
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
                message = "小黑盒需要选择社区和话题，请提供后使用 resume_task 恢复。"
                result: dict[str, Any] = {"tasks": views, "needs": ["community", "topic"]}
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
        description="恢复 paused 或 needs_selection 的发布任务；小黑盒 needs_selection 必须同时提供 community 和 topic。",
        structured_output=True,
    )
    async def resume_task(
        task_id: PositiveTaskId,
        community: OptionalShortText = None,
        topic: OptionalShortText = None,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
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
        description="退出 ZOL 或小黑盒账号，清除该平台 Cookie 并重置登录状态。",
        structured_output=True,
    )
    async def logout_account(platform: Platform) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
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
        description="清理 Chrome Profile 残留锁文件，不杀进程，不影响 Cookie。",
        structured_output=True,
    )
    async def cleanup_locks() -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
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
