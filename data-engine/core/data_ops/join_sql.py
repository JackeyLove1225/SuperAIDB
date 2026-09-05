"""data_ops 子模块·多表 JOIN：联邦分流 → 入参校验 → ON 条件解析 → SQL 拼装 → 执行装配
（20260830 拆包：core/data_ops.py 同名片段纯搬家，逻辑零变化）

patch 兼容（测试依赖，勿绕开）：get_driver / _find_fk_relation /
validate_select_fields / _translate_query_error 的引用一律在调用时经 facade
取值（_ops.X(...)），使 patch("core.data_ops.get_driver") 之类的打桩保持有效。
"""
import re as _re

from core.logger import get_logger
from core.tool_result import ToolResult
from core.contract.security_contract import safe_table_sql, SecurityContract
from core.exceptions import SecurityError

# facade 回旋引用：仅用于调用时取值（_ops.get_driver()），导入期不解引用
from core import data_ops as _ops

logger = get_logger(__name__)


# 手动 ON 条件安全校验：table.column OP table.column（多个条件用 AND 连接）
def _try_federated_join(main_table: str, join_tables: str, join_list: list,
                        select_fields: str, where: str, join_type: str):
    """联邦数据库分流：跨库 JOIN 委托跨库编排器

    返回 None 表示未跨库（或单数据源可回退），继续走原生单连接 JOIN；
    返回 ToolResult 表示编排已完成，或跨库编排失败的显式报错。
    """
    try:
        from core.federation.join_executor import federated_join
        fed_result = federated_join(main_table, join_tables, select_fields, where, join_type)
        if fed_result is not None:
            # 跨库 JOIN 已完成（编排器返回纯文本，结构未知——legacy 过渡态）
            return ToolResult.legacy(fed_result)
    except Exception as e:
        # 跨库编排失败：涉及多个数据源时，原生单连接 JOIN 注定失败或产出
        # 错误结果（同名表静默错连）——显式报错；单数据源才可回退
        from core.datasource_manager import DataSourceManager
        dsm = DataSourceManager()
        sources = {dsm.get_datasource_for_table(t) for t in [main_table] + join_list}
        if len(sources) > 1:
            return ToolResult.fail(
                f"跨库 JOIN 编排失败（涉及 {len(sources)} 个数据源: "
                f"{', '.join(sorted(sources))}），已中止: {e}",
                code="UNKNOWN", reason="federated_join_failed",
                table=main_table, tables=[main_table] + join_list)
        logger.warning(f"JOIN 编排失败，回退原生路径: {e}", exc_info=True)
    return None


def _validate_join_params(drv, main_table: str, join_list: list, join_type: str):
    """JOIN 入参校验：主表/关联表存在性、关联表非空、JOIN 类型白名单

    返回 (规范化 join_type, None)；任一校验失败返回 (join_type, 失败 ToolResult)。
    """
    # 校验主表
    if not drv.table_exists(main_table):
        return join_type, ToolResult.fail(f"表 {main_table} 不存在", code="NOT_FOUND",
                                          reason="table_not_found", table=main_table)
    # 校验关联表
    for jt in join_list:
        if not drv.table_exists(jt):
            return join_type, ToolResult.fail(f"表 {jt} 不存在", code="NOT_FOUND",
                                              reason="table_not_found", table=jt)
    if not join_list:
        return join_type, ToolResult.fail("请指定至少一个要关联的表", code="VALIDATION",
                                          reason="missing_params", table=main_table)
    # 校验 JOIN 类型（RIGHT JOIN 使用原生语法，SQLite 3.39+ / MySQL 均支持）
    join_type = join_type.upper().strip()
    if join_type not in ("INNER", "LEFT", "RIGHT"):
        return join_type, ToolResult.fail(
            f"不支持的 JOIN 类型: {join_type}，只支持 INNER、LEFT 和 RIGHT",
            code="VALIDATION", reason="unsupported_join_type", table=main_table)
    return join_type, None


