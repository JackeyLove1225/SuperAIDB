"""data_ops 子模块·聚合统计：入参校验 → DISTINCT 分流 → SELECT/HAVING/ORDER BY 拼装 → 执行装配
（20260830 拆包：core/data_ops.py 同名片段纯搬家，逻辑零变化）

patch 兼容（测试依赖，勿绕开）：get_driver / validate_order_by /
_translate_query_error 的引用一律在调用时经 facade 取值（_ops.X(...)），
使 patch("core.data_ops.get_driver") 之类的打桩保持有效。
"""
from core.tool_result import ToolResult
# 合法标识符正则统一从 core.contract.security_contract 导入（严格语义：
# 字母/下划线开头，只含字母数字下划线，长度 ≤ 64），本模块不再本地定义
from core.contract.security_contract import (
    safe_table_sql, safe_column_sql, SecurityContract, IDENTIFIER_RE,
)
from core.exceptions import SecurityError

# facade 回旋引用：仅用于调用时取值（_ops.get_driver()），导入期不解引用
from core import data_ops as _ops


def _validate_agg_params(drv, table: str, agg_func: str):
    """聚合入参校验：表存在性 + 聚合函数白名单

    返回 (规范化 agg_func, None)；任一校验失败返回 (agg_func, 失败 ToolResult)。
    """
    if not drv.table_exists(table):
        return agg_func, ToolResult.fail(f"表 {table} 不存在", code="NOT_FOUND",
                                         reason="table_not_found", table=table)
    # 校验聚合函数
    agg_func = agg_func.upper().strip()
    valid_funcs = {"COUNT", "SUM", "AVG", "MIN", "MAX", "DISTINCT"}
    if agg_func not in valid_funcs:
        return agg_func, ToolResult.fail(
            f"不支持的聚合函数: {agg_func}，只支持 {', '.join(sorted(valid_funcs))}",
            code="VALIDATION", reason="unsupported_agg_func", table=table)
    return agg_func, None


def _run_distinct_query(drv, table: str, agg_func: str, agg_field: str,
                        where: str) -> "ToolResult":
    """DISTINCT 模式：返回去重字段值列表（SELECT DISTINCT field FROM table），
    适用于"查询所有不同的XX"场景"""
    agg_field_clean = agg_field.strip()
    # 兼容 AI 传入 "DISTINCT drug_name" 或 "drug_name" 两种格式
    if agg_field_clean.upper().startswith("DISTINCT "):
        agg_field_clean = agg_field_clean[9:].strip()
    if agg_field_clean == "*" or not agg_field_clean:
        return ToolResult.fail("DISTINCT 模式必须指定具体字段名（不能用 *）",
                               code="VALIDATION", reason="missing_params", table=table)
    if not IDENTIFIER_RE.match(agg_field_clean):
        return ToolResult.fail(f"字段名不合法: {agg_field_clean}",
                               code="CONTRACT", reason="unsafe_identifier", table=table)
    if not drv.column_exists(table, agg_field_clean):
        return ToolResult.fail(f"字段 {agg_field_clean} 在表 {table} 中不存在",
                               code="NOT_FOUND", reason="column_not_found", table=table)
    sql = f'SELECT DISTINCT {safe_column_sql(agg_field_clean)} FROM {safe_table_sql(table)}'
    if where:
        try:
            SecurityContract.validate_where(where)
        except SecurityError:
            return ToolResult.fail("WHERE 条件不安全，请检查后重试",
                                   code="CONTRACT", reason="unsafe_where", table=table)
        sql += f" WHERE {where}"
    sql += f' ORDER BY {safe_column_sql(agg_field_clean)}'
    try:
        rows = drv.query(sql)
    except Exception as e:
        return ToolResult.fail(f"查询失败: {_ops._translate_query_error(e)}",
                               code="UNKNOWN", reason="query_failed", table=table)
    if not rows:
        return ToolResult.ok("查询结果为空", table=table, rows=[], row_count=0,
                             agg_func=agg_func, agg_field=agg_field_clean)
    from core.formatters import format_multi_table
    return ToolResult.ok(
        format_multi_table({f"DISTINCT {agg_field_clean}": rows}),
        table=table, rows=rows, row_count=len(rows),
        agg_func=agg_func, agg_field=agg_field_clean)


