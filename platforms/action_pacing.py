"""Shared bounds for editor actions; these are not anti-detection guarantees.

Adapters keep responsibility for exact targets, readiness and readback. The
controller never retries an action, including a partially completed input.
"""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable


class ActionPacer:
    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self.characters_per_second = self._number(config, "characters_per_second", 6, 1, 8)
        self.action_interval = self._number(config, "action_interval_seconds", 1, 0.5, 10)
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
        if self._last_finished is not None:
            remaining = self.action_interval - (time.monotonic() - self._last_finished)
            if remaining > 0:
                await self._sleep(remaining)

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
                await self._sleep(self.paragraph_pause)
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
        kwargs["delay"] = max(1000 / self.characters_per_second, kwargs.get("delay", 0))
        await self.perform(keyboard.type, text, **kwargs)

    async def _input(self, action, text: str, **kwargs) -> None:
        async with self._lock:
            await self._wait()
            try:
                for char in text:
                    await action(char, **kwargs)
                    await self._sleep(1 / self.characters_per_second)
                    if char == "\n":
                        await self._sleep(self.paragraph_pause)
                if text:
                    await self._sleep(self.paragraph_pause)
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
                    await self._sleep(1 / self.characters_per_second)
                    if char == "\n":
                        await self._sleep(self.paragraph_pause)
                if text:
                    await self._sleep(self.paragraph_pause)
            finally:
                self._last_finished = time.monotonic()
