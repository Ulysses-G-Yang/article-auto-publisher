from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

from flask import Flask
from mcp.server import MCPServer

import mcp_server.tools as mcp_tools
from account_sessions.mcp_access import (
    MCPInternalAccessResolver,
    MCPInternalAccessSettings,
)
from content_studio.web import create_content_studio_blueprint
from mcp_server.flask_client import FlaskClientError
from mcp_server.task_store import TaskStore
from mcp_server.tools import DraftDeliveryTarget, register_tools

TOKEN = "mcp-content-studio-token-0123456789-abcdef"
ACCOUNT_ID = "00000000-0000-4000-8000-000000000001"


def run(awaitable):
    return asyncio.run(awaitable)


class _StubContentService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def import_docx(self, data: bytes, filename: str):
        self.calls.append(("import", (len(data), filename)))
        return {"draft_id": "draft-mcp", "revision": 1}

    async def replace_targets(self, draft_id, payload, access):
        self.calls.append(("targets", (draft_id, payload, access)))
        return {"draft_id": draft_id, "revision": 2}

    async def create_delivery_plan(self, draft_id, revision, access):
        self.calls.append(("plan", (draft_id, revision, access)))
        return {"plan_id": "plan-mcp", "status": "READY", "targets": []}


def _content_app(tmp_path, *, draft_enabled: bool):
    delivery = SimpleNamespace(content_resolver=None)
    account_state = SimpleNamespace(accounts=SimpleNamespace(), delivery=delivery)
    resolver = MCPInternalAccessResolver(
        MCPInternalAccessSettings(
            TOKEN,
            frozenset({ACCOUNT_ID}),
            draft_delivery_enabled=draft_enabled,
        )
    )
    app = Flask("mcp-content-studio-route")
    app.register_blueprint(
        create_content_studio_blueprint(
            account_state=account_state,
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'content.db').as_posix()}",
            asset_root=tmp_path / "assets",
            work_root=tmp_path / "work",
            mcp_access_resolver=resolver,
        )
    )
    state = app.extensions["content_studio"]
    service = _StubContentService()
    state.service = service
    state.run = lambda coroutine, timeout=60: run(coroutine)

    async def execute_plan(plan_id, payload, access):
        service.calls.append(("execute", (plan_id, payload, access)))
        return {
            "plan_id": plan_id,
            "status": "EXECUTING",
            "targets": [
                {
                    "platform": "weibo",
                    "account_display_name": "测试账号",
                    "mode": "DRAFT",
                    "status": "QUEUED",
                    "operation_id": "operation-mcp",
                }
            ],
        }

    async def reconcile(plan_id, access):
        service.calls.append(("poll", (plan_id, access)))
        return {"plan_id": plan_id, "status": "SUCCESS", "targets": []}

    state.execute_plan = execute_plan
    state.reconcile_plan_operations = reconcile
    return app, service


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def _multipart_targets(**overrides):
    target = {
        "platform": "weibo",
        "account_id": ACCOUNT_ID,
        "persist_login": True,
        **overrides,
    }
    return {
        "file": (io.BytesIO(b"controlled-docx"), "article.docx"),
        "targets": json.dumps({"targets": [target]}),
    }


def test_internal_route_uses_mcp_actor_and_can_only_create_drafts(tmp_path):
    app, service = _content_app(tmp_path, draft_enabled=True)
    client = app.test_client()

    response = client.post(
        "/api/internal/mcp/draft-deliveries",
        headers=_headers(),
        data=_multipart_targets(),
        content_type="multipart/form-data",
    )

    assert response.status_code == 202
    assert response.get_json()["plan"]["status"] == "EXECUTING"
    target_request = next(value for name, value in service.calls if name == "targets")
    payload = target_request[1]
    access = target_request[2]
    assert payload.targets[0].mode == "DRAFT"
    assert access.actor_id == "articleops-mcp"
    assert access.source == "MCP"
    assert "draft.create" in access.capabilities
    assert "publish.execute" not in access.capabilities

    poll = client.get(
        "/api/internal/mcp/delivery-plans/plan-mcp",
        headers=_headers(),
    )
    assert poll.status_code == 200
    assert poll.get_json()["status"] == "SUCCESS"


