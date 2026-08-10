"""异步任务队列管理器"""
import asyncio
import json
import traceback
from datetime import datetime
from typing import Optional, Callable
from enum import Enum

from loguru import logger

from models.database import Database
from core.docx_parser import DocxParser, ParsedArticle, ContentBlock
from core.nlp_analyzer import NLPAnalyzer
from core.platform_guard import release as release_platform, try_acquire as try_acquire_platform
from platforms.zol import ZOLPlatform
from platforms.xiaoheihe import XiaoheihePlatform


class TaskStatus(Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    RETRYING = "retrying"
    PAUSED = "paused"
    NEEDS_SELECTION = "needs_selection"
    CANCELLED = "cancelled"


class TaskExecutionError(RuntimeError):
    """把平台返回的错误码传给队列重试策略。"""

    def __init__(self, message: str, error_code: str = None):
        super().__init__(message)
        self.error_code = error_code or "TASK_ERROR"


MANUAL_RECOVERABLE_CODES = {
    "BROWSER_CONTEXT_CLOSED",
    "ZOL_BLOG_EDITOR_REDIRECT",
    "ZOL_EDITOR_ROUTE_ERROR",
    "ZOL_SECURITY_CHALLENGE",
    "PLATFORM_BLOCKED",
    "SELECTOR_ERROR",
    "DRAFT_NOT_VERIFIED",
}


def normalize_selection_query(value, fallback: str = "", limit: int = 3) -> str:
    """把文章关键词字段转换成平台搜索可用的纯文本。"""
    if isinstance(value, (list, tuple)):
        words = []
        for item in value:
            if isinstance(item, dict):
                word = str(item.get("word") or "").strip()
            else:
                word = str(item or "").strip()
            if word:
                words.append(word)
        return " ".join(words[:limit]) or str(fallback or "").strip()

    raw = str(value or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, list):
            return normalize_selection_query(parsed, fallback=fallback, limit=limit)
        if isinstance(parsed, dict) and parsed.get("word"):
            return str(parsed["word"]).strip()
        if not raw.startswith(("[", "{")):
            plain = raw.replace(",", " ").replace("，", " ").replace("、", " ")
            return " ".join(plain.split())[:100] or str(fallback or "").strip()

    return str(fallback or "").strip()


def classify_task_error(exc: Exception) -> str:
    """将 Playwright/平台异常归一成有限的可诊断错误码。"""
    explicit = getattr(exc, "error_code", None)
    text = str(exc)
    upper = text.upper()

    if "BROWSER_CONTEXT_CLOSED" in upper or any(
        marker in text.lower()
        for marker in (
            "target page, context or browser has been closed",
            "page has been closed",
            "context has been closed",
            "browser has been closed",
            "target closed",
        )
    ):
        return "BROWSER_CONTEXT_CLOSED"
    if "ZOL_BLOG_EDITOR_REDIRECT" in upper:
        return "ZOL_BLOG_EDITOR_REDIRECT"
    if "ZOL_EDITOR_ROUTE_ERROR" in upper:
        return "ZOL_EDITOR_ROUTE_ERROR"
    if any(marker in upper for marker in ("SECURITY_CHALLENGE", "验证码", "风控", "安全验证")):
        return "ZOL_SECURITY_CHALLENGE" if "ZOL" in upper else "PLATFORM_BLOCKED"
    if "LOGIN_REQUIRED" in upper or "未登录" in text:
        return "LOGIN_REQUIRED"
    if "NEEDS_SELECTION" in upper or "没有找到社区" in text or "没有找到话题" in text:
        return "SELECTION_REQUIRED"
    if any(marker in upper for marker in (
        "SELECTOR_ERROR", "VALIDATION_ERROR", "验证失败", "未找到", "找不到",
        "OUTSIDE OF THE VIEWPORT", "ELEMENT IS OUTSIDE",
    )):
        return "SELECTOR_ERROR"
    if "DRAFT_NOT_VERIFIED" in upper or "草稿保存失败" in text or "草稿箱未找到" in text:
        return "DRAFT_NOT_VERIFIED"
    if explicit and explicit != "PLATFORM_ERROR":
        return explicit
    if any(marker in upper for marker in ("NET::ERR", "NETWORK", "CONNECTION RESET")):
        return "NETWORK_TRANSIENT"
    return "TASK_ERROR"


class QueueManager:
    """异步任务队列，管理批量文章的导入和发布"""

    def __init__(self):
        self.db = Database.get_instance()
        self.parser = DocxParser()
        self.nlp = NLPAnalyzer()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.running = False
        self._worker_task: Optional[asyncio.Task] = None

        # 平台实例（延迟初始化，因为需要 Playwright）
        self._platforms = {
            "zol": None,
            "xiaoheihe": None,
        }
        self._browser = None
        self._contexts = {}

    async def start(self):
        """启动队列 worker"""
        if self.running:
            return
        self.running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("队列管理器已启动")

    async def stop(self):
        """停止队列 worker"""
        self.running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        await self._cleanup_browser()
        logger.info("队列管理器已停止")

    async def _worker(self):
        """后台 worker：循环处理队列中的任务"""
        while self.running:
            try:
                # 只处理本次运行明确入队的任务；历史任务由服务启动流程暂停，
                # 必须通过 /resume 显式恢复，避免重启时自动打开旧平台页面。
                try:
                    task_id = await asyncio.wait_for(self.queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

                await self._process_task(task_id)
                self.queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker 异常: {e}")
                await asyncio.sleep(5)

    async def _process_task(self, task_id: int):
        """处理单个任务"""
        task = self.db.get_task(task_id)
        if not task:
            return

        if task.get("status") not in ("queued", "retrying"):
            logger.info("跳过非可执行任务 {}: {}", task_id, task.get("status"))
            return

        article = self.db.get_article(task["article_id"])
        if not article:
            self.db.update_task(task_id, status="failed", error_message="文章不存在")
            return

        platform_name = task["platform"]
        platform_claimed = try_acquire_platform(platform_name)
        if not platform_claimed:
            message = "PLATFORM_BUSY: 当前平台正在登录或执行其他任务，已暂停等待人工恢复"
            self.db.update_task(
                task_id,
                status="paused",
                error_code="PLATFORM_BUSY",
                error_message=message,
            )
            self.db.add_task_log(task_id, "WARN", message)
            return
        self.db.update_task(task_id, status="processing", started_at=datetime.now().isoformat())
        self.db.add_task_log(task_id, "INFO", f"开始处理: {platform_name}")

        try:
            platform = await self._get_platform(platform_name)
            if platform is None:
                raise Exception(f"平台 {platform_name} 未初始化")

            # 获取文章内容和图片
            content_json = json.loads(article["content_json"])
            images = self.db.get_article_images(article["id"])

            # 标题处理
            title_raw = article.get("title") or ""
            if not title_raw:
                # 从文章内容中提取标题
                article_obj = ParsedArticle(
                    filename=article["filename"],
                    original_path=article["original_path"],
                    blocks=[ContentBlock(type="text", text=article.get("content_text", ""))],
                    plain_text=article.get("content_text", ""),
                )
                title_raw = self.parser.get_title_from_article(article_obj)

            title = self.nlp.generate_title(
                article.get("content_text", ""), title_raw, platform_name
            )

            # ZOL 使用缓存/手动话题；小黑盒使用文章关键词进行编辑器实时搜索。
            topic = article.get(f"topic_{platform_name}", "")
            selection_override = {}
            if task.get("selection_json"):
                try:
                    selection_override = json.loads(task["selection_json"]) or {}
                except (TypeError, json.JSONDecodeError):
                    selection_override = {}
            community = (
                selection_override.get("community")
                or task.get("community_used")
                or article.get("community_xiaoheihe", "")
                or ""
            )
            if platform_name == "xiaoheihe":
                topic = selection_override.get("topic") or task.get("topic_used") or topic

            # 执行发布流水线（worker 模式下不自动弹出登录窗口，避免与手动登录抢锁）
            result = await platform.publish(
                title=title,
                content_blocks=content_json.get("blocks", []),
                images=images,
                topic=topic,
                community=community,
                selection_query=normalize_selection_query(article.get("keywords", ""), fallback=title),
                selection_override=selection_override,
                task_id=task_id,
                db=self.db,
                auto_login=False,
            )

            if result.get("success"):
                selection = result.get("selection") or {}
                selected_topic = selection.get("topic") or topic
                selected_community = selection.get("community") or community
                selection_status = result.get(
                    "selection_status",
                    "completed" if selection else "not_required",
                )
                media_status = result.get("media_status", "not_checked")
                warnings = []
                if selection_status == "needs_selection":
                    warnings.append(result.get("selection_error") or "社区/话题未完成选择")
                if media_status in ("failed", "partial"):
                    warnings.append(result.get("media_error") or "图片未全部上传")
                # 话题/社区未完成时，基础草稿可以保留，但任务必须停在
                # needs_selection，不能被任务页或后续流程当成完整成功。
                if selection_status == "needs_selection":
                    task_status = "needs_selection"
                else:
                    task_status = "completed_with_warnings" if warnings else "completed"
                if selection_status == "needs_selection" and media_status in ("failed", "partial"):
                    warning_code = "PARTIAL_METADATA"
                elif selection_status == "needs_selection":
                    warning_code = "SELECTION_REQUIRED"
                elif media_status == "failed":
                    warning_code = "IMAGES_ALL_FAILED"
                elif media_status == "partial":
                    warning_code = "PARTIAL_IMAGES"
                else:
                    warning_code = None
                self.db.update_task(
                    task_id,
                    status=task_status,
                    title_used=title,
                    topic_used=selected_topic,
                    community_used=selected_community,
                    selection_status=selection_status,
                    selection_json=json.dumps(selection, ensure_ascii=False) if selection else None,
                    error_code=warning_code,
                    error_message="；".join(warnings) if warnings else None,
                    draft_url=result.get("draft_url", ""),
                    media_status=media_status,
                    expected_images=result.get("expected_images", 0),
                    uploaded_images=result.get("uploaded_images", 0),
                    failed_images_json=(
                        json.dumps(result.get("failed_images") or [], ensure_ascii=False)
                        if result.get("failed_images") else None
                    ),
                    completed_at=datetime.now().isoformat(),
                )
                if warnings:
                    self.db.add_task_log(
                        task_id,
                        "WARN",
                        f"基础草稿保存成功，但附加内容未完成: {'；'.join(warnings)}",
                    )
                else:
                    self.db.add_task_log(task_id, "INFO", f"草稿保存成功: {result.get('draft_url', '')}")
                # 发布成功说明登录态有效，回写账号状态（修复账号页长期显示 login_failed 的问题）
                self.db.upsert_account(
                    platform_name,
                    status="logged_in",
                    last_login_time=datetime.now().isoformat(),
                )
                logger.info("任务 {} 完成: status={}", task_id, task_status)
            elif result.get("need_login"):
                # 需要重新登录：直接标记失败，提示去账号页登录，不再无谓重试
                error_msg = result.get("error", "未登录")
                self.db.update_task(
                    task_id,
                    status="failed",
                    retry_count=task["retry_count"],
                    error_code="LOGIN_REQUIRED",
                    error_message=error_msg,
                )
                self.db.upsert_account(
                    platform_name,
                    status="logged_out",
                    login_stage="needs_login",
                    login_error=error_msg,
                )
                self.db.add_task_log(task_id, "ERROR", error_msg)
                logger.error(f"任务 {task_id} 失败: {error_msg}")
            elif result.get("needs_selection"):
                error_msg = result.get("error", "社区或话题需要手动选择")
                self.db.update_task(
                    task_id,
                    status="needs_selection",
                    selection_status="required",
                    error_code="SELECTION_REQUIRED",
                    error_message=error_msg,
                )
                self.db.add_task_log(task_id, "WARN", error_msg)
                logger.warning("任务 {} 需要手动选择: {}", task_id, error_msg)
            else:
                raise TaskExecutionError(
                    result.get("error", "未知错误"),
                    result.get("error_code") or "TASK_ERROR",
                )

        except Exception as e:
            error_msg = str(e)
            error_code = classify_task_error(e)
            retry_count = task["retry_count"] + 1
            max_retries = 3

            if error_code in MANUAL_RECOVERABLE_CODES:
                message = f"[{error_code}] {error_msg}；已暂停，请处理后手动恢复"
                self.db.update_task(
                    task_id,
                    status="paused",
                    retry_count=retry_count,
                    error_code=error_code,
                    error_message=message,
                )
                self.db.add_task_log(task_id, "WARN", message)
                logger.warning("任务 {} 暂停等待人工处理: {}", task_id, message)
            elif retry_count < max_retries:
                self.db.update_task(
                    task_id,
                    status="retrying",
                    retry_count=retry_count,
                    error_code=error_code,
                    error_message=error_msg,
                )
                self.db.add_task_log(task_id, "WARN", f"[{error_code}] 重试 {retry_count}/{max_retries}: {error_msg}")
                await asyncio.sleep(10 * retry_count)
                self.queue.put_nowait(task_id)
            else:
                self.db.update_task(
                    task_id,
                    status="failed",
                    retry_count=retry_count,
                    error_code=error_code,
                    error_message=error_msg,
                )
                self.db.add_task_log(task_id, "ERROR", f"[{error_code}] 最终失败: {error_msg}")
                logger.error("任务 {} 失败: [{}] {}", task_id, error_code, error_msg)

        finally:
            # 每个任务完成后必须清理浏览器，避免 Chrome SingletonLock 冲突
            try:
                if platform_name in self._platforms and self._platforms[platform_name]:
                    try:
                        await self._platforms[platform_name].cleanup()
                        self.db.add_task_log(task_id, "INFO", "浏览器已关闭")
                    except Exception as cleanup_error:
                        logger.warning("任务 {} 浏览器清理失败: {}", task_id, cleanup_error)
                    self._platforms[platform_name] = None
                    # 等待 Chrome 完全释放资源
                    await asyncio.sleep(3)
            finally:
                if platform_claimed:
                    release_platform(platform_name)

    async def _get_platform(self, name: str):
        """获取或初始化平台实例"""
        if self._platforms.get(name) is None:
            if name == "zol":
                plat = ZOLPlatform()
            elif name == "xiaoheihe":
                plat = XiaoheihePlatform()
            else:
                return None

            await plat.initialize()
            self._platforms[name] = plat

        return self._platforms[name]

    async def _cleanup_browser(self):
        """清理所有浏览器资源"""
        for name, plat in self._platforms.items():
            if plat:
                try:
                    await plat.cleanup()
                except Exception:
                    pass
        self._platforms = {}

    def enqueue(self, task_id: int):
        """将任务加入队列"""
        self.queue.put_nowait(task_id)

    def get_queue_status(self) -> dict:
        """获取队列状态"""
        return {
            "queue_size": self.queue.qsize(),
            "running": self.running,
            "pending": len(self.db.get_pending_tasks(limit=100)),
        }


# 全局单例
_queue_manager: Optional[QueueManager] = None


def get_queue_manager() -> QueueManager:
    global _queue_manager
    if _queue_manager is None:
        _queue_manager = QueueManager()
    return _queue_manager
