"""schema 展示域：describe_schema_format 及其格式化助手
（20260822 拆包：core/schema_manager.py 同名片段纯搬家，逻辑零变化）
"""
import yaml

from ._shared import _get_schema_dir


def _load_schema_map(schema_dir) -> dict:
    """加载所有 YAML schema 文件，返回 {表名: 配置}"""
    table_map = {}
    for p in sorted(schema_dir.glob("*.yaml")):
        t = yaml.safe_load(p.read_text(encoding="utf-8"))
        if t and t.get("name"):
            table_map[t["name"]] = t
    return table_map


def _build_fk_display(tname, info, full_table_map) -> str:
    """计算一张表的 FK 引用/被引用关系，返回单元格文本"""
    # 被谁引用
    refs = []
    for n, i in full_table_map.items():
        for fk in i.get("foreign_keys", []):
            if fk.get("references", "").lower() == tname.lower():
                refs.append(n)
                break
    fk_parts = []
    if refs:
        fk_ref_pairs = []
        for r in refs:
            ri = full_table_map.get(r, {})
            matched_fields = []
            for col in info.get("columns", []):
                for fk in ri.get("foreign_keys", []):
                    if fk.get("references","").lower() == tname.lower() and col["name"].lower() in [x.lower() for x in (fk.get("ref_columns",[]) or fk.get("columns",[]))]:
                        matched_fields.append(col["name"])
                        break
            if not matched_fields:
                for fk in ri.get("foreign_keys", []):
                    if fk.get("references","").lower() == tname.lower():
                        matched_fields.append(fk.get("columns", [""])[0])
                        break
            if matched_fields:
                fk_ref_pairs.append((r, matched_fields))
        if fk_ref_pairs:
            parts = []
            for ref_name, fields_list in fk_ref_pairs:
                for f in fields_list:
                    parts.append(f"【{tname}.{f}】←【{ref_name}】")
            fk_parts.append("本表被引用关系\uff1a" + "、".join(parts))
    # 引用谁
    fk_forward = []
    for fk in info.get("foreign_keys", []):
        ref_t = fk.get("references", "")
        ref_cs = fk.get("ref_columns", []) or fk.get("columns", [])
        for fk_col, ref_c in zip(fk.get("columns", []), ref_cs):
            fk_forward.append(f"【{tname}.{fk_col}】→【{ref_t}.{ref_c}】")
    if fk_forward:
        fk_parts.append("本表引用关系\uff1a" + "、".join(fk_forward))
    return "；".join(fk_parts) if fk_parts else "-"


def _format_table_row(tname, info, max_cols, full_table_map, db_tables) -> str:
    """格式化一张表为 Markdown 行"""
    fields = []
    for col in info.get("columns", []):
        cn = col["name"]
        ct = col.get("type", "")
        if " " in cn and cn.rsplit(" ", 1)[1].upper() in ("TEXT","INTEGER","FLOAT","VARCHAR"):
            cn = cn.rsplit(" ", 1)[0]
        if ct:
            cn = cn + " (" + ct + ")"
        fields.append(cn)
    while len(fields) < max_cols:
        fields.append("")
    fk = _build_fk_display(tname, info, full_table_map)
    field_str = "".join("| " + f + " " for f in fields)
    exists = tname in db_tables
    if not exists:
        return "| " + tname + " " + field_str + "| " + fk + " (未创建) |"
    return "| " + tname + " " + field_str + "| " + fk + " |"

def _build_table_info_from_db(d, tname: str) -> dict:
    """从数据库实际表构造 schema info（YAML 未定义时回退使用）"""
    try:
        cols = d.get_columns(tname)
        return {
            "name": tname,
            "columns": [{"name": c["name"], "type": c.get("type", "")} for c in cols],
            "foreign_keys": [],
        }
    except Exception:
        return {"name": tname, "columns": [], "foreign_keys": []}


def describe_schema_format(table: str = "") -> str:
    """查询表结构并格式化为表格字符串

    数据来源优先级：数据库实际表为准，YAML 补充元数据（字段描述、外键关系）。
    这样保证 AI 看到的表列表与控制台概览一致。

    Driver 选择：使用 FederatedDriver（get_driver）而非 primary driver，
    确保跨数据源的表都能被列出（与控制台概览一致）。
    """
    from core.data_ops import get_driver as _get_fed_driver
    d = _get_fed_driver()
    db_tables = set(d.list_tables())
    schema_dir = _get_schema_dir()

    # YAML 配置（可能为空——行业未定义 schema 或目录不存在）
    table_map = _load_schema_map(schema_dir) if schema_dir.exists() else {}

    # 数据库实际表补充进 table_map（YAML 未定义的表，从 DB 获取字段）
    for tname in db_tables:
        if tname.startswith("sqlite_"):
            continue  # 跳过 SQLite 系统表
        if tname not in table_map:
            table_map[tname] = _build_table_info_from_db(d, tname)

    full_table_map = table_map.copy()

    if table:
        info = table_map.get(table)
        if not info:
            return f"表 {table} 不存在"
        table_map = {table: info}

    if not table_map:
        return "| 表名 | 字段1 | 外键 |\n|------|------|------|\n| （无任何表） | | |"

    max_cols = max(len(info.get("columns", [])) for info in table_map.values())
    col_headers = "".join(f"| 字段{i+1} " for i in range(max_cols))
    header_row = f"| 表名 {col_headers}| 外键 |"
    divider = "|" + "|".join("------" for _ in range(2 + max_cols)) + "|"
    all_exist = all(t in db_tables for t in table_map)
    head = "数据库中有以下表（YAML 配置与数据库一致）：" if all_exist else "数据库中有以下表（基于 YAML 配置）："

    lines = [head, header_row, divider]
    for tname, info in table_map.items():
        lines.append(_format_table_row(tname, info, max_cols, full_table_map, db_tables))
    return "\n".join(lines)