def _build_agg_select(drv, table: str, agg_func: str, agg_field: str,
                      group_by: str):
    """SELECT 段装配：聚合字段（支持 DISTINCT 前缀，如 "DISTINCT drug_name"）
    与分组字段逐一过标识符校验 + 存在性校验 → SELECT ... FROM 子句

    返回 ((sql, agg_field_clean, group_fields), None) 或 (None, 失败 ToolResult)。
    """
    # 校验聚合字段
    distinct_prefix = ""
    agg_field_clean = agg_field.strip()
    if agg_field_clean.upper().startswith("DISTINCT "):
        distinct_prefix = "DISTINCT "
        agg_field_clean = agg_field_clean[9:].strip()

    if agg_field_clean != "*":
        if not IDENTIFIER_RE.match(agg_field_clean):
            return None, ToolResult.fail(f"聚合字段名不合法: {agg_field}",
                                         code="CONTRACT", reason="unsafe_identifier",
                                         table=table)
        if not drv.column_exists(table, agg_field_clean):
            return None, ToolResult.fail(f"字段 {agg_field_clean} 在表 {table} 中不存在",
                                         code="NOT_FOUND", reason="column_not_found",
                                         table=table)

    # 构建 SELECT
    if agg_field_clean == "*":
        agg_expr = f"{agg_func}(*)"
    else:
        agg_expr = f'{agg_func}({distinct_prefix}{safe_column_sql(agg_field_clean)})'

    select_parts = []
    group_fields: list[str] = []
    if group_by:
        group_fields = [f.strip() for f in group_by.split(",") if f.strip()]
        for gf in group_fields:
            if not IDENTIFIER_RE.match(gf):
                return None, ToolResult.fail(f"分组字段名不合法: {gf}",
                                             code="CONTRACT", reason="unsafe_identifier",
                                             table=table)
            if not drv.column_exists(table, gf):
                return None, ToolResult.fail(f"字段 {gf} 在表 {table} 中不存在",
                                             code="NOT_FOUND", reason="column_not_found",
                                             table=table)
        select_parts.append(", ".join(safe_column_sql(gf) for gf in group_fields))

    select_parts.append(f'{agg_expr} AS agg_result')
    sql = f'SELECT {", ".join(select_parts)} FROM {safe_table_sql(table)}'
    return (sql, agg_field_clean, group_fields), None


def _build_agg_having(having, table: str):
    """HAVING 子句装配（结构化）：JSON 对象
    {"agg":"COUNT","field":"*","op":">","value":5}——聚合函数/运算符走封闭枚举、
    字段过标识符校验、值按类型字面量拼装；AI 不再产出任何 SQL 文本片段

    返回 (" HAVING ..." 子句文本, None) 或 (None, 失败 ToolResult)；
    空输入返回 ("", None)。
    """
    if not having:
        return "", None
    import json as _json
    try:
        _h = _json.loads(having) if isinstance(having, str) else having
    except Exception:
        return None, ToolResult.fail(
            "having 须为 JSON 对象："
            '{"agg":"COUNT","field":"*","op":">","value":5}',
            code="VALIDATION", reason="having_format", table=table)
    if not isinstance(_h, dict):
        return None, ToolResult.fail("having 须为 JSON 对象",
                                     code="VALIDATION", reason="having_format",
                                     table=table)
    _agg = str(_h.get("agg", "")).upper().strip()
    _field = str(_h.get("field", "*") or "*").strip()
    _op = str(_h.get("op", "")).upper().strip()
    _val = _h.get("value")
    if _agg not in ("COUNT", "SUM", "AVG", "MIN", "MAX"):
        return None, ToolResult.fail(f"having 聚合函数非法: {_agg}",
                                     code="CONTRACT", reason="unsafe_having",
                                     table=table)
    if _op not in ("=", "!=", "<>", ">", "<", ">=", "<="):
        return None, ToolResult.fail(f"having 运算符非法: {_op}",
                                     code="CONTRACT", reason="unsafe_having",
                                     table=table)
    if _field != "*":
        SecurityContract.validate_identifier(_field, "having 字段")
    from core.federation.saga import _sql_literal
    _col = "*" if _field == "*" else safe_column_sql(_field)
    return f" HAVING {_agg}({_col}) {_op} {_sql_literal(_val)}", None


