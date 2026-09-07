"""人类行为模拟器 —— 随机延迟、贝塞尔鼠标轨迹、分段打字、滚动"""
import asyncio
import random
import math
from typing import Tuple, List

from config import get_config


class HumanSimulator:
    """模拟真实用户操作行为，规避风控检测"""

    def __init__(self, config: dict = None):
        cfg = config or get_config().get("human_simulation", {})
        delay = cfg.get("delay", {})
        long_delay = cfg.get("long_delay", {})
        typing = cfg.get("typing", {})
        scroll = cfg.get("scroll", {})

        self.delay_min = delay.get("min", 0.5)
        self.delay_max = delay.get("max", 3.0)
        self.long_delay_prob = cfg.get("long_delay_probability", 0.1)
        self.long_delay_min = long_delay.get("min", 5.0)
        self.long_delay_max = long_delay.get("max", 15.0)
        self.char_delay_min = typing.get("char_delay_min", 0.05)
        self.char_delay_max = typing.get("char_delay_max", 0.25)
        self.scroll_step_min = scroll.get("step_min", 100)
        self.scroll_step_max = scroll.get("step_max", 300)
        self.backspace_prob = cfg.get("backspace_probability", 0.03)

    async def random_delay(self, min_sec: float = None, max_sec: float = None):
        """随机延迟"""
        lo = min_sec or self.delay_min
        hi = max_sec or self.delay_max
        delay = random.uniform(lo, hi)

        # 概率性长延迟（模拟看内容、思考）
        if random.random() < self.long_delay_prob:
            delay = random.uniform(self.long_delay_min, self.long_delay_max)

        await asyncio.sleep(delay)

    async def move_mouse_to(self, page, target_x: int, target_y: int,
                            steps: int = None, add_jitter: bool = True):
        """贝塞尔曲线移动鼠标到目标位置"""
        if steps is None:
            steps = random.randint(20, 40)

        # 获取当前位置
        current_x, current_y = random.randint(100, 500), random.randint(100, 300)

        # 随机控制点
        cp1_x = current_x + random.randint(-50, 100)
        cp1_y = current_y + random.randint(-50, 50)
        cp2_x = target_x + random.randint(-80, 50)
        cp2_y = target_y + random.randint(-50, 80)

        for i in range(steps + 1):
            t = i / steps
            # 三次贝塞尔曲线
            x = ((1 - t) ** 3) * current_x + 3 * ((1 - t) ** 2) * t * cp1_x \
                + 3 * (1 - t) * (t ** 2) * cp2_x + (t ** 3) * target_x
            y = ((1 - t) ** 3) * current_y + 3 * ((1 - t) ** 2) * t * cp1_y \
                + 3 * (1 - t) * (t ** 2) * cp2_y + (t ** 3) * target_y

            if add_jitter and i > 0:
                x += random.uniform(-1, 1)
                y += random.uniform(-1, 1)

            await page.mouse.move(int(x), int(y))
            await asyncio.sleep(random.uniform(0.005, 0.02))

    async def click_at(self, page, x: int, y: int):
        """移动到目标并点击"""
        await self.move_mouse_to(page, x, y)
        await self.random_delay(0.1, 0.3)
        await page.mouse.click(x, y)
        await self.random_delay()

    async def click_element(self, page, selector: str):
        """点击页面元素"""
        try:
            element = await page.wait_for_selector(selector, timeout=10000, state="visible")
            if element:
                box = await element.bounding_box()
                if box:
                    center_x = box["x"] + box["width"] / 2 + random.uniform(-5, 5)
                    center_y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
                    await self.click_at(page, int(center_x), int(center_y))
                    return True
        except Exception:
            pass
        return False

    async def type_text(self, page, selector: str, text: str):
        """模拟逐字输入文本"""
        if not text:
            return
        try:
            element = await page.wait_for_selector(selector, timeout=10000, state="visible")
            if element:
                await element.click()
                await self.random_delay(0.3, 0.8)

                char_count = 0
                pause_interval = random.randint(15, 30)
                for char in text:
                    # 模拟拼音输入法的分段输入
                    await page.keyboard.type(char, delay=random.randint(30, 150))

                    # 概率性退格修正
                    if random.random() < self.backspace_prob:
                        await page.keyboard.press("Backspace")
                        await asyncio.sleep(random.uniform(0.1, 0.3))
                        await page.keyboard.type(char, delay=random.randint(30, 100))

                    # 标点符号后额外停顿
                    if char in "。！？，、；：……":
                        await self.random_delay(0.3, 1.0)

                    # 每 15-30 个字符停顿一下
                    char_count += 1
                    if char_count >= pause_interval:
                        await self.random_delay(0.5, 2.0)
                        char_count = 0
                        pause_interval = random.randint(15, 30)

        except Exception as e:
            # 如果元素找不到，尝试直接键盘输入（焦点已在该区域）
            await self.random_delay()
            for char in text:
                await page.keyboard.type(char, delay=random.randint(30, 150))
                if random.random() < self.backspace_prob:
                    await page.keyboard.press("Backspace")
                    await page.keyboard.type(char)

    async def paste_text(self, page, text: str):
        """用粘贴方式快速输入长文本（模拟 Ctrl+V），但前面加一些手动打字"""
        # 先手动打几个字
        intro_len = min(len(text), random.randint(5, 15))
        intro = text[:intro_len]
        await self.type_text(page, "body", intro)

        # 粘贴剩余内容
        await asyncio.sleep(random.uniform(0.5, 1.5))

        # Playwright 剪贴板操作
        await page.evaluate("""
            (text) => {
                const ta = document.createElement('textarea');
                ta.value = text;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
        """, text[intro_len:])

        await page.keyboard.press("Control+v")
        await self.random_delay()

    async def simulate_scroll(self, page, scroll_times: int = None):
        """模拟自然滚动"""
        if scroll_times is None:
            scroll_times = random.randint(3, 8)

        for _ in range(scroll_times):
            delta = random.randint(self.scroll_step_min, self.scroll_step_max)
            await page.mouse.wheel(0, delta)
            await self.random_delay(0.3, 1.5)

            # 概率性回滚（模拟看内容）
            if random.random() < 0.05:
                await page.mouse.wheel(0, -random.randint(50, 150))
                await self.random_delay(0.5, 2.0)

        await self.random_delay()

    async def random_mouse_movement(self, page, count: int = None):
        """随机移动鼠标（模拟浏览行为）"""
        if count is None:
            count = random.randint(1, 5)

        viewport = page.viewport_size
        if viewport is None:
            viewport = await page.evaluate(
                "() => ({width: window.innerWidth, height: window.innerHeight})"
            )
        w = max(1, int(viewport.get("width") or 1))
        h = max(1, int(viewport.get("height") or 1))
        x_min = min(100, w - 1)
        x_max = max(x_min, w - 100)
        y_min = min(100, h - 1)
        y_max = max(y_min, h - 200)

        for _ in range(count):
            x = random.randint(x_min, x_max)
            y = random.randint(y_min, y_max)
            await self.move_mouse_to(page, x, y, steps=random.randint(10, 25))
            await self.random_delay(0.5, 2.0)