def _parse_join_on_condition(on_condition: str, main_table: str):
    """解析手动 ON 条件（JSON 数组字符串）→ ON 子句文本；空输入返回空串

    显式给出时优先于外键自动推断；多表关联时该条件对所有 JOIN 子句复用。
    返回 (on_clause_manual, None) 或 ("", 失败 ToolResult)。
    """
    # 手动 ON 条件（结构化，20260825）：JSON 数组
    # [{"left":"t1.a","op":"=","right":"t2.b"}]——AI 只填字段引用与运算符枚举，
    # 不产出 SQL 文本（"AI 永不生成 SQL"在 ON 子句路径上重新为真）
    on_condition = on_condition.strip() if on_condition else ""
    on_clause_manual = ""
    if on_condition:
        import json as _json
        try:
            _conds = _json.loads(on_condition)
        except Exception:
            return "", ToolResult.fail(
                "on_condition 须为 JSON 数组："
                '[{"left":"主表.字段","op":"=","right":"副表.字段"}]（多个条件多元素）',
                code="VALIDATION", reason="on_condition_format", table=main_table)
        if not isinstance(_conds, list) or not _conds:
            return "", ToolResult.fail("on_condition 须为非空 JSON 数组",
                                       code="VALIDATION", reason="on_condition_format",
                                       table=main_table)
        _parts = []
        for _c in _conds:
            _l, _r = str(_c.get("left", "")), str(_c.get("right", ""))
            _o = str(_c.get("op", "")).upper()
            if _o not in ("=", "!=", "<>", ">", "<", ">=", "<="):
                return "", ToolResult.fail(f"on_condition 运算符非法: {_o}",
                                           code="CONTRACT", reason="unsafe_on_condition",
                                           table=main_table)
            for _side in (_l, _r):
                _segs = _side.split(".")
                if len(_segs) != 2 or not all(_segs):
                    return "", ToolResult.fail(
                        f"on_condition 两端须为 表.字段 引用: {_side}",
                        code="CONTRACT", reason="unsafe_on_condition", table=main_table)
                for _seg in _segs:
                    SecurityContract.validate_identifier(_seg, "ON 条件引用")
            _parts.append(f"{_l} {_o} {_r}")
        on_clause_manual = " AND ".join(_parts)
    return on_clause_manual, None


def _build_join_sql(main_table: str, join_tables: str, join_list: list,
                    join_type: str, on_clause_manual: str,
                    select_fields: str, where: str):
    """拼装 JOIN 的 SELECT/COUNT 两条 SQL：FROM+JOIN 子句（手动 ON 优先，
    否则外键自动推断）→ WHERE 去重 → SELECT 字段与 WHERE 安全校验

    返回 ((sql, count_sql), None) 或 (None, 失败 ToolResult)。
    """
    # 构建 SQL FROM + JOIN 子句
    sql_parts = [f'FROM {safe_table_sql(main_table)}']

    for jt in join_list:
        if on_clause_manual:
            on_clause = on_clause_manual
        else:
            relation = _ops._find_fk_relation(main_table, jt)
            if not relation:
                return None, ToolResult.fail(
                    f"未找到 {main_table} 和 {jt} 之间的外键关系，"
                    f"无法自动推断 JOIN 条件，可通过 on_condition 参数手动指定",
                    code="NOT_FOUND", reason="no_fk_relation",
                    table=main_table, tables=[main_table, jt])
            from_table, from_col, to_table, to_col = relation
            on_clause = f'{from_table}.{from_col} = {to_table}.{to_col}'
        sql_parts.append(f'{join_type} JOIN {safe_table_sql(jt)} ON {on_clause}')

    # WHERE 去重：LLM 常把 FK 等值条件（id = fk）同时写进 WHERE——
    # 与 ON 重复的子条件必须剔除：LEFT JOIN 下它会强制明细表必须有行，
    # 把 LEFT 退化成 INNER，主表行在明细为空时整体返空
    if where and not on_clause_manual:
        fk_pairs = set()
        for jt in join_list:
            rel = _ops._find_fk_relation(main_table, jt)
            if rel:
                ft, fc, tt, tc = rel
                fk_pairs.add(f"{ft}.{fc} = {tt}.{tc}")
                fk_pairs.add(f"{tt}.{tc} = {ft}.{fc}")
        segs = [s.strip() for s in _re.split(r'\s+AND\s+', where, flags=_re.I) if s.strip()]
        kept = [s for s in segs if s not in fk_pairs]
        where = " AND ".join(kept)

    # SELECT 字段安全校验：允许 table.column, table.*, column, *, 聚合(field), field AS alias
    if select_fields.strip() != "*":
        # table.* 原生支持（SQLite/MySQL 均可），但表必须在本查询的 FROM/JOIN 中——
        # 不再静默改写为 *（会把 a.*, b.id 变成全表 *，列错位）
        tables_in_query = {main_table, *join_list}
        for part in [p.strip() for p in select_fields.split(",") if p.strip()]:
            if part.endswith(".*"):
                tname = part[:-2]
                if not _re.match(r'^[a-zA-Z_]\w*$', tname) or tname not in tables_in_query:
                    return None, ToolResult.fail(
                        f"SELECT 字段含未参与本查询的表通配: {select_fields}，"
                        f"table.* 中的表必须在 FROM/JOIN 中（本查询: {main_table}, {join_tables}）",
                        code="VALIDATION", reason="unsafe_select_fields", table=main_table)
                continue
            # 整段过全串锚定校验（标识符/聚合/AS 别名）——此前只验每段首词，
            # "id, username FROM users UNION SELECT ...--" 各段首词全合法即穿透（实测），
            # 改用与单表查询同款的 validate_select_fields（test_14 覆盖）
            if part != "*" and not _ops.validate_select_fields(part):
                return None, ToolResult.fail(
                    "查询字段格式不安全，请使用 table.column 格式（如 t1.code, t2.name）",
                    code="CONTRACT", reason="unsafe_select_fields", table=main_table)

    sql = f'SELECT {select_fields} ' + " ".join(sql_parts)

    # WHERE 安全校验（SecurityContract.validate_where 支持 table.column 格式）
    if where:
        try:
            SecurityContract.validate_where(where)
        except SecurityError:
            return None, ToolResult.fail("WHERE 条件不安全，请检查后重试",
                                         code="CONTRACT", reason="unsafe_where",
                                         table=main_table)
        sql += f" WHERE {where}"

    count_sql = f'SELECT COUNT(*) as c ' + " ".join(sql_parts)
    if where:
        count_sql += f" WHERE {where}"
    return (sql, count_sql), None


