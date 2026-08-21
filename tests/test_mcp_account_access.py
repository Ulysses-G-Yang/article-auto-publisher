from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from flask import Flask
from mcp.server import MCPServer

from account_sessions.database import AccountDatabase, sqlite_url
from account_sessions.mcp_access import (
    MCPAccessDeniedError,
    MCPAccessNotConfiguredError,
    MCPInternalAccessResolver,
    MCPInternalAccessSettings,
)
from account_sessions.models import AccountActivity, PlatformAccount
from account_sessions.web import create_account_session_blueprint
from mcp_server.flask_client import FlaskClient, FlaskClientError, safe_error_message
from mcp_server.server import load_settings
from mcp_server.task_store import TaskStore
from mcp_server.tools import register_tools

TOKEN = "mcp-internal-token-0123456789-abcdef"


def run(awaitable):
    return asyncio.run(awaitable)


def headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_resolver_is_independent_from_local_web_and_fail_closed(monkeypatch):
    resolver = MCPInternalAccessResolver(
        environ={
            "ARTICLEOPS_MCP_INTERNAL_TOKEN": TOKEN,
            "ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS": "allowed-a, allowed-b",
        }
    )
    context = resolver.resolve(headers())
    assert context.actor_id == "articleops-mcp"
    assert context.source == "MCP"
    assert context.capabilities == frozenset({"session.read", "logs.read"})
    assert context.allowed_account_ids == frozenset({"allowed-a", "allowed-b"})

    with pytest.raises(MCPAccessDeniedError):
        resolver.resolve(headers("wrong-token"))
    with pytest.raises(MCPAccessDeniedError):
        resolver.resolve({})

    missing = MCPInternalAccessResolver(environ={})
    with pytest.raises(MCPAccessNotConfiguredError):
        missing.resolve(headers())

    monkeypatch.setenv("ARTICLEOPS_MCP_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS", "\t\n")
    with pytest.raises(MCPAccessNotConfiguredError):
        MCPInternalAccessResolver().resolve(headers())


def test_resolver_grants_only_draft_capability_when_explicitly_enabled():
    resolver = MCPInternalAccessResolver(
        environ={
            "ARTICLEOPS_MCP_INTERNAL_TOKEN": TOKEN,
            "ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS": "allowed-a",
            "ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED": "true",
        }
    )

    context = resolver.resolve(headers())

    assert context.capabilities == frozenset(
        {"session.read", "logs.read", "draft.create"}
    )
    assert "publish.request" not in context.capabilities
    assert "publish.execute" not in context.capabilities


def test_resolver_rejects_unbounded_or_control_character_allowlist():
    too_long = "a" * 129
    for value in (too_long, "allowed\x00account", "allowed account"):
        resolver = MCPInternalAccessResolver(
            environ={
                "ARTICLEOPS_MCP_INTERNAL_TOKEN": TOKEN,
                "ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS": value,
            }
        )
        with pytest.raises(MCPAccessNotConfiguredError):
            resolver.resolve(headers())


def test_token_never_appears_in_settings_repr_or_auth_errors():
    settings = MCPInternalAccessSettings(TOKEN, frozenset({"account-a"}))
    assert TOKEN not in repr(settings)
    denied = MCPAccessDeniedError()
    assert TOKEN not in str(denied)
    assert TOKEN not in json.dumps({"error": denied.error_code}, ensure_ascii=False)


def test_mcp_server_reads_only_token_and_hides_it_in_settings_repr(monkeypatch):
    monkeypatch.setenv("ARTICLEOPS_MCP_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS", "account-a")
    settings = load_settings()
    assert settings.mcp_internal_token == TOKEN
    assert "account-a" not in repr(settings)
    assert TOKEN not in repr(settings)


def test_mcp_error_scrubber_handles_paths_and_multi_key_secrets():
    cases = (
        (r"D:\Secret Folder\private image.png", "Secret Folder"),
        (r'"D:\Secret Folder\private image.png"', "Secret Folder"),
        (r"\\server\share\private image.png", "server"),
        ("/var/private/image.png", "/var/private"),
        ("Authorization: Bearer short-secret", "short-secret"),
        ("Cookie: first=one; token=second-secret", "second-secret"),
    )
    for message, forbidden in cases:
        safe = safe_error_message(message)
        assert forbidden not in safe
        assert "[redacted" in safe


def test_flask_client_sends_auth_only_to_internal_endpoints():
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"accounts": [], "activities": []})

    async def exercise():
        client = FlaskClient("http://flask.test", internal_token=TOKEN)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://flask.test",
        )
        await client.get_accounts()
        await client.get_platform_accounts("zol", usable=True)
        await client.get_account_activity("account-a", limit=2)
        await client.aclose()

    run(exercise())
    assert seen == [
        ("/api/accounts", None),
        (
            "/api/internal/mcp/platforms/zol/accounts",
            f"Bearer {TOKEN}",
        ),
        (
            "/api/internal/mcp/account-sessions/account-a/activity",
            f"Bearer {TOKEN}",
        ),
    ]


@pytest.mark.parametrize("status_code,expected", [(401, "MCP_ACCESS_DENIED"), (403, "MCP_ACCESS_DENIED"), (503, "MCP_ACCESS_NOT_CONFIGURED")])
def test_flask_client_maps_internal_auth_failures_without_secret(status_code, expected):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": expected, "message": TOKEN})

    async def exercise():
        client = FlaskClient("http://flask.test", internal_token=TOKEN)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://flask.test",
        )
        with pytest.raises(FlaskClientError) as raised:
            await client.get_account_activity("account-a")
        await client.aclose()
        return raised.value

    error = run(exercise())
    assert error.code == expected
    assert TOKEN not in str(error)


