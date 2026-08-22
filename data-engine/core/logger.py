"""
结构化日志模块——统一项目内的日志输出
用法:
    from core.logger import logger
    logger.info("处理文件", file="test.pdf", pages=10)
    logger.error("AI 调用失败", error=str(e), cost=2.3)
"""

import logging
import sys
import json
from datetime import datetime


class StructuredFormatter(logging.Formatter):
    """JSON 结构化格式"""
    def format(self, record):
        log = {
            "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "lvl": record.levelname,
            "msg": record.getMessage(),
        }
        # 附加 extra 字段（从 __data 读取）
        extra = getattr(record, "__data", {})
        if extra:
            log["data"] = extra
        if record.exc_info:
            log["exception"] = str(record.exc_info[1])
        return json.dumps(log, ensure_ascii=False)


def _log(logger: logging.Logger, level: int, msg: str, **extra):
    """支持额外字段的日志输出"""
    if extra:
        logger.log(level, msg, extra={"__data": extra})
    else:
        logger.log(level, msg)


# 快捷函数
def info(msg: str, **extra):
    _log(_LOGGER, logging.INFO, msg, **extra)

def warning(msg: str, **extra):
    _log(_LOGGER, logging.WARNING, msg, **extra)

def error(msg: str, **extra):
    _log(_LOGGER, logging.ERROR, msg, **extra)


# 处理 extra 字段的 filter
class ExtraFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "__data"):
            record.__data = {}
        return True


# 按名缓存的模块日志器（R4 日志收口：生产代码统一经 get_logger 获取）
_loggers: dict = {}


def get_logger(name: str) -> logging.Logger:
    """按名缓存 logger，挂 StructuredFormatter（stdout），重复调用不重复挂 handler"""
    if name in _loggers:
        return _loggers[name]
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not any(getattr(h, "_structured", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        handler._structured = True  # 标记为本模块所挂，防重复
        logger.addHandler(handler)
    if not any(isinstance(f, ExtraFilter) for f in logger.filters):
        logger.addFilter(ExtraFilter())
    _loggers[name] = logger
    return logger


def get_root_logger() -> logging.Logger:
    """root logger（不挂 handler）——供 install_log_capture 等挂全局捕获用"""
    return logging.getLogger()


def setup_logger(name="app", level=logging.INFO, console=True):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addFilter(ExtraFilter())

    if console:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)

    return logger


# 默认日志器
_LOGGER = setup_logger()
