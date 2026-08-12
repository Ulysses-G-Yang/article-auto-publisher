"""小黑盒独立发布器：公开发布、响应捕获和映射必须原子编排。"""

import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Page,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from article_mvp.config import PlatformConfig, load_platform_config
from article_mvp.contracts import PublishRequest
from article_mvp.db.models import PlatformArticle, PlatformArticleStatus
from article_mvp.errors import (
    ConfigurationError,
    LoginRequiredError,
    PublishMappingConflictError,
    PublishResultUnknownError,
)
from article_mvp.platforms.base import BasePublisher
from article_mvp.platforms.locks import PlatformFileLock
from article_mvp.security import extract_json_path, redact_sensitive, sanitize_url

PUBLIC_CONFIRMATION = "I_UNDERSTAND_PUBLICATION_IS_PUBLIC"


class XiaoheihePublisher(BasePublisher):
    platform = "xiaoheihe"

    def __init__(
        self,
        *,
        config: PlatformConfig | None = None,
        page: Page | None = None,
        profile_dir: str | Path | None = None,
        headless: bool = False,
        public_confirmation: str = "",
    ) -> None:
        self.config = config or load_platform_config()
        self.page = page
        project_root = Path(__file__).resolve().parents[4]
        data_dir = Path(os.getenv("ARTICLE_MVP_DATA_DIR", project_root / "data"))
        self.profile_dir = Path(profile_dir or data_dir / "profiles" / self.platform)
        self.lock_path = data_dir / "locks" / f"{self.platform}.lock"
        self.headless = headless
        self.public_confirmation = public_confirmation

    def _assert_publication_allowed(self) -> None:
        enabled = os.getenv("ARTICLE_MVP_ALLOW_PUBLIC_PUBLISH", "").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            raise ConfigurationError(
                "公开发布默认关闭；未设置 ARTICLE_MVP_ALLOW_PUBLIC_PUBLISH=true"
            )
        if self.public_confirmation != PUBLIC_CONFIRMATION:
            raise ConfigurationError("缺少公开发布二次确认，不会点击发布按钮")

    async def publish(
        self,
        request: PublishRequest,
        session: AsyncSession,
    ) -> PlatformArticle:
        self._assert_publication_allowed()
        if self.page is not None:
            await self._prepare_editor(self.page, request)
            return await self.publish_and_capture(
                self.page,
                session,
                task_id=request.task_id,
                title=request.title,
            )

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with PlatformFileLock(self.lock_path):
            playwright = await async_playwright().start()
            context: BrowserContext | None = None
            try:
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    channel="chrome",
                    headless=self.headless,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    viewport={"width": 1366, "height": 900},
                )
                page = context.pages[0] if context.pages else await context.new_page()
                await self._ensure_login(page, context)
                await self._prepare_editor(page, request)
                return await self.publish_and_capture(
                    page,
                    session,
                    task_id=request.task_id,
                    title=request.title,
                )
            finally:
                if context is not None:
                    await context.close()
                await playwright.stop()

    async def _ensure_login(self, page: Page, context: BrowserContext) -> None:
        await page.goto(str(self.config.publish.login_url), wait_until="domcontentloaded")
        cookies = await context.cookies()
        cookie_names = {cookie.get("name", "") for cookie in cookies}
        if cookie_names.intersection({"heybox_id", "nickname", "pkey"}):
            return
        if self.headless:
            raise LoginRequiredError("小黑盒登录态失效；headless 模式不能执行人工登录")
        await __import__("asyncio").to_thread(
            input,
            "请在浏览器完成小黑盒登录，然后按 Enter 继续：",
        )
        cookies = await context.cookies()
        if not {cookie.get("name", "") for cookie in cookies}.intersection(
            {"heybox_id", "nickname", "pkey"}
        ):
            raise LoginRequiredError("未检测到有效的小黑盒登录 Cookie")

    async def _prepare_editor(self, page: Page, request: PublishRequest) -> None:
        await page.goto(str(self.config.publish.editor_url), wait_until="domcontentloaded")
        selectors = self.config.publish.selectors
        await page.locator(selectors.title).first.fill(request.title)
        await page.locator(selectors.body).first.fill(request.body)

        if request.image_paths:
            missing = [path for path in request.image_paths if not Path(path).is_file()]
            if missing:
                raise ConfigurationError(f"图片文件不存在: {missing}")
            file_input = page.locator(selectors.file_input).first
            if await file_input.count() == 0:
                raise ConfigurationError("编辑器未发现图片上传 input")
            await file_input.set_input_files(request.image_paths)

        if request.community:
            await self._select_editor_value(
                page,
                selectors.community_button,
                request.community,
            )
        if request.topic:
            await self._select_editor_value(page, selectors.topic_button, request.topic)

    async def _select_editor_value(self, page: Page, button_selector: str, value: str) -> None:
        selectors = self.config.publish.selectors
        if not button_selector or not selectors.dialog_search or not selectors.dialog_result:
            raise ConfigurationError(f"平台配置缺少选择器，无法选择 {value!r}")
        await page.locator(button_selector).first.click()
        search = page.locator(selectors.dialog_search).first
        await search.fill(value)
        result = page.locator(selectors.dialog_result).filter(has_text=value).first
        if await result.count() == 0:
            raise ConfigurationError(f"未找到精确匹配的平台候选: {value}")
        await result.click()

    async def _trigger_publish(self, page: Page) -> None:
        selectors = self.config.publish.selectors
        publish_button = page.locator(selectors.publish_button).filter(has_text="发布").first
        if await publish_button.count() == 0:
            raise PublishResultUnknownError(
                "未找到小黑盒发布按钮",
                error_code="PUBLISH_CONTROL_NOT_FOUND",
            )
        await publish_button.click()
        confirm = page.locator(selectors.confirm_button).first
        try:
            await confirm.wait_for(state="visible", timeout=3_000)
            await confirm.click()
        except PlaywrightTimeoutError:
            # 部分页面没有二次确认框，首次点击会直接发送 POST。
            pass

    async def publish_and_capture(
        self,
        page: Page,
        session: AsyncSession,
        *,
        task_id: int,
        title: str | None = None,
        trigger: Callable[[Page], Awaitable[None]] | None = None,
    ) -> PlatformArticle:
        """捕获发布响应并写入映射；调用方持有事务并负责 commit。"""

        endpoint = self.config.publish.submit_endpoint

        def matches(response) -> bool:
            return (
                endpoint.url_contains in response.url
                and response.request.method.upper() == endpoint.method
            )

        try:
            async with page.expect_response(
                matches,
                timeout=self.config.publish.response_timeout_ms,
            ) as response_info:
                await (trigger or self._trigger_publish)(page)
            response = await response_info.value
        except PlaywrightTimeoutError as exc:
            raise PublishResultUnknownError(
                "等待小黑盒发布响应超时；平台可能已发布，禁止自动重试",
                error_code="PUBLISH_RESPONSE_TIMEOUT",
            ) from exc
        except PublishResultUnknownError:
            raise
        except Exception as exc:
            raise PublishResultUnknownError(
                f"触发小黑盒发布时状态未知: {exc}",
                error_code="PUBLISH_TRIGGER_FAILED",
            ) from exc

        if not 200 <= response.status < 300:
            raise PublishResultUnknownError(
                f"小黑盒发布接口返回 HTTP {response.status}",
                error_code="PUBLISH_RESPONSE_REJECTED",
            )
        try:
            payload = await response.json()
        except Exception as exc:
            raise PublishResultUnknownError(
                "小黑盒发布响应不是合法 JSON",
                error_code="PUBLISH_RESPONSE_INVALID_JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise PublishResultUnknownError(
                "小黑盒发布响应根节点不是对象",
                error_code="PUBLISH_RESPONSE_INVALID_JSON",
            )

        external_id = self._first_path(payload, self.config.publish.post_id_paths)
        if external_id is None or not str(external_id).strip():
            raise PublishResultUnknownError(
                "小黑盒发布响应缺少 post_id",
                error_code="PUBLISH_POST_ID_MISSING",
            )
        external_article_id = str(external_id).strip()
        platform_url_value = self._first_path(
            payload,
            self.config.publish.platform_url_paths,
        )
        platform_url = str(platform_url_value).strip() if platform_url_value else None
        if not platform_url and "/creator/editor" not in (page.url or ""):
            platform_url = page.url or None
        published_at = self._parse_platform_datetime(
            self._first_path(payload, self.config.publish.published_at_paths)
        )

        return await self._persist_mapping(
            session,
            task_id=task_id,
            external_article_id=external_article_id,
            title=title,
            platform_url=platform_url,
            published_at=published_at,
            payload=payload,
            response_url=response.url,
            response_status=response.status,
        )

    @staticmethod
    def _first_path(payload: dict[str, Any], paths: list[str]) -> Any:
        for path in paths:
            try:
                return extract_json_path(payload, path)
            except KeyError:
                continue
        return None

    @staticmethod
    def _parse_platform_datetime(value: Any) -> datetime | None:
        """接受常见 ISO 8601 或秒/毫秒时间戳；无法确认时保留为空。"""

        if value in (None, "") or isinstance(value, bool):
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            seconds = float(value)
            if abs(seconds) >= 100_000_000_000:
                seconds /= 1000
            try:
                parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                return None
        elif isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            if raw.replace(".", "", 1).isdigit():
                return XiaoheihePublisher._parse_platform_datetime(float(raw))
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None

        if parsed.tzinfo is None:
            # 平台未声明时区时无法安全判断是 UTC 还是北京时间，宁可保留为空。
            return None
        return parsed.astimezone(timezone.utc)

    async def _persist_mapping(
        self,
        session: AsyncSession,
        *,
        task_id: int,
        external_article_id: str,
        title: str | None,
        platform_url: str | None,
        published_at: datetime | None,
        payload: dict[str, Any],
        response_url: str,
        response_status: int,
    ) -> PlatformArticle:
        task_mapping = await session.scalar(
            select(PlatformArticle).where(
                PlatformArticle.task_id == task_id,
                PlatformArticle.platform == self.platform,
            )
        )
        if task_mapping is not None:
            if task_mapping.external_article_id == external_article_id:
                return task_mapping
            raise PublishMappingConflictError(
                "同一任务已映射到不同的小黑盒文章 ID，禁止覆盖"
            )

        external_mapping = await session.scalar(
            select(PlatformArticle).where(
                PlatformArticle.platform == self.platform,
                PlatformArticle.external_article_id == external_article_id,
            )
        )
        if external_mapping is not None:
            if external_mapping.task_id == task_id:
                return external_mapping
            raise PublishMappingConflictError(
                "该小黑盒文章 ID 已属于另一发布任务，禁止重复关联"
            )

        mapping = PlatformArticle(
            task_id=task_id,
            platform=self.platform,
            external_article_id=external_article_id,
            title=title,
            platform_url=platform_url,
            published_at=published_at,
            status=PlatformArticleStatus.MAPPED,
            extra_data={
                "publish_response": redact_sensitive(payload),
                "capture": {
                    "response_url": sanitize_url(response_url),
                    "response_status": response_status,
                    "evidence_level": self.config.publish.submit_endpoint.evidence.level.value,
                    "evidence_source": self.config.publish.submit_endpoint.evidence.source,
                },
            },
        )
        session.add(mapping)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise PublishMappingConflictError("写入平台文章映射时发生唯一性冲突") from exc
        except Exception as exc:
            raise PublishResultUnknownError(
                f"平台已返回 post_id，但映射写入失败: {exc}",
                error_code="PUBLISH_MAPPING_PERSISTENCE_FAILED",
            ) from exc
        return mapping
