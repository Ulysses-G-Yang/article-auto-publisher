"""检查开发/测试/生产启动前的本机环境，不输出 Cookie、密钥或绝对敏感文件列表。"""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_MODULES = (
    "flask",
    "playwright",
    "docx",
    "jieba",
    "sklearn",
    "PIL",
    "cryptography",
    "yaml",
    "aiofiles",
    "loguru",
    "mcp",
    "httpx",
    "pytest",
    "waitress",
)


def _check_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _port_available(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        # SO_REUSEADDR can make bind() succeed even when another process is
        # already listening. Probe the actual listener first.
        if probe.connect_ex((host, port)) == 0:
            return False
    finally:
        probe.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _production_checks() -> list[str]:
    problems: list[str] = []
    if os.getenv("APP_ENV", "development").strip().lower() != "production":
        return problems
    secret = os.getenv("APP_SECRET_KEY", "")
    if len(secret) < 32:
        problems.append("APP_SECRET_KEY 未设置或长度小于 32")
    if os.getenv("APP_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}:
        problems.append("生产环境禁止 APP_DEBUG=true")
    if os.getenv("PUBLISH_AFTER_DRAFT", "false").strip().lower() in {"1", "true", "yes", "on"}:
        problems.append("生产第一阶段必须保持 PUBLISH_AFTER_DRAFT=false")
    if not os.getenv("MCP_ALLOWED_HOSTS", "").strip():
        problems.append("生产环境必须设置 MCP_ALLOWED_HOSTS")
    if not os.getenv("MCP_FILE_SERVICE_ALLOWED_HOSTS", "").strip():
        problems.append("生产环境必须设置 MCP_FILE_SERVICE_ALLOWED_HOSTS")
    draft_delivery_enabled = os.getenv(
        "ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if draft_delivery_enabled:
        if len(os.getenv("ARTICLEOPS_MCP_INTERNAL_TOKEN", "")) < 32:
            problems.append("启用 MCP 草稿投递时必须配置独立的 32 位以上内部令牌")
        if not os.getenv("ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS", "").strip():
            problems.append("启用 MCP 草稿投递时必须配置明确的账号 ID 白名单")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flask-port", type=int, default=int(os.getenv("FLASK_PORT", "5000")))
    parser.add_argument("--mcp-port", type=int, default=int(os.getenv("MCP_PORT", "8765")))
    args = parser.parse_args()

    failures = 0
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 12):
        print("FAIL Python 需要 3.12 或更高版本")
        failures += 1
    else:
        print("PASS Python 版本")

    for module in REQUIRED_MODULES:
        if _check_module(module):
            print(f"PASS module {module}")
        else:
            print(f"FAIL module {module}")
            failures += 1

    for relative in ("data", "data/logs", "data/chrome_profiles", "uploads", "images"):
        path = ROOT / relative
        if path.exists():
            print(f"PASS directory {relative}")
        else:
            print(f"WARN directory {relative} missing; startup will create it")

    for host, port, label in (
        ("127.0.0.1", args.flask_port, "Flask"),
        (os.getenv("MCP_BIND_HOST", "127.0.0.1"), args.mcp_port, "MCP"),
    ):
        if _port_available(host, port):
            print(f"PASS {label} port {host}:{port} available")
        else:
            print(f"WARN {label} port {host}:{port} already in use; identify the owner before startup")

    for problem in _production_checks():
        print(f"FAIL production config: {problem}")
        failures += 1

    if failures:
        print(f"Environment check failed: {failures} issue(s)")
        return 1
    print("Environment check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
