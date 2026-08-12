"""小黑盒 HTTPX 采集器；只有 verified 契约才能发起请求。"""

import asyncio
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from article_mvp.config import PlatformConfig, load_platform_config
from article_mvp.contracts import CollectorAuth, MetricValues
from article_mvp.db.models import PlatformArticle
from article_mvp.errors import (
    CollectionNetworkError,
    CollectorError,
    ConfigurationError,
    RateLimitedError,
    SchemaChangedError,
    SessionExpiredError,
)
from article_mvp.platforms.base import BaseCollector
from article_mvp.security import extract_json_path, redact_sensitive


class XiaoheiheCollector(BaseCollector):
    def __init__(self, *, config: PlatformConfig | None = None) -> None:
        self.config = config or load_platform_config()

    async def fetch_raw(
        self,
        article: PlatformArticle,
        client: httpx.AsyncClient,
        auth: CollectorAuth,
    ) -> dict[str, Any]:
        collector = self.config.collector
        collector.ensure_verified()
        headers = {"User-Agent": "article-mvp/0.1"}
        headers.update(auth.headers)
        self._validate_auth(headers, auth.cookies)
        # HTTPX 已弃用每个 request 单独传 cookies。阶段一每次采集使用专属
        # AsyncClient，因此把仅存在内存中的 Cookie 装入该客户端 CookieJar。
        client.cookies.update(auth.cookies)

        endpoint = collector.endpoint
        url = endpoint.url.format(external_article_id=article.external_article_id)
        params = {
            key: value.format(external_article_id=article.external_article_id)
            for key, value in collector.query_params.items()
        }
        pages: list[dict[str, Any]] = []
        cursor: Any = None
        max_pages = collector.pagination.max_pages if collector.pagination else 1

        for _page_number in range(max_pages):
            request_params = dict(params)
            if collector.pagination and cursor not in (None, ""):
                request_params[collector.pagination.cursor_param] = str(cursor)
            payload = await self._request_json(
                client,
                method=endpoint.method,
                url=url,
                params=request_params,
                headers=headers,
            )
            pages.append(payload)
            if collector.pagination is None:
                break
            try:
                next_cursor = extract_json_path(
                    payload,
                    collector.pagination.next_cursor_path,
                )
            except KeyError as exc:
                raise SchemaChangedError(
                    "分页配置中的 next_cursor_path 已失效"
                ) from exc
            if next_cursor in (None, "", cursor):
                break
            cursor = next_cursor
        else:
            raise CollectorError(
                f"分页超过安全上限 {max_pages}",
                error_code="PAGINATION_LIMIT_EXCEEDED",
            )

        return pages[0] if len(pages) == 1 else {"pages": pages}

    def _validate_auth(self, headers: dict[str, str], cookies: dict[str, str]) -> None:
        header_names = {name.casefold() for name in headers}
        missing_headers = [
            name
            for name in self.config.collector.required_headers
            if name.casefold() not in header_names
        ]
        missing_cookies = [
            name
            for name in self.config.collector.required_cookies
            if not cookies.get(name)
        ]
        if missing_headers or missing_cookies:
            raise ConfigurationError(
                "采集认证材料不完整: "
                f"missing_headers={missing_headers}, missing_cookies={missing_cookies}"
            )

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        *,
        method: str,
        url: str,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        collector = self.config.collector
        for attempt in range(1, collector.max_attempts + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    timeout=httpx.Timeout(
                        connect=5.0,
                        read=collector.response_timeout_seconds,
                        write=10.0,
                        pool=5.0,
                    ),
                )
            except httpx.RequestError as exc:
                if attempt >= collector.max_attempts:
                    raise CollectionNetworkError(f"小黑盒指标请求失败: {exc}") from exc
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
                continue

            if response.status_code in {401, 403}:
                raise SessionExpiredError(
                    f"小黑盒指标接口返回 HTTP {response.status_code}"
                )
            if response.status_code == 429:
                if attempt >= collector.max_attempts:
                    raise RateLimitedError("小黑盒指标接口持续限速")
                await asyncio.sleep(self._retry_after_seconds(response, attempt))
                continue
            if not 200 <= response.status_code < 300:
                raise CollectorError(
                    f"小黑盒指标接口返回 HTTP {response.status_code}",
                    error_code="COLLECTOR_HTTP_ERROR",
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise SchemaChangedError("小黑盒指标响应不是合法 JSON") from exc
            if not isinstance(payload, dict):
                raise SchemaChangedError("小黑盒指标响应根节点不是对象")
            return payload
        raise CollectionNetworkError("小黑盒指标请求未获得响应")  # pragma: no cover

    @staticmethod
    def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After", "").strip()
        if raw.isdigit():
            return min(max(int(raw), 1), 60)
        if raw:
            try:
                target = parsedate_to_datetime(raw)
                now = datetime.now(target.tzinfo)
                return min(max((target - now).total_seconds(), 1), 60)
            except (TypeError, ValueError, OverflowError):
                pass
        return min(2 ** (attempt - 1), 8)

    def normalize(self, payload: dict[str, Any]) -> MetricValues:
        paths = self.config.collector.json_paths
        try:
            read_count = extract_json_path(payload, paths["read_count"])
        except (KeyError, TypeError) as exc:
            raise SchemaChangedError("read_count JSON Path 已失效") from exc

        values: dict[str, Any] = {"read_count": read_count}
        for field_name in (
            "like_count",
            "comment_count",
            "collect_count",
            "exposure_count",
            "share_count",
            "revenue",
            "snapshot_time",
        ):
            path = paths.get(field_name)
            if not path:
                values[field_name] = None
                continue
            try:
                values[field_name] = extract_json_path(payload, path)
            except KeyError:
                values[field_name] = None
        values["raw_data"] = redact_sensitive(payload)
        try:
            return MetricValues.model_validate(values)
        except Exception as exc:
            raise SchemaChangedError(f"小黑盒指标字段类型不符合契约: {exc}") from exc
