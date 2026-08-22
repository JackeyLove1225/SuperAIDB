"""权限策略——按 数据源 × 操作类型 的访问控制

规则文件（config/permissions.yml）：

    default: full                 # 全局默认：full / read_only
    datasources:
      legacy:
        mode: read_only           # 只读：仅 query 允许
      analytics:
        mode: custom
        deny: [delete, drop, ddl] # 黑名单：其余全放
      staging:
        mode: custom
        allow: [query, insert]    # 白名单：其余全禁

操作类型（Operation）：query / insert / update / delete / ddl / drop
- ddl  = create_table/add_column/modify_column/alter_precision/add_foreign_key/
         create_index/recreate_table/rename_table
- drop = drop_table/drop_column/drop_foreign_key/drop_index

模式：
- full：全部操作允许
- read_only：仅 query 允许
- custom + allow：白名单内允许
- custom + deny：黑名单外允许
"""
from core.logger import get_logger
import time
from enum import Enum
from pathlib import Path

import yaml

from core.exceptions import AppError

logger = get_logger(__name__)

# 内置凭证保护（硬编码，permissions.yml 不可覆盖/关闭）：
# 认证表的口令哈希与盐禁止经数据操作层（驱动/工具/MCP）读出或改写——
# auth 模块内部校验走裸 open_db（core/auth.py:16），不经过本策略层，互不影响。
# 键一律小写规范形（见 _canon）
_BUILTIN_COLUMN_DENY: dict = {"users": {"password_hash", "salt"}}

# 内置认证表整表只读（数据面）：users 的写只允许经 core/auth 模块的裸 open_db
# 通道（用户管理接口）。否则"改 role 列"即自助提权（评审四轮 S-1：注册 user →
# update-by-pk 改自己 role=admin → 重登录接管）——凭证保护必须连授权列一起罩。
_BUILTIN_TABLE_READONLY = frozenset({"users"})


def _canon(name: str) -> str:
    """标识符规范形（casefold）。

    根因修复（评审三轮安全 A）：SQL 引擎对标识符大小写不敏感，而规则查表是
    大小写敏感的字典键——"Users"/"Password_Hash" 变体曾穿透全部表/列级规则
    （含内置凭证 deny）。规范形在权限栈边界统一收口：进参归一 + 配置键归一。
    """
    return (name or "").casefold()


def _get_ci(mapping: dict, key: str):
    """大小写不敏感的字典取值（配置侧的表/列键可能是任意大小写）"""
    if not isinstance(mapping, dict) or not key:
        return None
    v = mapping.get(key)
    if v is not None:
        return v
    cf = key.casefold()
    for k, v in mapping.items():
        if isinstance(k, str) and k.casefold() == cf:
            return v
    return None

# 当前请求的角色（auth 集成点：mgmt 认证中间件/端点经 set_current_role 注入；
# agent/chat 执行路径无用户上下文时默认 system 全权限，认证接入后经 token 注入）
# contextvars 而非 threading.local：FastAPI async 单线程事件循环内多请求并发，
# threading.local 会让并发请求串角色；ContextVar 按 task 隔离（20260804 认证接入改造）
import contextvars as _contextvars
_role_ctx: "_contextvars.ContextVar[str]" = _contextvars.ContextVar("perm_role", default="system")


def set_current_role(role: str):
    """设置当前上下文（线程/async task）的用户角色（由认证层调用）"""
    _role_ctx.set(role or "system")


def get_current_role() -> str:
    """当前上下文的用户角色（无用户上下文时返回 system）"""
    return _role_ctx.get() or "system"


# ── 临时提权（sudo 模式，20260809；20260822 跨进程化）──
# AI（MCP 通道）默认以 MCP_ROLE（user/readonly）身份操作；需要管理员
# 全权限时发起 escalate_permission → 管理端 admin 批准 → 提权落盘
# （config/escalation.json，TTL 到期自动失效）→ MCP 进程每次请求新鲜读取。
# 进程内存字典只作缓存；文件是跨进程契约（与 ConfigHub/pending_ops 同哲学）。
import json as _json
import os as _os
import threading as _threading
from pathlib import Path as _Path

_ESCALATION_LOCK = _threading.Lock()
_ESCALATION: dict = {}  # 进程内缓存 {"role": str, "expires_at": float}
_ESCALATION_MTIME: float = -1.0
ESCALATION_TTL_SECONDS = 600  # 默认 10 分钟，与 pending_ops 人审闸同量级
_ESCALATION_FILE = _Path(__file__).resolve().parent.parent.parent / "config" / "escalation.json"


