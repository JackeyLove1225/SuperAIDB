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
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


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


def _ensure_root_file_handler():
    """root 挂轮转文件 handler（D8 真轮转：10MB×3 档，运行期生效）

    多进程安全纪律：同一轮转文件只允许一个进程写——文件名按进程角色区分
    （SUPERAIDB_ROLE，launcher 拉起后端/daemon 子服务时注入对应角色名；
    未注入的引擎/测试进程写 engine.log，run_all 各层串行无并发）。
    跨进程同文件轮转在 Windows 上 rename 必撞锁，故绝不共用文件。
    """
    root = logging.getLogger()
    if any(getattr(h, "_rotating_file", False) for h in root.handlers):
        return
    if os.environ.get("SUPERAIDB_NO_FILE_LOG", "").lower() in ("1", "true", "yes"):
        return  # 逃逸门：测试/特殊环境显式关文件日志
    role = os.environ.get("SUPERAIDB_ROLE", "engine")
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    try:
        logs_dir.mkdir(exist_ok=True)
        # MCP 进程按 PID 分档：多客户端/多会话并存是常态，共享单文件在
        # Windows 上轮转 rename 必撞锁
        fname = f"{role}_{os.getpid()}.log" if role == "mcp" else f"{role}.log"
        fh = RotatingFileHandler(logs_dir / fname, maxBytes=10 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(StructuredFormatter())
        fh._rotating_file = True  # 幂等标记
        root.addHandler(fh)
    except OSError:
        pass  # 文件日志不可用不阻断主流程（控制台通道仍在）


_ensure_root_file_handler()


# 按名缓存的模块日志器（R4 日志收口：生产代码统一经 get_logger 获取）
_loggers: dict = {}


def get_logger(name: str) -> logging.Logger:
    """按名缓存 logger，挂 StructuredFormatter，重复调用不重复挂 handler

    输出面纪律：MCP 进程（SUPERAIDB_ROLE=mcp）一律走 stderr——stdout 是
    JSON-RPC 协议通道，任何日志/print 落 stdout 都会毒死 MCP 会话
    （daemon 拉起两行 INFO 即可杀死整个会话）。
    """
    if name in _loggers:
        return _loggers[name]
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not any(getattr(h, "_structured", False) for h in logger.handlers):
        stream = sys.stderr if os.environ.get("SUPERAIDB_ROLE") == "mcp" else sys.stdout
        handler = logging.StreamHandler(stream)
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
