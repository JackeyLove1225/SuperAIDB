"""裸 SQL 权限护栏——execute() 透传口的权限映射（20260804 权限矩阵实测后补）

背景：execute(sql) 是各 Driver 包装层的"任意 SQL"透传口。数据安全要求
写操作/DDL 不能只认命名方法（insert()/add_column()...），裸 SQL 同样受
permissions.yml 约束——否则 ALTER TABLE ... DROP COLUMN 这类语句即成后门
（实测发现：schema_manager 曾在回滚路径裸调 execute 绕过整个权限层）。

使用：在包装层 execute() 内、安全校验之后调用 guard_write_sql(sql)。
读语句（SELECT）与 PRAGMA/事务控制不在此拦截：读权限收口在 query() 的
列级屏蔽；PRAGMA/事务为会话级管理语句，不改用户数据。
"""
from core.logger import get_logger
import re

from .policy import Operation, PermissionDenied, PermissionPolicy

logger = get_logger(__name__)

# 语句头 → (操作类型, 方法名, 是否含表名)；alter_table 的 op 为 None，看语句体判定
_GUARDS = [
    (re.compile(r'^\s*DELETE\s+(?:OR\s+\w+\s+)?FROM\s+[`"\[]?(\w+)', re.I), Operation.DELETE, "delete", True),
    (re.compile(r'^\s*UPDATE\s+(?:OR\s+\w+\s+)?[`"\[]?(\w+)', re.I), Operation.UPDATE, "update", True),
    (re.compile(r'^\s*(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+[`"\[]?(\w+)', re.I), Operation.INSERT, "insert", True),
    (re.compile(r'^\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?[`"\[]?(\w+)', re.I), Operation.DROP, "drop_table", True),
    (re.compile(r'^\s*ALTER\s+TABLE\s+[`"\[]?(\w+)', re.I), None, "alter_table", True),
    (re.compile(r'^\s*RENAME\s+TABLE\s+[`"\[]?(\w+)', re.I), Operation.DDL, "rename_table", True),
    (re.compile(r'^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[]?(\w+)', re.I), Operation.DDL, "create_table", True),
    (re.compile(r'^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b[\s\S]*?\bON\s+[`"\[]?(\w+)', re.I), Operation.DDL, "create_index", True),
    (re.compile(r'^\s*DROP\s+INDEX\b', re.I), Operation.DROP, "drop_index", False),
    # TRUNCATE ≈ DELETE（清空表数据，readonly 应禁）。当前链路：FederatedDriver.execute
    # 走默认数据源的 ContractDriver.execute，_validate_execute_sql 与 guard_write_sql
    # 两道都过（TRUNCATE 不在首关键字白名单，会先被 _validate_execute_sql 拒绝）；
    # 本规则兜底直接调用 guard_write_sql 的旧路径（security_review MEDIUM）
    (re.compile(r'^\s*TRUNCATE\s+TABLE\s+[`"\[]?(\w+)', re.I), Operation.DELETE, "truncate", True),
    # WITH...DELETE（CTE 前缀的删除）；漏掉会让 readonly 经 CTE 清数据
    (re.compile(r'^\s*WITH\b[\s\S]*?\bDELETE\s+(?:OR\s+\w+\s+)?FROM\s+[`"\[]?(\w+)', re.I), Operation.DELETE, "cte_delete", True),
    # WITH...UPDATE / WITH...INSERT（CTE 前缀的写）；对称补上（security_review 残留 MEDIUM）
    (re.compile(r'^\s*WITH\b[\s\S]*?\bUPDATE\s+(?:OR\s+\w+\s+)?[`"\[]?(\w+)', re.I), Operation.UPDATE, "cte_update", True),
    (re.compile(r'^\s*WITH\b[\s\S]*?\b(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+[`"\[]?(\w+)', re.I), Operation.INSERT, "cte_insert", True),
    # TRIGGER/VIEW/虚拟表/REINDEX：此前未识别头 fail-open 只告警——CREATE TRIGGER
    # 可经 execute（daemon 方法白名单含 execute）落库成为持久化后门（评审 M2）
    (re.compile(r'^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?TRIGGER\b[\s\S]*?\bON\s+[`"\[]?(\w+)', re.I), Operation.DDL, "create_trigger", True),
    (re.compile(r'^\s*DROP\s+TRIGGER\b', re.I), Operation.DROP, "drop_trigger", False),
    (re.compile(r'^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?VIEW\s+[`"\[]?(\w+)', re.I), Operation.DDL, "create_view", True),
    (re.compile(r'^\s*DROP\s+VIEW\b', re.I), Operation.DROP, "drop_view", False),
    (re.compile(r'^\s*CREATE\s+VIRTUAL\s+TABLE\s+[`"\[]?(\w+)', re.I), Operation.DDL, "create_virtual_table", True),
    (re.compile(r'^\s*REINDEX\b', re.I), Operation.DDL, "reindex", False),
]