def _run_join_query(drv, sql: str, count_sql: str, main_table: str,
                    join_list: list) -> "ToolResult":
    """执行 JOIN 查询并装配 ToolResult：总数统计 → 行数帽 → 执行 → 格式化"""
    # 上下文窗口管理：限制返回行数，防止大结果集导致 LLM token 溢出
    JOIN_ROW_LIMIT = 100
    try:
        total_count = drv.query(count_sql)[0]["c"]
    except Exception as e:
        # 不再用 -1 哨兵伪装成功，COUNT 失败显式报错
        return ToolResult.fail(f"JOIN 查询总数统计失败: {e}", code="UNKNOWN",
                               reason="count_failed", table=main_table)
    sql += f" LIMIT {JOIN_ROW_LIMIT}"

    # 执行查询
    try:
        rows = drv.query(sql)
    except Exception as e:
        return ToolResult.fail(f"查询失败: {_ops._translate_query_error(e)}",
                               code="UNKNOWN", reason="query_failed", table=main_table)

    tables_meta = [main_table] + join_list
    if not rows:
        return ToolResult.ok("查询结果为空", table=main_table, tables=tables_meta,
                             rows=[], row_count=0, total_count=total_count)

    # 格式化结果（排版唯一实现在 core/formatters，不再寄生 DBChat 私有方法）
    from core.formatters import format_multi_table
    result = format_multi_table({"JOIN结果": rows})
    truncated = total_count > JOIN_ROW_LIMIT
    if truncated:
        result += (f"\n\n（共 {total_count} 条，已显示前 {JOIN_ROW_LIMIT} 条。"
                  "如需查看完整数据，请缩小查询范围或使用导出功能。）")
    return ToolResult.ok(result, table=main_table, tables=tables_meta, rows=rows,
                         row_count=len(rows), total_count=total_count,
                         truncated=truncated)


def join_query(main_table: str, join_tables: str = "", select_fields: str = "*",
               where: str = "", join_type: str = "LEFT", on_condition: str = "") -> "ToolResult":
    """多表联合查询——通过 schema 外键配置自动推断 ON 条件，或手动指定 ON 条件

    Args:
        main_table: 主表名
        join_tables: 要关联的表名，逗号分隔（如 "region,specialty"）
        select_fields: 查询字段（如 "t1.code, t2.name"），默认 "*"
        where: WHERE 条件（支持 table.column 格式，如 "region.id=1"）
        join_type: JOIN 类型，INNER / LEFT / RIGHT，默认 LEFT
            （明细联查以主表为准——明细表为空的行保留主表、明细列补 NULL；
            INNER 会把空明细表的主表行整体吞掉，主表行在明细为空时返空）
        on_condition: 手动 ON 条件——JSON 数组字符串，如 '[{"left":"a.id","op":"=","right":"b.a_id"}]'（结构化枚举，AI 不产出 SQL 文本）。
                      显式给出时优先使用，跳过外键自动推断；
                      多表关联时该条件对所有 JOIN 子句复用

    Returns:
        ToolResult：text 为格式化结果；data 带
        {rows, row_count, total_count, truncated, tables}，失败带 code/reason

    联邦数据库：跨数据源的 JOIN 自动走应用层编排（federated_join）
    """
    # 防御性校验：主表名合法性（防 SQL 注入）
    SecurityContract.validate_identifier(main_table, "表名")

    # 联邦数据库：检测跨库 → 委托给跨库 JOIN 编排器
    join_list = [t.strip() for t in join_tables.split(",") if t.strip()] if join_tables else []
    if join_list:
        fed_result = _try_federated_join(main_table, join_tables, join_list,
                                         select_fields, where, join_type)
        if fed_result is not None:
            return fed_result

    drv = _ops.get_driver()
    join_type, err = _validate_join_params(drv, main_table, join_list, join_type)
    if err is not None:
        return err
    on_clause_manual, err = _parse_join_on_condition(on_condition, main_table)
    if err is not None:
        return err
    sqls, err = _build_join_sql(main_table, join_tables, join_list, join_type,
                                on_clause_manual, select_fields, where)
    if err is not None:
        return err
    sql, count_sql = sqls
    return _run_join_query(drv, sql, count_sql, main_table, join_list)
