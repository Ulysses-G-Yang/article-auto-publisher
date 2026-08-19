"""平台自动化抽象基类 —— 使用系统 Chrome 浏览器，Cookie 天然持久化"""
import os
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
from platforms.media_progress import safe_media_progress


class PlatformAutomationError(RuntimeError):
    """带稳定错误码的平台自动化异常。"""

    error_code = "PLATFORM_ERROR"


class BrowserLifecycleError(PlatformAutomationError):
    """页面、Context 或 Browser 已关闭，当前尝试不能继续。"""

    error_code = "BROWSER_CONTEXT_CLOSED"


class PlatformAccessError(PlatformAutomationError):
    """已经打开平台页面，但没有进入目标业务页面。"""

    error_code = "PLATFORM_ACCESS_ERROR"


class LoginRequiredError(PlatformAutomationError):
    """当前会话需要用户重新登录。"""

    error_code = "LOGIN_REQUIRED"


class SelectorError(PlatformAutomationError):
    """页面仍然可用，但目标编辑器控件不存在或无法验证。"""

    error_code = "SELECTOR_ERROR"


class BasePlatform(ABC):
    """平台自动化基类，使用系统 Chrome 浏览器"""

    platform_name: str = ""

    def __init__(
        self,
        *,
        profile_dir: str | os.PathLike[str] | None = None,
        strict_profile_lock: bool = False,
    ):
        self.cfg = get_config()
        self.platform_cfg = self.cfg["platforms"].get(self.platform_name, {})
        self.simulator = HumanSimulator()
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.profile_dir = Path(profile_dir).resolve() if profile_dir else None
        self.strict_profile_lock = strict_profile_lock

    @staticmethod
    def _exception_means_browser_closed(exc: Exception) -> bool:
        """识别 Playwright 的页面生命周期错误，避免把它当普通失败重试。"""
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "target page, context or browser has been closed",
                "page has been closed",
                "context has been closed",
                "browser has been closed",
                "target closed",
            )
        )

    def _require_page_alive(self, stage: str = ""):
        """所有滚动/输入/点击前的统一页面生命周期检查。"""
        page = self.page
        if page is None:
            raise BrowserLifecycleError(f"BROWSER_CONTEXT_CLOSED: 页面不存在，阶段={stage}")

        try:
            is_closed = getattr(page, "is_closed", None)
            if callable(is_closed) and is_closed():
                raise BrowserLifecycleError(
                    f"BROWSER_CONTEXT_CLOSED: 页面已关闭，阶段={stage}"
                )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    f"BROWSER_CONTEXT_CLOSED: 无法读取页面状态，阶段={stage}"
                ) from exc

        if self.context is not None:
            try:
                # 访问 pages 会触发 Playwright Context 生命周期检查。
                _ = self.context.pages
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        f"BROWSER_CONTEXT_CLOSED: 浏览器上下文已关闭，阶段={stage}"
                    ) from exc

    async def _safe_simulate_scroll(self, scroll_times: int = None, stage: str = "滚动"):
        """安全执行人类化滚动；页面关闭时只抛一次标准生命周期错误。"""
        self._require_page_alive(stage)
        try:
            await self.simulator.simulate_scroll(self.page, scroll_times=scroll_times)
            self._require_page_alive(stage)
        except PlatformAutomationError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    f"BROWSER_CONTEXT_CLOSED: 滚动时页面已关闭，阶段={stage}"
                ) from exc
            raise

    async def _safe_random_mouse_movement(self, stage: str = "鼠标移动"):
        """安全执行人类化鼠标移动。"""
        self._require_page_alive(stage)
        try:
            await self.simulator.random_mouse_movement(self.page)
            self._require_page_alive(stage)
        except PlatformAutomationError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    f"BROWSER_CONTEXT_CLOSED: 鼠标移动时页面已关闭，阶段={stage}"
                ) from exc
            raise

    async def initialize(self):
        """使用系统 Chrome 浏览器初始化，每个平台独立用户数据目录"""
        if "NODE_OPTIONS" in os.environ:
            del os.environ["NODE_OPTIONS"]

        # 每个平台独立的 Chrome 用户数据目录，Cookie 自动持久化
        chrome_profile_dir = self.profile_dir or Path(
            self.cfg["paths"].get(
                "data",
                os.path.join(os.path.dirname(__file__), "..", "data"),
            )
        ) / "chrome_profiles" / self.platform_name
        chrome_profile_dir = chrome_profile_dir.resolve()
        if self.strict_profile_lock and not chrome_profile_dir.is_dir():
            raise PlatformAutomationError(
                "PROFILE_NOT_FOUND: 账号浏览器 Profile 不存在"
            )
        chrome_profile_dir.mkdir(parents=True, exist_ok=True)

        # Singleton 既可能是异常残留，也可能是活动 Chrome 的占用凭据。
        # 无论新旧入口都不能擅自删除；占用者退出后由 Chrome 自行清理。
        singleton_names = ("SingletonLock", "SingletonCookie", "SingletonSocket")
        occupied = [
            str(chrome_profile_dir / name)
            for name in singleton_names
            if (chrome_profile_dir / name).exists()
        ]
        if occupied:
            raise PlatformAutomationError(
                "PROFILE_IN_USE: 账号浏览器 Profile 正在被其他流程占用"
            )

        # 严格模式必须先完成目录和占用检查，再启动 Playwright 驱动，避免
        # PROFILE_IN_USE 分支遗留无主进程。
        self.playwright = await async_playwright().start()

        # 使用系统 Chrome 浏览器，带重试机制
        last_error = None
        max_attempts = 1 if self.strict_profile_lock else 3
        for attempt in range(max_attempts):
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(chrome_profile_dir),
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
                if self.strict_profile_lock:
                    raise PlatformAutomationError(
                        f"PROFILE_IN_USE: 无法安全打开账号 Profile: {e}"
                    ) from e
                # 旧入口保留有限重试，但同样不删除 Chrome 占用凭据。
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
                      auto_login: bool = True,
                      delivery_mode: str | None = None) -> dict:
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
                        login_error = getattr(self, "last_login_error", "") or (
                            f"{self.platform_name} 登录后验证失败"
                        )
                        login_code = login_error.split(":", 1)[0] if ":" in login_error else "LOGIN_REQUIRED"
                        return {
                            "success": False,
                            "error_code": login_code,
                            "error": login_error,
                            "need_login": login_code == "LOGIN_REQUIRED",
                        }
                else:
                    login_error = getattr(self, "last_login_error", "") or (
                        f"{self.platform_name} 未登录，请前往账号页完成扫码登录后再发布"
                    )
                    login_code = login_error.split(":", 1)[0] if ":" in login_error else "LOGIN_REQUIRED"
                    return {
                        "success": False,
                        "error_code": login_code,
                        "error": login_error,
                        "need_login": login_code == "LOGIN_REQUIRED",
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
            content_result = await self.fill_content(content_blocks, images)
            if not isinstance(content_result, dict):
                content_result = {
                    "text_ok": True,
                    "media_status": "not_checked",
                    "expected_images": 0,
                    "uploaded_images": 0,
                    "failed_images": [],
                }
            if not content_result.get("text_ok", True):
                return {
                    "success": False,
                    "error_code": "CONTENT_NOT_VERIFIED",
                    "error": content_result.get("text_error", "正文写入后验证失败"),
                }
            await self.simulator.random_delay(1, 3)

            # 5. 选择平台分类。小黑盒没有候选时不伪造结果，
            # 但基础文字草稿仍可保存，并在任务上标记 completed_with_warnings。
            selection = {}
            selection_status = "not_required"
            selection_error = None
            selection_error_code = None
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
                    if selection_result.get("needs_selection"):
                        # 即使话题失败，也保留已经成功选择的社区，供人工恢复时复用。
                        selection = selection_result.get("selection") or {}
                        selection_status = "needs_selection"
                        selection_error = selection_result.get("error", "社区或话题未完成选择")
                        selection_error_code = selection_result.get("error_code") or "SELECTION_REQUIRED"
                        db.add_task_log(task_id, "WARN", f"附加选择未完成，继续保存基础草稿: {selection_error}")
                    else:
                        return selection_result
                else:
                    selection = selection_result.get("selection") or {}
                    selection_status = selection_result.get(
                        "selection_status",
                        "completed" if selection else "not_required",
                    )
                    await self.simulator.random_delay()

            # 6. 模拟滚动检查。页面可能在选择弹窗、平台跳转或用户操作时关闭，
            # 必须先检查生命周期，不能再调用 page.mouse.wheel 触发重复错误。
            await self._safe_simulate_scroll(stage="发布前滚动检查")
            await self._safe_random_mouse_movement(stage="发布前鼠标检查")

            # 7. 保存草稿
            db.add_task_log(task_id, "INFO", "保存草稿...")
            draft_url = await self.save_draft(title)
            await self.simulator.random_delay(1, 2)

            # 草稿未真正保存（save_draft 返回空串）→ 如实报告失败，杜绝假成功
            if not draft_url:
                return {
                    "success": False,
                    "error_code": "DRAFT_NOT_VERIFIED",
                    "error": "草稿保存失败：未找到保存按钮或草稿箱未出现该草稿，请检查编辑器页面状态与登录态",
                }

            # 本轮回归默认只保存草稿，避免验证时误公开发布；未来需要公开发布时
            # 可显式打开 app.publish_after_draft 配置。
            post_url = ""
            should_publish = (
                delivery_mode == "PUBLISH"
                if delivery_mode is not None
                else self.cfg.get("app", {}).get("publish_after_draft", False)
            )
            if should_publish:
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
                "selection_status": selection_status,
                "selection_error": selection_error,
                "selection_error_code": selection_error_code,
                "media_status": content_result.get("media_status", "not_checked"),
                "expected_images": content_result.get("expected_images", 0),
                "uploaded_images": content_result.get("uploaded_images", 0),
                "failed_images": content_result.get("failed_images", []),
                "media_error": content_result.get("media_error"),
                "media_error_code": content_result.get("media_error_code"),
            }

        except Exception as e:
            logger.exception("{} 发布流水线失败: {}", self.platform_name, e)
            result = {
                "success": False,
                "error": str(e),
                "error_code": getattr(e, "error_code", None),
            }
            progress = safe_media_progress(getattr(e, "media_progress", None))
            if progress is not None:
                result["media_progress"] = progress
            return result
