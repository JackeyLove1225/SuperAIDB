"""
AI 数据库对话模块——自然语言增删改查 PostgreSQL + Chroma
内部解耦，分别调用 PG 和 Chroma 各自的方法
"""
import re

from typing import Optional

from core.ai_runtime.ai_client import AIClient
from core.logger import info as log_info, warning as log_warning


def _build_select_sql(table: str, fields: str, where: str = "",
                      order_by: str = "", group_by: str = "",
                      limit: int = 0) -> str:
    """构建 SELECT SQL——薄委托 data_ops.build_select_sql（查询路径收敛：
    拼装与校验的唯一实现点在 core/data_ops/base_ops.py）"""
    from core.data_ops import build_select_sql
    return build_select_sql(table, fields, where, order_by, group_by, limit)


def _load_code_pattern() -> str:
    """读行业 config.yml 的 code_pattern（业务编码的文本格式，行业知识属配置）"""
    try:
        import yaml as _yaml
        from pathlib import Path as _Path
        from config.settings import settings as _st
        cfg_path = (_Path(__file__).parent.parent / "industries" /
                    _st.INDUSTRY / "config" / "config.yml")
        if cfg_path.exists():
            return (_yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}).get("code_pattern", "") or ""
    except Exception:
        pass  # 配置读不出按空配置（后续字段映射恒等）
    return ""


def _build_fields_hint(table_map: dict) -> str:
    """可用字段名（按表分组，帮助 AI 正确匹配表名字段）"""
    table_fields = {}
    for t in table_map.values():
        tn = t["name"]
        table_fields[tn] = [c["name"] for c in t.get("columns", [])]
    fields_hint = "字段说明："
    for tn, cols in table_fields.items():
        fields_hint += f"{tn}({'/'.join(cols)}) "
    return fields_hint


def _build_query_tables_tool(table_enum: dict, fields_hint: str) -> list:
    """query_tables 的 FC schema（从 ask 函数体提取，表枚举与字段提示参数化）"""
    functions = [
        {
            "type": "function",
            "function": {
                "name": "query_tables",
                "description": "查询表中数据记录，支持 WHERE 条件筛选、聚合(COUNT/SUM/AVG)、排序、模糊搜索(LIKE)、分组(GROUP BY)。查具体数据时用这个！",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tables": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "table": table_enum,
                                    "where": {"type": "string", "description": f"[可选/向后兼容] WHERE 条件字符串。推荐使用 where_conditions。{fields_hint}"},
                                    "where_conditions": {
                                        "type": "array",
                                        "description": f"WHERE 条件（结构化，推荐使用这个）。{fields_hint}",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "field": {"type": "string", "description": "字段名"},
                                                "op": {"type": "string", "enum": ["=", ">", "<", ">=", "<=", "!=", "LIKE", "IN"]},
                                                "value": {"type": "string", "description": "值"},
                                                "link": {"type": "string", "enum": ["AND", "OR"], "description": "与上个条件的连接符"},
                                            },
                                            "required": ["field", "op", "value"],
                                        },
                                    },
                                    "select": {"type": "string", "description": "[可选/向后兼容] 查询字段字符串。推荐用 select_fields。"},
                                    "select_fields": {
                                        "type": "array",
                                        "description": f"查询字段（结构化，推荐）。{fields_hint}",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "field": {"type": "string", "description": "字段名"},
                                                "aggregate": {"type": "string", "enum": ["", "COUNT", "SUM", "AVG", "MAX", "MIN"], "description": "聚合函数（可选）"},
                                                "alias": {"type": "string", "description": "输出别名（可选）"},
                                            },
                                            "required": ["field"],
                                        },
                                    },
                                    "order_by": {"type": "string", "description": "[可选/向后兼容] 排序字符串。推荐用 order_by_fields。"},
                                    "order_by_fields": {
                                        "type": "array",
                                        "description": "排序（结构化，推荐）。如 [{\"field\": \"consumption\", \"direction\": \"DESC\"}]",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "field": {"type": "string", "description": "字段名"},
                                                "direction": {"type": "string", "enum": ["ASC", "DESC"]},
                                            },
                                            "required": ["field"],
                                        },
                                    },
                                    "limit": {"type": "integer", "description": "限制返回行数，如 5 或 10"},
                                    "group_by": {"type": "string", "description": "[可选/向后兼容] 分组字段字符串。推荐用 group_by_fields。"},
                                    "group_by_fields": {
                                        "type": "array",
                                        "description": "分组字段（结构化，推荐）。如 [\"unit\", \"code\"]",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["table"],
                            },
                        }
                    },
                    "required": ["tables"],
                },
            },
        },
    ]
    return functions


