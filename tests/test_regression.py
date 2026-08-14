import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from account_sessions.identity import extract_identity
from config import get_config
from core.queue_manager import classify_task_error, normalize_selection_query
from models.database import Database
from platforms.baijiahao import (
    BaijiahaoPlatform,
)
from platforms.baijiahao import (
    PlatformNotImplementedError as BaijiahaoNotImplementedError,
)
from platforms.base import BasePlatform, BrowserLifecycleError, PlatformAccessError
from platforms.content_validation import ContentValidationError
from platforms.douyin import DouyinPlatform, PlatformNotImplementedError
from platforms.smzdm import PlatformNotImplementedError as SmzdmNotImplementedError
from platforms.smzdm import SmzdmPlatform
from platforms.weibo import PlatformNotImplementedError as WeiboNotImplementedError
from platforms.weibo import WeiboPlatform
from platforms.xiaoheihe import XiaoheihePlatform
from platforms.xiaohongshu import (
    PlatformNotImplementedError as XhsNotImplementedError,
)
from platforms.xiaohongshu import XiaohongshuPlatform
from platforms.zol import ZOLPlatform


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        if self.page.active is None:
            return
        if key in ("Control+A", "Meta+A"):
            self.page.active.selected_all = True
        elif key == "Backspace" and getattr(self.page.active, "selected_all", False):
            self.page.active.value = ""
            self.page.active.text = ""
            self.page.active.selected_all = False
        elif key == "Enter":
            self.page.active.value += "\n"
            self.page.active.text += "\n"

    async def insert_text(self, value):
        if self.page.active is None:
            return
        if getattr(self.page.active, "selected_all", False):
            self.page.active.value = ""
            self.page.active.text = ""
            self.page.active.selected_all = False
        self.page.active.value += value
        self.page.active.text += value

    async def type(self, value, delay=0):
        await self.insert_text(value)


class FakeLocator:
    def __init__(self, page=None, tag="div", value="", text="", count=1,
                 visible=True, handle=None):
        self.page = page
        self.tag = tag
        self.value = value
        self.text = text
        self._count = count
        self.visible = visible
        self.handle = handle
        self.selected_all = False
        self.clicked = False

    @property
    def first(self):
        return self

    def nth(self, _index):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self.visible

    async def evaluate(self, script, *_args):
        if "tagName" in script:
            return self.tag
        return None

    async def fill(self, value):
        self.value = value
        self.text = value
        if self.page:
            self.page.active = self

    async def input_value(self):
        return self.value

    async def inner_text(self):
        return self.text or self.value

    async def text_content(self):
        return self.text or self.value

    async def click(self, **_kwargs):
        self.clicked = True
        if self.page:
            self.page.active = self

    async def element_handle(self):
        return self.handle or self

    async def content_frame(self):
        return None

    async def select_option(self, **_kwargs):
        self.value = "选择分类"
        self.text = "选择分类"

    def locator(self, _selector):
        return FakeLocator(self.page, tag="option", text=self.text, value=self.value)


class FakeFrame:
    def __init__(self, page, body):
        self.page = page
        self.body = body

    def locator(self, _selector):
        return self.body


class FakePage:
    def __init__(self, kind="textarea"):
        self.kind = kind
        self.url = "https://blog.zol.com.cn/post.php?act=add"
        self.active = None
        self.keyboard = FakeKeyboard(self)
        self.target = FakeLocator(
            page=self,
            tag={"textarea": "textarea", "contenteditable": "div"}.get(kind, "textarea"),
            text="",
            value="",
        )
        self.frame_body = FakeLocator(page=self, tag="body", text="", value="")
        self.frame = FakeFrame(self, self.frame_body)
        self.iframe_handle = FakeLocator(page=self, tag="iframe")
        self.iframe_handle.content_frame = AsyncMock(return_value=self.frame)

    async def goto(self, *_args, **_kwargs):
        return None

    def locator(self, selector):
        if selector.startswith("iframe") or "iframe" in selector:
            return self.iframe_handle if self.kind == "iframe" else FakeLocator(count=0)
        if self.kind == "textarea" and "textarea" in selector:
            return self.target
        if self.kind == "contenteditable" and "contenteditable" in selector:
            return self.target
        if selector in ("#title", "input[name='title']"):
            return FakeLocator(count=0)
        return FakeLocator(count=0)

    async def wait_for_selector(self, *_args, **_kwargs):
        return self.target

    async def query_selector(self, *_args, **_kwargs):
        return self.target

    async def evaluate(self, *_args, **_kwargs):
        return False


class FakeXiaoPage:
    def __init__(self):
        self.active = None
        self.keyboard = FakeKeyboard(self)
        self.title = FakeLocator(page=self, tag="div")
        self.body = FakeLocator(page=self, tag="div")

    def locator(self, selector):
        if ".editor-title__container" in selector:
            return self.title
        if ".article__edit-content--inner" in selector:
            return self.body
        return FakeLocator(count=0)


class ExistingSessionPlatform:
    login_calls = 0

    def __init__(self):
        self.page = type("Page", (), {"url": "https://my.zol.com.cn/"})()

    async def initialize(self):
        return None

    async def check_login(self):
        return True

    async def login(self):
        type(self).login_calls += 1

    async def cleanup(self):
        return None


class SelectionStub(BasePlatform):
    platform_name = "xiaoheihe"

    async def check_login(self):
        return True

    async def login(self):
        return None

    async def navigate_to_editor(self):
        return None

    async def fill_title(self, _title):
        return None

    async def fill_content(self, _blocks, _images):
        return None

    async def select_topic(self, **_kwargs):
        return {
            "success": False,
            "needs_selection": True,
            "error": "没有候选",
        }

    async def save_draft(self, _title=""):
        return "https://example.test/draft"


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cfg = get_config()
        self.old_db_path = self.cfg["paths"]["database"]
        self.old_logs_path = self.cfg["paths"]["logs"]
        self.cfg["paths"]["database"] = f"{self.temp_dir.name}\\test.db"
        self.cfg["paths"]["logs"] = self.temp_dir.name
        self.old_instance = Database._instance
        Database._instance = None
        self.db = Database.get_instance()

    def tearDown(self):
        from loguru import logger
        logger.remove()
        Database._instance = self.old_instance
        self.cfg["paths"]["database"] = self.old_db_path
        self.cfg["paths"]["logs"] = self.old_logs_path
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            # Windows 上 SQLite WAL 或 Loguru 异步 sink 可能稍晚释放；测试结果不应
            # 被临时目录清理的文件锁掩盖。
            shutil.rmtree(self.temp_dir.name, ignore_errors=True)

    def create_article(self):
        return self.db.insert_article(
            filename="test.docx",
            original_path="test.docx",
            content_text="测试正文",
            content_json='{"blocks": [{"type": "text", "text": "测试正文"}]}',
            keywords="测试,正文",
            image_count=0,
            char_count=4,
        )


