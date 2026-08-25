"""随机真人时间间隔模块（工具装饰器，零侵入原有业务）。

设计成工具装饰器 + 工具函数；原有业务代码只需给 click/fill/goto 等调用
套上装饰器，业务逻辑不变。区分三类延时：操作间隔、页面等待、请求休眠。

用法:
    from delivery_tools.human_delay_util import (
        human_action_delay,
        human_page_delay,
        human_type,
        task_gap_sleep,
    )

    @human_action_delay()
    async def do_click(page, sel):
        await page.click(sel)

    async def publish_flow(page, title, content):
        await page.goto(publish_url)
        await human_page_delay()()   # goto 后随机页面等待
        await human_type(page, "#title_input", title)
        await do_click(page, "#submit_btn")
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar

F = TypeVar("F", bound=Callable[..., Awaitable])

# 区间配置，按平台可以做不同配置（毫秒）
DEFAULT_DELAY_CONFIG: dict[str, float] = {
    "action_min": 0.8,     # 点击、切换操作间隔（秒）
    "action_max": 2.5,
    "page_wait_min": 1.5,  # 页面跳转后休眠（秒）
    "page_wait_max": 4.0,
    "task_gap_min": 5.0,   # 两篇发布任务之间休眠（秒）
    "task_gap_max": 15.0,
    "char_min": 0.05,      # 打字字符间隔（秒）
    "char_max": 0.25,
}


class DelayConfig:
    """延迟配置；可整体替换为平台专属配置。"""

    def __init__(self, values: dict[str, float] | None = None) -> None:
        cfg = dict(DEFAULT_DELAY_CONFIG)
        if values:
            cfg.update(values)
        self.values = cfg

    def action_seconds(self) -> float:
        return random.uniform(self.values["action_min"], self.values["action_max"])

    def page_wait_seconds(self) -> float:
        return random.uniform(self.values["page_wait_min"], self.values["page_wait_max"])

    def task_gap_seconds(self) -> float:
        return random.uniform(self.values["task_gap_min"], self.values["task_gap_max"])

    def char_seconds(self) -> float:
        return random.uniform(self.values["char_min"], self.values["char_max"])


# 全局默认配置实例（线程安全：只读字段，不修改）
_DEFAULT_CFG = DelayConfig()


def human_action_delay(
    delay: Callable[[], float] | None = None,
) -> Callable[[F], F]:
    """操作动作前随机休眠装饰器：click、select 等动作。

    Args:
        delay: 自定义延时函数（返回秒数）；缺省使用全局配置动作区间。
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            seconds = delay() if delay is not None else _DEFAULT_CFG.action_seconds()
            await asyncio.sleep(seconds)
            return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def human_page_delay(
    delay: Callable[[], float] | None = None,
) -> Callable[[], Awaitable[None]]:
    """页面跳转 goto 之后调用，返回一个可 await 的休眠函数。

    用法: await human_page_delay()()
    """
    async def page_sleep() -> None:
        seconds = delay() if delay is not None else _DEFAULT_CFG.page_wait_seconds()
        await asyncio.sleep(seconds)

    return page_sleep


async def task_gap_sleep(
    delay: Callable[[], float] | None = None,
) -> None:
    """任务级别休眠：两篇不同文章 / 不同平台发布完成之后调用一次。"""
    seconds = delay() if delay is not None else _DEFAULT_CFG.task_gap_seconds()
    await asyncio.sleep(seconds)


async def human_type(
    page,
    selector: str,
    text: str,
    char_min: float | None = None,
    char_max: float | None = None,
) -> None:
    """模拟打字：逐个字符输入，模拟真人输入，不要直接大段 paste。

    Args:
        page: Playwright Page。
        selector: 输入框选择器。
        text: 要输入的文本。
        char_min/char_max: 字符间隔秒数；缺省用全局配置。
    """
    await page.click(selector)
    lo = char_min if char_min is not None else _DEFAULT_CFG.values["char_min"]
    hi = char_max if char_max is not None else _DEFAULT_CFG.values["char_max"]
    for ch in text:
        delay_ms = int(random.uniform(lo * 1000, hi * 1000))
        await page.type(selector, ch, delay=delay_ms)


def configure_delays(values: dict[str, float]) -> DelayConfig:
    """替换全局延迟配置（可在平台初始化时调用）。

    Args:
        values: 覆盖的区间字段，如 {"action_min": 1.0, "action_max": 3.0}。
    """
    cfg = DelayConfig(values)
    _DEFAULT_CFG.values.update(cfg.values)
    return _DEFAULT_CFG


def no_delay() -> DelayConfig:
    """测试用：所有延时归零。"""
    zero = {k: 0.0 for k in _DEFAULT_CFG.values}
    return configure_delays(zero)