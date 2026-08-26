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
from platforms.content_validation import safe_media_error
from platforms.media_progress import safe_media_progress


class DraftVerificationEvidence:
    """草稿保存证据链（可观测性）。

    记录 save_draft() 内部各步骤的验证结果，供执行单响应返回给前端，
    让业务人员不依赖终端日志即可判断"草稿是否真的存在"。

    字段语义：None = 未执行到该步骤；bool/int = 该步骤验证结果。
    纯数据结构，不携带原始异常文本、Profile 路径、Cookie、Token 或正文。
    """

    def __init__(self) -> None:
        self.save_response_2xx: bool | None = None
        self.save_http_status: int | None = None
        self.save_platform_code: str | None = None
        self.draft_list_title_unique: bool | None = None
        self.draft_list_match_count: int | None = None
        self.reopen_title_match: bool | None = None
        self.reopen_dom_blocks_match: bool | None = None
        self.draft_url: str | None = None
        # 标题匹配不是本次副作用的证明。同名草稿允许存在，只有适配器已经
        # 用保存响应 ID 或保存前后实体差集绑定本次实体时才写 True。
        self.draft_entity_bound: bool | None = None
        self.draft_entity_source: str | None = None
        self.draft_entity_id_match: bool | None = None
        self.unknown: bool = False
        self.summary: str | None = None

    def mark_save_response(self, *, status: int | None, code: str | None = None) -> None:
        """记录保存接口响应（多次自动保存以最后一次为准）。"""
        if status is not None:
            self.save_http_status = int(status)
            self.save_response_2xx = 200 <= self.save_http_status < 300
        if code is not None:
            self.save_platform_code = str(code)

    def mark_draft_list(self, match_count: int | None) -> None:
        """记录草稿箱标题精确匹配数。"""
        if match_count is not None:
            self.draft_list_match_count = int(match_count)
            self.draft_list_title_unique = self.draft_list_match_count == 1

    def mark_reopen(self, *, title_match: bool | None, dom_blocks_match: bool | None) -> None:
        """记录重开草稿后的标题与 DOM 结构核验。"""
        if title_match is not None:
            self.reopen_title_match = bool(title_match)
        if dom_blocks_match is not None:
            self.reopen_dom_blocks_match = bool(dom_blocks_match)

    def set_draft_url(self, url: str | None) -> None:
        if url:
            self.draft_url = str(url)

    def mark_entity_binding(
        self,
        *,
        bound: bool,
        source: str,
        id_match: bool | None = None,
    ) -> None:
        """记录本次草稿实体绑定结果，只接受受控来源标签。"""

        allowed_sources = {
            "save_response_id",
            "existing_draft_id",
            "baseline_new_id",
            "title_match_without_baseline",
        }
        self.draft_entity_bound = bool(bound)
        self.draft_entity_source = source if source in allowed_sources else "unknown"
        if id_match is not None:
            self.draft_entity_id_match = bool(id_match)

    def finalize(self, *, error_code: str | None = None) -> "DraftVerificationEvidence":
        """生成脱敏 summary；在 raise/return 前调用，此时证据已完整。"""
        self.unknown = bool(error_code and "UNKNOWN" in str(error_code).upper())
        self.summary = self._build_summary()
        return self

    def _build_summary(self) -> str:
        parts: list[str] = []
        if self.save_response_2xx is False:
            parts.append("保存接口响应未捕获")
        elif self.save_response_2xx is True:
            parts.append("保存接口已返回 2xx")
        if self.draft_list_title_unique is True:
            parts.append("草稿箱存在标题唯一匹配的草稿")
        elif self.draft_list_title_unique is False:
            parts.append(f"草稿箱标题匹配数为 {self.draft_list_match_count}")
        if self.reopen_title_match is True:
            parts.append("重开后标题一致")
        elif self.reopen_title_match is False:
            parts.append("重开后标题不一致")
        if self.reopen_dom_blocks_match is False:
            parts.append("重开后图文结构与冻结版本不一致")
        if self.draft_entity_bound is True:
            parts.append("本次草稿实体已绑定")
        elif self.draft_entity_bound is False:
            parts.append("本次草稿实体未能绑定")
        if self.draft_url:
            parts.append("已取得草稿链接")
        if not parts:
            parts.append("未记录到可核验证据")
        return "；".join(parts)

    def to_dict(self) -> dict:
        if self.summary is None:
            self.summary = self._build_summary()
        return {
            "save_response_2xx": self.save_response_2xx,
            "save_http_status": self.save_http_status,
            "save_platform_code": self.save_platform_code,
            "draft_list_title_unique": self.draft_list_title_unique,
            "draft_list_match_count": self.draft_list_match_count,
            "reopen_title_match": self.reopen_title_match,
            "reopen_dom_blocks_match": self.reopen_dom_blocks_match,
            "draft_url": self.draft_url,
            "draft_entity_bound": self.draft_entity_bound,
            "draft_entity_source": self.draft_entity_source,
            "draft_entity_id_match": self.draft_entity_id_match,
            "unknown": self.unknown,
            "summary": self.summary,
        }


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


