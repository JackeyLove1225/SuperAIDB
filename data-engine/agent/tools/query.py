"""查询域——只读查询类工具 handler。

list_databases / describe_schema / query / join_query / aggregate_query /
list_selections。
"""
import math
import json as _json

from core.tool_result import ToolResult
from core.condition_parser import extract_conditions, build_where
from core.contract.security_contract import SecurityContract

from agent.tools._shared import _validate_table_name


def list_databases():
    """列出所有已配置的数据库（联邦：从 DataSourceManager 聚合全部注册数据源）

    输出列：名称/类型/是否默认/表数量/路径。
    无 datasources.yml 配置时，DataSourceManager 自动从 settings 回退出
    单个默认数据源，输出与旧版单库表格结构兼容（仅多出"默认/表数量"两列）。
    """
    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()
    dsm.load_config()
    ds_list = dsm.list_datasources()
    lines = ["| 序号 | 数据库名称 | 类型 | 默认 | 表数量 | 路径 |",
             "|------|----------|------|------|--------|------|"]
    if not ds_list:
        lines.append("| - | 暂无数据库 | - | - | - | - |")
        return ToolResult.ok("\n".join(lines), datasources=[], count=0)
    ds_meta = []
    for i, ds in enumerate(ds_list, 1):
        # 表数量优先实测（list_tables），数据源不可用时回退到已注册表映射数
        try:
            table_count = len(dsm.get_driver(ds["name"]).list_tables())
        except Exception:
            table_count = ds.get("table_count", 0)
        is_default = "是" if ds.get("is_default") else ""
        lines.append(
            f"| {i} | {ds['name']} | {ds['type']} | {is_default} "
            f"| {table_count} | {ds.get('database', '')} |"
        )
        ds_meta.append({"name": ds["name"], "type": ds["type"],
                        "is_default": bool(ds.get("is_default")),
                        "table_count": table_count,
                        "database": ds.get("database", "")})
    return ToolResult.ok("\n".join(lines), datasources=ds_meta, count=len(ds_meta))


def join_query_tool(main_table="", join_tables="", select_fields="*", where="",
                    join_type="LEFT", on_condition="", database=""):
    """多表联合查询——通过外键关系自动推断 ON 条件，或手动指定 ON 条件"""
    if not main_table:
        return ToolResult.fail("请指定主表名", code="VALIDATION", reason="missing_params")
    if not join_tables:
        return ToolResult.fail("请指定要关联的表名（逗号分隔）", code="VALIDATION",
                               reason="missing_params")
    from core.data_ops import join_query
    return join_query(main_table=main_table, join_tables=join_tables,
                      select_fields=select_fields, where=where, join_type=join_type,
                      on_condition=on_condition)


def aggregate_query_tool(table="", agg_func="COUNT", agg_field="*", group_by="", having="", where="", order_by="", database=""):
    """聚合统计查询——COUNT/SUM/AVG/MIN/MAX + GROUP BY + HAVING"""
    if not table:
        return ToolResult.fail("请指定表名", code="VALIDATION", reason="missing_params")
    if not agg_func:
        return ToolResult.fail("请指定聚合函数（COUNT/SUM/AVG/MIN/MAX）",
                               code="VALIDATION", reason="missing_params")
    from core.data_ops import aggregate_query
    return aggregate_query(table=table, agg_func=agg_func, agg_field=agg_field,
                           group_by=group_by, having=having, where=where, order_by=order_by)


def describe_schema(table="", column="", database=""):
    """查看表结构（双轨）：data 带 {table, column}；column 查询不存在时 NOT_FOUND"""
    from core.schema_manager import describe_schema_format
    result = describe_schema_format(table)
    if not table or not column:
        return ToolResult.ok(result, table=table or None)
    col_lower = column.lower()
    for line in result.split("\n"):
        if col_lower in line.lower() and "(" in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            for p in parts:
                if col_lower in p.lower() and "(" in p:
                    col_type = p.split("(")[1].rstrip(")")
                    return ToolResult.ok(f"{column} 的数据类型是 {col_type}",
                                         table=table, column=column, col_type=col_type)
    return ToolResult.fail(f"字段 '{column}' 在表 {table} 中不存在",
                           code="NOT_FOUND", reason="column_not_found",
                           table=table, column=column)



