"""共享 Playwright SDK 实例模块（外挂包装，不改原有发布业务）。

单例模式：全局只启动 1 次 chromium browser，账号用独立 BrowserContext。
原有发布函数只改获取 context 的入口，内部发布逻辑完全不动。

用法:
    sdk = SharedPlaywright.get_instance()
    await sdk.init_global_browser(headless=True)
    ctx = await sdk.get_or_create_ctx("zhihu", "acc001", cookies=...)
    page = await ctx.new_page()
    # ---- 原有发布业务代码，一行不用改 ----
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright


class SharedPlaywright:
    """全局共享 Playwright 实例。

    服务启动时调用一次 init_global_browser，后续所有发布任务通过
    get_or_create_ctx 获取隔离的 BrowserContext。
    """

    _instance: SharedPlaywright | None = None
    _lock: asyncio.Lock = asyncio.Lock()

    def __init__(self) -> None:
        self.pw: Playwright | None = None
        self.browser: Browser | None = None
        self.ctx_pool: dict[tuple[str, str], BrowserContext] = {}
        self.is_inited: bool = False

    @classmethod
    def get_instance(cls) -> SharedPlaywright:
        """获取全局单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def init_global_browser(self, headless: bool = True) -> None:
        """服务启动执行 1 次，只启动 playwright 和 browser，不创建账号上下文。

        幂等：多次调用不会重复启动。
        """
        if self.is_inited:
            return
        async with self._lock:
            if self.is_inited:
                return
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(headless=headless)
            self.is_inited = True

    async def get_or_create_ctx(
        self,
        site: str,
        account_id: str,
        cookies: list[dict] | None = None,
    ) -> BrowserContext:
        """给业务层调用：拿到该账号独立上下文。

        传入 cookies 就恢复登录态。同一 (site, account_id) 返回已缓存的 context。
        """
        key = (site, account_id)
        if key in self.ctx_pool:
            ctx = self.ctx_pool[key]
            # 检查上下文是否已关闭（例如被外部异常关闭）
            if ctx.is_connected():
                return ctx
            # 已关闭的上下文从池中移除，重新创建
            del self.ctx_pool[key]

        if self.browser is None:
            raise RuntimeError(
                "SharedPlaywright 未初始化。请先调用 init_global_browser()。"
            )

        # 在全局 browser 上新建隔离上下文，不新开浏览器进程
        ctx = await self.browser.new_context()
        if cookies:
            try:
                await ctx.add_cookies(cookies)
            except Exception:
                # Cookie 格式不匹配时不影响上下文创建
                pass
        self.ctx_pool[key] = ctx
        return ctx

    async def dump_ctx_cookies(
        self,
        ctx: BrowserContext,
        domain_url: str,
    ) -> list[dict]:
        """导出当前上下文最新 cookie，交给存储层保存。

        Args:
            ctx: 要导出 cookie 的 BrowserContext。
            domain_url: 用于过滤域名的 URL。

        Returns:
            Cookie 列表（playwright 原生格式）。
        """
        return await ctx.cookies(domain_url)

    async def close_one_ctx(self, site: str, account_id: str) -> None:
        """关闭并移除指定账号的上下文。

        安全：key 不存在时静默跳过。
        """
        key = (site, account_id)
        if key in self.ctx_pool:
            ctx = self.ctx_pool[key]
            try:
                await ctx.close()
            except Exception:
                pass
            del self.ctx_pool[key]

    async def close_all_ctx(self) -> None:
        """关闭所有账号上下文，保留全局 browser。"""
        keys = list(self.ctx_pool.keys())
        for key in keys:
            await self.close_one_ctx(key[0], key[1])

    async def shutdown(self) -> None:
        """彻底关闭：关闭所有上下文、browser 和 playwright。"""
        await self.close_all_ctx()
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.pw:
            try:
                await self.pw.stop()
            except Exception:
                pass
            self.pw = None
        self.is_inited = False
        SharedPlaywright._instance = None

    @property
    def active_context_count(self) -> int:
        """当前池中活跃的上下文数量。"""
        return len(self.ctx_pool)