"""裸 SQL 权限护栏——execute() 透传口的权限映射（20260804 权限矩阵全覆盖）

背景：execute(sql) 是各 Driver 包装层的"任意 SQL"透传口。数据安全要求
写操作/DDL 不能只认命名方法（insert()/add_column()...），裸 SQL 同样受
permissions.yml 约束——否则 ALTER TABLE ... DROP COLUMN 这类语句即成后门
（schema_manager 回滚路径裸调 execute 会绕过整个权限层）。

使用：在包装层 execute() 内、安全校验之后调用 guard_write_sql(sql)。
读语句（SELECT）与事务控制不在此拦截：读权限收口在 query() 的
列级屏蔽；PRAGMA 走只读白名单（可写 PRAGMA 会改库头/加密态，fail-closed）。
"""
from core.logger import get_logger
import re

from core.sql_safe import TABLE_REF_FRAGMENT  # 中立模块唯一定义（不绕契约再导出）
from .policy import Operation, PermissionDenied, PermissionPolicy

logger = get_logger(__name__)


def _c(pattern: str, flags: int = 0):
    """_GUARDS 模式编译：表名占位 {TREF} 展开为共享归一片段（TABLE_REF_FRAGMENT）。

    读写两侧同一实现：schema 前缀任意段、引号/方括号/
    空白任意混排全部归一到末段表名——不再按变体形态逐个特判。
    """
    return re.compile(pattern.replace("{TREF}", TABLE_REF_FRAGMENT), flags)


# 语句头 → (操作类型, 方法名, 是否含表名)；alter_table 的 op 为 None，看语句体判定
_GUARDS = [
    (_c(r'^\s*DELETE\s+(?:OR\s+\w+\s+)?FROM\s+{TREF}', re.I), Operation.DELETE, "delete", True),
    (_c(r'^\s*UPDATE\s+(?:OR\s+\w+\s+)?{TREF}', re.I), Operation.UPDATE, "update", True),
    (_c(r'^\s*(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+{TREF}', re.I), Operation.INSERT, "insert", True),
    (_c(r'^\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?{TREF}', re.I), Operation.DROP, "drop_table", True),
    (_c(r'^\s*ALTER\s+TABLE\s+{TREF}', re.I), None, "alter_table", True),
    (_c(r'^\s*RENAME\s+TABLE\s+{TREF}', re.I), Operation.DDL, "rename_table", True),
    (_c(r'^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{TREF}', re.I), Operation.DDL, "create_table", True),
    (_c(r'^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b[\s\S]*?\bON\s+{TREF}', re.I), Operation.DDL, "create_index", True),
    (_c(r'^\s*DROP\s+INDEX\b', re.I), Operation.DROP, "drop_index", False),
    # TRUNCATE ≈ DELETE（清空表数据，readonly 应禁）。当前链路：FederatedDriver.execute
    # 走默认数据源的 ContractDriver.execute，_validate_execute_sql 与 guard_write_sql
    # 两道都过（TRUNCATE 不在首关键字白名单，会先被 _validate_execute_sql 拒绝）；
    # 本规则兜底直接调用 guard_write_sql 的旧路径（security_review MEDIUM）
    (_c(r'^\s*TRUNCATE\s+TABLE\s+{TREF}', re.I), Operation.DELETE, "truncate", True),
    # WITH...DELETE（CTE 前缀的删除）；漏掉会让 readonly 经 CTE 清数据
    (_c(r'^\s*WITH\b[\s\S]*?\bDELETE\s+(?:OR\s+\w+\s+)?FROM\s+{TREF}', re.I), Operation.DELETE, "cte_delete", True),
    # WITH...UPDATE / WITH...INSERT（CTE 前缀的写）；对称补上（security_review 残留 MEDIUM）
    (_c(r'^\s*WITH\b[\s\S]*?\bUPDATE\s+(?:OR\s+\w+\s+)?{TREF}', re.I), Operation.UPDATE, "cte_update", True),
    (_c(r'^\s*WITH\b[\s\S]*?\b(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+{TREF}', re.I), Operation.INSERT, "cte_insert", True),
    # TRIGGER/VIEW/虚拟表/REINDEX：未识别头 fail-open 只告警会成为逃逸面——
    # CREATE TRIGGER 可经 execute（daemon 方法白名单含 execute）落库成为持久化后门
    (_c(r'^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?TRIGGER\b[\s\S]*?\bON\s+{TREF}', re.I), Operation.DDL, "create_trigger", True),
    (_c(r'^\s*DROP\s+TRIGGER\b', re.I), Operation.DROP, "drop_trigger", False),
    (_c(r'^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?VIEW\s+{TREF}', re.I), Operation.DDL, "create_view", True),
    (_c(r'^\s*DROP\s+VIEW\b', re.I), Operation.DROP, "drop_view", False),
    (_c(r'^\s*CREATE\s+VIRTUAL\s+TABLE\s+{TREF}', re.I), Operation.DDL, "create_virtual_table", True),
    (_c(r'^\s*REINDEX\b', re.I), Operation.DDL, "reindex", False),
]