def _ai_parse_conditions(query, conditions, table):
    """条件解析：按运行模式决定是否用 AI 从自然语言提取条件

    返回 (conditions, err)——err 非 None 时主流程直接返回该 ToolResult。
    """
    if conditions or not query:
        return conditions, None
    # FC_AI_ENABLED=false 时，LangGraph 应在 structured_args 中提供 conditions
    # 如果没有 conditions，说明 LangGraph 没提取出条件，这里不再用 AI 兜底
    from config.settings import settings as _s
    if not _s.FC_AI_ENABLED:
        # structured_args 模式：不用 AI 解析条件，直接用 query 查询
        return conditions, None
    # 单步模式/FC_AI_ENABLED=True：用 AI 从自然语言解析条件
    from core.ai_runtime.ai_client import AIClient
    try:
        conds = extract_conditions(query, AIClient.get_instance())
        if conds:
            conditions = _json.dumps(conds, ensure_ascii=False)
    except Exception as e:
        # 解析失败必须显式报错——静默继续会退化为无 WHERE 全表查询
        return None, ToolResult.fail(f"查询条件解析失败: {e}", code="VALIDATION",
                                     reason="conditions_parse_failed", table=table)
    return conditions, None


def _parse_cond_list(conditions, table):
    """conditions JSON 解析与 dict→list 归一化：返回 (cond_list, err)"""
    cond_list = []
    if not conditions:
        return cond_list, None
    try:
        parsed = _json.loads(conditions) if isinstance(conditions, str) else conditions
        # dict 格式归一化为 list（AI 偶尔输出 {"field":"value"} 而非
        # [{"field","op","value"}]）：value 是 list 展开为多个 "=" 条件，
        # 由 build_where 自动归并为 IN——query 归一化处理 20260803
        if isinstance(parsed, dict):
            _items = []
            for _k, _v in parsed.items():
                if isinstance(_v, list):
                    _items.extend({"field": _k, "op": "=", "value": _vi} for _vi in _v)
                else:
                    _items.append({"field": _k, "op": "=", "value": _v})
            parsed = _items
        if isinstance(parsed, list):
            cond_list = [{"field": c["field"], "op": c["op"], "value": str(c["value"])} for c in parsed]
    except Exception as e:
        # 解析失败必须显式报错——静默继续会退化为无 WHERE 全表查询
        return None, ToolResult.fail(f"查询条件解析失败: {e}", code="VALIDATION",
                                     reason="conditions_parse_failed", table=table)
    return cond_list, None


def _build_safe_where(cond_list, table):
    """WHERE 子句构建 + 安全契约校验：返回 (cond_sql, err)"""
    try:
        cond_sql = build_where(cond_list)
    except ValueError as e:
        return None, ToolResult.fail(f"查询条件不安全: {e}", code="CONTRACT",
                                     reason="unsafe_conditions", table=table)
    if cond_sql:
        # 最终 WHERE 子句强制过安全契约校验（剥离 "WHERE " 前缀）
        try:
            _w = cond_sql.strip()
            if _w.upper().startswith("WHERE "):
                _w = _w[6:]
            SecurityContract.validate_where(_w)
        except Exception as e:
            return None, ToolResult.fail(f"WHERE 条件不安全，已拒绝执行: {e}",
                                         code="CONTRACT", reason="unsafe_where", table=table)
    return cond_sql, None


def _normalize_paging(page, page_size):
    """分页参数校验与规范化：返回 (page, page_size, offset)"""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, min(int(page_size), 500))  # 上限 500 防止滥用
    except (TypeError, ValueError):
        page_size = 100
    return page, page_size, (page - 1) * page_size


def _count_total(drv, table, cond_sql):
    """查询总数——使用 Driver 标准 query() 方法（联邦数据库兼容）：

    返回 (total_count, err)"""
    count_sql = 'SELECT COUNT(*) as c FROM "' + table + '"' + cond_sql
    try:
        count_rows = drv.query(count_sql)
        total_count = count_rows[0]["c"] if count_rows else 0
    except Exception as e:
        # 不再用 -1 哨兵伪装成功，COUNT 失败显式报错
        return None, ToolResult.fail(f"查询总数统计失败: {e}", code="UNKNOWN",
                                     reason="count_failed", table=table)
    return total_count, None


def _fetch_page_rows(drv, table, cond_sql, order_by, page_size, offset):
    """分页查询执行——query() 返回 list[dict]；ORDER BY 先行（排序是基础查询意图，
    不再靠上层 AI 在答复文本里手工排序，20260825）

    返回 (rows_dict, err)"""
    order_sql = ""
    if order_by:
        from core.data_ops import validate_order_by
        if not validate_order_by(order_by):
            return None, ToolResult.fail(f"ORDER BY 子句不安全，已拒绝执行: '{order_by[:80]}'",
                                         code="CONTRACT", reason="unsafe_order_by", table=table)
        order_sql = f" ORDER BY {order_by}"
    sql = 'SELECT * FROM "' + table + '"' + cond_sql + order_sql + f' LIMIT {page_size} OFFSET {offset}'
    rows_dict = drv.query(sql)
    # 字段列表通过 get_columns() 获取（跨驱动标准接口）
    return rows_dict, None


