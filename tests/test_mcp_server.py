import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from mcp_server.server import MCPSettings, create_server
from mcp_server.task_store import TaskStore
from mcp_server.tools import register_tools


class FakeFlaskClient:
    def __init__(self):
        self.login_statuses = ["logging_in", "logged_in"]
        self.tasks = []

    async def get_accounts(self):
        status = self.login_statuses[0]
        return [{
            "platform": "zol",
            "status": status,
            "last_login_time": None,
            "cookie_file": "D:\\secret\\Cookies",
        }]

    async def get_articles(self):
        return [{
            "id": 1,
            "filename": "C:\\private\\article.docx",
            "original_path": "D:\\private\\article.docx",
            "title": "测试文章",
            "keywords": "显示器,测试",
            "image_count": 1,
            "char_count": 20,
            "tasks": [],
        }]

    async def get_tasks(self):
        return self.tasks

    async def get_task_logs(self, _task_id):
        return [{"level": "INFO", "message": "测试日志", "created_at": "2026-08-07"}]

    async def get_queue_status(self):
        return {"queue_size": 1, "running": True, "pending": 2}

    async def start_login(self, _platform):
        return {"status": "login_started"}

    async def clear_cookies(self, _platform):
        return {"status": "ok", "cookies_cleared": True}

    async def logout(self, _platform):
        return {"status": "ok", "cookies_cleared": True}

    async def cleanup_locks(self):
        return {"status": "ok", "killed": 2}

    async def resume_task(self, _task_id, _payload):
        return {"status": "queued", "selection_status": "manual"}


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp_dir.name) / "mcp_tasks.db")
        self.client = FakeFlaskClient()
        self.server = MCPServer("content.article-publisher", version="1.0.0")
        self.handlers = register_tools(self.server, self.client, self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_async(self, awaitable):
        return asyncio.run(awaitable)

    def test_all_input_schemas_are_closed(self):
        tools = self.run_async(self.server.list_tools())
        self.assertEqual(len(tools), 12)
        self.assertTrue(all(tool.input_schema.get("additionalProperties") is False for tool in tools))

    def test_list_accounts_does_not_return_cookie_file(self):
        result = self.run_async(self.handlers["list_accounts"]())
        self.assertEqual(result["accounts"][0]["platform"], "zol")
        self.assertNotIn("cookie_file", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("D:\\secret", json.dumps(result, ensure_ascii=False))

    def test_start_login_and_poll_are_persistent(self):
        started = self.run_async(self.handlers["start_login"]("zol", False))
        self.assertEqual(started["status"], "awaiting_user_action")
        task_id = started["task_id"]
        stored = self.run_async(self.store.get(task_id))
        self.assertEqual(stored["kind"], "login")
        self.assertEqual(stored["status"], "awaiting_user_action")

        self.client.login_statuses = ["logged_in"]
        completed = self.run_async(self.handlers["get_login_result"](task_id))
        self.assertEqual(completed["status"], "completed")
        stored = self.run_async(self.store.get(task_id))
        self.assertEqual(stored["status"], "completed")

    def test_publish_poll_maps_needs_selection(self):
        task_id = "publish-test-1"
        self.run_async(self.store.create(
            task_id,
            "publish",
            "pending",
            internal_task_ids=[11, 12],
        ))
        self.client.tasks = [
            {"id": 11, "platform": "zol", "status": "completed", "article_title": "测试"},
            {"id": 12, "platform": "xiaoheihe", "status": "needs_selection", "article_title": "测试"},
        ]
        result = self.run_async(self.handlers["get_publish_result"](task_id))
        self.assertEqual(result["status"], "awaiting_user_action")
        self.assertEqual(result["result"]["needs"], ["community", "topic"])

    def test_healthz_is_available_without_mcp_session(self):
        settings = MCPSettings(
            "http://localhost:5000",
            "127.0.0.1",
            8788,
            ("localhost", "localhost:*"),
            ("http://localhost:*",),
            str(Path(self.temp_dir.name) / "health.db"),
        )
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

        async def request_health(host):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
            ) as http_client:
                return await http_client.get("/healthz", headers={"Host": host})

        response = self.run_async(request_health("localhost"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["server_id"], "content.article-publisher")
        rejected = self.run_async(request_health("evil.example"))
        self.assertEqual(rejected.status_code, 421)


if __name__ == "__main__":
    unittest.main()