class DBChat:
    """
    自然语言查询数据库
    
    用法：
        chat = DBChat()
        chat.ask("A1-28 定额的全费用是多少？")
        # → "6521.80 元"
        
        chat.ask("查询所有人工费大于 1000 的定额")
        # → "A1-29 人工费 2435.90..."
    """

    def __init__(self, ai: Optional[AIClient] = None):
        # 惰性实例化：只用纯函数方法（_resolve_field 等）时不强制要求 AI key——
        # 首次真正调用 AI 时才取单例（test_06/14 无 .env 失败的根因修复）
        self._ai = ai
        self.db_type = "sqlite"
        self._tables_desc = ""
        self._fields_desc = ""

    @property
    def ai(self) -> AIClient:
        if self._ai is None:
            self._ai = AIClient.get_instance()
        return self._ai

    def set_db_type(self, db_type: str):
        """设置数据库类型（sqlite / postgresql）"""
        self.db_type = db_type

    def set_schema(self, schema: dict):
        """设置数据库表结构描述"""
        tables = schema.get("tables", [])
        descs = []
        for t in tables:
            cols = ", ".join(c.get("name", "") for c in t.get("columns", []))
            descs.append(f"{t['name']}({cols})")
        self._tables_desc = "\n".join(f"  {d}" for d in descs)

        field_dict = schema.get("field_dict", {})
        fd_lines = []
        for k, v in field_dict.items():
            if isinstance(v, dict):
                aliases = v.get("alias", [])
                fd_lines.append(f"  {k} = {', '.join(aliases)}")
            else:
                fd_lines.append(f"  {k} = {v}")
        self._fields_desc = "\n".join(fd_lines)

    def _get_table_map(self) -> dict:
        """返回 {表名: {desc, columns}}（复用 schema_matcher.load_schemas 唯一加载入口）

        缓存行为：_load_schemas 本身不缓存，与原实现一致（每次调用重读 YAML），
        并额外把 schema 中的 datasource 注册到 DataSourceManager（联邦路由前提）。
        """
        from core.schema_matcher import load_schemas
        return {t["name"]: t for t in load_schemas()}

    def _resolve_fields(self, expr: str, table: str) -> str:
        """将 SQL 表达式中的别名/错误字段名映射为真实字段名

        唯一实现收敛到 core.data_ops.resolve_field（本方法仅为兼容薄委托）。
        行为差异：原实现从 YAML schema 取表字段做自身映射（nick==real 恒跳过，
        无实际效果）；resolve_field 改为从 driver.get_columns 取表字段，用于
        限制跨表误替换，语义更严格。
        """
        from core.data_ops import resolve_field
        return resolve_field(expr, table)

    def _format_multi_table(self, results: dict[str, list[dict]]) -> str:
        """格式化多表查询结果——薄委托 core/formatters.format_multi_table（收敛后
        排版唯一实现在 formatters；本方法保留仅为测试 mock 面与旧调用兼容）"""
        from core.formatters import format_multi_table
        field_names = {}
        if self._fields_desc:
            for line in self._fields_desc.split("\n"):
                if "=" in line:
                    eng, cn_list = line.split("=", 1)
                    field_names[eng.strip()] = cn_list.split(",")[0].strip()
        return format_multi_table(results, self._get_table_map(), field_names)

    def _normalize_fc_tables(self, fn_args: dict, question: str) -> list:
        """兼容 AI 输出 singular "table" 而非 "tables" 数组"""
        tables_raw = fn_args.get("tables", [])
        if not tables_raw and fn_args.get("table"):
            where = fn_args.get("where", "")
            # AI 没有输出 WHERE 时的业务编码兜底识别：编码字段读 YAML 唯一键声明、
            # 编码格式读行业 config.yml 的 code_pattern（不再硬编码业务编码字段名
            # 与定额编码正则——非工程行业的订单号/病历号由各自行业配置声明）
            if not where:
                from core.schema_matcher import get_unique_key_column as _gukc
                _key = _gukc(fn_args.get("table", ""))
                _pat = _load_code_pattern()
                if _key and _pat:
                    m = re.search(_pat, question)
                    if m:
                        where = f"{_key}='{m.group(1)}'"
            tables_raw = [{"table": fn_args["table"],
                          "where": where,
                          "select": fn_args.get("select", "*"),
                          "order_by": fn_args.get("order_by", ""),
                          "limit": fn_args.get("limit", 0)}]
        return tables_raw

    def _run_table_item(self, item: dict, table_map: dict, db):
        """处理单个表查询项：子句装配 → 安全校验 → 执行。

        返回 (error, table, rows)：
        - error 非 None：安全校验拒绝，ask 直接把 error 作为最终结果返回；
        - rows 为 None：表未知 / 查询异常 / 无命中行，主循环跳过不入 results。
        """
        table = item.get("table", "")
        table_info = table_map.get(table)
        if not table_info:
            return None, table, None
        where = item.get("where", "")
        # 优先使用结构化 where_conditions
        wcs = item.get("where_conditions", [])
        if wcs:
            from core.condition_parser import build_where
            try:
                where = build_where(wcs)
            except ValueError as e:
                return f"WHERE 条件不安全，已拒绝执行: {e}", table, None
            if where.startswith("WHERE "):
                where = where[6:]
        # 优先使用结构化 select_fields
        sfs = item.get("select_fields", [])
        if sfs:
            parts = []
            for sf in sfs:
                f = sf.get("field", "")
                agg = sf.get("aggregate", "").strip().upper()
                alias = sf.get("alias", "").strip()
                expr = f"{agg}({f})" if agg else f
                if alias:
                    expr += f" AS {alias}"
                parts.append(expr)
            fields = ", ".join(parts) if parts else "*"
        else:
            fields = item.get("select", "*")
        # 字段名校验：将 AI 使用的别名映射为真实字段名
        if fields != "*" and fields.upper().strip() != "COUNT(*)":
            fields = self._resolve_fields(fields, table)
        if where:
            where = self._resolve_fields(where, table)
        if fields.upper().strip() == "COUNT(*)":
            fields = "COUNT(*)"
        # 优先使用结构化 order_by_fields
        obs = item.get("order_by_fields", [])
        if obs:
            order_parts = []
            for ob in obs:
                f = ob.get("field", "")
                d = ob.get("direction", "ASC").upper()
                if d not in ("ASC", "DESC"):
                    d = "ASC"
                order_parts.append(f"{f} {d}")
            order_by = ", ".join(order_parts)
        else:
            order_by = item.get("order_by", "")
        # GROUP BY — 优先结构化 group_by_fields
        group_by = ""
        gbfs = item.get("group_by_fields", [])
        if gbfs:
            group_by = ", ".join(gbfs)
        else:
            group_by = item.get("group_by", "")
        limit = item.get("limit", 0)
        # 子句安全校验 + SQL 拼装（任一子句不安全则拒绝执行）
        try:
            sql = _build_select_sql(table, fields, where, order_by, group_by, limit)
        except ValueError as e:
            return str(e), table, None
        try:
            rows = db.query(sql)
        except Exception as e:
            # 单表失败保持容错语义（继续查其余表），但记录真实错误，
            # 避免被"没有找到匹配的数据"掩盖
            log_warning("db_chat 单表查询失败，已跳过", table=table,
                        sql=sql[:200], error=str(e)[:200])
            return None, table, None
        return None, table, rows

    def _save_selection_note(self, results: dict, tables_raw: list,
                             question: str, output: str) -> str:
        """选择集补齐：仅当「单表 + SELECT * + 无 GROUP BY」时保存。
        此时每行对应一条真实表记录（含 id），edit_data/delete_data 才能
        基于选择集做"先查后改"。多表/聚合/GROUP BY 结果的行不对应单表
        记录，存了反而会误导 edit/delete，故不存。"""
        if len(tables_raw) == 1 and len(results) == 1:
            _item = tables_raw[0]
            _table = _item.get("table", "")
            _sfs = _item.get("select_fields", [])
            _sel = _item.get("select", "*") if not _sfs else ""
            _gb = bool(_item.get("group_by_fields", []) or _item.get("group_by", ""))
            if (not _sfs and _sel.strip() == "*" and not _gb
                    and _table in results and results[_table]):
                from core.context import get_context
                sid = get_context().save_selection(
                    _table, results[_table],
                    query=_item.get("where", "") or question)
                output += (f"\n已暂存为选择集 selection_id={sid}"
                           f"（{len(results[_table])}条，表：{_table}）")
        elif len(results) == 1 and len(tables_raw) == 1:
            # 单表但为聚合/分组/投影查询：不存选择集（行不对应表记录）
            log_info("db_chat 单表聚合/分组结果不存选择集")
        else:
            # 多表结果：不存选择集
            log_info("db_chat 多表结果不存选择集", tables=list(results.keys()))
        return output

    def ask(self, question: str) -> str:
        """自然语言查询数据库——按表查询（Function Calling）"""
        from industries.base import get_industry
        from config.settings import settings as s
        cfg = get_industry(s.INDUSTRY)
        role = cfg.expert_role if cfg else ""
        role_prefix = f"你是一位{role}。" if role else ""

        table_map = self._get_table_map()
        table_names = list(table_map.keys())
        table_enum = {"type": "string", "enum": table_names, "description": "表名"}
        tables_desc = " ".join(f"{n}({table_map[n].get('business_name','')})" for n in table_names)
        fields_hint = _build_fields_hint(table_map)
        functions = _build_query_tables_tool(table_enum, fields_hint)

        # 调用 AI 通过 Function Calling 提取查询参数
        try:
            from core.llm_usage import set_role as _usage_role
            with _usage_role("extract_param"):
                fn_name, fn_args = self.ai.call_function(
                    functions, question,
                    system_prompt=f"{role_prefix}从用户问题中提取查询参数。可用表：{tables_desc}"
                )
        except Exception as e:
            return f"查询参数提取失败: {e}"

        # -- SQL 构建与执行（live 路径：fn_args → 安全拼装 → 逐表执行 → 格式化）--
        results = {}
        from core.data_ops import get_driver
        db = get_driver()

        tables_raw = self._normalize_fc_tables(fn_args, question)

        for item in tables_raw:
            error, table, rows = self._run_table_item(item, table_map, db)
            if error is not None:
                return error
            if rows:
                results[table] = rows

        if not results:
            return "没有找到匹配的数据"

        # 组装输出 + 选择集补齐
        output = self._format_multi_table(results)
        return self._save_selection_note(results, tables_raw, question, output)
