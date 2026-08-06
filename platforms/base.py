"""平台自动化抽象基类 —— 使用系统 Chrome 浏览器，Cookie 天然持久化"""
import os
import json
import random
import asyncio
from abc import ABC, abstractmethod
from typing import Optional
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from loguru import logger

from human.simulator import HumanSimulator
from models.database import Database
from config import get_config


class BasePlatform(ABC):
    """平台自动化基类，使用系统 Chrome 浏览器"""

    platform_name: str = ""

    def __init__(self):
        self.cfg = get_config()
        self.platform_cfg = self.cfg["platforms"].get(self.platform_name, {})
        self.simulator = HumanSimulator()
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def initialize(self):
        """使用系统 Chrome 浏览器初始化，每个平台独立用户数据目录"""
        if "NODE_OPTIONS" in os.environ:
            del os.environ["NODE_OPTIONS"]

        self.playwright = await async_playwright().start()

        # 每个平台独立的 Chrome 用户数据目录，Cookie 自动持久化
        chrome_profile_dir = os.path.join(
            self.cfg["paths"].get("data", os.path.join(os.path.dirname(__file__), "..", "data")),
            "chrome_profiles",
            self.platform_name,
        )
        os.makedirs(chrome_profile_dir, exist_ok=True)

        # 清理 Chrome 残留的 SingletonLock（上次异常退出时遗留）
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_path = os.path.join(chrome_profile_dir, lock_file)
            try:
                if os.path.exists(lock_path):
                    os.remove(lock_path)
            except Exception:
                pass

        # 使用系统 Chrome 浏览器，带重试机制
        last_error = None
        for attempt in range(3):
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=chrome_profile_dir,
                    channel="chrome",
                    headless=False,
                    viewport={"width": 1366, "height": 900},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--no-proxy-server",          # 不走系统代理
                        "--disable-features=IsolateOrigins,site-per-process",
                    ],
                )
                break
            except Exception as e:
                last_error = e
                # 清理 lock 重试
                for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
                    lock_path = os.path.join(chrome_profile_dir, lock_file)
                    try:
                        if os.path.exists(lock_path):
                            os.remove(lock_path)
                    except Exception:
                        pass
                await asyncio.sleep(3)
        else:
            raise last_error

        # 注入反检测脚本
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
            window.chrome = { runtime: {} };
        """)

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def cleanup(self):
        """关闭浏览器"""
        if self.context:
            await self.context.close()
        # 注意：不关闭 browser，因为 persistent context 会自己管理
        if self.playwright:
            await self.playwright.stop()

    async def save_cookies(self):
        """Cookie 已由 Chrome 用户数据目录自动管理，此方法仅保留接口兼容"""
        # 使用 persistent context 后，Cookie 自动保存在 Chrome 的 profile 中
        # 无需手动序列化
        pass

    def _random_ua(self) -> str:
        """随机 User-Agent（persistent context 使用 Chrome 默认 UA，此方法备用）"""
        ua_pool = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        ]
        return random.choice(ua_pool)

    # ==================== 流水线接口 ====================

    @abstractmethod
    async def check_login(self) -> bool:
        """检查登录状态"""
        ...

    @abstractmethod
    async def login(self):
        """执行登录（打开系统 Chrome，等待手动登录）"""
        ...

    @abstractmethod
    async def navigate_to_editor(self):
        """导航到编辑器页面"""
        ...

    @abstractmethod
    async def fill_title(self, title: str):
        """填写标题"""
        ...

    @abstractmethod
    async def fill_content(self, content_blocks: list, images: list):
        """填写正文内容（文字 + 图片）"""
        ...

    @abstractmethod
    async def select_topic(self, topic: str = "", community: str = "",
                           selection_query: str = "", selection_override: dict = None):
        """选择话题/分类"""
        ...

    @abstractmethod
    async def save_draft(self, title: str = "") -> str:
        """保存草稿，返回草稿URL；保存失败必须返回空串，绝不能以当前页URL冒充成功"""
        ...

    async def publish_now(self, title: str = "") -> str:
        """真正发布文章，返回发布后的文章URL；不支持/失败返回空串。
        默认实现不执行任何操作（草稿保存即视为完成），各平台可覆盖。"""
        return ""

    # ==================== 完整发布流水线 ====================

    async def publish(self, title: str, content_blocks: list,
                      images: list, topic: str = "",
                      community: str = "", selection_query: str = "",
                      selection_override: dict = None,
                      task_id: int = 0, db: Database = None,
                      auto_login: bool = True) -> dict:
        """执行完整发布流水线

        auto_login=False 时（用于后台队列 worker）：若未登录直接优雅失败，
        并提示用户去账号页手动扫码登录，避免 worker 与手动登录争夺同一
        Chrome profile 锁导致 launch 失败。
        """
        if db is None:
            db = Database.get_instance()
        try:
            # 1. 检查登录
            if not await self.check_login():
                if auto_login:
                    db.add_task_log(task_id, "INFO", "未登录，打开浏览器等待手动登录...")
                    await self.login()
                    db.add_task_log(task_id, "INFO", "登录完成")
                    if not await self.check_login():
                        return {
                            "success": False,
                            "error": f"{self.platform_name} 登录后验证失败",
                            "need_login": True,
                        }
                else:
                    return {
                        "success": False,
                        "error": f"{self.platform_name} 未登录，请前往账号页完成扫码登录后再发布",
                        "need_login": True,
                    }

            # 2. 导航到编辑器
            db.add_task_log(task_id, "INFO", "打开编辑器...")
            await self.navigate_to_editor()
            await self.simulator.random_delay(1, 3)

            # 3. 填写标题
            db.add_task_log(task_id, "INFO", f"填写标题: {title}")
            await self.fill_title(title)
            await self.simulator.random_delay()

            # 4. 填写正文
            db.add_task_log(task_id, "INFO", "填写正文内容...")
            await self.fill_content(content_blocks, images)
            await self.simulator.random_delay(1, 3)

            # 5. 选择平台分类。小黑盒即使没有缓存话题，也必须走实时搜索；
            # 选择失败会返回 needs_selection，不能继续保存成假成功。
            selection = {}
            should_select = bool(topic or community or selection_query or self.platform_name == "xiaoheihe")
            if should_select:
                db.add_task_log(
                    task_id,
                    "INFO",
                    f"选择社区/话题: community={community or '-'}, topic={topic or '-'}",
                )
                selection_result = await self.select_topic(
                    topic=topic,
                    community=community,
                    selection_query=selection_query,
                    selection_override=selection_override or {},
                )
                if selection_result is None:
                    selection_result = {"success": True}
                if not selection_result.get("success", False):
                    return selection_result
                selection = selection_result.get("selection") or {}
                await self.simulator.random_delay()

            # 6. 模拟滚动检查
            await self.simulator.simulate_scroll(self.page)
            await self.simulator.random_mouse_movement(self.page)

            # 7. 保存草稿
            db.add_task_log(task_id, "INFO", "保存草稿...")
            draft_url = await self.save_draft(title)
            await self.simulator.random_delay(1, 2)

            # 草稿未真正保存（save_draft 返回空串）→ 如实报告失败，杜绝假成功
            if not draft_url:
                return {
                    "success": False,
                    "error": "草稿保存失败：未找到保存按钮或草稿箱未出现该草稿，请检查编辑器页面状态与登录态",
                }

            # 本轮回归默认只保存草稿，避免验证时误公开发布；未来需要公开发布时
            # 可显式打开 app.publish_after_draft 配置。
            post_url = ""
            if self.cfg.get("app", {}).get("publish_after_draft", False):
                db.add_task_log(task_id, "INFO", "提交发布...")
                post_url = await self.publish_now(title)
                await self.simulator.random_delay(1, 2)
            else:
                db.add_task_log(task_id, "INFO", "按当前配置仅保存草稿，未公开发布")

            return {
                "success": True,
                "draft_url": draft_url,
                "post_url": post_url,
                "selection": selection,
                "selection_status": "completed" if selection else "not_required",
            }

        except Exception as e:
            logger.exception("{} 发布流水线失败: {}", self.platform_name, e)
            return {"success": False, "error": str(e)}
