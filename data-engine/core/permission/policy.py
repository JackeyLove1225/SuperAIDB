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
import contextvars as _contextvars
import threading as _threading
import time
from enum import Enum
from pathlib import Path
from pathlib import Path as _Path


import hashlib as _hashlib
import hmac as _hmac

from core.config_hub import load_yaml
from core.crypto.key_manager import get_signing_key
from core.exceptions import AppError

logger = get_logger(__name__)

# 内置凭证保护（硬编码，permissions.yml 不可覆盖/关闭）：
# 认证表的口令哈希与盐禁止经数据操作层（驱动/工具/MCP）读出或改写——
# auth 模块内部校验走裸 open_db（core/auth.py:16），不经过本策略层，互不影响。
# 边界如实说：列级读屏蔽挂在 query() 数据面；daemon 的 execute 透传口
# （方法白名单内、需 IPC 令牌的内部多步骤操作通道）不读屏蔽——该通道
# 无 AI/HTTP 可达面，属纵深缺口而非暴露面。
# 键一律小写规范形（见 _canon）
_BUILTIN_COLUMN_DENY: dict = {"users": {"password_hash", "salt"}}

# 内置认证表整表只读（数据面）：users 的写只允许经 core/auth 模块的裸 open_db
# 通道（用户管理接口）。否则"改 role 列"即自助提权（注册 user →
# update-by-pk 改自己 role=admin → 重登录接管）——凭证保护必须连授权列一起罩。
_BUILTIN_TABLE_READONLY = frozenset({"users"})