_DROP_COLUMN_RE = re.compile(r'\bDROP\s+COLUMN\b', re.I)


def _execute_columns(sql: str, op: Operation) -> list:
    """execute 透传口 INSERT/UPDATE 的目标列提取（供列级权限收口；
    解析与驱动执行同源——split_set_pairs 全仓唯一 SET 解析器）"""
    from core.contract.security_contract import split_top_commas, split_set_pairs, \
        split_update_set_where
    if op == Operation.INSERT:
        m = re.match(
            r'^\s*(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+[`"\[]?\w+[`"\]]?\s*\(([^)]*)\)',
            sql, re.I)
        if not m:
            return []  # 无列清单（INSERT ... SELECT 等），列级无从判定，表级闸已守
        return [c.strip().strip('`"[]') for c in split_top_commas(m.group(1)) if c.strip()]
    # UPDATE：SET 段与 WHERE 段按引号感知切分（字面量内的 WHERE 不切），再取列
    set_clause, _where = split_update_set_where(sql)
    return [col for col, _ in split_set_pairs(set_clause)]


def resolve_datasource(table: str) -> str:
    """表名 → 数据源名（表不存在/未注册时回退默认数据源，如 create_table 场景）"""
    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()
    dsm.load_config()
    if table:
        return dsm.get_datasource_for_table(table)
    return dsm.get_default_name()


def guard_write_sql(sql: str):
    """裸 SQL 写/DDL 权限校验；禁止时抛 PermissionDenied，允许时返回 None。

    识别不了的语句（SELECT/PRAGMA/BEGIN 等）直接放行。
    上游（ContractDriver._validate_execute_sql）已强制单语句，命中一个模式即返回。
    """
    # schema 直写后门单独硬阻断（全仓无任何合法用途，fail-closed）
    if re.match(r'^\s*PRAGMA\s+writable_schema\s*=', sql, re.I):
        raise PermissionDenied(
            "PRAGMA writable_schema 为 schema 直写后门，禁止经 execute 执行")
    for pattern, op, method, has_table in _GUARDS:
        m = pattern.match(sql)
        if not m:
            continue
        if op is None:  # ALTER TABLE：语句体含 DROP COLUMN 归 drop，其余归 ddl
            op = Operation.DROP if _DROP_COLUMN_RE.search(sql) else Operation.DDL
        table = m.group(1) if has_table else ""
        policy = PermissionPolicy.get_instance()
        policy.check(
            resolve_datasource(table), op, f"execute:{method}", table=table)
        # 列级收口（execute 透传口的 INSERT/UPDATE）：内置凭证列等禁写列在此拦截——
        # 解析与驱动执行同源（split_set_pairs 全仓唯一解析器，评审四轮 S-2）；
        # op 如实传（INSERT 曾误标 UPDATE，deny:[insert] 规则会漏判，评审四轮）
        if op in (Operation.INSERT, Operation.UPDATE) and table:
            for col in _execute_columns(sql, op):
                policy.check_column(resolve_datasource(table), table, col, op)
        return
    # fail-closed（评审五轮收口）：透传口的未识别写头一律拒绝。
    # 旧策略"默认放行但留痕"在 execute 这种透传口迟早被方言变体打穿
    #（UPDATE OR IGNORE 穿透 users 只读的实测）。读/会话头仍放行；
    # 合法调用方只用 _GUARDS 内的 DDL/DML 头（全仓 inventory 已核）。
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head and head not in _KNOWN_READ_HEADS:
        raise PermissionDenied(
            f"裸 SQL 护栏拒绝未识别的语句头: {head}（读/事务语句不受限；"
            "写/DDL 请使用命名方法或已识别的语句形态）")


# 已知只读/会话管理语句头（不触发兜底告警）
_KNOWN_READ_HEADS = frozenset({
    "SELECT", "PRAGMA", "EXPLAIN", "VALUES",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
})
