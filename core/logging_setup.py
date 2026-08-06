"""项目统一日志配置。

所有模块使用 Loguru；文件日志只记录运行阶段和错误摘要，不记录 Cookie、令牌
或页面敏感内容。
"""
import os
import sys

from loguru import logger


_configured = False


def configure_logging(log_dir: str):
    """配置控制台和持久化文件日志，重复调用不会重复添加 sink。"""
    global _configured
    if _configured:
        return logger

    os.makedirs(log_dir, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    logger.add(
        os.path.join(log_dir, "app.log"),
        level="INFO",
        rotation="10 MB",
        retention=5,
        encoding="utf-8",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    _configured = True
    logger.info("统一日志已启动: {}", os.path.join(log_dir, "app.log"))
    return logger
