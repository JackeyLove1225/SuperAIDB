
"""Phase 2: Schema 匹配器

职责：根据 Phase 1 输出 + trace_path，从数据库层级逐层向下匹配。
上层不确定就不往下走。
"""

import yaml
import re
from pathlib import Path
from config.settings import settings


def _get_db_name() -> str:
    """获取当前数据库名称"""
    from config.settings import settings
    return settings.SQLITE_DB_PATH if hasattr(settings, 'SQLITE_DB_PATH') else "default"




def _resolve_table_by_fields(user_input: str, tables: list) -> tuple:
    """通过字段名加权评分反查表名
    返回: (table_name, candidates, message)
    - 唯一匹配: ("t100", [], "")
    - 多表匹配: ("", ["t100","t200"], "字段匹配到多个表")
    - 无匹配:   ("", [], "")
    """
    field_cand = {}
    for t in tables:
        for c in t.get("columns", []):
            cn = c["name"]
            if re.search(r"(?<![a-zA-Z0-9_])" + re.escape(cn) + r"(?![a-zA-Z0-9_])", user_input):
                field_cand.setdefault(t["name"], 0)
                field_cand[t["name"]] += 1
    if not field_cand:
        return ("", [], "")
    max_score = max(field_cand.values())
    top = [t for t, s in field_cand.items() if s == max_score]
    if len(top) == 1:
        return (top[0], [], "")
    return ("", top, "字段匹配到多个表")

def _load_schemas(schema_dir=None) -> list:
    """加载行业全部 schema YAML（规范入口）。

    schema_dir：可选目录覆盖（测试夹具注入用）；缺省为当前行业 schemas 目录。
    """
    if schema_dir is None:
        schema_dir = Path(__file__).resolve().parent.parent / "industries" / settings.INDUSTRY / "schemas"
    tables = []
    for p in sorted(schema_dir.glob("*.yaml")):
        t = yaml.safe_load(p.read_text(encoding="utf-8"))
        if t and t.get("name"):
            tables.append(t)
    # 联邦数据库：将 schema 中的 datasource 字段注册到 DataSourceManager
    # 这样 FederatedDriver 能根据表名路由到正确的数据源
    # 单数据源模式：所有表注册到默认数据源，行为不变
    try:
        from core.datasource_manager import DataSourceManager
        DataSourceManager().register_tables_from_schemas(tables)
    except Exception:
        # 注册失败不影响 schema 加载（单数据源模式不需要注册）
        pass
    return tables


def load_table_schema(table: str) -> dict | None:
    """加载单张表的 schema YAML——规范加载入口（P2-5 收敛点）。

    data_ops/schema_graph_service/sqlite_driver 的散置加载统一走这里。
    找不到/解析失败返回 None（调用方按 None 处理，不静默吞错）。
    """
    schema_dir = Path(__file__).resolve().parent.parent / "industries" / settings.INDUSTRY / "schemas"
    for ext in (".yaml", ".yml"):
        f = schema_dir / f"{table}{ext}"
        if f.exists():
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
            except Exception:
                return None
            return data if isinstance(data, dict) else None
    return None


def get_unique_key_column(table: str) -> str:
    """读表的唯一业务键列名——通用唯一键机制（P1-2，唯一实现点）。

    声明方式（schema YAML，二选一）：
    - indexes: [{columns: [xxx], unique: true}] 单字段唯一索引
    - 列级 unique: true
    返回列名；无声明或仅多列复合唯一索引时返回 ""
    （复合唯一键不做应用层冲突检测，留给 DB 约束兜底）。
    调用方：sqlite_driver.insert 冲突检测、db_chat 编码兜底。
    """
    for t in _load_schemas():
        if t.get("name") != table:
            continue
        for idx in t.get("indexes", []):
            cols = idx.get("columns", [])
            if idx.get("unique") and len(cols) == 1 and cols[0] != "id":
                return cols[0]
        for col in t.get("columns", []):
            if col.get("name") != "id" and col.get("unique"):
                return col["name"]
    return ""


