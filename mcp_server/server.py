"""Streamable HTTP entry point for the CS_Admin article-publisher adapter."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import SERVER_ID, SERVER_VERSION
from .flask_client import FlaskClient
from .task_store import TaskStore
from .tools import register_tools

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class MCPSettings:
    flask_base_url: str
    bind_host: str
    port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    task_db_path: str
    trusted_proxy_ips: tuple[str, ...] = ()


def _normalise_hosts(raw: str) -> tuple[str, ...]:
    values = []
    for item in raw.split(","):
        host = item.strip().rstrip("/")
        if not host:
            continue
        if "//" in host or host == "*":
            raise ValueError("MCP_ALLOWED_HOSTS 只能填写具体 Host，不能使用 URL 或通配符 *")
        values.append(host)
        # Host headers normally include the listening port.  Bare values in
        # the documented env var therefore also allow their configured port.
        if not host.endswith(":*") and not _has_explicit_port(host):
            values.append(f"{host}:*")
    if not values:
        raise ValueError("MCP_ALLOWED_HOSTS 不能为空")
    return tuple(dict.fromkeys(values))


def _has_explicit_port(host: str) -> bool:
    if host.startswith("["):
        return "]" in host and host.rsplit("]", 1)[-1].startswith(":")
    return host.count(":") == 1 and host.rsplit(":", 1)[-1].isdigit()


def _normalise_origins(hosts: tuple[str, ...]) -> tuple[str, ...]:
    origins = []
    for host in hosts:
        if host.endswith(":*"):
            base = host[:-2]
            origins.extend([f"http://{base}:*", f"https://{base}:*"])
        else:
            origins.extend([f"http://{host}", f"https://{host}"])
    return tuple(dict.fromkeys(origins))


def _host_is_allowed(host: str | None, allowed_hosts: tuple[str, ...]) -> bool:
    if not host:
        return False
    if host in allowed_hosts:
        return True
    return any(
        allowed.endswith(":*") and host.startswith(allowed[:-2] + ":")
        for allowed in allowed_hosts
    )


def _trusted_forwarded_host(request: Request, settings: MCPSettings) -> str | None:
    """Use X-Forwarded-Host only when the direct peer is trusted."""

    direct_host = request.headers.get("host")
    peer_host = request.client.host if request.client else None
    if peer_host in settings.trusted_proxy_ips:
        forwarded_host = request.headers.get("x-forwarded-host")
        if forwarded_host:
            # Proxies may append values; the first value is the original host.
            return forwarded_host.split(",", 1)[0].strip()
    return direct_host


def load_settings() -> MCPSettings:
    flask_base_url = os.getenv("FLASK_BASE_URL", "http://localhost:5000").strip().rstrip("/")
    parsed = urlsplit(flask_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("FLASK_BASE_URL 必须是 HTTP(S) 地址")

    # 8765 is the LAN-facing MCP port used in the CS_Admin integration
    # convention.  The Flask application remains an internal dependency on
    # port 5000 and is never the MCP URL.
    port_raw = os.getenv("MCP_PORT", "8765").strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError("MCP_PORT 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("MCP_PORT 必须在 1 到 65535 之间")

    allowed_hosts = _normalise_hosts(os.getenv("MCP_ALLOWED_HOSTS", "localhost,127.0.0.1"))
    allowed_origins = _normalise_origins(allowed_hosts)
    task_db_path = os.getenv(
        "MCP_TASK_DB",
        str(PROJECT_ROOT / "data" / "mcp_tasks.db"),
    )
    trusted_proxy_ips = tuple(
        item.strip()
        for item in os.getenv("MCP_TRUSTED_PROXY_IPS", "").split(",")
        if item.strip()
    )
    return MCPSettings(
        flask_base_url=flask_base_url,
        # Keep local development safe by default.  LAN deployment can set
        # MCP_BIND_HOST=0.0.0.0 and add the assigned Host to the allowlist.
        bind_host=os.getenv("MCP_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=port,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        task_db_path=task_db_path,
        trusted_proxy_ips=trusted_proxy_ips,
    )


def create_server(settings: MCPSettings | None = None) -> MCPServer:
    config = settings or load_settings()
    client = FlaskClient(config.flask_base_url)
    store = TaskStore(config.task_db_path)

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        try:
            yield {}
        finally:
            await client.aclose()

    server = MCPServer(
        name=SERVER_ID,
        version=SERVER_VERSION,
        description="自动化文章发布工具的 CS_Admin MCP Adapter",
        instructions=(
            "本服务只操作现有 Flask 服务，支持 ZOL 和小黑盒。登录和发布是异步任务，"
            "必须按返回的 async_task.poll_tool 轮询；本服务不返回 Cookie、密钥或本地绝对路径。"
        ),
        lifespan=lifespan,
    )
    register_tools(server, client, store)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        if not _host_is_allowed(_trusted_forwarded_host(request, config), config.allowed_hosts):
            return JSONResponse({"error": "Invalid Host header"}, status_code=421)
        return JSONResponse({
            "status": "ok",
            "server_id": SERVER_ID,
            "version": SERVER_VERSION,
        })

    # Keep the objects available for in-process tests without exposing them as
    # MCP tools or returning their paths to callers.
    server._article_publisher_settings = config  # type: ignore[attr-defined]
    server._article_publisher_client = client  # type: ignore[attr-defined]
    server._article_publisher_store = store  # type: ignore[attr-defined]
    return server


def main() -> None:
    settings = load_settings()
    server = create_server(settings)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )
    server.run(
        transport="streamable-http",
        host=settings.bind_host,
        port=settings.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=transport_security,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
