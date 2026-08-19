from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import mcp_server.tools as tools_module
from mcp_server.server import create_server, load_settings
from mcp_server.task_store import TaskStore
from mcp_server.tools import LEGACY_MCP_MUTATIONS_DISABLED, register_tools


def run(awaitable):
    return asyncio.run(awaitable)


class _MutationClient:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    async def clear_cookies(self, platform):
        self.calls.append(("clear_cookies", platform))
        return {"status": "ok"}

    async def start_login(self, platform):
        self.calls.append(("start_login", platform))
        return {"status": "login_started"}

    async def upload_docx(self, file_path, platforms):
        self.calls.append(("upload_docx", (file_path, platforms)))
        return {
            "results": [
                {"id": 1, "status": "created", "tasks": [{"task_id": 99}]}
            ]
        }

    async def resume_task(self, task_id, payload):
        self.calls.append(("resume_task", (task_id, payload)))
        return {"status": "queued", "selection_status": "manual"}

    async def logout(self, platform):
        self.calls.append(("logout", platform))
        return {"status": "ok", "cookies_cleared": True}

    async def cleanup_locks(self):
        self.calls.append(("cleanup_locks", None))
        return {"status": "ok", "killed": 1}


class _NoWriteStore:
    async def create(self, *args, **kwargs):
        raise AssertionError("disabled legacy tool must not create an MCP task")


def _server_and_handlers(client, store, *, enabled=False):
    server = MCPServer("content.article-publisher", version="1.0.0")
    handlers = register_tools(
        server,
        client,
        store,
        legacy_mutations_enabled=enabled,
    )
    return server, handlers


def test_legacy_mutations_are_disabled_before_any_side_effect():
    client = _MutationClient()
    _server, handlers = _server_and_handlers(client, _NoWriteStore())

    async def exercise():
        return [
            await handlers["start_login"]("zol", True),
            await handlers["publish_article"]("https://dev.sccsai.com/a.docx", ["zol"]),
            await handlers["resume_task"](0, "community", "topic"),
            await handlers["logout_account"]("unknown-platform"),
            await handlers["cleanup_locks"](),
        ]

    results = run(exercise())
    assert [result["error"]["code"] for result in results] == [
        LEGACY_MCP_MUTATIONS_DISABLED
    ] * 5
    assert client.calls == []


def test_explicit_legacy_switch_preserves_old_mutation_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    client = _MutationClient()
    store = TaskStore(tmp_path / "tasks.db")

    async def fake_download(_url, destination):
        destination.write_bytes(b"docx")

    monkeypatch.setattr(tools_module, "_download_docx", fake_download)
    _server, handlers = _server_and_handlers(client, store, enabled=True)

    started = run(handlers["start_login"]("zol", True))
    published = run(
        handlers["publish_article"](
            "https://dev.sccsai.com/a.docx",
            ["zol"],
        )
    )
    resumed = run(handlers["resume_task"](99, "社区", "话题"))
    logged_out = run(handlers["logout_account"]("zol"))
    cleaned = run(handlers["cleanup_locks"]())

    assert started["status"] == "awaiting_user_action"
    assert published["status"] == "pending"
    assert resumed["status"] == "pending"
    assert logged_out["status"] == "ok"
    assert cleaned["status"] == "ok"
    assert [name for name, _value in client.calls] == [
        "clear_cookies",
        "start_login",
        "upload_docx",
        "resume_task",
        "logout",
        "cleanup_locks",
    ]


@pytest.mark.parametrize("raw", ["", "false", "0", "no", "off", "random"])
def test_legacy_switch_is_false_unless_explicit_true(monkeypatch, raw):
    monkeypatch.setenv("MCP_LEGACY_MUTATIONS_ENABLED", raw)
    assert load_settings().legacy_mutations_enabled is False


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on", " TRUE "])
def test_legacy_switch_accepts_only_documented_true_values(monkeypatch, raw):
    monkeypatch.setenv("MCP_LEGACY_MUTATIONS_ENABLED", raw)
    assert load_settings().legacy_mutations_enabled is True


def test_settings_and_health_never_expose_internal_access_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    token = "phase2-test-internal-token-0123456789"
    allowlist = "account-scope-alpha,account-scope-beta"
    monkeypatch.setenv("ARTICLEOPS_MCP_INTERNAL_TOKEN", token)
    monkeypatch.setenv("ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS", allowlist)
    monkeypatch.setenv("MCP_TASK_DB", str(tmp_path / "health.db"))

    settings = load_settings()
    assert token not in repr(settings)
    assert allowlist not in repr(settings)

    server = create_server(settings)
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host=settings.bind_host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins),
        ),
    )

    async def request_health():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
        ) as http_client:
            return await http_client.get("/healthz", headers={"Host": "localhost"})

    response = run(request_health())
    body = json.dumps(response.json(), ensure_ascii=False)
    assert response.status_code == 200
    assert token not in body
    assert "account-scope-alpha" not in body
    assert "account-scope-beta" not in body
