"""Shared bounds for editor actions; these are not anti-detection guarantees.

Adapters keep responsibility for exact targets, readiness and readback. The
controller never retries an action, including a partially completed input.
"""

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable


class ActionPacer:
    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self.characters_per_second = self._number(config, "characters_per_second", 6, 1, 8)
        # Migrate old fixed intervals without retaining the previous fast path.
        self._number(config, "action_interval_seconds", 5, 0.5, 10)
        self.action_interval_min = self._number(config, "action_interval_min_seconds", 5, 5, 10)
        self.action_interval_max = self._number(config, "action_interval_max_seconds", 10, 5, 10)
        if self.action_interval_min >= self.action_interval_max:
            raise ValueError("Action pacing requires a nonempty random interval")
        # Compatibility for callers budgeting an armed event: use the upper bound.
        self.action_interval = self.action_interval_max
        self.paragraph_pause = self._number(config, "paragraph_pause_seconds", 1.2, 1, 5)
        self._last_finished: float | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _number(config: dict, name: str, default: float, low: float, high: float) -> float:
        value = config.get(name, default)
        if isinstance(value, bool):
            raise ValueError(f"Invalid action pacing setting: {name}")
        value = float(value)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Action pacing setting out of bounds: {name}")
        return value

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def event_timeout(self, timeout_ms: float, *, actions: int = 1) -> int:
        """Do not spend an armed response/file-chooser timeout on our own pause."""
        return math.ceil(timeout_ms + actions * self.action_interval * 1000) if timeout_ms else 0

    async def _wait(self) -> None:
        interval = random.uniform(self.action_interval_min, self.action_interval_max)
        elapsed = 0 if self._last_finished is None else time.monotonic() - self._last_finished
        if interval > elapsed:
            await self._sleep(interval - elapsed)

    def _character_pause(self) -> float:
        return random.uniform(1, 1.5) / self.characters_per_second

    async def _paragraph_wait(self) -> None:
        await self._sleep(random.uniform(self.paragraph_pause, self.paragraph_pause * 2))

    async def perform(self, action: Callable[..., Awaitable], *args, **kwargs):
        """Serialize one native action and propagate failure without a retry."""
        async with self._lock:
            await self._wait()
            try:
                return await action(*args, **kwargs)
            finally:
                self._last_finished = time.monotonic()

    async def structured_block(self, action, text: str, *args, **kwargs):
        """Preserve atomic native heading markup, with a per-character time budget."""
        async with self._lock:
            await self._wait()
            try:
                await self._sleep(len(text) / self.characters_per_second)
                result = await action(*args, **kwargs)
                await self._paragraph_wait()
                return result
            finally:
                self._last_finished = time.monotonic()

    async def insert_text(self, keyboard, text: str, **kwargs) -> None:
        await self._input(keyboard.insert_text, text, **kwargs)

    async def insert_metadata(self, keyboard, text: str, **kwargs) -> None:
        """Contenteditable titles keep a single complete native input event."""
        await self.structured_block(keyboard.insert_text, text, text, **kwargs)

    async def type_text(self, keyboard, text: str, **kwargs) -> None:
        # Keep native key sequences (e.g. editor Markdown shortcuts) intact.
        # These short control sequences keep the existing native call boundary;
        # body text uses insert_text/fill_body with a new sample per character.
        kwargs["delay"] = max(1000 * self._character_pause(), kwargs.get("delay", 0))
        await self.perform(keyboard.type, text, **kwargs)

    async def _input(self, action, text: str, **kwargs) -> None:
        async with self._lock:
            await self._wait()
            try:
                for char in text:
                    await action(char, **kwargs)
                    await self._sleep(self._character_pause())
                    if char == "\n":
                        await self._paragraph_wait()
                if text:
                    await self._paragraph_wait()
            finally:
                self._last_finished = time.monotonic()

    async def fill(self, locator, text: str, **kwargs) -> None:
        """Atomic metadata fields: never create drafts with partial titles.

        Title/search fields retain one native fill; the time budget is reserved
        before it. Body input uses fill_body or insert_text instead.
        """
        async with self._lock:
            await self._wait()
            try:
                if text:
                    await self._sleep(len(text) / self.characters_per_second)
                await locator.fill(text, **kwargs)
            finally:
                self._last_finished = time.monotonic()

    async def fill_body(self, locator, text: str, **kwargs) -> None:
        """Clear once, then type into the same locator using native input events."""
        async with self._lock:
            await self._wait()
            try:
                await locator.fill("", **kwargs)
                for char in text:
                    await locator.press_sequentially(char, **kwargs)
                    await self._sleep(self._character_pause())
                    if char == "\n":
                        await self._paragraph_wait()
                if text:
                    await self._paragraph_wait()
            finally:
                self._last_finished = time.monotonic()
