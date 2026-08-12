"""给 Flask 提供账号会话域专属 asyncio 运行时。"""

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any


class AccountRuntime:
    def __init__(self) -> None:
        self._ready = threading.Event()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="account-session-async",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("账号会话异步运行时启动超时")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def run(self, coroutine: Coroutine[Any, Any, Any], *, timeout: float = 30) -> Any:
        future = self.submit(coroutine)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError("账号会话操作超时") from None

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> Future:
        if self._closed or self._loop is None:
            coroutine.close()
            raise RuntimeError("账号会话异步运行时已关闭")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def close(self, dispose: Coroutine[Any, Any, Any] | None = None) -> None:
        if self._closed:
            if dispose is not None:
                dispose.close()
            return
        self._closed = True
        loop = self._loop
        if loop is None:
            if dispose is not None:
                dispose.close()
            return
        if dispose is not None:
            try:
                asyncio.run_coroutine_threadsafe(dispose, loop).result(timeout=10)
            except Exception:
                pass
        loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=10)
