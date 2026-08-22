"""DDL 域——表结构/字段/外键/索引类工具 handler。

batch_create_tables / create_standard_tables / drop_table / add_column /
drop_column / modify_column / alter_precision / set_not_null /
add_foreign_key / drop_foreign_key / create_index / drop_index。
"""
import json as _json

from core.tool_result import ToolResult

from agent.tools._shared import _msg_result, _require_params, _schema_tool


# ============ 表结构 ============

def batch_create_tables(definitions="", database=""):
    if not definitions:
        return ToolResult.fail("请提供表结构定义", code="VALIDATION",
                               reason="missing_params")
    from core.schema_manager import batch_create_tables as _bt
    if isinstance(definitions, str):
        try:
            definitions = _json.loads(definitions)
        except Exception:
            return ToolResult.fail("表结构定义 JSON 解析失败", code="VALIDATION",
                                   reason="data_format")
    if isinstance(definitions, dict):
        definitions = [definitions]
    # 一次性传入所有定义，让 schema_manager 做 FK 拓扑排序
    r = _bt(definitions)
    tr = _msg_result(r)
    if tr.data.get("ok"):
        tr.data.setdefault("effects", {
            "table": [d.get("name", "") for d in definitions if isinstance(d, dict)],
            "action": "DDL", "affected": 0,
            "affected_ids": [], "changed_fields": [],
        })
    return tr


create_standard_tables = _schema_tool(
    "core.schema_manager.create_standard_tables_with_check",
    {"database": ""},
)


def drop_table_tool(table="", all=False, database=""):
    _batch_keywords = {"全部", "所有", "清空", "一切"}
    # 路由兜底：「删除所有表格」被 LLM 归类为"表"对象时，table 参数可能是
    # "所有表格/全部表"等不存在表名 → 含批量关键词即转 all=True 清库，
    # 不再误报"表不存在"（_batch_keywords 此前定义未启用，20260804 补上）
    if not all and table and any(k in table for k in _batch_keywords):
        all = True
        table = ""
    if all:
        from core.schema_manager import clear_database
        r = clear_database(drop_tables=True)
        tr = _msg_result(r)
        if tr.data.get("ok"):
            tr.data.setdefault("effects", {"table": "*", "action": "DROP",
                                           "affected": 0, "affected_ids": [],
                                           "changed_fields": []})
        return tr
    if not table:
        return ToolResult.fail("请指定表名", code="VALIDATION", reason="missing_params")
    from core.schema_manager import drop_table
    r = drop_table(table)
    tr = _msg_result(r)
    if tr.data.get("ok"):
        tr.data.setdefault("effects", {"table": table, "action": "DROP",
                                       "affected": 0, "affected_ids": [],
                                       "changed_fields": []})
    return tr


# ============ 字段操作 ============

def _default_col_type(kwargs):
    if not kwargs.get("col_type"):
        kwargs["col_type"] = "TEXT"


add_column_tool = _schema_tool(
    "core.schema_manager.add_column",
    {"table": "", "column": "", "col_type": "TEXT", "not_null": False, "database": ""},
    validate=_require_params("table", "column", msg="请指定表名和字段名"),
    transform=_default_col_type,
)

drop_column_tool = _schema_tool(
    "core.schema_manager.drop_column",
    {"table": "", "column": "", "force": False, "database": ""},
    validate=_require_params("table", "column", msg="请指定表名和字段名"),
)

modify_column_tool = _schema_tool(
    "core.schema_manager.modify_column",
    {"table": "", "column": "", "new_type": "TEXT", "force": False, "database": ""},
    validate=_require_params("table", "column", msg="请指定表名和字段名"),
)


def _precision_arg(kwargs):
    # schema_manager.alter_precision 的形参名为 precision_str
    kwargs["precision_str"] = kwargs.pop("precision")


alter_precision_tool = _schema_tool(
    "core.schema_manager.alter_precision",
    {"table": "", "column": "", "precision": "", "force": False, "database": ""},
    validate=_require_params("table", "column", "precision", msg="请指定表名、字段名和新精度"),
    transform=_precision_arg,
)

set_not_null_tool = _schema_tool(
    "core.schema_manager.set_not_null",
    {"table": "", "column": "", "database": ""},
    validate=_require_params("table", "column", msg="请指定表名和字段名"),
)


# ============ 外键 ============

add_foreign_key_tool = _schema_tool(
    "core.schema_manager.add_foreign_key",
    {"table": "", "column": "", "ref_table": "", "force": False, "database": ""},
    validate=_require_params("table", "column", "ref_table", msg="请指定表名、外键字段名和被引用的表名"),
)


def _fk_constraint_arg(kwargs):
    # schema_manager.drop_foreign_key 的形参名为 constraint_name
    kwargs["constraint_name"] = kwargs.pop("column")


drop_foreign_key_tool = _schema_tool(
    "core.schema_manager.drop_foreign_key",
    {"table": "", "column": "", "force": False, "database": ""},
    validate=_require_params("table", "column", msg="请指定表名和要删除的外键字段名"),
    transform=_fk_constraint_arg,
)


# ============ 索引 ============

def _index_columns_arg(kwargs):
    # schema_manager.create_index 的形参名为 columns
    kwargs["columns"] = kwargs.pop("column")


create_index_tool = _schema_tool(
    "core.schema_manager.create_index",
    {"table": "", "column": "", "database": "", "unique": True},
    validate=_require_params("table", "column", msg="请指定表名和字段名"),
    transform=_index_columns_arg,
)


def drop_index_tool(table="", column="", database="", index_name=""):
    if not table or not column:
        return ToolResult.fail("请指定表名和索引列名", code="VALIDATION",
                               reason="missing_params")
    name = index_name or f"idx_{table}_{column}"
    from core.schema_manager import drop_index
    r = drop_index(name)
    return _msg_result(r)