def _assemble_query_result(table, rows_dict, query, total_count, page, page_size):
    """结果装配：格式化表格 + 分页信息 + 选择集暂存"""
    from core.context import get_context
    if not rows_dict:
        return ToolResult.ok("查询结果为空", table=table, rows=[], row_count=0,
                             total_count=total_count, page=page, page_size=page_size)
    # 排版唯一实现在 core/formatters，不再寄生 DBChat 私有方法
    from core.formatters import format_multi_table
    sid = get_context().save_selection(table, rows_dict, query or "")
    result = format_multi_table({table: rows_dict})

    # 分页信息
    total_pages = math.ceil(total_count / page_size) if total_count >= 0 else 0
    has_more = (page * page_size) < total_count if total_count >= 0 else False

    page_info_lines = []
    if total_count >= 0:
        page_info_lines.append(f"（共 {total_count} 条，第 {page}/{total_pages} 页，每页 {page_size} 条）")
    if has_more:
        page_info_lines.append(f"下一页：page={page+1}（如需加载更多，请指定 page={page+1}）")
    if page > 1:
        page_info_lines.append(f"上一页：page={page-1}")
    if page_info_lines:
        result += "\n\n" + " ".join(page_info_lines)

    if sid and len(rows_dict) > 0:
        result += f"\n已暂存为选择集 selection_id={sid}（{len(rows_dict)}条，表：{table}）"
    return ToolResult.ok(result, table=table, rows=rows_dict,
                         row_count=len(rows_dict), total_count=total_count,
                         page=page, page_size=page_size,
                         total_pages=total_pages, has_more=has_more,
                         selection_id=sid)


def _query_with_fallback(query="", table="", column="", conditions="", database="", page=1, page_size=100, order_by=""):
    """查询工具（双轨）：text 为格式化表格/提示文案；data 带
    {table, rows, row_count, total_count, page, page_size, selection_id}"""
    if not query and not table:
        from core.db_chat import DBChat
        chat = DBChat()
        return ToolResult.ok(str(chat.ask(query)), source="db_chat")
    # 关键词短路分支已删除——意图路由是决策树的工作（白盒），
    # tools 层不得按 query 文本劫持（"暂存"可能是数据值、"安装"是工程行业高频词）。
    # field_op 消歧由 agent/__init__.py 主流程统一处理。
    conditions, err = _ai_parse_conditions(query, conditions, table)
    if err is not None:
        return err
    if table:
        from core.data_ops import get_driver
        drv = get_driver()  # FederatedDriver：自动路由到表所属数据源
        _validate_table_name(table)
        cond_list, err = _parse_cond_list(conditions, table)
        if err is not None:
            return err
        cond_sql, err = _build_safe_where(cond_list, table)
        if err is not None:
            return err
        page, page_size, offset = _normalize_paging(page, page_size)
        total_count, err = _count_total(drv, table, cond_sql)
        if err is not None:
            return err
        rows_dict, err = _fetch_page_rows(drv, table, cond_sql, order_by, page_size, offset)
        if err is not None:
            return err
        return _assemble_query_result(table, rows_dict, query, total_count, page, page_size)
    from core.db_chat import DBChat
    chat = DBChat()
    return ToolResult.ok(str(chat.ask(query or "")), source="db_chat")


def list_selections_tool(as_json: bool = False):
    """列出选择集（双轨）：data 带 {selections, count}，无选择集时 count=0"""
    from core.context import get_context
    sels = get_context().list_selections()
    if not sels:
        return ToolResult.ok("[]" if as_json else "当前无选择集",
                             selections=[], count=0)
    sel_meta = [{
        "id": s["id"], "table": s["table"], "count": s["count"],
        "query": s["query"],
        "sample": [{k: v for k, v in r.items()} for r in s["sample"]],
    } for s in sels]
    if as_json:
        return ToolResult.ok(_json.dumps([{
            "id": "selection_id=%s" % s["id"],
            "table": s["table"],
            "count": s["count"],
            "query": s["query"],
            "sample": s["sample"]
        } for s in sel_meta], ensure_ascii=False), selections=sel_meta, count=len(sel_meta))
    hdr = "| ID | 查询内容 | 表 | 条数 | 样例 |"
    sep = "|----|---------|-----|------|------|"
    lines = [hdr, sep]
    for s in sels:
        sample = s["sample"][0] if s["sample"] else {}
        svals = ", ".join("%s=%s" % (k, v) for k, v in list(sample.items())[:3])
        lines.append("| **selection_id=%s** | %s | %s | %s | %s |" % (s["id"], s["query"], s["table"], s["count"], svals))
    return ToolResult.ok("\n".join(lines), selections=sel_meta, count=len(sel_meta))
