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
from platforms.zol import ZOLPlatform
from platforms.xiaoheihe import XiaoheihePlatform


class TaskStatus(Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


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
        in_queue = set()  # 防止重复入队
        while self.running:
            try:
                # 从数据库加载待处理任务（仅在队列为空时加载，避免重复）
                if self.queue.empty():
                    pending = self.db.get_pending_tasks(limit=5)
                    for task in pending:
                        tid = task["id"]
                        if tid not in in_queue:
                            self.queue.put_nowait(tid)
                            in_queue.add(tid)

                # 从队列取任务处理
                try:
                    task_id = await asyncio.wait_for(self.queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

                in_queue.discard(task_id)
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

        article = self.db.get_article(task["article_id"])
        if not article:
            self.db.update_task(task_id, status="failed", error_message="文章不存在")
            return

        platform_name = task["platform"]
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

            # 话题
            topic = article.get(f"topic_{platform_name}", "")

            # 执行发布流水线（worker 模式下不自动弹出登录窗口，避免与手动登录抢锁）
            result = await platform.publish(
                title=title,
                content_blocks=content_json.get("blocks", []),
                images=images,
                topic=topic,
                task_id=task_id,
                db=self.db,
                auto_login=False,
            )

            if result.get("success"):
                self.db.update_task(
                    task_id,
                    status="completed",
                    title_used=title,
                    topic_used=topic,
                    draft_url=result.get("draft_url", ""),
                    completed_at=datetime.now().isoformat(),
                )
                self.db.add_task_log(task_id, "INFO", f"草稿保存成功: {result.get('draft_url', '')}")
                # 发布成功说明登录态有效，回写账号状态（修复账号页长期显示 login_failed 的问题）
                self.db.upsert_account(
                    platform_name,
                    status="logged_in",
                    last_login_time=datetime.now().isoformat(),
                )
                logger.info(f"任务 {task_id} 完成")
            elif result.get("need_login"):
                # 需要重新登录：直接标记失败，提示去账号页登录，不再无谓重试
                error_msg = result.get("error", "未登录")
                self.db.update_task(task_id, status="failed", retry_count=task["retry_count"], error_message=error_msg)
                self.db.add_task_log(task_id, "ERROR", error_msg)
                logger.error(f"任务 {task_id} 失败: {error_msg}")
            else:
                raise Exception(result.get("error", "未知错误"))

        except Exception as e:
            error_msg = str(e)
            retry_count = task["retry_count"] + 1
            max_retries = 3

            if retry_count < max_retries:
                self.db.update_task(task_id, status="retrying", retry_count=retry_count, error_message=error_msg)
                self.db.add_task_log(task_id, "WARN", f"重试 {retry_count}/{max_retries}: {error_msg}")
                await asyncio.sleep(10 * retry_count)
                self.queue.put_nowait(task_id)
            else:
                self.db.update_task(task_id, status="failed", retry_count=retry_count, error_message=error_msg)
                self.db.add_task_log(task_id, "ERROR", f"最终失败: {error_msg}")
                logger.error(f"任务 {task_id} 失败: {error_msg}")

        finally:
            # 每个任务完成后必须清理浏览器，避免 Chrome SingletonLock 冲突
            if platform_name in self._platforms and self._platforms[platform_name]:
                try:
                    await self._platforms[platform_name].cleanup()
                    self.db.add_task_log(task_id, "INFO", "浏览器已关闭")
                except Exception:
                    pass
                self._platforms[platform_name] = None
                # 等待 Chrome 完全释放资源
                await asyncio.sleep(3)

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
