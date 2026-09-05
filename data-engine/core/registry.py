"""单例/模块态重置注册表

行业切换等"全局状态重置"曾是手工点名制：切换点逐个 import、逐个调
reset_instance——新单例漏登记即状态串台（Steward 驱动缓存漏重置，
DDL 落旧行业库）。改为自注册：持有进程态的模块在 import 时
登记 reset 钩子，重置方遍历注册表一次调完。

语义完备性：未 import 的模块在本进程本无实例态，无需重置——
故"遍历已注册钩子"与"重置全部进程态"等价。

纪律：钩子在模块内聚处登记（模块自己的状态自己负责），
重置方只认注册表，不再点名任何具体模块。
"""
import threading

from core.logger import get_logger

logger = get_logger(__name__)

_hooks: list = []  # [(name, fn)] 登记序即执行序
_lock = threading.Lock()


def register_reset(name: str, fn) -> None:
    """登记一个进程态重置钩子（模块 import 时自注册；同名去重幂等）"""
    with _lock:
        if any(n == name for n, _ in _hooks):
            return
        _hooks.append((name, fn))


def reset_all(context: str = "") -> list:
    """遍历调用全部已注册钩子。单个失败记 warning 并继续；
    返回失败名单（调用方决定如何如实上报），绝不静默。"""
    with _lock:
        hooks = list(_hooks)
    failed = []
    for name, fn in hooks:
        try:
            fn()
        except Exception as e:
            logger.warning("状态重置钩子失败[%s]: %s (%s)", name, context or "?", e)
            failed.append(name)
    return failed


def registered() -> list:
    """当前已登记钩子名单（测试断言用）"""
    with _lock:
        return [n for n, _ in _hooks]


# ── 变更通知通道（P-D：写完成事件的解耦总线）──
# 用途：schema_manager 写完 YAML 后通知订阅方（graph service 同步 MetaDB/Ladybug），
# 发布方不 import 订阅方（方向铁律不破）。与 reset 钩同纪律：失败记 warning 不静默。
_change_hooks: dict = {}
_change_lock = threading.Lock()


def register_on_change(channel: str, name: str, fn) -> None:
    """订阅变更通道（同名幂等）"""
    with _change_lock:
        lst = _change_hooks.setdefault(channel, [])
        if any(n == name for n, _ in lst):
            return
        lst.append((name, fn))


def notify_change(channel: str, *args, **kwargs) -> list:
    """发布变更：逐个调订阅方，失败记 warning 并继续；返回失败名单。

    批处理合并：begin_batch/end_batch 包裹的多步写只触发
    一次对账——批量建 N 表曾是 N 次全量 reconcile（O(N²) 元数据写放大）"""
    global _batch_depth
    with _change_lock:
        if _batch_depth > 0:
            _batch_dirty.add(channel)
            return []
        subs = list(_change_hooks.get(channel, []))
    failed = []
    for name, fn in subs:
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logger.warning("变更钩子失败[%s@%s]: %s", name, channel, e)
            failed.append(name)
    return failed


_batch_depth = 0
_batch_dirty: set = set()


def begin_batch() -> None:
    """进入批处理窗（窗口内的变更通知只记账不点火）"""
    global _batch_depth
    with _change_lock:
        _batch_depth += 1


def end_batch() -> None:
    """退出批处理窗：窗口内记过账的通道各点火一次（合并未决）。
    必须在 finally 中调用——中途失败也要把已发生的写对账出去。"""
    global _batch_depth
    with _change_lock:
        _batch_depth = max(0, _batch_depth - 1)
        if _batch_depth > 0:
            return
        pending = list(_batch_dirty)
        _batch_dirty.clear()
    for channel in pending:
        notify_change(channel)