def test_internal_route_fails_before_import_when_draft_capability_is_off(tmp_path):
    app, service = _content_app(tmp_path, draft_enabled=False)
    response = app.test_client().post(
        "/api/internal/mcp/draft-deliveries",
        headers=_headers(),
        data=_multipart_targets(),
        content_type="multipart/form-data",
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "ACCOUNT_PERMISSION_DENIED"
    assert service.calls == []


def test_internal_route_rejects_publish_mode_before_import(tmp_path):
    app, service = _content_app(tmp_path, draft_enabled=True)
    response = app.test_client().post(
        "/api/internal/mcp/draft-deliveries",
        headers=_headers(),
        data=_multipart_targets(mode="PUBLISH"),
        content_type="multipart/form-data",
    )

    assert response.status_code == 422
    assert response.get_json()["error"] == "REQUEST_VALIDATION_FAILED"
    assert service.calls == []


def test_internal_route_rejects_non_delivery_platform_before_import(tmp_path):
    app, service = _content_app(tmp_path, draft_enabled=True)
    response = app.test_client().post(
        "/api/internal/mcp/draft-deliveries",
        headers=_headers(),
        data=_multipart_targets(platform="xiaohongshu"),
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "MCP_INVALID_ARGUMENT"
    assert service.calls == []


class _DraftToolClient:
    def __init__(self) -> None:
        self.create_calls = 0
        self.plan_status = "SUCCESS"

    async def create_draft_delivery(self, _file_path, targets):
        self.create_calls += 1
        return {
            "draft_id": "draft-tool",
            "plan": {
                "plan_id": "plan-tool",
                "status": "EXECUTING",
                "targets": [
                    {
                        "platform": targets[0]["platform"],
                        "account_display_name": "安全账号",
                        "mode": "DRAFT",
                        "status": "RUNNING",
                        "operation_id": "operation-tool",
                    }
                ],
            },
        }

    async def get_draft_delivery_plan(self, _plan_id):
        return {
            "plan_id": "plan-tool",
            "status": self.plan_status,
            "targets": [
                {
                    "platform": "weibo",
                    "account_display_name": "安全账号",
                    "mode": "DRAFT",
                    "status": "DRAFT_SAVED",
                    "operation_id": "operation-tool",
                    "draft_url": "https://example.invalid/draft/1",
                }
            ],
        }


def test_current_draft_tool_is_idempotent_persistent_and_pollable(
    tmp_path,
    monkeypatch,
):
    async def fake_download(_url, destination):
        destination.write_bytes(b"docx")

    monkeypatch.setattr(mcp_tools, "_download_docx", fake_download)
    monkeypatch.setenv("MCP_FILE_SERVICE_ALLOWED_HOSTS", "files.internal")
    server = MCPServer("content.article-publisher", version="1.1.0")
    client = _DraftToolClient()
    store = TaskStore(tmp_path / "tasks.db")
    handlers = register_tools(server, client, store)
    target = DraftDeliveryTarget(
        platform="weibo",
        account_id=ACCOUNT_ID,
        persist_login=True,
    )

    first = run(
        handlers["start_article_draft_delivery"](
            "https://files.internal/source.docx",
            [target],
            "cs-admin-request-0001",
        )
    )
    repeated = run(
        handlers["start_article_draft_delivery"](
            "https://files.internal/source.docx",
            [target],
            "cs-admin-request-0001",
        )
    )

    assert first["status"] == "running"
    assert repeated["task_id"] == first["task_id"]
    assert client.create_calls == 1
    completed = run(
        handlers["get_article_draft_delivery_result"](first["task_id"])
    )
    assert completed["status"] == "completed"
    assert completed["result"]["overall_status"] == "SUCCESS"
    assert completed["result"]["targets"][0]["status"] == "DRAFT_SAVED"

    tools = run(server.list_tools())
    by_name = {tool.name: tool for tool in tools}
    schema = by_name["start_article_draft_delivery"].input_schema
    assert schema["additionalProperties"] is False
    target_schema = next(
        value
        for value in schema.get("$defs", {}).values()
        if value.get("title") == "DraftDeliveryTarget"
    )
    assert target_schema["additionalProperties"] is False


def test_timeout_is_persisted_as_unknown_and_never_resubmitted(tmp_path, monkeypatch):
    class TimeoutClient(_DraftToolClient):
        async def create_draft_delivery(self, _file_path, _targets):
            self.create_calls += 1
            raise FlaskClientError("TIMEOUT", "upstream timeout")

    async def fake_download(_url, destination):
        destination.write_bytes(b"docx")

    monkeypatch.setattr(mcp_tools, "_download_docx", fake_download)
    monkeypatch.setenv("MCP_FILE_SERVICE_ALLOWED_HOSTS", "files.internal")
    server = MCPServer("content.article-publisher", version="1.1.0")
    client = TimeoutClient()
    handlers = register_tools(server, client, TaskStore(tmp_path / "tasks.db"))
    target = DraftDeliveryTarget(platform="weibo", account_id=ACCOUNT_ID)

    first = run(
        handlers["start_article_draft_delivery"](
            "https://files.internal/source.docx",
            [target],
            "cs-admin-request-timeout",
        )
    )
    repeated = run(
        handlers["start_article_draft_delivery"](
            "https://files.internal/source.docx",
            [target],
            "cs-admin-request-timeout",
        )
    )

    assert first["error"]["code"] == "SUBMISSION_RESULT_UNKNOWN"
    assert repeated["status"] == "failed"
    assert repeated["result"]["error"]["code"] == "SUBMISSION_RESULT_UNKNOWN"
    assert client.create_calls == 1