def _canon(name: str) -> str:
    """标识符规范形（casefold）。

    根因修复：SQL 引擎对标识符大小写不敏感，而规则查表是
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
_role_ctx: "_contextvars.ContextVar[str]" = _contextvars.ContextVar("perm_role", default="system")
# 当前请求的用户名（按用户授权 users.<name> 段与自助收紧规则的消费点；
# 与角色同通道注入，MCP/系统路径为空）
_user_ctx: "_contextvars.ContextVar[str]" = _contextvars.ContextVar("perm_user", default="")


def set_current_role(role: str):
    """设置当前上下文（线程/async task）的用户角色（由认证层调用）"""
    _role_ctx.set(role or "system")


def get_current_role() -> str:
    """当前上下文的用户角色（无用户上下文时返回 system）"""
    return _role_ctx.get() or "system"


def set_current_user(username: str):
    """设置当前上下文用户名（认证层与角色同步注入）"""
    _user_ctx.set(username or "")


def get_current_user() -> str:
    """当前上下文用户名（无用户上下文返回空串）"""
    return _user_ctx.get() or ""


# ── 临时提权（sudo 模式，20260809；20260822 跨进程化）──
# AI（MCP 通道）默认以 MCP_USER 绑定用户身份操作（未绑定只读）；需要管理员
# 全权限时发起 escalate_permission → 管理端 admin 批准 → 提权落盘
# （config/escalation.json，TTL 到期自动失效）→ MCP 进程每次请求新鲜读取。
# 进程内存字典只作缓存；文件是跨进程契约（与 ConfigHub/pending_ops 同哲学）。
from core.file_contract import JsonContract

# RLock：get_escalated_role 持锁判过期后内调 clear_escalation（同锁嵌套），
# 非重入 Lock 在 TTL 到期路径必死锁（此前无任何测试踩到该分支）
_ESCALATION_LOCK = _threading.RLock()
ESCALATION_TTL_SECONDS = 600  # 默认 10 分钟，与 pending_ops 人审闸同量级
_ESCALATION_FILE = _Path(__file__).resolve().parent.parent.parent / "config" / "escalation.json"
# 用户自助收紧规则文件（deny-only；与管理员授权的 users 段分离存放——
# 自助写通道只进不出 allow，物理上无法覆盖管理员限制）
_SELF_RULES_PATH = _Path(__file__).resolve().parent.parent.parent / "config" / "user_self_rules.yml"
# 提权契约（JsonContract 公共实现：mtime 新鲜读+原子写；缺失/损坏=未提权 fail-closed）
_ESCALATION_CONTRACT = JsonContract(_ESCALATION_FILE)


def _sign_escalation(role: str, expires_at: float) -> str:
    """提权契约签名（HMAC-SHA256，签名钥走 key_manager 独立条目；
    "esc:" 域分离前缀——与挂起表签名（"pending:"）消息空间硬隔离）"""
    msg = f"esc:{role}|{expires_at:.6f}".encode("utf-8")
    return _hmac.new(get_signing_key().encode("utf-8"), msg,
                     _hashlib.sha256).hexdigest()


def _verify_escalation(esc: dict) -> bool:
    """验签（常量时间比较；无签/签错=伪造或残旧契约，一律拒认 fail-closed）"""
    sig = esc.get("sig", "")
    if not sig:
        return False
    try:
        expect = _sign_escalation(str(esc.get("role", "")),
                                  float(esc.get("expires_at", 0)))
    except Exception:
        return False
    return _hmac.compare_digest(sig, expect)


def _escalation_fresh() -> dict:
    """读提权状态（文件 mtime 新鲜通道；文件不存在=未提权）"""
    esc = _ESCALATION_CONTRACT.read()
    if esc and not _verify_escalation(esc):
        logger.warning("提权契约验签失败（疑似伪造或残旧文件），按未提权处理")
        return {}
    return esc


def set_escalated_role(role: str, ttl_seconds: int = ESCALATION_TTL_SECONDS) -> None:
    """设置提权（用户已批准）。role 通常是 'admin'；ttl 到期自动失效。
    落盘跨进程生效：管理端批准 → MCP 进程下一次请求即吃到。
    契约带 HMAC 签名：同用户进程直接改写 escalation.json 会被验签拒认。"""
    with _ESCALATION_LOCK:
        if role and role != "system":
            exp = time.time() + ttl_seconds
            _ESCALATION_CONTRACT.write(
                {"role": role, "expires_at": exp,
                 "sig": _sign_escalation(role, exp)})


def clear_escalation() -> None:
    """立即撤销提权（管理员主动回收/测试用）——跨进程生效"""
    with _ESCALATION_LOCK:
        _ESCALATION_CONTRACT.delete()


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

    提权通道域化：sudo 提权是"批给 MCP 通道的 AI"的——
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
        """显式实例化（测试注入用）——注入的规则不走文件通道"""
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
        # MCP 未绑定用户的纵深兜底（正常到不了这里——mcp_server 启动期拒起、
        # 调用点拒绝服务；此分支是引擎层最后一道 fail-closed）
        if _MCP_CHANNEL and not get_current_user() and effective_role != "admin":
            if op != Operation.QUERY:
                raise PermissionDenied(
                    "AI 通道未绑定用户（MCP_USER 未配置）：写操作禁止。")
        # 用户级规则 + 自助收紧（只能更严不能放松）。admin 为系统主权角色不受
        # 用户规则约束（含 MCP 提权窗口——sudo 语义即"临时 admin 批给 AI"）。
        _user = get_current_user()
        if _user and effective_role != "admin":
            self._check_user(_user, op, datasource, table)
            self._check_self(_user, op, table)
        # 级联判定（20260901，上级禁止不可被下级解禁）：default → 库级 → 表级
        # 逐级求值，任一级禁止即抛。原"表级覆盖顶替库级"语义是 fail-open
        # 陷阱：库级 read_only 被表级壳规则顶掉后会恢复可写。
        denied = self.first_deny(datasource, op, table)
        if denied:
            scope = denied["scope"]
            scope_msg = f"表 '{table}'（{scope}规则）" if table else f"（{scope}规则）"
            raise PermissionDenied(
                f"权限不足：数据源 '{datasource}' 的{scope_msg}禁止 {op.value} 操作。"
                f"解决：权限页「{scope}」对应位置取消该禁止项（或编辑 config/permissions.yml）")

    def _check_user(self, username: str, op: Operation, datasource: str = "",
                    table: str = ""):
        """用户级规则叠加（users.<用户名>，与角色规则同构、级联其后）：
        管理员按具体用户授权——同角色两用户可差异化（user_1/user_2 各配各的）。
        allow=白名单（单并须先过上级全部层级）/deny=黑名单/tables.<表>./columns 嵌套
        与角色规则同语义。未配置的用户不加额外限制。"""
        if not username:
            return
        u_rules = _get_ci(self._current_rules().get("users") or {}, username) or {}
        if not u_rules:
            return
        allow, deny = u_rules.get("allow"), u_rules.get("deny")
        if allow is not None and op.value not in set(allow):
            raise PermissionDenied(
                f"用户 '{username}' 权限不足：禁止 {op.value} 操作（用户白名单: {sorted(allow)}）")
        if deny is not None and op.value in set(deny):
            raise PermissionDenied(
                f"用户 '{username}' 权限不足：禁止 {op.value} 操作（用户黑名单: {sorted(deny)}）")
        if table:
            t_rules = _get_ci((u_rules.get("tables") or {}), table) or {}
            if any(k in t_rules for k in self._SCOPE_KEYS):
                if not self._allowed(t_rules.get("mode", "custom"),
                                     t_rules.get("allow"), t_rules.get("deny"), op):
                    raise PermissionDenied(
                        f"用户 '{username}' 权限不足：禁止对表 {table} 的 {op.value} 操作"
                        f"（用户表级黑名单: {sorted(t_rules.get('deny') or [])}）")

    def _self_rules(self, username: str) -> dict:
        """用户自助收紧规则（config/user_self_rules.yml，deny-only——
        只允许比管理员更严，结构上无 allow/mode 入口，物理上无法放松）"""
        if not username:
            return {}
        from core.config_hub import load_yaml
        all_rules = load_yaml(_SELF_RULES_PATH, default={})
        return _get_ci(all_rules, username) or {}

    def _check_self(self, username: str, op: Operation, table: str = ""):
        """自助收紧判定：自助规则只读 deny 键（写入端已校验 deny-only，
        这里再防一道手改文件——allow/mode 字段一律忽略）"""
        s = self._self_rules(username)
        if not s:
            return
        deny = s.get("deny") or []
        if op.value in set(deny):
            raise PermissionDenied(
                f"用户 '{username}' 已自我禁止 {op.value} 操作（自助收紧，权限页「我的收紧」可自助解除）")
        if table:
            t_rules = _get_ci((s.get("tables") or {}), table) or {}
            t_deny = t_rules.get("deny") or []
            if op.value in set(t_deny):
                raise PermissionDenied(
                    f"用户 '{username}' 已自我禁止对表 {table} 的 {op.value} 操作（自助收紧）")

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
        col_rules = self._column_rules(datasource, table, column)
        deny = col_rules.get("deny") or []
        if op.value in set(deny):
            raise PermissionDenied(
                f"列 {datasource}.{table}.{column} 权限不足：禁止 {op.value} 操作"
                f"（列级黑名单: {sorted(deny)}）")
        # 用户列规则叠加（users.<用户名>.tables.<表>.columns.<列>.deny）
        _user = get_current_user()
        if _user:
            u_rules = _get_ci(self._current_rules().get("users") or {}, _user) or {}
            ut_rules = _get_ci((u_rules.get("tables") or {}), table) or {}
            uc_rules = _get_ci((ut_rules.get("columns") or {}), column) or {}
            uc_deny = uc_rules.get("deny") or []
            if op.value in set(uc_deny):
                raise PermissionDenied(
                    f"用户 '{_user}' 权限不足：禁止访问列 {datasource}.{table}.{column}"
                    f"（用户列级黑名单: {sorted(uc_deny)}）")
            # 自助收紧列规则（deny-only，只允许更严）
            st_rules = _get_ci((self._self_rules(_user).get("tables") or {}), table) or {}
            sc_deny = (_get_ci((st_rules.get("columns") or {}), column) or {}).get("deny") or []
            if op.value in set(sc_deny):
                raise PermissionDenied(
                    f"用户 '{_user}' 已自我禁止访问列 {datasource}.{table}.{column}（自助收紧）")

    def denied_columns(self, datasource: str, table: str, op: Operation,
                       role: str = "") -> set:
        """返回 数据源×表 下对指定操作被禁的列名集合（query 屏蔽用）

        合并全局列规则与当前角色列规则——不同角色看到的列集合不同。
        """
        denied = set(_BUILTIN_COLUMN_DENY.get(_canon(table), ()))  # 内置凭证列恒被屏蔽
        ds_rules = _get_ci(self._current_rules().get("datasources") or {}, datasource) or {}
        t_rules = _get_ci((ds_rules.get("tables") or {}), table) or {}
        for col, col_rules in (t_rules.get("columns") or {}).items():
            if op.value in set(col_rules.get("deny") or []):
                denied.add(_canon(col))
        # 用户列规则 + 自助收紧列规则叠加（query 屏蔽同口径）
        _user = get_current_user()
        if _user:
            u_rules = _get_ci(self._current_rules().get("users") or {}, _user) or {}
            ut_rules = _get_ci((u_rules.get("tables") or {}), table) or {}
            for col, col_rules in (ut_rules.get("columns") or {}).items():
                if op.value in set(col_rules.get("deny") or []):
                    denied.add(_canon(col))
            st_rules = _get_ci((self._self_rules(_user).get("tables") or {}), table) or {}
            for col, col_rules in (st_rules.get("columns") or {}).items():
                if op.value in set(col_rules.get("deny") or []):
                    denied.add(_canon(col))
        return denied

    def _column_rules(self, datasource: str, table: str, column: str) -> dict:
        ds_rules = _get_ci(self._current_rules().get("datasources") or {}, datasource) or {}
        t_rules = _get_ci((ds_rules.get("tables") or {}), table) or {}
        return _get_ci((t_rules.get("columns") or {}), column) or {}

    def _cascade_levels(self, datasource: str, table: str = "") -> list:
        """级联层级表（default → 库级 → 表级，壳规则不产生语义自动跳过）。

        每级 (scope, mode, allow, deny)。check()/first_deny() 与权限演练端点
        同源消费——判定与展示不漂移。"""
        rules = self._current_rules()
        ds_rules = _get_ci(rules.get("datasources") or {}, datasource) or {}
        t_rules = (_get_ci((ds_rules.get("tables") or {}), table) or {}) if table else {}
        levels = [("default", rules.get("default", "full"), None, None)]
        if any(k in ds_rules for k in self._SCOPE_KEYS):
            levels.append(("库级", ds_rules.get("mode", "custom"),
                           ds_rules.get("allow"), ds_rules.get("deny")))
        if table and any(k in t_rules for k in self._SCOPE_KEYS):
            levels.append(("表级", t_rules.get("mode", "custom"),
                           t_rules.get("allow"), t_rules.get("deny")))
        return levels

    def first_deny(self, datasource: str, op, table: str = ""):
        """级联逐级判定，返回首个禁止级的详情 dict；全级放行返回 None。

        级联语义（20260901）：上级禁止不可被下级解禁——任一级 _allowed 为
        False 即禁止。原"表级覆盖顶替库级"语义弃用（fail-open 陷阱）。"""
        for scope, mode, allow, deny in self._cascade_levels(datasource, table):
            if not self._allowed(mode, allow, deny, op):
                return {"scope": scope, "mode": mode, "allow": allow, "deny": deny}
        return None

    # 级联层级判定键：scope 声明了 mode/allow/deny 才算一级有效层级；
    # 仅挂 tables/columns 子节点的"壳"不产生语义，跳过（继续向上）
    _SCOPE_KEYS = ("mode", "allow", "deny")

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
