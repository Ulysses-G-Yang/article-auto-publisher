"""平台会话 Cookie 心跳模块（后台 async 后台任务，不侵入发布主流程）。

独立后台协程，不和发布任务抢业务 page；每个账号用临时 page 做心跳检测。
只做状态检测、刷新 cookie、写状态存储，不干预正在跑的发布任务。

存储约定：外部提供读写函数，本模块不接管存储。每个账号维护：
    last_heartbeat_ts, login_status: alive / expire / dead

用法:
    from delivery_tools.pw_shared_sdk import SharedPlaywright
    from delivery_tools.session_heartbeat import heartbeat_loop

    # FastAPI/Flask 启动钩子
    asyncio.create_task(heartbeat_loop(account_meta_list, heartbeat_interval_sec=300))
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from delivery_tools.pw_shared_sdk import SharedPlaywright

LOGGER = logging.getLogger(__name__)

# 账号登录状态
STATUS_ALIVE = "alive"
STATUS_EXPIRE = "expire"
STATUS_DEAD = "dead"


class AccountMeta:
    """单个账号心跳配置。"""

    def __init__(
        self,
        site: str,
        account_id: str,
        check_url: str,
        judge_fn: Callable | None = None,
    ) -> None:
        self.site = site
        self.account_id = account_id
        self.check_url = check_url
        # 登录态判断函数：judge_fn(page, site) -> bool
        # 缺省时用内置启发式：URL 不含 /login 且无登录表单即认为已登录
        self.judge_fn = judge_fn

    def to_dict(self) -> dict:
        return {
            "site": self.site,
            "account_id": self.account_id,
            "check_url": self.check_url,
        }


async def single_account_heartbeat(
    site: str,
    account_id: str,
    check_url: str,
    judge_fn: Callable | None = None,
    *,
    load_cookie_fn: Callable[[str, str], Awaitable[list[dict] | None]] | None = None,
    save_cookie_fn: Callable[[str, str, list[dict]], Awaitable[None]] | None = None,
    update_status_fn: Callable[[str, str, str], Awaitable[None]] | None = None,
    timeout_ms: int = 15000,
) -> str:
    """单个账号心跳检测，返回该账号最新登录状态。

    流程：读取持久化 cookie → 获取/创建 context → 临时 page 访问校验页 →
    判断登录状态 → 有效则导出最新 cookie 回写 → 更新状态存储。

    任何异常都降级为 dead，但不会抛出，保证单个账号失败不影响整个心跳循环。
    """
    sdk = SharedPlaywright.get_instance()

    # 1. 读取本地持久化 cookie（外部存储层注入）
    cookies = None
    if load_cookie_fn is not None:
        try:
            cookies = await load_cookie_fn(site, account_id)
        except Exception as exc:
            LOGGER.warning("心跳读取 cookie 失败: site=%s account=%s err=%s", site, account_id, exc)

    # 2. 获取账号隔离 context
    try:
        ctx = await sdk.get_or_create_ctx(site, account_id, cookies)
    except Exception as exc:
        LOGGER.error("心跳获取 context 失败: site=%s account=%s err=%s", site, account_id, exc)
        await _safe_update_status(update_status_fn, site, account_id, STATUS_DEAD)
        return STATUS_DEAD

    temp_page = None
    try:
        temp_page = await ctx.new_page()
        # 3. 访问个人中心/校验页
        await temp_page.goto(check_url, timeout=timeout_ms, wait_until="domcontentloaded")

        # 4. 判断登录状态
        is_login = await _judge_login(temp_page, site, judge_fn)

        if is_login:
            # 登录有效：导出最新 cookie 回写存储
            try:
                new_cookies = await sdk.dump_ctx_cookies(ctx, check_url)
                if save_cookie_fn is not None:
                    await save_cookie_fn(site, account_id, new_cookies)
            except Exception as exc:
                LOGGER.warning(
                    "心跳导出/回写 cookie 失败: site=%s account=%s err=%s",
                    site,
                    account_id,
                    exc,
                )

            await _safe_update_status(update_status_fn, site, account_id, STATUS_ALIVE)
            return STATUS_ALIVE
        else:
            # 未登录：只改状态，不自动尝试登录（扫码无法自动化）
            await _safe_update_status(update_status_fn, site, account_id, STATUS_EXPIRE)
            return STATUS_EXPIRE
    except Exception as exc:
        # 页面崩溃/上下文失效，标记死亡，下次任务重建
        LOGGER.warning("心跳检测异常: site=%s account=%s err=%s", site, account_id, exc)
        await _safe_update_status(update_status_fn, site, account_id, STATUS_DEAD)
        return STATUS_DEAD
    finally:
        # 临时页面一定要关闭，不占用资源
        if temp_page is not None:
            try:
                await temp_page.close()
            except Exception:
                pass


async def _safe_update_status(
    update_status_fn: Callable[[str, str, str], Awaitable[None]] | None,
    site: str,
    account_id: str,
    status: str,
) -> None:
    """安全调用状态更新回调，失败不抛出。"""
    if update_status_fn is None:
        return
    try:
        await update_status_fn(site, account_id, status)
    except Exception as exc:
        LOGGER.warning(
            "心跳更新账号状态失败: site=%s account=%s status=%s err=%s",
            site,
            account_id,
            status,
            exc,
        )


async def _judge_login(page: Any, site: str, judge_fn: Callable | None = None) -> bool:
    """判断页面登录状态。

    优先使用平台自定义 judge_fn(page, site) -> bool；
    缺省用启发式：URL 不含 /login /signin 且页面无登录表单节点。
    """
    if judge_fn is not None:
        return bool(await judge_fn(page, site))

    try:
        url = page.url
        if any(seg in url.lower() for seg in ("/login", "/signin", "/passport")):
            return False
        # 常见登录表单/输入框标识
        selectors = [
            'input[type="password"]',
            'input[name="password"]',
            'input[type="text"][name*="login"]',
            'form[action*="login"]',
        ]
        for sel in selectors:
            try:
                count = await page.locator(sel).count()
                if count and count > 0:
                    return False
            except Exception:
                continue
        return True
    except Exception:
        return False


async def heartbeat_loop(
    account_meta_list: list[AccountMeta | dict],
    heartbeat_interval_sec: int = 300,
    *,
    load_cookie_fn: Callable[[str, str], Awaitable[list[dict] | None]] | None = None,
    save_cookie_fn: Callable[[str, str, list[dict]], Awaitable[None]] | None = None,
    update_status_fn: Callable[[str, str, str], Awaitable[None]] | None = None,
    max_accounts_per_round: int = 0,
) -> None:
    """后台常驻协程，每 heartbeat_interval_sec 秒跑一轮全部账号心跳。

    Args:
        account_meta_list: [{site, account_id, check_url}] 或 AccountMeta 实例列表。
        heartbeat_interval_sec: 心跳周期（秒），默认 300（5 分钟）。
        max_accounts_per_round: 单轮并发上限，0 表示不限制。
    """
    metas = [_coerce_meta(m) for m in account_meta_list]
    while True:
        start = time.monotonic()
        tasks = []
        for meta in metas:
            tasks.append(
                single_account_heartbeat(
                    meta.site,
                    meta.account_id,
                    meta.check_url,
                    judge_fn=meta.judge_fn,
                    load_cookie_fn=load_cookie_fn,
                    save_cookie_fn=save_cookie_fn,
                    update_status_fn=update_status_fn,
                )
            )

        if max_accounts_per_round and max_accounts_per_round > 0:
            # 分批并发，避免一次性创建过多临时 page
            for i in range(0, len(tasks), max_accounts_per_round):
                batch = tasks[i : i + max_accounts_per_round]
                await asyncio.gather(*batch, return_exceptions=True)
        else:
            await asyncio.gather(*tasks, return_exceptions=True)

        elapsed = time.monotonic() - start
        sleep_for = max(1, heartbeat_interval_sec - int(elapsed))
        await asyncio.sleep(sleep_for)


async def check_account_healthy(
    site: str,
    account_id: str,
    load_cookie_fn: Callable[[str, str], Awaitable[list[dict] | None]] | None = None,
) -> bool:
    """发布任务前置校验：读取账号状态。

    业务发布任务拿到任务后，先读取账号状态；expire/dead 返回 False，
    调用方应直接报错，不执行发布逻辑，避免无效操作。

    注意：此函数只做快速状态读取，不做真实页面探测；真实探测由心跳循环完成。
    若外部提供了状态读取回调，可传入；否则返回 True（不拦截）。
    """
    if load_cookie_fn is None:
        return True
    try:
        cookies = await load_cookie_fn(site, account_id)
    except Exception:
        return False
    # cookie 存在且非空视为健康（无状态存储时的保守判断）
    return bool(cookies)


def _coerce_meta(item: AccountMeta | dict) -> AccountMeta:
    """把 dict 或 AccountMeta 统一为 AccountMeta。"""
    if isinstance(item, AccountMeta):
        return item
    if isinstance(item, dict):
        return AccountMeta(
            site=item["site"],
            account_id=item["account_id"],
            check_url=item["check_url"],
        )
    raise TypeError(f"不支持的账号元数据类型: {type(item)!r}")