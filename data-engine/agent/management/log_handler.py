"""日志环形缓冲——捕获系统日志，供前端实时查看"""

import logging
import time
from collections import deque
from threading import Lock

from core.logger import get_root_logger


class LogBuffer(logging.Handler):
    """环形日志缓冲区，保留最近 N 条日志供前端查询"""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self._buffer = deque(maxlen=capacity)
        self._lock = Lock()

    def emit(self, record: logging.LogRecord):
        """捕获日志记录，存入环形缓冲"""
        entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        with self._lock:
            self._buffer.append(entry)

    def get_recent(self, limit: int = 50) -> list[dict]:
        """获取最近 N 条日志"""
        with self._lock:
            items = list(self._buffer)
        return items[-limit:] if limit < len(items) else items

    def count(self) -> int:
        """获取当前缓冲区日志总数（不拷贝，O(1)）"""
        with self._lock:
            return len(self._buffer)

    def clear(self):
        """清空日志缓冲"""
        with self._lock:
            self._buffer.clear()


# 全局单例
_log_buffer = LogBuffer()


def get_log_buffer() -> LogBuffer:
    """获取全局日志缓冲实例"""
    return _log_buffer


def install_log_capture():
    """安装日志捕获——将系统日志导入环形缓冲"""
    root_logger = get_root_logger()
    # 避免重复安装
    for handler in root_logger.handlers:
        if isinstance(handler, LogBuffer):
            return
    root_logger.addHandler(_log_buffer)
    # 确保最低日志级别
    if root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)