def _build_agg_sql(drv, table: str, agg_func: str, agg_field: str,
                   group_by: str, having: str, where: str, order_by: str):
    """拼装聚合查询 SQL：SELECT 段（聚合/分组字段校验）→ WHERE →
    GROUP BY → HAVING → ORDER BY 各子句（安全校验不过即失败）

    返回 ((sql, agg_field_clean, group_fields), None) 或 (None, 失败 ToolResult)。
    """
    built, err = _build_agg_select(drv, table, agg_func, agg_field, group_by)
    if err is not None:
        return None, err
    sql, agg_field_clean, group_fields = built

    # WHERE
    if where:
        try:
            SecurityContract.validate_where(where)
        except SecurityError:
            return None, ToolResult.fail("WHERE 条件不安全，请检查后重试",
                                         code="CONTRACT", reason="unsafe_where",
                                         table=table)
        sql += f" WHERE {where}"

    # GROUP BY
    if group_fields:
        sql += f' GROUP BY {", ".join(safe_column_sql(gf) for gf in group_fields)}'

    # HAVING
    having_clause, err = _build_agg_having(having, table)
    if err is not None:
        return None, err
    sql += having_clause

    # ORDER BY（排序是聚合查询的基础意图——演示"按单价降序"不再
    # 靠上层 AI 在答复文本里手工排序；validate_order_by 标识符+ASC/DESC 白名单）
    if order_by:
        if not _ops.validate_order_by(order_by):
            return None, ToolResult.fail(f"ORDER BY 子句不安全，已拒绝执行: '{order_by[:80]}'",
                                         code="CONTRACT", reason="unsafe_order_by",
                                         table=table)
        sql += f" ORDER BY {order_by}"

    return (sql, agg_field_clean, group_fields), None


def _run_agg_query(drv, sql: str, table: str, agg_func: str,
                   agg_field_clean: str, group_fields: list) -> "ToolResult":
    """执行聚合查询并装配 ToolResult（执行 → 空结果 → 格式化）"""
    # 执行查询
    try:
        rows = drv.query(sql)
    except Exception as e:
        return ToolResult.fail(f"查询失败: {_ops._translate_query_error(e)}",
                               code="UNKNOWN", reason="query_failed", table=table)

    meta = {"table": table, "agg_func": agg_func, "agg_field": agg_field_clean,
            "group_by": group_fields}
    if not rows:
        return ToolResult.ok("查询结果为空", rows=[], row_count=0, **meta)

    # 格式化结果（排版唯一实现在 core/formatters）
    from core.formatters import format_multi_table
    return ToolResult.ok(format_multi_table({f"{agg_func}统计": rows}),
                         rows=rows, row_count=len(rows), **meta)


def aggregate_query(table: str, agg_func: str, agg_field: str = "*",
                    group_by: str = "", having: str = "", where: str = "",
                    order_by: str = "") -> "ToolResult":
    """聚合统计查询——支持 COUNT/SUM/AVG/MIN/MAX + GROUP BY + HAVING

    也可执行 DISTINCT 去重查询：当 agg_func=DISTINCT 时，返回去重字段值列表
    （SELECT DISTINCT field FROM table），适用于"查询所有不同的XX"场景。

    Args:
        table: 表名
        agg_func: 聚合函数（COUNT/SUM/AVG/MIN/MAX/DISTINCT）
                  DISTINCT 时返回去重字段值列表，而非单一聚合值
        agg_field: 聚合字段（COUNT 时用 *），默认 "*"
        group_by: 分组字段，逗号分隔（如 "region_id,specialty_id"）
        having: HAVING 条件——JSON 对象字符串，如 '{"agg":"COUNT","field":"*","op":">","value":5}'（结构化枚举，AI 不产出 SQL 文本）
        where: WHERE 条件

    Returns:
        ToolResult：text 为格式化结果；data 带
        {rows, row_count, agg_func, agg_field, group_by}，失败带 code/reason
    """
    # 防御性校验：表名合法性（防 SQL 注入）
    SecurityContract.validate_identifier(table, "表名")

    drv = _ops.get_driver()
    agg_func, err = _validate_agg_params(drv, table, agg_func)
    if err is not None:
        return err

    # DISTINCT 模式：返回去重字段值列表（SELECT DISTINCT field FROM table）
    # 适用于"查询所有不同的药品名称"等去重列表查询场景
    if agg_func == "DISTINCT":
        return _run_distinct_query(drv, table, agg_func, agg_field, where)

    built, err = _build_agg_sql(drv, table, agg_func, agg_field,
                                group_by, having, where, order_by)
    if err is not None:
        return err
    sql, agg_field_clean, group_fields = built
    return _run_agg_query(drv, sql, table, agg_func, agg_field_clean, group_fields)