class RegressionTests(DatabaseTestCase):
    def test_data_center_is_mounted_on_current_flask_service(self):
        from app import create_app

        article_id = self.create_article()
        task_id = self.db.create_task(article_id, "xiaoheihe")
        self.db.update_task(task_id, status="completed", title_used="已保存草稿")
        mvp_database = Path(self.temp_dir.name) / "article_mvp.db"
        mvp_url = f"sqlite+aiosqlite:///{mvp_database.as_posix()}"
        with patch.dict("os.environ", {"ARTICLE_MVP_DATABASE_URL": mvp_url}):
            app = create_app()
            try:
                client = app.test_client()
                page_response = client.get("/data-center/")
                self.assertEqual(page_response.status_code, 200)
                self.assertIn(
                    "发布与采集数据中心", page_response.get_data(as_text=True)
                )
                asset_response = client.get("/data-center/assets/dashboard.js")
                self.assertEqual(asset_response.status_code, 200)
                asset_response.close()
                coreui_response = client.get(
                    "/data-center/assets/vendor/coreui/coreui.min.css"
                )
                self.assertEqual(coreui_response.status_code, 200)
                coreui_response.close()
                gridstack_response = client.get(
                    "/data-center/assets/vendor/gridstack/gridstack-all.js"
                )
                self.assertEqual(gridstack_response.status_code, 200)
                gridstack_response.close()
                self.assertIn("/data-center/", client.get("/").get_data(as_text=True))

                dashboard_response = client.get("/data-center/api/dashboard")
                self.assertEqual(dashboard_response.status_code, 200)
                dashboard_payload = dashboard_response.get_json()
                self.assertEqual(dashboard_payload["summary"]["total_articles"], 0)
                self.assertNotIn("current_workflow", dashboard_payload)

                legacy_response = client.get("/api/legacy-summary")
                self.assertEqual(legacy_response.status_code, 200)
                legacy_payload = legacy_response.get_json()
                self.assertEqual(legacy_payload["summary"]["total_articles"], 1)
                self.assertEqual(legacy_payload["summary"]["total_tasks"], 1)
                self.assertEqual(legacy_payload["summary"]["xiaoheihe_tasks"], 1)
                self.assertEqual(legacy_payload["summary"]["saved_drafts"], 1)
                self.assertNotIn("测试正文", legacy_response.get_data(as_text=True))
            finally:
                app.extensions["article_mvp_dashboard"].close()

    def test_create_app_recovers_orphaned_login_state(self):
        from app import create_app

        self.db.upsert_account(
            "zol",
            status="logging_in",
            login_stage="awaiting_user_login",
            login_attempt_id="old-attempt",
        )
        response = create_app().test_client().get("/api/accounts")
        self.assertEqual(response.status_code, 200)
        account = next(item for item in response.get_json() if item["platform"] == "zol")
        self.assertEqual(account["status"], "logged_out")
        self.assertEqual(account["login_stage"], "recovered_after_restart")
        self.assertIn("服务启动", account["login_error"])

    def test_platform_login_lease_prevents_duplicate_claim(self):
        from web import routes

        first = routes._claim_login(self.db, "zol")
        second = routes._claim_login(self.db, "zol")
        try:
            self.assertTrue(first)
            self.assertIsNone(second)
            self.assertEqual(self.db.get_account("zol")["status"], "logging_in")
        finally:
            routes._release_login(
                self.db,
                "zol",
                first,
                status="logged_out",
                login_stage="test_finished",
                login_error=None,
            )

    def test_closed_page_gets_stable_browser_error(self):
        platform = ZOLPlatform()

        class ClosedPage:
            def is_closed(self):
                return True

        platform.page = ClosedPage()
        with self.assertRaises(BrowserLifecycleError) as ctx:
            asyncio.run(platform._safe_simulate_scroll(scroll_times=1))
        self.assertEqual(ctx.exception.error_code, "BROWSER_CONTEXT_CLOSED")
        self.assertEqual(classify_task_error(ctx.exception), "BROWSER_CONTEXT_CLOSED")

    def test_zol_forum_redirect_is_not_generic_retry_error(self):
        platform = ZOLPlatform()
        page = FakePage("textarea")

        async def goto(*_args, **_kwargs):
            page.url = "https://bbs.zol.com.cn/?act=add"

        page.goto = goto
        platform.page = page
        platform.simulator.random_delay = AsyncMock()
        with self.assertRaises(PlatformAccessError) as ctx:
            asyncio.run(platform.navigate_to_editor())
        self.assertIn("ZOL_BLOG_EDITOR_REDIRECT", str(ctx.exception))
        self.assertEqual(classify_task_error(ctx.exception), "ZOL_BLOG_EDITOR_REDIRECT")

    def test_queue_pauses_browser_closed_without_requeue(self):
        from core.queue_manager import QueueManager

        article_id = self.create_article()
        task_id = self.db.create_task(article_id, "zol")
        self.db.upsert_account("zol", status="logged_in")

        class ClosedPublishPlatform:
            async def publish(self, **_kwargs):
                return {
                    "success": False,
                    "error_code": "BROWSER_CONTEXT_CLOSED",
                    "error": "BROWSER_CONTEXT_CLOSED: page closed",
                }

            async def cleanup(self):
                return None

        async def run():
            manager = QueueManager()
            platform = ClosedPublishPlatform()
            manager._platforms["zol"] = platform
            manager._get_platform = AsyncMock(return_value=platform)
            await manager._process_task(task_id)
            return manager.queue.qsize()

        queue_size = asyncio.run(run())
        task = self.db.get_task(task_id)
        self.assertEqual(queue_size, 0)
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["error_code"], "BROWSER_CONTEXT_CLOSED")

    def test_queue_persists_draft_with_media_warning(self):
        from core.queue_manager import QueueManager

        article_id = self.create_article()
        task_id = self.db.create_task(article_id, "xiaoheihe")
        self.db.upsert_account("xiaoheihe", status="logged_in")

        class WarningPublishPlatform:
            async def publish(self, **_kwargs):
                return {
                    "success": True,
                    "draft_url": "https://example.test/draft",
                    "selection": {},
                    "selection_status": "needs_selection",
                    "selection_error": "没有候选社区",
                    "media_status": "failed",
                    "media_error": "2 张图片全部上传失败",
                    "expected_images": 2,
                    "uploaded_images": 0,
                    "failed_images": [{"filename": "a.png", "error": "遮罩"}],
                }

            async def cleanup(self):
                return None

        async def run():
            manager = QueueManager()
            platform = WarningPublishPlatform()
            manager._get_platform = AsyncMock(return_value=platform)
            await manager._process_task(task_id)

        asyncio.run(run())
        task = self.db.get_task(task_id)
        self.assertEqual(task["status"], "needs_selection")
        self.assertEqual(task["error_code"], "PARTIAL_METADATA")
        self.assertEqual(task["media_status"], "failed")
        self.assertEqual(task["expected_images"], 2)
        self.assertEqual(task["uploaded_images"], 0)

    def test_startup_pauses_unfinished_tasks(self):
        article_id = self.create_article()
        queued_id = self.db.create_task(article_id, "zol")
        retrying_id = self.db.create_task(article_id, "xiaoheihe")
        self.db.update_task(retrying_id, status="retrying")
        paused = self.db.pause_unfinished_tasks()
        self.assertEqual(paused, 2)
        self.assertEqual(self.db.get_task(queued_id)["status"], "paused")
        self.assertEqual(self.db.get_task(retrying_id)["status"], "paused")

    def test_update_article_topics_keeps_title_and_platform_topics_in_order(self):
        article_id = self.create_article()
        self.db.update_article_topics(article_id, "ZOL话题", "小黑盒话题", "测试标题")
        article = self.db.get_article(article_id)
        self.assertEqual(article["topic_zol"], "ZOL话题")
        self.assertEqual(article["topic_xiaoheihe"], "小黑盒话题")
        self.assertEqual(article["title"], "测试标题")

    def test_known_historical_errors_get_error_codes(self):
        article_id = self.create_article()
        task_id = self.db.create_task(article_id, "zol")
        self.db.update_task(
            task_id,
            status="failed",
            error_message="Mouse.wheel: Target page, context or browser has been closed",
            error_code=None,
        )
        # 模拟服务重启时执行的幂等数据库迁移。
        self.db._init_tables()
        self.assertEqual(self.db.get_task(task_id)["error_code"], "BROWSER_CONTEXT_CLOSED")

    def test_cleanup_never_removes_ambiguous_profile_lock_files(self):
        import app as app_module

        old_base_dir = app_module.BASE_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_base = Path(temp_dir) / "data" / "chrome_profiles"
            lock_paths = []
            for platform in ("zol", "xiaoheihe"):
                profile_dir = profile_base / platform
                profile_dir.mkdir(parents=True)
                for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                    lock_path = profile_dir / lock_name
                    lock_path.write_text("lock", encoding="utf-8")
                    lock_paths.append(lock_path)

            cookie_path = profile_base / "zol" / "Default" / "Network" / "Cookies"
            cookie_path.parent.mkdir(parents=True)
            cookie_path.write_bytes(b"cookie database")

            app_module.BASE_DIR = temp_dir
            try:
                with patch.object(app_module.os, "kill") as kill_mock:
                    cleaned = app_module.kill_zombie_chrome()
            finally:
                app_module.BASE_DIR = old_base_dir

            self.assertEqual(cleaned, 0)
            self.assertTrue(all(path.exists() for path in lock_paths))
            self.assertTrue(cookie_path.exists())
            kill_mock.assert_not_called()

    def test_clear_platform_cookies_only_clears_requested_profile(self):
        import app as app_module

        old_base_dir = app_module.BASE_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_base = Path(temp_dir) / "data" / "chrome_profiles"
            zol_default = profile_base / "zol" / "Default"
            zol_network = zol_default / "Network"
            zol_network.mkdir(parents=True)
            (zol_network / "Cookies").write_bytes(b"zol cookie database")
            (zol_network / "Cookies-wal").write_bytes(b"wal")
            (zol_default / "Local Storage").mkdir()
            (zol_default / "Local Storage" / "leveldb").write_bytes(b"site data")

            xh_cookie = profile_base / "xiaoheihe" / "Default" / "Network" / "Cookies"
            xh_cookie.parent.mkdir(parents=True)
            xh_cookie.write_bytes(b"xiaoheihe cookie database")

            app_module.BASE_DIR = temp_dir
            try:
                self.assertTrue(app_module.clear_platform_cookies("zol"))
            finally:
                app_module.BASE_DIR = old_base_dir

            self.assertFalse((zol_network / "Cookies").exists())
            self.assertFalse((zol_network / "Cookies-wal").exists())
            self.assertFalse((zol_default / "Local Storage").exists())
            self.assertTrue(xh_cookie.exists())
            self.assertFalse(app_module.clear_platform_cookies("unsupported"))

    def test_clear_platform_cookies_refuses_active_profile(self):
        import app as app_module
        from core.platform_guard import release, try_acquire

        old_base_dir = app_module.BASE_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = (
                Path(temp_dir)
                / "data"
                / "chrome_profiles"
                / "xiaoheihe"
                / "Default"
                / "Network"
                / "Cookies"
            )
            cookie_path.parent.mkdir(parents=True)
            cookie_path.write_bytes(b"active cookie database")
            app_module.BASE_DIR = temp_dir
            self.assertTrue(try_acquire("xiaoheihe"))
            try:
                self.assertFalse(app_module.clear_platform_cookies("xiaoheihe"))
            finally:
                release("xiaoheihe")
                app_module.BASE_DIR = old_base_dir
            self.assertTrue(cookie_path.exists())

    def test_clear_platform_cookies_refuses_singleton_marker(self):
        import app as app_module

        old_base_dir = app_module.BASE_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "data" / "chrome_profiles" / "zol"
            cookie_path = profile / "Default" / "Network" / "Cookies"
            cookie_path.parent.mkdir(parents=True)
            cookie_path.write_bytes(b"active cookie database")
            (profile / "SingletonLock").write_text("occupied", encoding="utf-8")
            app_module.BASE_DIR = temp_dir
            try:
                self.assertFalse(app_module.clear_platform_cookies("zol"))
            finally:
                app_module.BASE_DIR = old_base_dir
            self.assertTrue(cookie_path.exists())

    def test_logout_endpoint_resets_selected_account(self):
        import app as app_module
        from app import create_app

        self.db.upsert_account(
            "zol",
            status="logged_in",
            last_login_time="2026-08-06T14:30:00",
        )
        with patch.object(app_module, "clear_platform_cookies", return_value=True) as clear_mock:
            response = create_app().test_client().post("/api/accounts/zol/logout")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "status": "ok",
            "platform": "zol",
            "cookies_cleared": True,
        })
        clear_mock.assert_called_once_with("zol")
        account = self.db.get_account("zol")
        self.assertEqual(account["status"], "logged_out")
        self.assertIsNone(account["last_login_time"])

    def test_clear_cookies_endpoint_preserves_login_state(self):
        import app as app_module
        from app import create_app

        self.db.upsert_account(
            "zol",
            status="logged_in",
            last_login_time="2026-08-06T14:30:00",
        )
        with patch.object(app_module, "clear_platform_cookies", return_value=True) as clear_mock:
            response = create_app().test_client().post("/api/accounts/zol/clear-cookies")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["cookies_cleared"], True)
        clear_mock.assert_called_once_with("zol")
        account = self.db.get_account("zol")
        self.assertEqual(account["status"], "logged_in")
        self.assertEqual(account["last_login_time"], "2026-08-06T14:30:00")

    def test_existing_cookie_session_skips_login(self):
        from web import routes

        ExistingSessionPlatform.login_calls = 0
        with patch.object(routes, "ZOLPlatform", ExistingSessionPlatform, create=True):
            # routes imports platform classes inside the coroutine, so patch the module too.
            with patch("platforms.zol.ZOLPlatform", ExistingSessionPlatform):
                routes._run_login_in_thread("zol")
        self.assertEqual(ExistingSessionPlatform.login_calls, 0)
        self.assertEqual(self.db.get_account("zol")["status"], "logged_in")

    def test_resume_endpoint_requires_login_and_selection(self):
        import web.routes as routes
        from app import create_app

        article_id = self.create_article()
        task_id = self.db.create_task(article_id, "xiaoheihe")
        self.db.update_task(task_id, status="needs_selection", selection_status="required")
        fake_queue = type("Queue", (), {"enqueue": lambda self, value: setattr(self, "last", value)})()
        with patch.object(routes, "get_queue_manager", return_value=fake_queue):
            client = create_app().test_client()
            response = client.post(f"/api/tasks/{task_id}/resume", json={})
            self.assertEqual(response.status_code, 409)
            self.db.upsert_account("xiaoheihe", status="logged_in")
            response = client.post(f"/api/tasks/{task_id}/resume", json={"community": "社区"})
            self.assertEqual(response.status_code, 400)
            response = client.post(
                f"/api/tasks/{task_id}/resume",
                json={"community": "社区", "topic": "话题"},
            )
        self.assertEqual(response.status_code, 202)
        task = self.db.get_task(task_id)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["selection_status"], "manual")
        self.assertEqual(task["community_used"], "社区")
        self.assertEqual(fake_queue.last, task_id)

    def test_queue_does_not_load_historical_tasks(self):
        from core.queue_manager import QueueManager

        article_id = self.create_article()
        self.db.create_task(article_id, "zol")

        async def run():
            manager = QueueManager()
            manager.running = True
            worker = asyncio.create_task(manager._worker())
            await asyncio.sleep(0.05)
            self.assertEqual(manager.queue.qsize(), 0)
            manager.running = False
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        asyncio.run(run())

    def test_zol_cookie_check_uses_context_cookies(self):
        platform = ZOLPlatform()
        page = FakePage("textarea")
        page.url = "https://blog.zol.com.cn/post.php?act=add"
        page.goto = AsyncMock()
        page.evaluate = AsyncMock(return_value={
            "url": page.url,
            "hasLoginForm": False,
            "hasUser": False,
            "hasLogout": False,
        })
        page.locator = lambda selector: (
            page.target if "textarea[name='content']" in selector else FakeLocator(count=0)
        )
        platform.page = page
        platform.context = type("Context", (), {
            "cookies": AsyncMock(return_value=[{"name": "last_userid", "value": "opaque"}]),
        })()
        platform.context.cookies = AsyncMock(return_value=[{"name": "last_userid", "value": "opaque"}])
        with patch("platforms.zol.asyncio.sleep", new=AsyncMock()):
            self.assertTrue(asyncio.run(platform.check_login()))

    def test_zol_creator_cookie_bridge_copies_auth_scope_without_logging_values(self):
        platform = ZOLPlatform()
        platform.context = type("Context", (), {})()
        platform.context.cookies = AsyncMock(return_value=[
            {
                "name": "zol_sid",
                "value": "opaque-session",
                "domain": ".zol.com",
                "path": "/",
                "expires": 4102444800,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
            {
                "name": "ip_ck",
                "value": "opaque-analytics",
                "domain": ".zol.com",
                "path": "/",
                "expires": 4102444800,
            },
        ])
        platform.context.add_cookies = AsyncMock()

        count = asyncio.run(platform._bridge_creator_cookies())

        self.assertEqual(count, 1)
        copied = platform.context.add_cookies.await_args.args[0]
        self.assertEqual(copied[0]["name"], "zol_sid")
        self.assertEqual(copied[0]["domain"], ".zol.com.cn")

    def test_zol_creator_routes_are_configured(self):
        zol_cfg = get_config()["platforms"]["zol"]
        self.assertEqual(zol_cfg["login_url"], "https://post.zol.com.cn/v2/login")
        self.assertEqual(zol_cfg["editor_url"], "https://post.zol.com.cn/v2/create/article")
        self.assertEqual(zol_cfg["draft_url"], "https://post.zol.com.cn/v2/manage/works/draft")
        self.assertTrue(
            ZOLPlatform._is_blog_editor_url(
                "https://post.zol.com.cn/v2/create/article"
            )
        )

    def test_zol_title_generation_matches_creator_center_limit(self):
        from core.nlp_analyzer import NLPAnalyzer

        title = NLPAnalyzer().generate_title(
            "正文内容",
            "开学季买49英寸显示器，先量宿舍桌再下单并检查电脑接口和线材带宽",
            "zol",
        )
        self.assertLessEqual(len(title), 35)

    def test_zol_public_profile_is_not_login_success(self):
        platform = ZOLPlatform()
        page = FakePage("none")
        page.url = "https://my.zol.cn/u9nltx/"
        page.evaluate = AsyncMock(return_value={
            "url": page.url,
            "hasLoginForm": False,
            "hasUser": True,
            "hasLogout": False,
            "hasSecurityChallenge": False,
        })
        platform.page = page

        self.assertFalse(asyncio.run(platform._check_login_current_page()))

    def test_zol_homepage_cookie_only_is_not_login_success(self):
        platform = ZOLPlatform()
        page = FakePage("none")
        page.url = "https://www.zol.com.cn/"
        page.evaluate = AsyncMock(return_value={
            "url": page.url,
            "hasLoginForm": False,
            "hasUser": False,
            "hasLogout": False,
            "hasSecurityChallenge": False,
        })
        platform.page = page
        platform.context = type("Context", (), {
            "cookies": AsyncMock(return_value=[
                {"name": "last_userid", "expires": -1},
                {"name": "lv", "expires": -1},
            ]),
        })()

        self.assertFalse(asyncio.run(platform._check_login_current_page()))

    def test_zol_cookie_check_rejects_expired_context_cookie(self):
        import time

        platform = ZOLPlatform()
        page = FakePage("textarea")
        page.url = "https://blog.zol.com.cn/post.php?act=add"
        page.goto = AsyncMock()
        page.evaluate = AsyncMock(return_value={
            "url": page.url,
            "hasLoginForm": False,
            "hasUser": False,
            "hasLogout": False,
        })
        page.locator = lambda selector: (
            page.target if "textarea[name='content']" in selector else FakeLocator(count=0)
        )
        platform.page = page
        platform.context = type("Context", (), {})()
        platform.context.cookies = AsyncMock(return_value=[{
            "name": "last_userid",
            "value": "expired",
            "expires": time.time() - 60,
        }])

        with patch("platforms.zol.asyncio.sleep", new=AsyncMock()):
            self.assertFalse(asyncio.run(platform.check_login()))

    def test_zol_title_failure_is_explicit(self):
        platform = ZOLPlatform()
        platform.page = FakePage("none")
        with self.assertRaises(RuntimeError):
            asyncio.run(platform.fill_title("标题"))

    def test_xiaoheihe_title_and_body_use_different_editors(self):
        platform = XiaoheihePlatform()
        platform.page = FakeXiaoPage()
        platform.simulator.random_delay = AsyncMock()
        asyncio.run(platform.fill_title("标题内容"))
        asyncio.run(platform.fill_content([
            {"type": "heading", "text": "正文标题"},
            {"type": "text", "text": "正文内容"},
        ], []))
        self.assertEqual(platform.page.title.text, "标题内容")
        self.assertIn("正文标题", platform.page.body.text)
        self.assertIn("正文内容", platform.page.body.text)
        self.assertNotIn("正文内容", platform.page.title.text)

    def test_zol_content_textarea_strategy(self):
        platform = ZOLPlatform()
        platform.page = FakePage("textarea")
        platform.simulator.random_delay = AsyncMock()
        asyncio.run(platform.fill_content([
            {"type": "heading", "text": "标题段"},
            {"type": "text", "text": "正文段"},
        ], []))
        self.assertIn("标题段", platform.page.target.value)
        self.assertIn("正文段", platform.page.target.value)

    def test_zol_content_contenteditable_strategy(self):
        platform = ZOLPlatform()
        platform.page = FakePage("contenteditable")
        platform.simulator.random_delay = AsyncMock()
        asyncio.run(platform.fill_content([
            {"type": "text", "text": "正文段"},
        ], []))
        self.assertIn("正文段", platform.page.target.text)

    def test_zol_content_iframe_strategy(self):
        platform = ZOLPlatform()
        platform.page = FakePage("iframe")
        platform.simulator.random_delay = AsyncMock()
        asyncio.run(platform.fill_content([
            {"type": "text", "text": "iframe正文"},
        ], []))
        self.assertIn("iframe正文", platform.page.frame_body.text)

    def test_xiaoheihe_multiline_validation_accepts_editor_entities(self):
        platform = XiaoheihePlatform()
        platform.page = FakeXiaoPage()
        platform.simulator.random_delay = AsyncMock()
        original_inner_text = platform.page.body.inner_text

        async def entity_transformed_text():
            value = await original_inner_text()
            return value.replace("A&B", "A&amp;B").replace("第二 段", "第二&nbsp;段\u200b")

        platform.page.body.inner_text = entity_transformed_text
        result = asyncio.run(platform.fill_content([
            {"type": "text", "text": "第一段\nA&B\n第二 段"},
        ], []))
        self.assertTrue(result["text_ok"])
        self.assertEqual(result["media_status"], "not_required")

    def test_xiaoheihe_revalidates_body_after_image_rerender(self):
        platform = XiaoheihePlatform()
        platform.page = FakeXiaoPage()
        platform.simulator.random_delay = AsyncMock()

        async def remove_body_after_upload(_path):
            platform.page.body.text = "第一段"
            platform.page.body.value = "第一段"
            return {"success": True, "filename": "image.png"}

        platform._upload_image = AsyncMock(side_effect=remove_body_after_upload)
        with self.assertRaises(ContentValidationError) as context:
            asyncio.run(platform.fill_content([
                {"type": "text", "text": "第一段\n第二段"},
                {"type": "image", "position": 1, "local_path": "D:/test/image.png"},
            ], []))
        self.assertEqual(context.exception.error_code, "CONTENT_VALIDATION_ERROR")

    def test_zol_three_editor_read_strategies_are_explicit(self):
        for kind, expected_kind in (
            ("textarea", "textarea"),
            ("contenteditable", "contenteditable"),
            ("iframe", "iframe"),
        ):
            with self.subTest(kind=kind):
                platform = ZOLPlatform()
                platform.page = FakePage(kind)
                editor, actual_kind = asyncio.run(platform._resolve_content_editor())
                self.assertEqual(actual_kind, expected_kind)
                if kind == "textarea":
                    editor.value = "textarea正文"
                    editor.input_value = AsyncMock(return_value=editor.value)
                    editor.inner_text = AsyncMock(side_effect=AssertionError("textarea 不得读 inner_text"))
                else:
                    editor.text = f"{kind}正文"
                    editor.inner_text = AsyncMock(return_value=editor.text)
                    editor.input_value = AsyncMock(side_effect=AssertionError("富文本不得读 input_value"))
                actual = asyncio.run(platform._read_content_editor_text(editor, actual_kind))
                self.assertIn(kind, actual)
                if kind == "textarea":
                    editor.input_value.assert_awaited_once()
                    editor.inner_text.assert_not_awaited()
                else:
                    editor.inner_text.assert_awaited_once()
                    editor.input_value.assert_not_awaited()
    def test_zol_hidden_iframe_falls_back_to_visible_direct_editor(self):
        platform = ZOLPlatform()
        page = FakePage("textarea")
        hidden_iframe = FakeLocator(page=page, tag="iframe", visible=False)
        original_locator = page.locator

        def locator(selector):
            if selector.startswith("iframe") or "iframe" in selector:
                return hidden_iframe
            return original_locator(selector)

        page.locator = locator
        platform.page = page
        editor, editor_kind = asyncio.run(platform._resolve_content_editor())
        self.assertIs(editor, page.target)
        self.assertEqual(editor_kind, "textarea")
    def test_zol_revalidates_replaced_iframe_after_image_upload(self):
        platform = ZOLPlatform()
        page = FakePage("iframe")
        platform.page = page
        platform.simulator.random_delay = AsyncMock()

        async def replace_iframe_after_upload(_path):
            replacement_body = FakeLocator(page=page, tag="body", text="第一段")
            page.frame_body = replacement_body
            page.frame = FakeFrame(page, replacement_body)
            page.iframe_handle.content_frame = AsyncMock(return_value=page.frame)
            return {"success": True, "filename": "image.png"}

        platform._upload_image = AsyncMock(side_effect=replace_iframe_after_upload)
        with self.assertRaises(ContentValidationError):
            asyncio.run(platform.fill_content([
                {"type": "text", "text": "第一段\n第二段"},
                {"type": "image", "position": 1},
            ], [{"position_index": 1, "local_path": "D:/test/image.png"}]))
    def test_xiaoheihe_separates_community_and_topic(self):
        platform = XiaoheihePlatform()
        platform.select_community = AsyncMock(return_value={"success": True, "value": "自动社区"})
        platform._select_from_editor_dialog = AsyncMock(return_value={"success": True, "value": "自动话题"})
        result = asyncio.run(platform.select_topic(selection_query="显示器"))
        self.assertTrue(result["success"])
        self.assertEqual(result["selection"], {"community": "自动社区", "topic": "自动话题"})
        self.assertEqual(platform._select_from_editor_dialog.await_args.args[0], "话题")
        topic_selector = platform._select_from_editor_dialog.await_args.args[2]
        self.assertIn("editor-model__hashtag-list-item", topic_selector)
        self.assertNotIn("[class*='topic-item']", topic_selector)

    def test_xiaoheihe_no_candidate_enters_needs_selection(self):
        platform = XiaoheihePlatform()
        platform.select_community = AsyncMock(return_value={
            "success": False,
            "needs_selection": True,
            "error": "没有社区",
        })
        result = asyncio.run(platform.select_topic(selection_query="无结果"))
        self.assertFalse(result["success"])
        self.assertTrue(result["needs_selection"])

    def test_xiaoheihe_topic_query_uses_single_keyword_candidates(self):
        platform = XiaoheihePlatform()
        self.assertEqual(
            platform._query_candidates("显示器 桌面 输出", split_terms=True),
            ["显示器", "桌面", "输出"],
        )
        self.assertEqual(
            platform._query_candidates("显示器 桌面 输出"),
            ["显示器 桌面 输出"],
        )

    def test_xiaoheihe_topic_failure_keeps_selected_community(self):
        platform = XiaoheihePlatform()
        platform.select_community = AsyncMock(return_value={
            "success": True,
            "value": "导师模拟器：桌面实验室",
        })
        platform._select_from_editor_dialog = AsyncMock(return_value={
            "success": False,
            "needs_selection": True,
            "error": "话题候选名称无效",
        })
        result = asyncio.run(platform.select_topic(selection_query="显示器 桌面 输出"))
        self.assertFalse(result["success"])
        self.assertEqual(
            result["selection"],
            {"community": "导师模拟器：桌面实验室", "topic": ""},
        )
        self.assertEqual(result["selection_status"], "needs_selection")
        topic_query = platform._select_from_editor_dialog.await_args.args[1]
        self.assertEqual(topic_query, ["显示器", "桌面", "输出"])

    def test_xiaoheihe_invalid_candidate_validation(self):
        platform = XiaoheihePlatform()
        self.assertTrue(platform._candidate_is_invalid("显示器 桌面 输出", "显示器 桌面 输出"))
        self.assertTrue(platform._candidate_is_invalid('{"word": "显示器"}', "显示器"))
        self.assertFalse(platform._candidate_is_invalid("导师模拟器：桌面实验室", "显示器"))

    def test_xiaoheihe_cookie_check_accepts_renamed_login_cookies(self):
        """小黑盒已把登录 cookie 更名为 user_heybox_id / user_pkey，必须识别。"""
        platform = XiaoheihePlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "user_heybox_id", "value": "102503316"},
                        {"name": "user_pkey", "value": "opaque"},
                        {"name": "x_xhh_tokenid", "value": "opaque"},
                    ]
                ),
            },
        )()
        self.assertTrue(asyncio.run(platform._is_logged_in_dom()))

    def test_xiaoheihe_cookie_check_accepts_legacy_login_cookies(self):
        platform = XiaoheihePlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "heybox_id", "value": "102503316"},
                        {"name": "pkey", "value": "opaque"},
                    ]
                ),
            },
        )()
        self.assertTrue(asyncio.run(platform._is_logged_in_dom()))

    def test_xiaoheihe_cookie_check_rejects_guest_state(self):
        platform = XiaoheihePlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[{"name": "x_xhh_tokenid", "value": "opaque"}]
                ),
            },
        )()
        platform.page = type(
            "Page", (), {"evaluate": AsyncMock(return_value=False), "locator": None}
        )()
        self.assertFalse(asyncio.run(platform._is_logged_in_dom()))

    def test_xiaoheihe_identity_uses_renamed_cookie_and_dom_nickname(self):
        platform = XiaoheihePlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "user_heybox_id", "value": "102503316"},
                        {"name": "x_xhh_tokenid", "value": "opaque"},
                    ]
                ),
            },
        )()
        platform.page = type(
            "Page", (), {"evaluate": AsyncMock(return_value="夜航员")}
        )()
        identity = asyncio.run(extract_identity(platform))
        self.assertEqual(identity.platform_user_id, "102503316")
        self.assertEqual(identity.display_name, "夜航员")

    def test_xiaoheihe_identity_prefers_restore_login_payload(self):
        platform = XiaoheihePlatform()
        platform.fetch_identity_payload = AsyncMock(
            return_value={
                "ok": True,
                "user_id": "102503316",
                "display_name": "玩家102503316",
            }
        )
        identity = asyncio.run(extract_identity(platform))
        self.assertEqual(identity.platform_user_id, "102503316")
        self.assertEqual(identity.display_name, "玩家102503316")

    def test_xiaoheihe_fetch_identity_replays_captured_restore_url(self):
        platform = XiaoheihePlatform()
        platform._restore_url = (
            "https://api.xiaoheihe.cn/account/restore_login?hkey=X&nonce=Y"
        )
        platform.page = type(
            "Page",
            (),
            {
                "evaluate": AsyncMock(
                    return_value={
                        "ok": True,
                        "user_id": "102503316",
                        "display_name": "玩家102503316",
                    }
                )
            },
        )()
        result = asyncio.run(platform.fetch_identity_payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["display_name"], "玩家102503316")

    def test_xiaoheihe_fetch_identity_fails_closed_without_capture(self):
        platform = XiaoheihePlatform()
        result = asyncio.run(platform.fetch_identity_payload())
        self.assertEqual(result, {"ok": False, "user_id": "", "display_name": ""})

    def test_douyin_cookie_check_accepts_session_cookie(self):
        platform = DouyinPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "sessionid", "value": "opaque"},
                        {"name": "ttwid", "value": "opaque"},
                    ]
                ),
            },
        )()
        self.assertTrue(asyncio.run(platform._has_session_cookie_signal()))

    def test_douyin_cookie_check_rejects_guest_state(self):
        platform = DouyinPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "ttwid", "value": "opaque"},
                        {"name": "passport_csrf_token", "value": "opaque"},
                    ]
                ),
            },
        )()
        self.assertFalse(asyncio.run(platform._has_session_cookie_signal()))

    def test_douyin_fetch_identity_uses_captured_payload(self):
        platform = DouyinPlatform()
        platform._identity_payload = {
            "ok": True,
            "user_id": "MS4wLjABAAAAx",
            "display_name": "抖音昵称",
        }
        result = asyncio.run(platform.fetch_identity_payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["display_name"], "抖音昵称")

    def test_douyin_fetch_identity_fails_closed_without_capture(self):
        platform = DouyinPlatform()
        platform.page = type(
            "Page",
            (),
            {
                "goto": AsyncMock(),
                "on": lambda *a, **k: None,
                "remove_listener": lambda *a, **k: None,
            },
        )()
        with patch("platforms.douyin.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(platform.fetch_identity_payload())
        self.assertEqual(result, {"ok": False, "user_id": "", "display_name": ""})

    def test_douyin_identity_extraction_requires_payload(self):
        platform = DouyinPlatform()
        platform.fetch_identity_payload = AsyncMock(
            return_value={
                "ok": True,
                "user_id": "MS4wLjABAAAAx",
                "display_name": "抖音昵称",
            }
        )
        identity = asyncio.run(extract_identity(platform))
        self.assertEqual(identity.platform_user_id, "MS4wLjABAAAAx")
        self.assertEqual(identity.display_name, "抖音昵称")

    def test_douyin_publish_now_still_fails_closed(self):
        platform = DouyinPlatform()
        with self.assertRaises(PlatformNotImplementedError):
            asyncio.run(platform.publish_now())

    def test_douyin_navigate_to_editor_opens_publish_page(self):
        platform = DouyinPlatform()
        platform.page = type(
            "Page",
            (),
            {
                "goto": AsyncMock(),
                "wait_for_selector": AsyncMock(return_value=True),
            },
        )()
        asyncio.run(platform.navigate_to_editor())
        self.assertIn("content/publish", platform.page.goto.await_args.args[0])

    def test_douyin_fill_title_writes_into_semi_input(self):
        platform = DouyinPlatform()
        title_field = type(
            "First",
            (),
            {
                "count": AsyncMock(return_value=1),
                "is_visible": AsyncMock(return_value=True),
                "click": AsyncMock(),
                "fill": AsyncMock(),
            },
        )()
        title_locator = type("Loc", (), {"first": title_field})()
        platform.page = type(
            "Page",
            (),
            {"locator": staticmethod(lambda _sel: title_locator)},
        )()
        asyncio.run(platform.fill_title("抖音标题"))
        title_field.fill.assert_awaited_with("抖音标题")

    def test_douyin_extract_identity_json_finds_name_and_uid(self):
        platform = DouyinPlatform()
        found = platform._extract_identity_from_json(
            {
                "base_resp": {"status_code": 0},
                "user_info": {"nickname": "夜航员", "sec_uid": "MS4wLjABAAAAx"},
            }
        )
        self.assertEqual(found, ("MS4wLjABAAAAx", "夜航员"))
        self.assertIsNone(platform._extract_identity_from_json({"foo": "bar"}))

    def test_xiaohongshu_cookie_check_accepts_web_session(self):
        platform = XiaohongshuPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "web_session", "value": "opaque"},
                        {"name": "webId", "value": "opaque"},
                    ]
                ),
            },
        )()
        self.assertTrue(asyncio.run(platform._has_session_cookie_signal()))

    def test_xiaohongshu_cookie_check_accepts_renamed_session_cookies(self):
        platform = XiaohongshuPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "customer-sso-sid", "value": "opaque"},
                        {"name": "customerClientId", "value": "opaque"},
                        {"name": "x-user-id-creator.xiaohongshu.com", "value": "opaque"},
                        {"name": "a1", "value": "opaque"},
                    ]
                ),
            },
        )()
        self.assertTrue(asyncio.run(platform._has_session_cookie_signal()))

    def test_xiaohongshu_cookie_check_rejects_guest_state(self):
        platform = XiaohongshuPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[{"name": "webId", "value": "opaque"}]
                ),
            },
        )()
        self.assertFalse(asyncio.run(platform._has_session_cookie_signal()))

    def test_xiaohongshu_fetch_identity_uses_captured_payload(self):
        platform = XiaohongshuPlatform()
        platform._identity_payload = {
            "ok": True,
            "user_id": "5f3e2a1b",
            "display_name": "小红书昵称",
        }
        result = asyncio.run(platform.fetch_identity_payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["display_name"], "小红书昵称")

    def test_xiaohongshu_fetch_identity_fails_closed_without_capture(self):
        platform = XiaohongshuPlatform()
        platform.page = type(
            "Page",
            (),
            {
                "goto": AsyncMock(),
                "on": lambda *a, **k: None,
                "remove_listener": lambda *a, **k: None,
            },
        )()
        with patch("platforms.xiaohongshu.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(platform.fetch_identity_payload())
        self.assertEqual(result, {"ok": False, "user_id": "", "display_name": ""})

    def test_xiaohongshu_fetch_identity_falls_back_to_home_dom(self):
        platform = XiaohongshuPlatform()
        platform.page = type(
            "Page",
            (),
            {
                "goto": AsyncMock(),
                "on": lambda *a, **k: None,
                "remove_listener": lambda *a, **k: None,
                "evaluate": AsyncMock(
                    return_value={
                        "user_id": "42221583540",
                        "display_name": "jayoma",
                    }
                ),
            },
        )()
        with patch("platforms.xiaohongshu.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(platform.fetch_identity_payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["user_id"], "42221583540")
        self.assertEqual(result["display_name"], "jayoma")

    def test_xiaohongshu_identity_extraction_requires_payload(self):
        platform = XiaohongshuPlatform()
        platform.fetch_identity_payload = AsyncMock(
            return_value={
                "ok": True,
                "user_id": "5f3e2a1b",
                "display_name": "小红书昵称",
            }
        )
        identity = asyncio.run(extract_identity(platform))
        self.assertEqual(identity.platform_user_id, "5f3e2a1b")
        self.assertEqual(identity.display_name, "小红书昵称")

    def test_xiaohongshu_publish_now_still_fails_closed(self):
        platform = XiaohongshuPlatform()
        with self.assertRaises(XhsNotImplementedError):
            asyncio.run(platform.publish_now())

    def test_xiaohongshu_navigate_to_editor_sequence(self):
        platform = XiaohongshuPlatform()
        platform.simulator.random_delay = AsyncMock()
        page = type(
            "Page",
            (),
            {
                "goto": AsyncMock(),
                "wait_for_selector": AsyncMock(return_value=True),
                "evaluate": AsyncMock(return_value=True),
            },
        )()
        platform.page = page
        asyncio.run(platform.navigate_to_editor())
        self.assertIn("publish", page.goto.await_args.args[0])
        self.assertEqual(page.evaluate.call_count, 2)

    def test_xiaohongshu_fill_title_writes_into_title_field(self):
        platform = XiaohongshuPlatform()
        title_field = type(
            "First",
            (),
            {
                "count": AsyncMock(return_value=1),
                "is_visible": AsyncMock(return_value=True),
                "click": AsyncMock(),
                "fill": AsyncMock(),
            },
        )()
        title_locator = type("Loc", (), {"first": title_field})()
        platform.page = type(
            "Page",
            (),
            {"locator": staticmethod(lambda _sel: title_locator)},
        )()
        asyncio.run(platform.fill_title("小红书标题"))
        title_field.fill.assert_awaited_with("小红书标题")

    def test_weibo_on_home_detects_redirect_after_login(self):
        platform = WeiboPlatform()
        platform.page = type(
            "Page",
            (),
            {
                "url": "https://weibo.com/",
                "is_closed": lambda: False,
                "context": None,
            },
        )()
        self.assertTrue(asyncio.run(platform._on_home()))

    def test_weibo_cookie_check_accepts_login_only_cookies(self):
        platform = WeiboPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "SCF", "value": "opaque"},
                        {"name": "ALF", "value": "opaque"},
                        {"name": "SUB", "value": "opaque"},
                    ]
                ),
            },
        )()
        self.assertTrue(asyncio.run(platform._has_session_cookie_signal()))

    def test_weibo_cookie_check_rejects_guest_state(self):
        platform = WeiboPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "SUB", "value": "opaque"},
                        {"name": "SUBP", "value": "opaque"},
                        {"name": "WBPSESS", "value": "opaque"},
                    ]
                ),
            },
        )()
        self.assertFalse(asyncio.run(platform._has_session_cookie_signal()))

    def test_weibo_on_home_false_on_passport(self):
        platform = WeiboPlatform()
        platform.page = type(
            "Page",
            (),
            {
                "url": "https://passport.weibo.com/sso/signin?entry=miniblog",
                "is_closed": lambda: False,
            },
        )()
        self.assertFalse(asyncio.run(platform._on_home()))

    def test_weibo_fetch_identity_uses_captured_payload(self):
        platform = WeiboPlatform()
        platform._identity_payload = {
            "ok": True,
            "user_id": "1234567890",
            "display_name": "微博昵称",
        }
        result = asyncio.run(platform.fetch_identity_payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["display_name"], "微博昵称")

    def test_weibo_fetch_identity_fails_closed_without_capture(self):
        platform = WeiboPlatform()
        platform.page = type(
            "Page",
            (),
            {
                "goto": AsyncMock(),
                "on": lambda *a, **k: None,
                "remove_listener": lambda *a, **k: None,
                "evaluate": AsyncMock(return_value={"user_id": "", "display_name": ""}),
            },
        )()
        with patch("platforms.weibo.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(platform.fetch_identity_payload())
        self.assertEqual(result, {"ok": False, "user_id": "", "display_name": ""})

    def test_weibo_identity_extraction_requires_payload(self):
        platform = WeiboPlatform()
        platform.fetch_identity_payload = AsyncMock(
            return_value={
                "ok": True,
                "user_id": "1234567890",
                "display_name": "微博昵称",
            }
        )
        identity = asyncio.run(extract_identity(platform))
        self.assertEqual(identity.platform_user_id, "1234567890")
        self.assertEqual(identity.display_name, "微博昵称")

    def test_weibo_extract_identity_json_supports_nick_field(self):
        platform = WeiboPlatform()
        found = platform._extract_identity_from_json(
            {"data": {"uid": "1234567890", "nick": "微博昵称"}}
        )
        self.assertEqual(found, ("1234567890", "微博昵称"))
        self.assertIsNone(platform._extract_identity_from_json({"foo": "bar"}))

    def test_weibo_delivery_methods_fail_closed(self):
        platform = WeiboPlatform()
        for method, args in [
            ("navigate_to_editor", ()),
            ("fill_title", ("标题",)),
            ("fill_content", ([], [])),
            ("select_topic", ()),
            ("save_draft", ()),
            ("publish_now", ()),
        ]:
            with self.assertRaises(WeiboNotImplementedError):
                asyncio.run(getattr(platform, method)(*args))

    def test_baijiahao_cookie_check_accepts_bduss(self):
        platform = BaijiahaoPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "BDUSS", "value": "opaque"},
                        {"name": "BAIDUID", "value": "opaque"},
                    ]
                ),
            },
        )()
        self.assertTrue(asyncio.run(platform._has_session_cookie_signal()))

    def test_baijiahao_cookie_check_rejects_guest_state(self):
        platform = BaijiahaoPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[{"name": "BAIDUID", "value": "opaque"}]
                ),
            },
        )()
        self.assertFalse(asyncio.run(platform._has_session_cookie_signal()))

    def test_baijiahao_fetch_identity_uses_captured_payload(self):
        platform = BaijiahaoPlatform()
        platform._identity_payload = {
            "ok": True,
            "user_id": "170123456789",
            "display_name": "百家号昵称",
        }
        result = asyncio.run(platform.fetch_identity_payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["display_name"], "百家号昵称")

    def test_baijiahao_fetch_identity_fails_closed_without_capture(self):
        platform = BaijiahaoPlatform()
        platform.page = type(
            "Page",
            (),
            {
                "goto": AsyncMock(),
                "on": lambda *a, **k: None,
                "remove_listener": lambda *a, **k: None,
            },
        )()
        with patch("platforms.baijiahao.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(platform.fetch_identity_payload())
        self.assertEqual(result, {"ok": False, "user_id": "", "display_name": ""})

    def test_baijiahao_identity_extraction_requires_payload(self):
        platform = BaijiahaoPlatform()
        platform.fetch_identity_payload = AsyncMock(
            return_value={
                "ok": True,
                "user_id": "170123456789",
                "display_name": "百家号昵称",
            }
        )
        identity = asyncio.run(extract_identity(platform))
        self.assertEqual(identity.platform_user_id, "170123456789")
        self.assertEqual(identity.display_name, "百家号昵称")

    def test_baijiahao_delivery_methods_fail_closed(self):
        platform = BaijiahaoPlatform()
        for method, args in [
            ("navigate_to_editor", ()),
            ("fill_title", ("标题",)),
            ("fill_content", ([], [])),
            ("select_topic", ()),
            ("save_draft", ()),
            ("publish_now", ()),
        ]:
            with self.assertRaises(BaijiahaoNotImplementedError):
                asyncio.run(getattr(platform, method)(*args))

    def test_smzdm_cookie_check_accepts_sess(self):
        platform = SmzdmPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[
                        {"name": "sess", "value": "opaque"},
                        {"name": "smzdm_id", "value": "opaque"},
                    ]
                ),
            },
        )()
        self.assertTrue(asyncio.run(platform._has_session_cookie_signal()))

    def test_smzdm_cookie_check_rejects_guest_state(self):
        platform = SmzdmPlatform()
        platform.context = type(
            "Context",
            (),
            {
                "cookies": AsyncMock(
                    return_value=[{"name": "smzdm_id", "value": "opaque"}]
                ),
            },
        )()
        self.assertFalse(asyncio.run(platform._has_session_cookie_signal()))

    def test_smzdm_fetch_identity_uses_captured_payload(self):
        platform = SmzdmPlatform()
        platform._identity_payload = {
            "ok": True,
            "user_id": "10234567",
            "display_name": "值友昵称",
        }
        result = asyncio.run(platform.fetch_identity_payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["display_name"], "值友昵称")

    def test_smzdm_fetch_identity_fails_closed_without_capture(self):
        platform = SmzdmPlatform()
        platform.page = type(
            "Page",
            (),
            {
                "goto": AsyncMock(),
                "on": lambda *a, **k: None,
                "remove_listener": lambda *a, **k: None,
                "evaluate": AsyncMock(return_value={"user_id": "", "display_name": ""}),
            },
        )()
        with patch("platforms.smzdm.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(platform.fetch_identity_payload())
        self.assertEqual(result, {"ok": False, "user_id": "", "display_name": ""})

    def test_smzdm_identity_extraction_requires_payload(self):
        platform = SmzdmPlatform()
        platform.fetch_identity_payload = AsyncMock(
            return_value={
                "ok": True,
                "user_id": "10234567",
                "display_name": "值友昵称",
            }
        )
        identity = asyncio.run(extract_identity(platform))
        self.assertEqual(identity.platform_user_id, "10234567")
        self.assertEqual(identity.display_name, "值友昵称")

    def test_smzdm_parse_jsonp_extracts_payload(self):
        platform = SmzdmPlatform()
        payload = platform._parse_jsonp(
            'jQuery123({"smzdm_id": "4668373440", "nickname": "值友3424774480"})'
        )
        self.assertEqual(payload["smzdm_id"], "4668373440")
        self.assertEqual(payload["nickname"], "值友3424774480")
        self.assertIsNone(platform._parse_jsonp("not jsonp"))

    def test_smzdm_extract_identity_json_supports_smzdm_id(self):
        platform = SmzdmPlatform()
        found = platform._extract_identity_from_json(
            {"smzdm_id": "4668373440", "nickname": "值友3424774480"}
        )
        self.assertEqual(found, ("4668373440", "值友3424774480"))
        self.assertIsNone(platform._extract_identity_from_json({"foo": "bar"}))

    def test_smzdm_delivery_methods_fail_closed(self):
        platform = SmzdmPlatform()
        for method, args in [
            ("navigate_to_editor", ()),
            ("fill_title", ("标题",)),
            ("fill_content", ([], [])),
            ("select_topic", ()),
            ("save_draft", ()),
            ("publish_now", ()),
        ]:
            with self.assertRaises(SmzdmNotImplementedError):
                asyncio.run(getattr(platform, method)(*args))

    def test_xiaoheihe_manual_override_is_forwarded(self):
        platform = XiaoheihePlatform()
        platform.select_community = AsyncMock(return_value={"success": True, "value": "手动社区"})
        platform._select_from_editor_dialog = AsyncMock(return_value={"success": True, "value": "手动话题"})
        result = asyncio.run(platform.select_topic(
            selection_override={"community": "手动社区", "topic": "手动话题"},
            selection_query="文章关键词",
        ))
        self.assertEqual(result["selection"], {"community": "手动社区", "topic": "手动话题"})
        self.assertEqual(platform.select_community.await_args.args[0], "手动社区")
        self.assertEqual(platform._select_from_editor_dialog.await_args.args[0], "话题")

    def test_base_pipeline_saves_draft_when_selection_is_unavailable(self):
        platform = SelectionStub()
        platform.page = object()
        platform.simulator.random_delay = AsyncMock()
        platform.simulator.simulate_scroll = AsyncMock()
        platform.simulator.random_mouse_movement = AsyncMock()
        task_id = self.db.create_task(self.create_article(), "xiaoheihe")
        result = asyncio.run(platform.publish(
            title="测试标题",
            content_blocks=[{"type": "text", "text": "正文"}],
            images=[],
            selection_query="关键词",
            task_id=task_id,
            db=self.db,
            auto_login=False,
        ))
        self.assertTrue(result["success"])
        self.assertEqual(result["selection_status"], "needs_selection")
        self.assertEqual(result["draft_url"], "https://example.test/draft")

    def test_selection_query_parses_keyword_json(self):
        value = '[{"word": "显示器", "weight": 1.2}, {"word": "桌面", "weight": 1.0}, {"word": "输出", "weight": 0.8}]'
        self.assertEqual(normalize_selection_query(value, fallback="标题"), "显示器 桌面 输出")
        self.assertEqual(normalize_selection_query("测试,正文", fallback="标题"), "测试 正文")
        self.assertEqual(normalize_selection_query("", fallback="标题"), "标题")

    def test_xiaoheihe_image_failures_are_reported(self):
        platform = XiaoheihePlatform()
        platform.page = FakeXiaoPage()
        platform.simulator.random_delay = AsyncMock()
        platform._upload_image = AsyncMock(return_value={
            "success": False,
            "error": "按钮被遮罩拦截",
        })
        result = asyncio.run(platform.fill_content([
            {"type": "text", "text": "正文段"},
            {"type": "image", "position": 1, "local_path": "D:/test/image.png"},
        ], []))
        self.assertTrue(result["text_ok"])
        self.assertEqual(result["expected_images"], 1)
        self.assertEqual(result["uploaded_images"], 0)
        self.assertEqual(result["media_status"], "failed")
        self.assertEqual(len(result["failed_images"]), 1)


    def test_xiaoheihe_media_errors_do_not_leak_physical_paths(self):
        platform = XiaoheihePlatform()
        platform.page = FakeXiaoPage()
        platform.simulator.random_delay = AsyncMock()
        platform._upload_image = AsyncMock(return_value={
            "success": False,
            "error": r"set_input_files D:\secret\private\image.png token=abcdef123456789012345678901234",
        })
        result = asyncio.run(platform.fill_content([
            {"type": "text", "text": "正文段"},
            {"type": "image", "position": 1, "local_path": "D:/test/image.png"},
        ], []))
        serialized = repr(result["failed_images"])
        self.assertNotIn(r"D:\secret", serialized)
        self.assertNotIn("abcdef123456", serialized)
        self.assertEqual(result["failed_images"][0]["filename"], "image.png")

    def test_zol_media_errors_do_not_leak_physical_paths(self):
        platform = ZOLPlatform()
        platform.page = FakePage("iframe")
        platform.simulator.random_delay = AsyncMock()
        platform._upload_image = AsyncMock(return_value={
            "success": False,
            "error_code": "ZOL_IMAGE_UPLOAD_FAILED",
            "error": r"set_input_files D:\secret\private\image.png cookie=abcdef123456789012345678901234",
        })
        result = asyncio.run(platform.fill_content([
            {"type": "text", "text": "正文段"},
            {"type": "image", "position": 1},
        ], [{"position_index": 1, "local_path": "D:/test/image.png"}]))
        serialized = repr(result["failed_images"])
        self.assertNotIn(r"D:\secret", serialized)
        self.assertNotIn("abcdef123456", serialized)
        self.assertEqual(result["failed_images"][0]["filename"], "image.png")
    def test_zol_topic_match_rejects_ambiguous_candidates(self):
        self.assertEqual(
            ZOLPlatform._pick_topic_candidate(["显示器分屏", "显示器推荐"], "显示器"),
            "",
        )
        self.assertEqual(
            ZOLPlatform._pick_topic_candidate(["显示器分屏", "显示器推荐"], "显示器分屏"),
            "显示器分屏",
        )
        self.assertEqual(
            ZOLPlatform._pick_topic_candidate(["显示器分屏"], "分屏"),
            "显示器分屏",
        )

    def test_zol_image_failure_is_reported_in_content_result(self):
        platform = ZOLPlatform()
        platform.page = FakePage("iframe")
        platform.simulator.random_delay = AsyncMock()
        platform._upload_image = AsyncMock(return_value={
            "success": False,
            "error_code": "ZOL_IMAGE_UPLOAD_CONTROL_NOT_FOUND",
            "error": "未找到图片按钮",
        })
        result = asyncio.run(platform.fill_content([
            {"type": "text", "text": "正文段"},
            {"type": "image", "position": 1},
        ], [{"position_index": 1, "local_path": "D:/test/image.png"}]))
        self.assertTrue(result["text_ok"])
        self.assertEqual(result["expected_images"], 1)
        self.assertEqual(result["uploaded_images"], 0)
        self.assertEqual(result["media_status"], "failed")
        self.assertEqual(result["media_error_code"], "ZOL_IMAGES_ALL_FAILED")
        self.assertEqual(result["failed_images"][0]["error_code"], "ZOL_IMAGE_UPLOAD_CONTROL_NOT_FOUND")

    def test_zol_partial_image_failure_is_not_reported_as_complete(self):
        platform = ZOLPlatform()
        platform.page = FakePage("iframe")
        platform.simulator.random_delay = AsyncMock()
        platform._upload_image = AsyncMock(side_effect=[
            {"success": True, "filename": "one.png"},
            {"success": False, "error_code": "ZOL_IMAGE_UPLOAD_VERIFY_FAILED", "error": "数量未增加"},
        ])
        result = asyncio.run(platform.fill_content([
            {"type": "text", "text": "正文段"},
            {"type": "image", "position": 1},
            {"type": "image", "position": 2},
        ], [
            {"position_index": 1, "local_path": "D:/test/one.png"},
            {"position_index": 2, "local_path": "D:/test/two.png"},
        ]))
        self.assertEqual(result["media_status"], "partial")
        self.assertEqual(result["media_error_code"], "ZOL_IMAGES_PARTIAL")
        self.assertEqual(result["uploaded_images"], 1)
        self.assertEqual(len(result["failed_images"]), 1)


if __name__ == "__main__":
    unittest.main()
