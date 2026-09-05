"""操作密码闸——高危操作的人因防线（A/B 双威胁模型）

设计（与 docs/SuperAIDB_高危操作密码保护方案.md 同步）：
- 密码验证唯一真源在 core.auth（users 表 PBKDF2 慢哈希），本模块只做
  进程内能力凭证管理、闸点收口、系统自愈旁路——业务代码零密码逻辑
- **哪些操作要密码由本模块声明表统一收口**（GATED_CONTRACT_OPS：
  契约层全部被闸方法名；调用点只传操作名，增删闸点只改这张表）
- 能力凭证只活在单进程内存：不落盘、不进环境变量、不进日志；
  TTL 10 分钟，过期自动失效；lock() 立即作废
- 直调路径（脚本/REPL）由契约层闸点调 require_capability()：
  无凭证直接 SecurityError 并引导 interactive_unlock()
- 系统自愈旁路（system_bypass）：saga 补偿等系统自恢复写操作
  在本模块登记的上下文内放行——人因闸管"人发起的写"，不管"系统自
  愈"；同用户进程可伪造该上下文属已声明残余（见下）
- SUPERAI_TEST_MODE=1 是开发/CI 便利（run_all.py 注入）——
  同用户进程本就可设置该变量，不构成生产安全边界（如实声明；
  同用户刻意绕过的根治是 daemon 独立 OS 用户的系统级隔离）
"""
import contextvars
import getpass
import os
import threading
import time

from core.exceptions import SecurityError
from core.logger import get_logger

logger = get_logger(__name__)

_CAP_TTL_SECONDS = 600          # 能力凭证有效期 10 分钟
TEST_MODE_ENV = "SUPERAI_TEST_MODE"

# 契约层操作密码闸声明表（唯一真源）：
# 写皆密码——增/删/改/结构变更全部在闸内；查询类不在（有权限的读免签）。
# 调用点（core/contract/base.py）按此表逐个设闸，层 21 有覆盖锁防漂移。
GATED_CONTRACT_OPS = frozenset({
    # 记录级写
    "insert", "update", "delete", "delete_by_pk",
    # 结构级不可逆
    "drop_table", "drop_column", "drop_foreign_key", "drop_index",
    "recreate_table", "execute(DROP)",
})

_cap = {"until": 0.0}
_lock = threading.Lock()
_system_bypass: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "operator_gate_system_bypass", default=False)


def has_capability() -> bool:
    """当前进程是否持有有效能力凭证"""
    return time.time() < _cap["until"]


def capability_present_for_rpc() -> bool:
    """RPC 能力传递判定（daemon client 用）：真凭证 或 测试模式
    （TEST_MODE 是开发/CI 便利，与 require_capability 同口径声明）。"""
    return has_capability() or os.environ.get(TEST_MODE_ENV) == "1"


def capability_remaining() -> int:
    """凭证剩余秒数（0=无凭证），供状态展示"""
    return max(0, int(_cap["until"] - time.time()))


def unlock(password: str, username: str = "") -> bool:
    """用操作密码换取进程内能力凭证（TTL 10 分钟）。

    密码只用于与 users 表哈希比对，永不留存——本函数不记录、
    不缓存明文。验证失败不说明是密码错还是无 admin（防枚举）。
    SUPERAI_TEST_MODE=1 直接放行（开发/CI 便利，见模块 docstring 声明）。"""
    if os.environ.get(TEST_MODE_ENV) == "1":
        with _lock:
            _cap["until"] = time.time() + _CAP_TTL_SECONDS
        return True
    from core.auth import verify_operator_password
    if not verify_operator_password(password, username=username):
        logger.warning("操作密码闸：解锁失败（密码不匹配）")
        return False
    with _lock:
        _cap["until"] = time.time() + _CAP_TTL_SECONDS
    logger.info("操作密码闸：进程能力凭证已解锁（%d 分钟有效）", _CAP_TTL_SECONDS // 60)
    return True


def lock() -> None:
    """立即作废本进程能力凭证"""
    with _lock:
        _cap["until"] = 0.0
    logger.info("操作密码闸：进程能力凭证已作废")


def interactive_unlock() -> bool:
    """交互式解锁（脚本/REPL 入口）：getpass 不回显输入，密码不经命令行
    （命令行参数会被 ps 看到，环境变量会被子进程继承，均不用）。"""
    try:
        user = input("操作员用户名: ").strip()
        pwd = getpass.getpass("操作密码（高危直调解锁，10 分钟有效）: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return unlock(pwd, username=user)


class system_bypass:
    """系统自愈旁路上下文（saga 补偿/崩溃续滚等系统自恢复写操作专用）。

    人因闸管"人发起的写"，不管"系统自愈"——补偿被密码卡死等于把
    可恢复故障变成永久脏数据。用法：with system_bypass(): ...
    同用户进程可伪造本上下文（已声明残余，见模块 docstring）。"""

    def __enter__(self):
        self._tok = _system_bypass.set(True)
        return self

    def __exit__(self, *exc):
        _system_bypass.reset(self._tok)
        return False


class rpc_capability_grant:
    """daemon RPC 能力传递的接收端（server 侧用）：调用方进程持有效能力
    凭证时随 RPC 带 _op_cap=true，daemon 在本调用期间授予能力。
    信任模型与 _role/_user 传递一致（daemon 是隔离边界不是认证边界，
    认证在 mgmt/MCP 层）。"""

    def __enter__(self):
        with _lock:
            _cap["until"] = time.time() + _CAP_TTL_SECONDS
        return self

    def __exit__(self, *exc):
        lock()
        return False


def require_capability(operation: str = "") -> None:
    """高危直调闸点：无有效凭证即 SecurityError（fail-closed）。

    豁免三类：进程已 unlock；系统自愈旁路上下文内；
    SUPERAI_TEST_MODE=1（开发/CI 便利，见模块 docstring 的诚实声明）。"""
    if has_capability() or _system_bypass.get():
        return
    if os.environ.get(TEST_MODE_ENV) == "1":
        return
    raise SecurityError(
        f"高危操作{'「' + operation + '」' if operation else ''}需要操作密码："
        f"当前进程无能力凭证。脚本/REPL 请先执行 "
        f"python -c \"import sys; sys.path.insert(0, '.'); "
        f"from core.operator_gate import interactive_unlock; interactive_unlock()\"，"
        f"或在代码中调 core.operator_gate.interactive_unlock() 后重试。"
    )


def request_scoped_unlock(password: str, username: str = ""):
    """请求级解锁上下文管理器（管理端 API 用）：验证密码 → 本请求内持有
    凭证 → 请求结束立即作废。密码错误抛 SecurityError（路由层转 403）。"""
    class _Ctx:
        def __enter__(self):
            if os.environ.get(TEST_MODE_ENV) != "1":
                from core.auth import verify_operator_password
                if not verify_operator_password(password, username=username):
                    raise SecurityError("操作密码错误")
            with _lock:
                _cap["until"] = time.time() + _CAP_TTL_SECONDS
            return self

        def __exit__(self, *exc):
            lock()
            return False

    return _Ctx()