_DROP_COLUMN_RE = _c(r'\bDROP\s+COLUMN\b', re.I)


def _execute_columns(sql: str, op: Operation) -> list:
    """execute 透传口 INSERT/UPDATE 的目标列提取（供列级权限收口；
    解析与驱动执行同源——split_set_pairs 全仓唯一 SET 解析器）"""
    from core.sql_safe import split_top_commas, split_set_pairs, \
        split_update_set_where
    if op == Operation.INSERT:
        m = re.match(
            r'^\s*(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+'
            + TABLE_REF_FRAGMENT + r'\s*\(([^)]*)\)',
            sql, re.I)
        if not m:
            return []  # 无列清单（INSERT ... SELECT 等），列级无从判定，表级闸已守
        # 组 1 = 表名（FRAGMENT 捕获），组 2 = 列清单
        return [c.strip().strip('`"[]') for c in split_top_commas(m.group(2)) if c.strip()]
    # UPDATE：SET 段与 WHERE 段按引号感知切分（字面量内的 WHERE 不切），再取列
    set_clause, _where = split_update_set_where(sql)
    return [col for col, _ in split_set_pairs(set_clause)]


# 表→数据源映射解析钩子（依赖倒置）：映射知识在 DataSourceManager（上层），
# 权限层不向上 import——由 dsm 实例化时注册；未注册（独立场景）退化默认语义
_TABLE_DS_RESOLVER = None
_DEFAULT_DS_NAME = None


def register_table_ds_resolver(resolve_fn, default_fn) -> None:
    """注册表→数据源映射解析器与默认库名解析器（DataSourceManager.__init__ 调用）"""
    global _TABLE_DS_RESOLVER, _DEFAULT_DS_NAME
    _TABLE_DS_RESOLVER = resolve_fn
    _DEFAULT_DS_NAME = default_fn


def resolve_datasource(table: str) -> str:
    """表名 → 数据源名（表不存在/未注册时回退默认数据源，如 create_table 场景）"""
    if _TABLE_DS_RESOLVER is None:
        logger.debug("表→数据源映射钩子未注册（独立脚本/测试语境），退化默认库语义")
        return ""
    if table:
        return _TABLE_DS_RESOLVER(table)
    return _DEFAULT_DS_NAME() if _DEFAULT_DS_NAME else ""