def _load_biz_mapping():
    map_path = Path(__file__).resolve().parent.parent / "industries" / settings.INDUSTRY / "config" / "db_mapping.yml"
    if not map_path.exists():
        return {}
    try:
        return yaml.safe_load(map_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


_LEVEL_MAP = {
    "q_table":"表","del_table":"表","add_table":"表","mod_table":"表",
    "q_table_str":"表",
    "q_record":"记录","del_record":"记录","add_record":"记录","mod_record":"记录",
    "del_field":"字段","add_field":"字段","mod_field":"字段",
    "del_ss":"会话","add_ss":"会话",
    "q_tmpl":"模板","del_tmpl":"模板","add_tmpl":"模板",
    "q_file":"文件","del_file":"文件","add_file":"文件",
    "q_db":"数据库","del_db":"数据库",
}


def _match_database_level(user_input: str) -> str:
    """数据库层级匹配：当前仅一个 SQLite 库，检查别名后返回库名

    多库时代：从 db_mapping.yml 读取 database_mapping 做匹配。
    注意：返回值当前未被 resolve 使用（最终返回用 _get_db_name()），保留与原逻辑一致。
    """
    from config.settings import settings as _s
    _db_name = getattr(_s, "SQLITE_DB_PATH", "data_engine.db")
    database = _db_name
    # 检查用户输入是否指向特定库
    db_aliases = {"main": _db_name, "数据引擎": _db_name}
    for alias, real_name in db_aliases.items():
        if alias in user_input:
            database = real_name
            break
    return database


def _match_tables_by_mapping(user_input: str, table_map: dict) -> list:
    """表匹配第 1 级：业务映射别名（table_mapping），优先级 100"""
    cand = []
    for biz, tname in table_map.items():
        if biz in user_input:
            cand.append((100, tname, biz))
    return cand


def _match_tables_by_schema(user_input: str, tables: list) -> list:
    """表匹配第 2 级：schema 原始表名（优先级 50）+ 业务名/注释（优先级 80）"""
    cand = []
    ui_lower = user_input.lower()
    for t in tables:
        tn = t["name"]
        if tn.lower() in ui_lower or tn in user_input:
            cand.append((50, tn, tn))
        biz_name = t.get("business_name", "") or t.get("comment", "")
        if biz_name and biz_name.lower() in ui_lower:
            cand.append((80, tn, biz_name))
    return cand


def _match_tables_by_db(user_input: str, tables: list) -> list:
    """表匹配第 3 级（兜底）：DB 中用户自建表，优先级 50"""
    cand = []
    ui_lower = user_input.lower()
    try:
        from core.steward import Steward
        for _tbl in Steward()._get_driver().list_tables():
            if (_tbl.lower() in ui_lower or _tbl in user_input) and _tbl not in [t["name"] for t in tables]:
                cand.append((50, _tbl, _tbl))
    except Exception:
        pass
    return cand


def _pick_table(cand: list) -> tuple:
    """候选排序取最优 + 歧义检测

    排序规则：优先级降序，同优先级按匹配串长度降序（稳定排序，保持收集顺序）。
    返回: (table, error_dict_or_None)
    - 唯一最优: (table, None)
    - 同优先级多表: ("", ambiguous_dict)
    """
    cand.sort(key=lambda x: (-x[0], -len(x[2])))
    table = cand[0][1]
    top_priority = cand[0][0]
    top_tables = [c[1] for c in cand if c[0] == top_priority]
    if len(set(top_tables)) > 1:
        return ("", {"table":"", "column":"", "conditions":[], "ambiguous":True,
                     "candidates":[{"table":t} for t in set(top_tables)],
                     "message":"多个表匹配：" + "、".join(set(top_tables)) + "，请确认"})
    return (table, None)


def _resolve_table_level(user_input: str, tables: list, table_map: dict) -> tuple:
    """表层级匹配编排：映射别名(100) → schema表名/业务名(50/80) → DB自建表兜底(50)

    前两级候选合并收集；仅当都未命中时才走 DB 兜底。
    返回: (table, error_dict_or_None)；("", None) 表示未命中（不阻断，
    表名可能由选择集等下游提供）。
    """
    cand = _match_tables_by_mapping(user_input, table_map)
    cand += _match_tables_by_schema(user_input, tables)
    if not cand:
        cand = _match_tables_by_db(user_input, tables)
    if not cand:
        return ("", None)
    return _pick_table(cand)


def _resolve_record_level(user_input: str, tables: list, field_map: dict, table: str) -> tuple:
    """记录层级匹配：确定表名 + 提取条件字段

    返回: (table, conditions, error_dict_or_None)
    - ("", [], None): 未命中（不阻断，表名可能由选择集等下游提供）
    """
    conditions = []
    if not table:
        # 用 field_mapping 反查哪些表有匹配的字段
        cand = []
        for biz, fn in field_map.items():
            if biz in user_input:
                for t in tables:
                    for col in t.get("columns",[]):
                        if col["name"] == fn:
                            cand.append(t["name"])
                            break
        if not cand:
            tbl, candidates, msg = _resolve_table_by_fields(user_input, tables)
            if tbl:
                table = tbl
            elif candidates:
                return ("", [], {"database":"","table":"","column":"","conditions":[],"ambiguous":True,
                                 "candidates":[{"table":t} for t in candidates],
                                 "message":msg + "，请确认表名"})
            else:
                return ("", [], None)  # 不阻断：表名可能由选择集等下游提供
        else:
            uni = list(set(cand))
            if len(uni) == 1:
                table = uni[0]
            else:
                return ("", [], {"table":"","column":"","conditions":[],"ambiguous":True,
                                 "candidates":[{"table":t} for t in uni],
                                 "message":"字段在多个表中存在：" + "、".join(uni) + "，请确认"})
    # 提取条件
    for biz, fn in field_map.items():
        if biz in user_input and table:
            # 确认该字段在确定后的表中存在
            for t in tables:
                if t["name"] == table:
                    for col in t.get("columns",[]):
                        if col["name"] == fn:
                            conditions.append({"field":fn,"op":"=","value":user_input.split(biz,1)[1].strip()})
                            break
    return (table, conditions, None)


def _resolve_column_level(user_input: str, tables: list, table_map: dict, field_map: dict,
                          table: str, column: str) -> tuple:
    """字段层级匹配：先确定表，再找字段

    返回: (table, column)；表无法确定时原样返回（不阻断，
    表名可能由选择集等下游提供）。
    """
    if not table:
        for biz, tname in table_map.items():
            if biz in user_input:
                table = tname
                break
    if not table:
        return (table, column)  # 不阻断：表名可能由选择集等下游提供
    for biz, fn in field_map.items():
        if biz in user_input:
            if table:
                for t in tables:
                    if t["name"] == table:
                        for col in t.get("columns",[]):
                            if col["name"] == fn:
                                column = fn
                                break
            if not column:
                column = fn  # 即使表里没有，也保留映射名
            break
    # 如果 field_map 没匹配到，直接扫描表的实际字段名（大小写不敏感）
    if not column and table:
        ui_lower = user_input.lower()
        for t in tables:
            if t["name"] == table:
                for col in t.get("columns",[]):
                    if col["name"].lower() in ui_lower:
                        column = col["name"]
                        break
                break
    return (table, column)


def resolve(user_input, trace_path, tool_name, db_category_key, table="", column="") -> dict:
    """层级匹配入口：按 trace_path 的层级逐层向下，上层不确定就停

    只做编排：各级匹配逻辑在 _match_* / _resolve_*_level 函数中。
    """
    tables = _load_schemas()
    if not tables:
        return {"database":"","table":"","column":"","conditions":[],"ambiguous":False,"message":"无可用表结构"}

    # 上游已确定 table 和 column → 跳过扫描，不校验（校验是工具/DB的事）
    if table and column:
        return {"database":_get_db_name(),"table":table,"column":column,"conditions":[],"ambiguous":False,"message":""}

    # 提取 trace_path 中出现的层级（去重+保持顺序）
    levels = []
    for nid in trace_path:
        l = _LEVEL_MAP.get(nid)
        if l and l not in levels:
            levels.append(l)

    mapping = _load_biz_mapping()
    table_map = mapping.get("table_mapping", {})
    field_map = mapping.get("field_mapping", {})

    # 保留调用方传入的 table/column（如 AI FC 已识别），不重置
    # table 和 column 由参数传入，仅在未被早返回（上方 table and column 分支）时才需要扫描补充
    column = column or ""
    conditions = []

    for level in levels:
        if level == "数据库":
            _match_database_level(user_input)
        elif level == "表":
            # 调用方（如 AI FC）已确定 table → 跳过扫描，避免子串误匹配
            # （例如 "user_id" 含 "user" 导致多表匹配）
            if table:
                continue
            table, err = _resolve_table_level(user_input, tables, table_map)
            if err:
                return err
        elif level == "记录":
            # 记录层：需要确定表名 + 条件字段
            table, new_conditions, err = _resolve_record_level(user_input, tables, field_map, table)
            if err:
                return err
            conditions.extend(new_conditions)
        elif level == "字段":
            # 字段层：需要先确定表，再找字段
            table, column = _resolve_column_level(user_input, tables, table_map, field_map, table, column)

    return {"database":_get_db_name(),"table":table,"column":column,"conditions":conditions,"ambiguous":False,"message":""}