class DraftBaselineError(PlatformAutomationError):
    """保存前无法建立可靠草稿基线，禁止继续输入或点击保存。"""

    error_code = "DRAFT_BASELINE_UNAVAILABLE"

    def __init__(self, message: str = "", *, evidence=None):
        super().__init__(message)
        self.evidence = evidence


class DraftResultUnknownError(PlatformAutomationError):
    """保存动作可能已经发生，但平台未提供可证明的结果。"""

    error_code = "DRAFT_RESULT_UNKNOWN"

    def __init__(self, message: str = "", *, evidence=None):
        super().__init__(message)
        self.evidence = evidence


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

    async def preflight_delivery(self, title: str) -> None:
        """进入编辑器前的平台副作用预检；默认无需额外检查。"""

        return None

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

    async def verify_draft_readonly(self, title: str) -> dict:
        """只读核验：按标题在平台草稿箱查找唯一草稿并返回结构摘要。

        只读约束：全程拦截非 GET 请求；不点保存/发布/删除；不清 Cookie；
        不自动登录；失败返回错误码而非抛出（调用方按错误码处理）。

        默认实现返回 unsupported（fail-closed，不猜测选择器）；各平台
        显式覆盖后才具备核验能力。

        返回契约：
        - {"unsupported": True}                      平台未实现只读核验
        - {"title_matched": bool, "match_count": int, "draft_url": str|None,
           "structure": {...}}                       核验完成
        - {"error_code": str, "error_message": str}  核验失败（稳定错误码）
        """
        return {"unsupported": True}

    async def apply_cover(self, cover: dict | None = None) -> dict:
        """应用冻结封面；平台未实现时明确报告，不得静默忽略用户选择。"""

        strategy = str((cover or {}).get("strategy") or "NONE")
        if strategy == "NONE":
            return {"success": True, "cover_status": "not_required"}
        return {
            "success": False,
            "cover_status": "unsupported",
            "error_code": "PLATFORM_COVER_UNSUPPORTED",
            "error": f"{self.platform_name} 尚未实现封面投递",
        }

    async def verify_persisted_cover(
        self,
        *,
        title: str,
        draft_url: str,
        cover: dict | None,
        apply_result: dict,
    ) -> dict:
        """保存后核验封面。

        显式上传型平台通常在 :meth:`apply_cover` 内即可确认；自动首图或
        平台生成封面的适配器可以返回 ``pending_verification``，并在草稿
        实体已经被证明存在后覆盖本钩子。默认实现保持原结果，绝不把待核验
        状态自动晋级为成功。
        """

        return apply_result

    # ==================== 完整发布流水线 ====================

    async def publish(self, title: str, content_blocks: list,
                       images: list, topic: str = "",
                      community: str = "", selection_query: str = "",
                      selection_override: dict = None,
                       task_id: int = 0, db: Database = None,
                       auto_login: bool = True,
                       delivery_mode: str | None = None,
                       cover: dict | None = None) -> dict:
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

            # 2. 在任何编辑器自动保存副作用发生前执行平台预检。
            await self.preflight_delivery(title)

            # 3. 导航到编辑器
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

            # 6. 应用冻结封面。未实现/失败时仍允许保存正文草稿，但结果必须
            # 进入 WITH_WARNINGS，不能把“正文成功”冒充“完整 Word 成功”。
            try:
                cover_result = await self.apply_cover(cover)
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        f"BROWSER_CONTEXT_CLOSED: {self.platform_name} 设置封面时页面已关闭"
                    ) from exc
                cover_result = {
                    "success": False,
                    "cover_status": "failed",
                    "error_code": getattr(exc, "error_code", None)
                    or "PLATFORM_COVER_FAILED",
                    "error": safe_media_error(
                        str(exc),
                        fallback="平台封面设置失败",
                    ),
                }
            if not isinstance(cover_result, dict):
                cover_result = {
                    "success": False,
                    "cover_status": "failed",
                    "error_code": "PLATFORM_COVER_UNVERIFIED",
                    "error": "平台没有返回可验证的封面结果",
                }
            if not cover_result.get("success", False):
                cover_result["error"] = safe_media_error(
                    cover_result.get("error"),
                    fallback="平台封面结果未验证",
                )
                db.add_task_log(
                    task_id,
                    "WARN",
                    f"封面未完整设置: {cover_result['error']}",
                )
                if cover_result.get("safe_to_continue") is False:
                    return {
                        "success": False,
                        "error_code": cover_result.get("error_code")
                        or "PLATFORM_COVER_UI_NOT_CLEAN",
                        "error": cover_result["error"],
                        "cover_status": cover_result.get(
                            "cover_status", "unverified"
                        ),
                    }

            # 7. 模拟滚动检查。页面可能在选择弹窗、平台跳转或用户操作时关闭，
            # 必须先检查生命周期，不能再调用 page.mouse.wheel 触发重复错误。
            await self._safe_simulate_scroll(stage="发布前滚动检查")
            await self._safe_random_mouse_movement(stage="发布前鼠标检查")

            # 7. 保存草稿
            db.add_task_log(task_id, "INFO", "保存草稿...")
            degraded = None
            draft_verification_warning = False
            draft_verification_warning_message = None
            evidence_payload = self._evidence_to_dict()
            public_publish_blocked = False
            try:
                draft_url = await self.save_draft(title)
                degraded = None
                evidence_payload = self._evidence_to_dict()
            except DraftResultUnknownError as exc:
                # 降级判定：保存动作已触发但完整证据链未走通时，
                # 只有已经绑定本次实体才允许降级成功。旧实现只看标题唯一，
                # 会把旧同名草稿或错误 ID 误报为本次成功。
                evidence = getattr(exc, "evidence", None)
                ev_dict = (
                    evidence.to_dict()
                    if evidence is not None
                    else (self._evidence_to_dict() or {})
                )
                evidence_payload = ev_dict
                entity_bound = ev_dict.get("draft_entity_bound")
                if entity_bound is True:
                    draft_url = ev_dict.get("draft_url") or ""
                    # 保留既有 degraded 枚举，实体绑定细节通过证据字段表达。
                    degraded = "draft_list_confirmed"
                    draft_verification_warning = (
                        ev_dict.get("reopen_title_match") is False
                        or ev_dict.get("reopen_dom_blocks_match") is False
                    )
                    draft_verification_warning_message = (
                        "本次草稿实体已保存，但重开后的正文/图片与冻结版本不一致"
                        if draft_verification_warning
                        else None
                    )
                    db.add_task_log(
                        task_id,
                        "INFO",
                        (
                            "本次草稿实体已确认，但正文/图片完整性存在警告"
                            if draft_verification_warning
                            else "本次草稿实体已确认（完整性待核对）"
                        ),
                    )
                    # 弱证据只适用于草稿保存。公开流程必须停在保存结果，
                    # 不能把降级草稿继续送入 publish_now。
                    if delivery_mode == "PUBLISH":
                        public_publish_blocked = True
                else:
                    db.add_task_log(
                        task_id,
                        "WARN",
                        "投递未完成：草稿保存结果无法在平台确认",
                    )
                    return {
                        "success": False,
                        "error_code": "DELIVERY_INCOMPLETE",
                        "error": (
                            "投递未完成：未能在平台确认草稿保存结果，"
                            "请使用只读核验确认后手动处理"
                        ),
                        "verification_evidence": ev_dict,
                    }
            await self.simulator.random_delay(1, 2)

            # 草稿未真正保存（save_draft 返回空串且未降级）→ 报告未完成，
            # 不伪装成功；降级成功时 draft_url 可能为空，保留降级标记。
            if not draft_url and degraded is None:
                return {
                    "success": False,
                    "error_code": "DELIVERY_INCOMPLETE",
                    "error": "投递未完成：未找到保存按钮或草稿箱未出现该草稿，请使用只读核验确认",
                }

            if cover_result.get("cover_status") == "pending_verification" and draft_url:
                try:
                    persisted_cover = await self.verify_persisted_cover(
                        title=title,
                        draft_url=draft_url,
                        cover=cover,
                        apply_result=cover_result,
                    )
                    if not isinstance(persisted_cover, dict):
                        raise TypeError("平台封面核验结果必须是字典")
                    cover_result = persisted_cover
                except Exception as exc:
                    if self._exception_means_browser_closed(exc):
                        cover_result = {
                            "success": False,
                            "cover_status": "unverified",
                            "safe_to_continue": True,
                            "error_code": "PLATFORM_COVER_RESULT_UNKNOWN",
                            "error": "草稿已保存，但封面核验时浏览器已关闭",
                        }
                    else:
                        cover_result = {
                            "success": False,
                            "cover_status": "failed",
                            "safe_to_continue": True,
                            "error_code": getattr(exc, "error_code", None)
                            or "PLATFORM_COVER_PERSISTENCE_FAILED",
                            "error": safe_media_error(
                                exc,
                                fallback="草稿已保存，但封面持久化未通过核验",
                            ),
                        }
                if cover_result.get("cover_status") != "completed":
                    db.add_task_log(
                        task_id,
                        "WARN",
                        "草稿已保存，但封面持久化未通过核验",
                    )

            # 本轮回归默认只保存草稿，避免验证时误公开发布；未来需要公开发布时
            # 可显式打开 app.publish_after_draft 配置。
            post_url = ""
            should_publish = (
                delivery_mode == "PUBLISH"
                if delivery_mode is not None
                else self.cfg.get("app", {}).get("publish_after_draft", False)
            )
            # 若调用方使用旧的 delivery_mode=None 且配置误开公开发布，
            # 降级结果同样必须停在草稿；正常无 degraded 的路径不受影响。
            if public_publish_blocked or (degraded is not None and should_publish):
                return {
                    "success": False,
                    "error_code": "DELIVERY_INCOMPLETE",
                    "error": "公开发布前草稿完整性未确认，已停止发布",
                    "draft_url": draft_url,
                    "post_url": "",
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
                    "cover_strategy": str((cover or {}).get("strategy") or "NONE"),
                    "cover_status": cover_result.get("cover_status", "failed"),
                    "cover_mode": cover_result.get("cover_mode"),
                    "cover_error": cover_result.get("error"),
                    "cover_error_code": cover_result.get("error_code"),
                    "verification_evidence": evidence_payload,
                    "degraded": degraded,
                    "draft_verification_warning": draft_verification_warning,
                    "draft_verification_warning_message": draft_verification_warning_message,
                }
            if degraded is not None:
                should_publish = False
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
                "cover_strategy": str((cover or {}).get("strategy") or "NONE"),
                "cover_status": cover_result.get("cover_status", "failed"),
                "cover_mode": cover_result.get("cover_mode"),
                "cover_error": cover_result.get("error"),
                "cover_error_code": cover_result.get("error_code"),
                "verification_evidence": evidence_payload,
                "degraded": degraded,
                "draft_verification_warning": draft_verification_warning,
                "draft_verification_warning_message": draft_verification_warning_message,
            }

        except Exception as e:
            logger.exception("{} 发布流水线失败: {}", self.platform_name, e)
            result = {
                "success": False,
                "error": str(e),
                "error_code": getattr(e, "error_code", None),
            }
            evidence = getattr(e, "evidence", None)
            if evidence is not None:
                result["verification_evidence"] = evidence.to_dict()
            else:
                result["verification_evidence"] = self._evidence_to_dict()
            progress = safe_media_progress(getattr(e, "media_progress", None))
            if progress is not None:
                result["media_progress"] = progress
            return result

    def _evidence_to_dict(self) -> dict | None:
        """返回最近一次 save_draft 收集的证据（无则 None）。"""
        evidence = getattr(self, "_last_draft_evidence", None)
        if evidence is None:
            return None
        return evidence.to_dict()
