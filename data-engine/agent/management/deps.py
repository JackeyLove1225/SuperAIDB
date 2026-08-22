"""Management API 共享依赖——sys.path 引导、设置对象、日志捕获与通用辅助函数

routers/ 下的各路由模块只依赖本模块（以及 core/config 包），
server.py 负责应用组装，避免循环依赖。
"""

import os
import sys
import time
from core.logger import get_logger
from pathlib import Path

# 模块级 logger
logger = get_logger(__name__)

# 确保项目根目录在 sys.path 中
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 隔离运行环境可能缺少 chromadb 依赖
_system_site = os.path.join(sys.base_prefix, 'Lib', 'site-packages')
if os.path.isdir(_system_site) and _system_site not in sys.path:
    sys.path.append(_system_site)

from config.settings import settings  # noqa: E402
from agent.management.log_handler import get_log_buffer, install_log_capture  # noqa: E402

# 安装日志捕获
install_log_capture()

# 启动时间
_start_time = time.time()

# ── 辅助函数 ──

def _get_driver():
    """获取数据库驱动实例"""
    try:
        from core.data_ops import _get_driver as get_drv
        return get_drv()
    except Exception:
        return None


def _get_vector_store():
    """获取向量数据库实例"""
    try:
        from core.vector_store import get_vector_store as get_vs
        return get_vs()
    except Exception:
        return None


def _get_db_path() -> str:
    """获取 SQLite 数据库文件路径"""
    path = settings.SQLITE_DB_PATH
    if not os.path.isabs(path):
        path = os.path.join(_project_root, path)
    return path


def _get_chroma_path() -> str:
    """获取 ChromaDB 数据目录"""
    path = settings.CHROMA_PATH
    if not os.path.isabs(path):
        path = os.path.join(_project_root, path)
    return path


def _dir_size_mb(path: str) -> float:
    """计算目录总大小（MB）"""
    if not os.path.isdir(path):
        return 0.0
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return round(total / 1024 / 1024, 2)


def _get_cached_dir_size(path: str, ttl: int = 60) -> float:
    """带 TTL 缓存的目录大小计算（避免频繁 os.walk 大目录）"""
    if not hasattr(_get_cached_dir_size, "_cache"):
        _get_cached_dir_size._cache = {}
    now = time.time()
    cached = _get_cached_dir_size._cache.get(path)
    if cached and now - cached[1] < ttl:
        return cached[0]
    size = _dir_size_mb(path)
    _get_cached_dir_size._cache[path] = (size, now)
    return size


def _format_uptime(seconds: int) -> str:
    """格式化运行时间"""
    if seconds < 60:
        return f"{seconds}秒"
    elif seconds < 3600:
        return f"{seconds // 60}分{seconds % 60}秒"
    elif seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}小时{m}分"
    else:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        return f"{d}天{h}小时"
