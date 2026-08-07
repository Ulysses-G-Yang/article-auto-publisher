"""进程内的平台互斥租约。

登录线程和发布队列共享同一个 Chrome Profile，不能同时操作同一平台。
这里使用线程锁而不是 asyncio.Lock，因为两个调用方可能运行在不同事件循环
和不同线程中。
"""

import threading


_LOCKS = {
    "zol": threading.Lock(),
    "xiaoheihe": threading.Lock(),
}


def try_acquire(platform: str) -> bool:
    """尝试取得平台租约；返回 False 表示已有登录/发布流程占用。"""
    lock = _LOCKS.get(platform)
    return bool(lock and lock.acquire(blocking=False))


def release(platform: str) -> None:
    """释放平台租约；重复释放或未知平台不抛出异常。"""
    lock = _LOCKS.get(platform)
    if not lock:
        return
    try:
        lock.release()
    except RuntimeError:
        # 释放方已经退出或没有取得锁时，不影响清理流程。
        pass