def guard_write_sql(sql: str, datasource: str = ""):
    """裸 SQL 写/DDL 权限校验；禁止时抛 PermissionDenied，允许时返回 None。

    读语句（SELECT/EXPLAIN）与事务控制（BEGIN/COMMIT...）直接放行；
    PRAGMA 走只读白名单（_READONLY_PRAGMAS，可写项 fail-closed）。
    上游（ContractDriver._validate_execute_sql）已强制单语句，命中一个模式即返回。

    datasource=执行目标数据源：透传口真实执行库。
    无表维度的操作（drop_index/trigger/view/reindex）按此域判定——
    旧实现按默认库判定，daemon 却按调用方指定库执行，判定与执行错位。
    """
    exec_ds = datasource or None
    # schema 直写后门单独硬阻断（全仓无任何合法用途，fail-closed）
    # rekey 同理：经 execute 改钥会让 SQLCipher 静态加密静默失效
    if re.match(r'^\s*PRAGMA\s+(?:writable_schema|rekey)\b', sql, re.I):
        raise PermissionDenied(
            "PRAGMA writable_schema/rekey 为 schema 直写/改钥后门，禁止经 execute 执行")
    # PRAGMA 白名单（除上面两项黑名单外，user_version/schema_version/
    # journal_mode 等可写 PRAGMA 会改写库头，一律不放）——execute 透传口只放行
    # 只读查询类；驱动建连的 PRAGMA（key/journal_mode/WAL 等）走裸连接不经过本闸
    _pm = re.match(r'^\s*PRAGMA\s+(\w+)', sql, re.I)
    if _pm and _pm.group(1).lower() not in _READONLY_PRAGMAS:
        raise PermissionDenied(
            f"PRAGMA {_pm.group(1)} 不在只读白名单（{', '.join(sorted(_READONLY_PRAGMAS))}），"
            "禁止经 execute 执行")
    for pattern, op, method, has_table in _GUARDS:
        m = pattern.match(sql)
        if not m:
            continue
        if op is None:  # ALTER TABLE：语句体含 DROP COLUMN 归 drop，其余归 ddl
            op = Operation.DROP if _DROP_COLUMN_RE.search(sql) else Operation.DDL
        table = m.group(1) if has_table else ""
        policy = PermissionPolicy.get_instance()
        if has_table:
            policy.check(
                resolve_datasource(table), op, f"execute:{method}", table=table)
        else:
            # 无表维度：判定域=执行目标数据源名，直接喂 check（
            # 数据源名不过表名解析器—— exec_ds 恒为数据源名，空则回退默认）
            policy.check(
                exec_ds or resolve_datasource(""), op, f"execute:{method}", table="")
        # 列级收口（execute 透传口的 INSERT/UPDATE）：内置凭证列等禁写列在此拦截——
        # 解析与驱动执行同源（split_set_pairs 全仓唯一解析器）；
        # op 如实传（INSERT 曾误标 UPDATE，deny:[insert] 规则会漏判）
        if op in (Operation.INSERT, Operation.UPDATE) and table:
            for col in _execute_columns(sql, op):
                policy.check_column(resolve_datasource(table), table, col, op)
        return
    # fail-closed：透传口的未识别写头一律拒绝。
    # 旧策略"默认放行但留痕"在 execute 这种透传口迟早被方言变体打穿
    #（UPDATE OR IGNORE 可穿透 users 只读）。读/会话头仍放行；
    # 合法调用方只用 _GUARDS 内的 DDL/DML 头（全仓 inventory 已核）。
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head and head not in _KNOWN_READ_HEADS:
        raise PermissionDenied(
            f"裸 SQL 护栏拒绝未识别的语句头: {head}（读/事务语句不受限；"
            "写/DDL 请使用命名方法或已识别的语句形态）")


# execute 透传口只读 PRAGMA 白名单：只读查询类；其余（含可写
# user_version/schema_version/journal_mode/application_id 等）fail-closed
_READONLY_PRAGMAS = frozenset({
    "table_info", "index_list", "index_info", "foreign_key_list",
    "database_list", "integrity_check", "quick_check", "compile_options",
    "foreign_key_check", "table_xinfo",
    # 会话级开关（不改持久状态/用户数据）：驱动与修复链路的合法形态
    "foreign_keys", "busy_timeout", "query_only", "defer_foreign_keys",
})


# 已知只读/会话管理语句头（不触发兜底告警）
_KNOWN_READ_HEADS = frozenset({
    "SELECT", "PRAGMA", "EXPLAIN", "VALUES",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
})
