"""跨进程平台锁；进程退出时由操作系统自动释放。"""

import os
from pathlib import Path
from typing import BinaryIO

from article_mvp.errors import PlatformBusyError


class PlatformFileLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - Windows 是当前生产环境
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise PlatformBusyError(f"平台锁已被占用: {self.path}") from exc
        self._file = handle

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "PlatformFileLock":
        self.acquire()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()