def _escalation_fresh() -> dict:
    """读提权状态（文件 mtime 新鲜通道；文件不存在=未提权）"""
    global _ESCALATION, _ESCALATION_MTIME
    try:
        mtime = _os.path.getmtime(_ESCALATION_FILE)
    except OSError:
        _ESCALATION, _ESCALATION_MTIME = {}, -1.0
        return _ESCALATION
    if mtime != _ESCALATION_MTIME:
        try:
            data = _json.loads(_ESCALATION_FILE.read_text(encoding="utf-8"))
            _ESCALATION = data if isinstance(data, dict) else {}
        except Exception:
            _ESCALATION = {}  # 损坏=未提权（fail-closed）
        _ESCALATION_MTIME = mtime
    return _ESCALATION


def set_escalated_role(role: str, ttl_seconds: int = ESCALATION_TTL_SECONDS) -> None:
    """设置提权（用户已批准）。role 通常是 'admin'；ttl 到期自动失效。
    落盘跨进程生效：管理端批准 → MCP 进程下一次请求即吃到。"""
    with _ESCALATION_LOCK:
        if role and role != "system":
            data = {"role": role, "expires_at": time.time() + ttl_seconds}
            _ESCALATION_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _ESCALATION_FILE.with_suffix(".tmp")
            tmp.write_text(_json.dumps(data), encoding="utf-8")
            _os.replace(tmp, _ESCALATION_FILE)
            _ESCALATION.clear()
            _ESCALATION.update(data)
            try:
                global _ESCALATION_MTIME
                _ESCALATION_MTIME = _os.path.getmtime(_ESCALATION_FILE)
            except OSError:
                pass


def clear_escalation() -> None:
    """立即撤销提权（管理员主动回收/测试用）——跨进程生效"""
    with _ESCALATION_LOCK:
        _ESCALATION.clear()
        try:
            _ESCALATION_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def get_escalated_role() -> str:
    """当前有效提权角色（未提权或已过期返回空串；跨进程新鲜读取）"""
    with _ESCALATION_LOCK:
        esc = _escalation_fresh()
        if not esc:
            return ""
        if time.time() > esc.get("expires_at", 0):
            clear_escalation()
            return ""
        return esc.get("role", "")


def get_effective_role(role: str = "") -> str:
    """生效角色：显式传入 > 提权（仅 MCP 通道进程）> ContextVar（请求级）

    提权通道域化（评审四轮 M）：sudo 提权是"批给 MCP 通道的 AI"的——
    提权契约文件只对置了 MCP 通道旗标的进程（mcp_server.py）生效；
    管理端/web 请求永远用 ContextVar 的请求角色，提权窗口内不会被带飞。
    """
    if role:
        return role
    if _MCP_CHANNEL:
        esc = get_escalated_role()
        if esc:
            return esc
    return get_current_role()


# MCP 通道旗标（仅 mcp_server.py 启动时置真）
_MCP_CHANNEL = False


def set_mcp_channel(on: bool = True) -> None:
    """声明本进程为 MCP 通道（提权契约仅在此类进程生效）"""
    global _MCP_CHANNEL
    _MCP_CHANNEL = on


class Operation(str, Enum):
    QUERY = "query"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    DDL = "ddl"
    DROP = "drop"


# 驱动方法名 → 操作类型（FederatedDriver 查表用）
METHOD_TO_OP = {
    "query": Operation.QUERY,
    "insert": Operation.INSERT,
    "update": Operation.UPDATE,
    "delete": Operation.DELETE,
    "delete_by_pk": Operation.DELETE,
    "create_table": Operation.DDL,
    "add_column": Operation.DDL,
    "modify_column": Operation.DDL,
    "alter_precision": Operation.DDL,
    "add_foreign_key": Operation.DDL,
    "create_index": Operation.DDL,
    "recreate_table": Operation.DDL,
    "rename_table": Operation.DDL,
    "drop_table": Operation.DROP,
    "drop_column": Operation.DROP,
    "drop_foreign_key": Operation.DROP,
    "drop_index": Operation.DROP,
}


class PermissionDenied(AppError):
    """权限不足：数据源对该操作的访问被策略禁止"""