async def _seed_accounts(database_url: str, first: str, second: str) -> None:
    database = AccountDatabase(database_url)
    await database.initialize()
    async with database.session() as session:
        session.add_all(
            [
                PlatformAccount(
                    account_id=first,
                    platform="zol",
                    display_name="允许账号",
                    profile_path=f"profile-{first}",
                    session_status="VALID",
                ),
                PlatformAccount(
                    account_id=second,
                    platform="zol",
                    display_name="越权账号",
                    profile_path=f"profile-{second}",
                    session_status="VALID",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                AccountActivity(
                    account_id=first,
                    platform="zol",
                    display_name_snapshot="允许账号",
                    actor_id="local-web-user",
                    source="WEB",
                    action="SESSION_POLICY_UPDATED",
                    message="安全活动",
                ),
                AccountActivity(
                    account_id=second,
                    platform="zol",
                    display_name_snapshot="越权账号",
                    actor_id="local-web-user",
                    source="WEB",
                    action="SECRET_ACTION",
                    message="不应返回",
                ),
            ]
        )
    await database.dispose()


def test_internal_routes_filter_accounts_and_activity_by_allowlist(tmp_path: Path, monkeypatch):
    allowed = "allowed-account"
    denied = "denied-account"
    database_url = sqlite_url(tmp_path / "accounts.db")
    run(_seed_accounts(database_url, allowed, denied))
    monkeypatch.setenv("ARTICLEOPS_MCP_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS", allowed)

    app = Flask("mcp-account-access")
    app.register_blueprint(
        create_account_session_blueprint(
            database_url=database_url,
            seed_legacy_profiles=False,
            auto_execute=False,
            public_publish_enabled=False,
        )
    )
    client = app.test_client()

    response = client.get(
        "/api/internal/mcp/platforms/zol/accounts?usable=true",
        headers=headers(),
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert [item["account_id"] for item in payload["accounts"]] == [allowed]
    assert "profile_path" not in payload["accounts"][0]
    assert "platform_user_id" not in payload["accounts"][0]

    activity = client.get(
        f"/api/internal/mcp/account-sessions/{allowed}/activity?limit=1",
        headers=headers(),
    )
    assert activity.status_code == 200
    assert len(activity.get_json()["activities"]) == 1

    forbidden = client.get(
        f"/api/internal/mcp/account-sessions/{denied}/activity",
        headers=headers(),
    )
    assert forbidden.status_code == 403
    assert forbidden.get_json()["error"] == "ACCOUNT_PERMISSION_DENIED"

    invalid_limit = client.get(
        f"/api/internal/mcp/account-sessions/{allowed}/activity?limit=201",
        headers=headers(),
    )
    assert invalid_limit.status_code == 400
    assert invalid_limit.get_json()["error"] == "MCP_INVALID_ARGUMENT"

    wrong = client.get(
        "/api/internal/mcp/platforms/zol/accounts",
        headers=headers("wrong-token"),
    )
    assert wrong.status_code == 401
    assert wrong.get_json()["error"] == "MCP_ACCESS_DENIED"

    monkeypatch.delenv("ARTICLEOPS_MCP_INTERNAL_TOKEN")
    not_configured = client.get(
        "/api/internal/mcp/platforms/zol/accounts",
        headers=headers(),
    )
    assert not_configured.status_code == 503
    assert not_configured.get_json()["error"] == "MCP_ACCESS_NOT_CONFIGURED"
    app.extensions["account_sessions"].close()


class _ToolClient:
    async def get_platform_accounts(self, platform: str, *, usable: bool = False):
        return {
            "platform": platform,
            "accounts": [
                {
                    "account_id": "account-a",
                    "display_name": "安全账号",
                    "masked_platform_user_id": "****1234",
                    "status": "ACTIVE",
                    "session_status": "VALID",
                    "profile_path": "D:\\secret\\profile",
                    "platform_user_id": "raw-user-id",
                    "cookie": TOKEN,
                }
            ],
        }

    async def get_account_activity(self, account_id: str, *, limit: int = 100):
        return {
            "account_id": account_id,
            "activities": [
                {
                    "id": 1,
                    "platform": "zol",
                    "display_name": "安全账号",
                    "source": "MCP",
                    "action": "READ",
                    "level": "INFO",
                    "message": f"D:\\secret\\profile cookie={TOKEN}",
                    "profile_path": "D:\\secret\\profile",
                }
            ][:limit],
        }


def test_new_mcp_tools_are_closed_read_only_and_reproject_output(tmp_path: Path):
    server = MCPServer("content.article-publisher", version="1.0.0")
    handlers = register_tools(
        server,
        _ToolClient(),
        TaskStore(tmp_path / "tasks.db"),
    )
    tools = run(server.list_tools())
    assert len(tools) == 16
    assert all(tool.input_schema.get("additionalProperties") is False for tool in tools)
    by_name = {tool.name: tool for tool in tools}
    assert "MCP 白名单" in by_name["list_platform_accounts"].description
    assert "LEGACY" in by_name["list_accounts"].description
    assert "LEGACY" in by_name["start_login"].description

    accounts = run(handlers["list_platform_accounts"]("zol", True))
    activities = run(handlers["get_account_activity"]("account-a", 1))
    serialized = json.dumps({"accounts": accounts, "activities": activities}, ensure_ascii=False)
    assert "profile_path" not in serialized
    assert '"platform_user_id"' not in serialized
    assert "cookie" not in serialized.lower()
    assert TOKEN not in serialized
    assert activities["activities"][0]["message"] != f"D:\\secret\\profile cookie={TOKEN}"