class PermissionPolicy:
    """权限策略单例（规则不再进程内常驻——经 ConfigHub 按 mtime 取新鲜值）

    热生效语义（真解耦）：文件即契约，任何进程改 permissions.yml 后，
    本进程下一次 check 自然吃到新规则——不需要重启，也不需要跨进程广播。
    """

    _instance = None

    @classmethod
    def get_instance(cls) -> "PermissionPolicy":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def new_instance(cls, rules: dict = None) -> "PermissionPolicy":
        """显式实例化（测试注入，P2-4 惯例）——注入的规则不走文件通道"""
        inst = cls()
        inst._injected = rules if rules is not None else {"default": "full"}
        return inst

    @classmethod
    def reset_instance(cls):
        cls._instance = None

    def __init__(self):
        self._injected = None
        self._path = Path(__file__).resolve().parent.parent.parent / "config" / "permissions.yml"

    def _current_rules(self) -> dict:
        """当前规则（ConfigHub 新鲜通道；文件不存在=默认 full，损坏=fail-closed）"""
        if self._injected is not None:
            return self._injected
        from core.config_hub import load_yaml
        data = load_yaml(self._path, default={"default": "full"}, fail_policy="closed")
        return data if isinstance(data, dict) else {"default": "full"}

    def describe(self) -> dict:
        """当前规则快照（管理端展示用）"""
        return self._current_rules()

    def check(self, datasource: str, op: Operation, method: str = "",
              table: str = "", role: str = ""):
        """校验 数据源×操作 是否允许（表级覆盖 + 角色叠加）。

        解析顺序（最具体者胜）：表级规则 > 库级规则 > default；
        角色规则（roles.<role>.deny/allow）在此基础上叠加（交集，deny 优先）。
        允许：返回 None；禁止：抛 PermissionDenied。
        """
        # 内置认证表整表只读（最高优先级，角色/规则都不可覆盖）——
        # 写 users 表只允许 core/auth 模块的裸 open_db 通道（不经过本策略层）
        if table and _canon(table) in _BUILTIN_TABLE_READONLY and op != Operation.QUERY:
            raise PermissionDenied(
                f"表 {table} 为内置认证表，数据面只读"
                "（用户管理请走管理端「用户」接口，不经数据操作层）")
        # 角色叠加：角色 deny 命中即禁（无论数据源规则多宽松）
        # 未显式传 role 时取生效角色（提权优先 > ContextVar）；system 不查角色规则
        effective_role = get_effective_role(role)
        if effective_role and effective_role != "system":
            self._check_role(effective_role, op, datasource, table)
        mode, allow, deny, scope = self._resolve(datasource, table)
        allowed = self._allowed(mode, allow, deny, op)
        if not allowed:
            scope_msg = f"表 '{table}'（{scope}规则）" if table else f"（{scope}规则）"
            raise PermissionDenied(
                f"数据源 '{datasource}' 的{scope_msg}权限不足：禁止 {op.value} 操作"
                f"（{mode} 模式{self._mode_hint(mode, allow, deny)}）。"
                f"如需调整，请编辑 config/permissions.yml")

    def _check_role(self, role: str, op: Operation, datasource: str = "",
                    table: str = ""):
        """角色规则叠加：roles.<role> 的 deny 命中即禁；有 allow 时白名单外全禁。

        角色规则可声明：
        - allow/deny：操作级（对任何表生效）
        - tables.<表>.deny/allow：表级（该角色在特定表上的操作受限）
        - tables.<表>.columns.<列>.deny/allow：列级（该角色在特定表的特定列受限）
        表/列级规则只在当前操作涉及该表/列时生效；未声明表/列的角色规则
        保持原有的全表操作级语义。
        """
        r_rules = (self._current_rules().get("roles") or {}).get(role, {})
        if not r_rules:
            return  # 未配置的角色不加额外限制
        allow, deny = r_rules.get("allow"), r_rules.get("deny")
        if allow is not None and op.value not in set(allow):
            raise PermissionDenied(
                f"角色 '{role}' 权限不足：禁止 {op.value} 操作（角色白名单: {sorted(allow)}）")
        if deny is not None and op.value in set(deny):
            raise PermissionDenied(
                f"角色 '{role}' 权限不足：禁止 {op.value} 操作（角色黑名单: {sorted(deny)}）")
        # 角色内表级规则：仅当当前操作涉及该表时生效
        if table:
            t_rules = _get_ci((r_rules.get("tables") or {}), table) or {}
            if any(k in t_rules for k in self._SCOPE_KEYS):
                if not self._allowed(t_rules.get("mode", "custom"),
                                     t_rules.get("allow"), t_rules.get("deny"), op):
                    raise PermissionDenied(
                        f"角色 '{role}' 权限不足：禁止对表 {table} 的 {op.value} 操作"
                        f"（角色表级黑名单: {sorted(t_rules.get('deny') or [])}）")

    def check_column(self, datasource: str, table: str, column: str, op: Operation,
                     role: str = ""):
        """列级权限：校验 数据源×表×列×操作（deny 命中即禁）

        列规则来源：
        0. 内置凭证保护（_BUILTIN_COLUMN_DENY，不可覆盖）——最高优先级
        1. 全局列规则（datasources.<ds>.tables.<t>.columns.<c>.deny）——对一切角色生效
        2. 角色列规则（roles.<role>.tables.<t>.columns.<c>.deny）——仅该角色生效
        """
        if _canon(column) in _BUILTIN_COLUMN_DENY.get(_canon(table), ()):
            raise PermissionDenied(
                f"列 {table}.{column} 为内置凭证字段，禁止经数据操作层访问")
        effective_role = get_effective_role(role)
        col_rules = self._column_rules(datasource, table, column)
        deny = col_rules.get("deny") or []
        if op.value in set(deny):
            raise PermissionDenied(
                f"列 {datasource}.{table}.{column} 权限不足：禁止 {op.value} 操作"
                f"（列级黑名单: {sorted(deny)}）")
        # 角色列规则叠加
        if effective_role and effective_role != "system":
            r_rules = (self._current_rules().get("roles") or {}).get(effective_role, {})
            t_rules = _get_ci((r_rules.get("tables") or {}), table) or {}
            c_rules = _get_ci((t_rules.get("columns") or {}), column) or {}
            c_deny = c_rules.get("deny") or []
            if op.value in set(c_deny):
                raise PermissionDenied(
                    f"角色 '{effective_role}' 权限不足：禁止访问列 {datasource}.{table}.{column}"
                    f"（角色列级黑名单: {sorted(c_deny)}）")

    def denied_columns(self, datasource: str, table: str, op: Operation,
                       role: str = "") -> set:
        """返回 数据源×表 下对指定操作被禁的列名集合（query 屏蔽用）

        合并全局列规则与当前角色列规则——不同角色看到的列集合不同。
        """
        effective_role = get_effective_role(role)
        denied = set(_BUILTIN_COLUMN_DENY.get(_canon(table), ()))  # 内置凭证列恒被屏蔽
        ds_rules = (self._current_rules().get("datasources") or {}).get(datasource, {})
        t_rules = _get_ci((ds_rules.get("tables") or {}), table) or {}
        for col, col_rules in (t_rules.get("columns") or {}).items():
            if op.value in set(col_rules.get("deny") or []):
                denied.add(_canon(col))
        # 角色列规则叠加
        if effective_role and effective_role != "system":
            r_rules = (self._current_rules().get("roles") or {}).get(effective_role, {})
            t_rules = _get_ci((r_rules.get("tables") or {}), table) or {}
            for col, col_rules in (t_rules.get("columns") or {}).items():
                if op.value in set(col_rules.get("deny") or []):
                    denied.add(_canon(col))
        return denied

    def _column_rules(self, datasource: str, table: str, column: str) -> dict:
        ds_rules = (self._current_rules().get("datasources") or {}).get(datasource, {})
        t_rules = _get_ci((ds_rules.get("tables") or {}), table) or {}
        return _get_ci((t_rules.get("columns") or {}), column) or {}

    # 覆盖判定键：scope 声明了 mode/allow/deny 才算一级覆盖；
    # 仅挂 tables/columns 子节点的"壳"不算覆盖，继续向上继承
    _SCOPE_KEYS = ("mode", "allow", "deny")

    def _resolve(self, datasource: str, table: str = ""):
        """解析有效规则 → (mode, allow, deny, scope)

        表级覆盖（datasources.<ds>.tables.<table>）优先于库级，库级优先于 default。
        覆盖的判定是"声明了 mode/allow/deny"：仅携带 tables/columns 子节点的壳
        不产生语义，向上继承——否则给某列加黑名单会把整表/整库误锁成 fail-closed。
        """
        rules = self._current_rules()
        ds_rules = (rules.get("datasources") or {}).get(datasource) or {}
        if table:
            t_rules = _get_ci((ds_rules.get("tables") or {}), table) or {}
            if any(k in t_rules for k in self._SCOPE_KEYS):
                return (t_rules.get("mode", "custom"),
                        t_rules.get("allow"), t_rules.get("deny"), "表级")
        if not any(k in ds_rules for k in self._SCOPE_KEYS):
            default = rules.get("default", "full")
            return (default, None, None, "default")
        return (ds_rules.get("mode", "custom"),
                ds_rules.get("allow"), ds_rules.get("deny"), "库级")

    def _allowed(self, mode: str, allow, deny, op: Operation) -> bool:
        if mode == "full":
            return True
        if mode == "read_only":
            return op == Operation.QUERY
        if allow is not None:
            return op.value in set(allow)
        if deny is not None:
            return op.value not in set(deny)
        # custom 但无 allow/deny：fail-closed 全禁
        return False

    def _mode_hint(self, mode, allow, deny) -> str:
        if mode == "custom":
            if allow is not None:
                return f"，仅允许 {sorted(allow)}"
            if deny is not None:
                return f"，禁止 {sorted(deny)}"
        return ""
