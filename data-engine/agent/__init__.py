import json
from pathlib import Path
from core.ai_runtime.ai_client import AIClient
from core.tool_registry import execute_tool, _tools
from core.session import add_turn, save_history, load_history
from config.settings import settings

from . import tools  # noqa: F401
from core.exceptions import AppError, safe_call
from core.tool_result import ToolResult
from core.contract.security_contract import safe_table_sql, safe_column_sql
from .router import route as tree_route
import re

def _truncate_result(result, limit: int = 5000) -> "ToolResult":
    """截断 ToolResult 文本但保留 data 通道（code/reason/effects 不丢）"""
    if not isinstance(result, ToolResult):
        result = ToolResult.legacy(str(result) if result is not None else "执行完成")
    if len(result.text) > limit:
        return ToolResult(result.text[:limit], result.data)
    return result


def _parse_conditions(sargs: dict) -> "tuple[list, str]":
    """解析 structured_args 的 conditions 与目标表名（信号采集器共用的归一入口）

    conditions 兼容 JSON 字符串与列表；缺失/解析失败/非列表一律归一为 []。
    表名取 table，缺省回退 main_table，再缺省为空串。
    """
    table = sargs.get("table") or sargs.get("main_table") or ""
    conditions = sargs.get("conditions")
    if not conditions:
        return [], table
    if isinstance(conditions, str):
        try:
            conditions = json.loads(conditions)
        except (ValueError, TypeError):
            return [], table
    if not isinstance(conditions, list):
        return [], table
    return conditions, table


# 依赖倒置注册（评审四轮 P1）：core/data_ops.mutate_natural 的工具路由
# 由编排层在此注入——core 不再反向 import agent.router，分层方向恢复单向。
# 注意 get_tree 必须调用期动态解析（函数内 import）：否则 from-import 绑死
# 原函数，测试对 agent.router.get_tree 的 patch 会失效（层 29 实测拦截）
from core.data_ops import register_tree_router as _register_tree_router


def _route_via_tree(behavior: str, category: str, constraint: str = "") -> str:
    from agent.router import get_tree
    return get_tree().route(behavior, category, constraint)


_register_tree_router(_route_via_tree)


class Agent:
    def __init__(self):
        # 复用 AIClient 单例（避免每次 new Agent 都创建新的 OpenAI client + httpx 连接池）
        self.ai = AIClient.get_instance()
        self._history = load_history()
        self.open_ai_mode = True  # 开关：True=开放式AI（LangGraph编排），False=直接P1→树→P2

    @staticmethod
    def _enumerate_objects():
        """枚举数据库、表、列，供 FC 构建

        数据来源：YAML schema 优先（有元数据），数据库实际表补充（YAML 未定义的表）。
        确保即使行业 schema 未配置，AI 也能看到数据库中的实际表。
        """
        from config.settings import settings as _s
        from core.schema_matcher import _load_schemas as _ls
        schemas = _ls()

        # 数据库实际表补充（YAML 未定义的表，从 DB 获取字段）
        from core.data_ops import _get_driver as _get_fed_driver
        try:
            drv = _get_fed_driver()
            db_tables = drv.list_tables()
            existing_names = {s["name"] for s in schemas}
            for tname in db_tables:
                if tname.startswith("sqlite_") or tname in existing_names:
                    continue
                try:
                    cols = drv.get_columns(tname)
                    schemas.append({
                        "name": tname,
                        "columns": [{"name": c["name"], "type": c.get("type", "")} for c in cols],
                        "foreign_keys": [],
                    })
                except Exception:
                    continue
        except Exception:
            pass

        db = [_s.SQLITE_DB_PATH]
        tables = sorted(set(t["name"] for t in schemas))
        cols = sorted(set(c["name"] for s in schemas for c in s.get("columns", [])))
        types = ["INTEGER", "TEXT", "FLOAT", "VARCHAR"]
        return db, schemas, tables, cols, types

    @staticmethod
    def _build_fc(tool_def, all_tables, all_columns, all_databases, type_enum):
        """构建 Function Calling schema"""
        properties = {}
        for p in tool_def.params:
            if p.schema:
                # 结构化参数：直接使用 JSON Schema，从根源约束 AI 输出格式
                prop = dict(p.schema)
                prop["description"] = p.description
            else:
                ptype = {"str": "string", "int": "integer", "bool": "boolean"}.get(p.type, "string")
                prop = {"type": ptype, "description": p.description}
                if p.default is not None:
                    prop["default"] = p.default
            if p.name == "table":
                prop["description"] += f"（已知表：{', '.join(all_tables) or '无'}，用户可能指定不存在的表名也照填）"
            elif p.name == "main_table":
                prop["description"] += f"（已知表：{', '.join(all_tables) or '无'}）"
            elif p.name == "join_tables":
                prop["description"] += f"（已知表：{', '.join(all_tables) or '无'}，多个用逗号分隔）"
            elif p.name == "column":
                prop["description"] += f"（已知列：{', '.join(all_columns[:10])}）"
            elif p.name in ("col_type", "new_type"):
                prop["enum"] = type_enum
            elif p.name == "database":
                prop["enum"] = all_databases if all_databases else [""]
            elif p.name == "conditions":
                prop["description"] += '。JSON数组：[{"field":"列名","op":"操作符","value":"值"}]。操作符：=, !=, <, >, <=, >=, LIKE'
            elif p.name == "agg_func":
                prop["enum"] = ["COUNT", "SUM", "AVG", "MIN", "MAX", "DISTINCT"]
            elif p.name == "agg_field":
                prop["description"] += f"（已知列：{', '.join(all_columns[:15])}）"
            elif p.name == "group_by":
                prop["description"] += f"（已知列：{', '.join(all_columns[:15])}，多个用逗号分隔）"
            elif p.name == "selection_id":
                from core.context import get_context
                sels = get_context().list_selections()
                if sels:
                    sel_desc = ", ".join(f"#{s['id']}[{s['table']},{s['count']}条]" for s in sels)
                    last_id = get_context().get_last_selection_id()
                    prop["description"] += f"（当前可用：{sel_desc}。最近查询：#{last_id}，如未指定默认用最近查询）"
                else:
                    prop["description"] += "（当前无选择集，需先查询数据创建）"
            properties[p.name] = prop
        return [{"type": "function", "function": {
            "name": tool_def.name,
            "description": tool_def.description,
            "parameters": {"type": "object", "properties": properties,
                           "required": [p.name for p in tool_def.params if p.required]}
        }}]

    @staticmethod
    def _extract_params(args, tool_def, all_tables, all_columns):
        """从 AI FC 返回值中提取参数，纠正字段名/表名混淆"""
        filtered = {}
        for p in tool_def.params:
            val = args.get(p.name)
            if val is None or val == "":
                continue
            # 纠正: AI 把字段名填进了 table → 跳过
            if p.name == "table" and val not in all_tables and val in all_columns:
                continue
            filtered[p.name] = val
        return filtered

    @staticmethod
    def _detect_ambiguity(filtered, schemas, instruction, tool_name):
        """字段歧义检测：column 在多表中 → 提示用户；唯一 → 自动确定表"""
        col = filtered.get("column", "")
        tbl = filtered.get("table", "")
        if tbl and tbl.lower() not in [t["name"].lower() for t in schemas]:
            return None  # 表不存在，交给后续处理
        if col:
            owners = sorted(set(t["name"] for t in schemas
                                for c in t.get("columns",[]) if c["name"].lower() == col.lower()))
            if len(owners) > 1 and tbl not in owners:
                from core.context import get_context
                get_context().save("field_op", {**filtered, "_tool": tool_name, "_input": instruction, "column": col, "owners": owners})
                return f"字段 '{col}' 在 {'、'.join(owners)} 中都存在，请指定表名 [trace:{get_context().get_trace_id()}]"
            elif len(owners) == 1 and not tbl:
                filtered["table"] = owners[0]
        return None

    @staticmethod
    def _apply_phase2(filtered, parsed, instruction, tool_name):
        """Phase 2 集成：调用 resolve，合并结果，返回阻断消息或 None"""
        # 建表工具的参数是完整 JSON 定义，不需要 P2 反查表名/字段名
        # 多表工具（JOIN/聚合）的表名由 AI 通过 main_table/join_tables 指定，P2 子串匹配会误判
        # DDL 增加类工具（add_column/add_foreign_key/create_index）操作的是"新"字段/外键/索引，
        # P2 反查"字段是否存在"会误报不存在
        if tool_name in ("batch_create_tables", "join_query", "aggregate_query",
                         "add_column", "add_foreign_key", "create_index"):
            return None
        from core.schema_matcher import resolve as _resolve
        ph2 = _resolve(instruction, parsed.get("_path", []), tool_name,
                       parsed.get("db_category", ""), filtered.get("table",""), filtered.get("column",""))
        for k in ("table", "column", "database", "query"):
            if ph2.get(k) and not filtered.get(k):
                filtered[k] = ph2[k]
        if ph2.get("message") and "无可匹配" not in ph2["message"] and "无可用表结构" not in ph2["message"]:
            from core.context import get_context; tid = get_context().get_trace_id()
            return f"{ph2["message"]} [trace:{tid}]"
        return None

    def _validate_tool_args(self, tool_name: str, args: dict, schemas: list, all_tables: set) -> str:
        """生成后校验（P0）：AI 参数在执行前过确定性检查

        覆盖 structured_args 透传与 FC AI 两条路径，拦截四类高发错误：
        1. 表名不存在（假想表） 2. 字段名不存在且非别名 3. 中文列名直传 4. 外键引用非 id 列
        返回 None=通过；否则返回中文错误说明（含合法候选）。
        """
        from core.contract.security_contract import is_valid_identifier

        def _table_columns(tname):
            for s in schemas:
                if s.get("name") == tname:
                    return {c["name"] for c in s.get("columns", [])}
            return set()

        def _aliases():
            """fields.yml 业务别名集合（字段英文名 + 中文别名）

            真身：industries/base.py IndustryConfig.field_dict——
            行业目录 fields/fields.yml 的规范加载器（目录签名新鲜度，改配置免重启）。
            此前引用的 core.data_ops._load_fields_config 已不存在（僵尸引用），
            ImportError 被静默吞掉导致别名集恒空、业务别名参数被误报不存在。
            """
            try:
                from industries.base import get_current_industry
                fc = get_current_industry().field_dict
                out = set()
                for k, v in (fc or {}).items():
                    if isinstance(v, dict):
                        out.add(k)
                        out.update(v.get("alias", []) or [])
                return out
            except Exception:
                return set()

        problems = []
        # 表名归一（确定性）：LLM 产出的 table 参数常带噪声尾巴（'表'/'批量插入'…），
        # 能唯一解析回真实表名就纠正，不能就如实报不存在——不在执行层猜
        def _norm_table(v: str) -> str:
            v = (v or "").strip()
            if not v or v in all_tables:
                return v
            core = v.rstrip("表 ")
            if core in all_tables:
                return core
            cands = [t for t in all_tables if v.startswith(t) or t in v]
            return cands[0] if len(cands) == 1 else v

        raw_table = args.get("table", "")
        table = _norm_table(raw_table)
        if table != raw_table:
            args["table"] = table
            from core.logger import info as _li
            _li("表名归一纠正", raw=raw_table, resolved=table)
        if table and table not in all_tables:
            problems.append(f"表 '{table}' 不存在")
        for key in ("main_table", "ref_table"):
            t = args.get(key, "")
            if t and t not in all_tables:
                problems.append(f"表 '{t}' 不存在")
        if args.get("join_tables"):
            for t in [x.strip() for x in str(args["join_tables"]).split(",") if x.strip()]:
                if t not in all_tables:
                    problems.append(f"关联表 '{t}' 不存在")

        real = _table_columns(table) if table else set()
        alias = _aliases()
        for key in ("column", "agg_field", "group_by", "order_by"):
            v = str(args.get(key, "") or "")
            if not v or not table or not real:
                continue
            # add_column 的字段本就不该存在（存在性检查只适用于操作既有字段的工具）
            if tool_name == "add_column" and key == "column":
                continue
            for f in [x.strip() for x in v.split(",") if x.strip()]:
                base = f.split(".")[-1].split(" ")[0]
                if base in ("*", "COUNT(*)"):
                    continue
                if base not in real and base not in alias:
                    problems.append(f"字段 '{base}' 在表 {table} 中不存在")

        defs = args.get("definitions")
        if defs:
            if isinstance(defs, str):
                try:
                    defs = json.loads(defs)
                except Exception:
                    defs = []
            for t in defs if isinstance(defs, list) else []:
                tn = t.get("name", "")
                if not is_valid_identifier(tn):
                    problems.append(f"建表名 '{tn}' 非法（须英文 snake_case）")
                for c in t.get("columns", []):
                    cn = c.get("name", "")
                    if not is_valid_identifier(cn):
                        problems.append(f"字段名 '{cn}' 非法（须英文 snake_case，中文业务名放 business_name）")
                for fk in t.get("foreign_keys", []):
                    refcols = fk.get("ref_columns", [])
                    if refcols and refcols != ["id"]:
                        problems.append(
                            f"表 {tn} 的外键 {fk.get('columns')} 引用了非 id 列 {refcols}（外键只能指向引用表的 id）")

        if problems:
            valid_tables = "、".join(sorted(all_tables)) if all_tables else "（无）"
            return ("参数校验失败（生成后校验拦截）：\n- " + "\n- ".join(problems)
                    + f"\n\n可用表：{valid_tables}。请修正后重试。")
        return None

    def _execute_single(
        self,
        instruction: str,
        behavior_key: str = "",
        db_category_key: str = "",
        constraint: str = "",
        structured_args: dict | None = None,
    ) -> "ToolResult":
        """执行单条指令（双轨）：返回 ToolResult——text 给用户，data.code 给机器"""
        from core.schema_matcher import _load_schemas as _ls
        from config.settings import settings as _settings

        # P1 语义解析：优先使用 LangGraph 提供的结构化标签（跳过 AI 解析）
        # 当 P1_AI_ENABLED=True 或未提供结构化标签时，走 AI 语义解析
        from agent.router import text_behavior_override as _tbo, text_db_override as _tdbo
        _text_bk = _tbo(instruction)
        _text_dk = _tdbo(instruction)
        if behavior_key and db_category_key and not _settings.P1_AI_ENABLED:
            # 结构化标签透传：直接用标签走路由树，跳过 P1 的 AI 解析
            from agent.router import get_tree as _get_tree
            # 文本铁证纠偏：LLM 标签与指令文本的确定性关键词冲突时以文本为准
            # （如标签"改"但文本是"加一个字段"——路由进 alter_precision 的真事故）
            if _text_bk and _text_bk != behavior_key:
                from core.logger import info as _li
                _li("意图标签纠偏：以文本关键词为准", tag=behavior_key, text=_text_bk)
                behavior_key = _text_bk
            if _text_dk and _text_dk != db_category_key:
                from core.logger import info as _li2
                _li2("对象标签纠偏：以文本关键词为准", tag=db_category_key, text=_text_dk)
                db_category_key = _text_dk
            tool_name = _get_tree().route(behavior_key, db_category_key, constraint)
            parsed = {
                "behavior": behavior_key,
                "db_category": db_category_key,
                "constraint": constraint,
                "_path": [],
                "behavior_value": "",
                "db_category_value": "",
            }
        else:
            # P1 的 AI 语义解析（单步模式或 P1_AI_ENABLED=True 时）
            tool_name, parsed = tree_route(instruction)

        from core.tool_registry import _tools as _treg
        if tool_name not in _treg:
            return ToolResult.fail(f"无法确定操作: {instruction}", code="VALIDATION",
                                   reason="cannot_route")
        tool_def = _tools[tool_name]
        if not tool_def.params:
            return _truncate_result(safe_call(execute_tool, tool_name))

        # === 显式三层枚举：数据库 → 表 → 字段 ===
        all_databases, schemas, all_tables, all_columns, type_enum = self._enumerate_objects()
        db = all_databases[0] if all_databases else ""

        # FC 参数提取：优先使用 LangGraph 提供的 structured_args（跳过 AI 调用）
        # 当 FC_AI_ENABLED=True 或未提供 structured_args 时，走 AI Function Calling
        sargs = structured_args or {}
        has_valid_sargs = bool(sargs) and not _settings.FC_AI_ENABLED

        if has_valid_sargs:
            # structured_args 透传：直接用 JSON 构造工具参数，跳过 FC AI 调用
            args = self._build_args_from_structured(sargs, tool_def, all_tables, all_columns)
        else:
            # FC AI 调用（单步模式或 FC_AI_ENABLED=True 时）
            fc = self._build_fc(tool_def, all_tables, all_columns, all_databases, type_enum)
            try:
                from core.llm_usage import set_role as _usage_role
                with _usage_role("extract_param"):
                    _, args = self.ai.call_function(fc, instruction,
                    system_prompt=(
                        "从用户指令中提取参数。只提取用户明确提到的，没提到的留空。"
                        "表名/字段名必须从已知列表中选择，严禁猜测或编造字段名（如把 name 猜成 name_id、把 title 猜成 title_text）。"
                        "如不确定字段名，留空让系统自动补全。"
                        f"已知表：{', '.join(all_tables)}。已知字段：{', '.join(all_columns[:30])}。"
                        "JOIN查询的select_fields用*或table.column格式（如 t1.name），不要用table.*格式。"
                        "聚合查询时：'查询不同的XX'/'去重查询XX'→agg_func=DISTINCT, agg_field=XX字段名（返回去重列表）；'统计数量'→agg_func=COUNT；'求和'→agg_func=SUM。"
                        "建表（batch_create_tables）时：definitions 的字段 name 必须是英文 snake_case（如 名称→name、容量→capacity、位置→location），严禁用中文作为字段 name；中文业务名放 business_name（如 {\"name\":\"capacity\",\"type\":\"FLOAT\",\"business_name\":\"容量\"}）。"
                    ))
            except Exception as e:
                # FC AI 调用失败必须显式报错——静默用空参数继续会执行非预期操作。
                # LLM 调用异常按临时故障处理（超时/限流/网络占绝对多数），executor 可重试
                return ToolResult.fail(f"参数解析失败（FC AI 调用异常）: {e}",
                                       code="TRANSIENT", reason="fc_ai_failed")

        filtered = self._extract_params(args, tool_def, all_tables, all_columns)

        # 数据库兜底 + 歧义检测
        if not filtered.get("database") and all_databases:
            filtered["database"] = all_databases[0]
        if ambi_err := self._detect_ambiguity(filtered, schemas, instruction, tool_name):
            return ToolResult.fail(ambi_err, code="VALIDATION", reason="field_ambiguous")

        if ph2_err := self._apply_phase2(filtered, parsed, instruction, tool_name):
            return ToolResult.fail(ph2_err, code="VALIDATION", reason="phase2_resolve")
        if not filtered.get("query"):
            filtered["query"] = instruction
        # 生成后校验（P0）：执行前拦截假想表名/字段名/中文列名/非 id 外键
        if v_err := self._validate_tool_args(tool_name, filtered, schemas, all_tables):
            return ToolResult.fail(v_err, code="VALIDATION", reason="arg_validation")
        msg = _truncate_result(safe_call(execute_tool, tool_name, **filtered))

        # 选择集缺失的确定性兜底（白盒，不赌 LLM 纠错自觉）：
        # edit_data/delete_data 要求先查成选择集，而"把 X 的 Y 改成 Z"/"删除 X 的记录"
        # 这类带明确条件的指令走统一改/删语义（方案C：候选 1 条直接执行，
        # 候选 N 条挂起等人审确认；契约层安全闸全程在位）
        if msg.data.get("reason") == "need_selection" and tool_name in ("edit_data", "delete_data"):
            from core.data_ops import mutate_natural
            from core.logger import info as _log_info
            _log_info("选择集缺失，改走统一改/删语义（候选集分流）", tool=tool_name)
            msg = _truncate_result(mutate_natural(
                instruction,
                action=("update" if tool_name == "edit_data" else "delete")))

        # === 体系A：异常信号采集 + 轻量OODA纠错（仅structured_args模式触发）===
        # 设计：代码采集"事实"信号 → AI综合判断（看到原始指令）→ 决定是否修正
        if has_valid_sargs and not _settings.FC_AI_ENABLED:
            for _ in range(self._OODA_MAX_ROUNDS):
                signals = self._collect_anomaly_signals(sargs, tool_name, msg, all_tables, schemas)
                if not signals:
                    break  # 无异常信号，退出纠错循环
                # OODA：AI综合判断所有信号 + 决定是否重新生成structured_args
                new_sargs = self._ooda_regenerate(instruction, signals, sargs, all_tables, all_columns)
                if not new_sargs or new_sargs == sargs:
                    break  # AI判断不需修正或无法改善，放弃纠错
                # 用新的structured_args重新构造参数并执行
                sargs = new_sargs
                args = self._build_args_from_structured(sargs, tool_def, all_tables, all_columns)
                filtered = self._extract_params(args, tool_def, all_tables, all_columns)
                if not filtered.get("database") and all_databases:
                    filtered["database"] = all_databases[0]
                if not filtered.get("query"):
                    filtered["query"] = instruction
                # 重生成参数必须过同一道 P0 校验闸（生成后校验）——
                # LLM 再生成内容享受免检与"生成后校验"原则矛盾（评审四轮）
                if v_err := self._validate_tool_args(tool_name, filtered, schemas, all_tables):
                    _log_ooda = f"OODA 重生成参数未过校验，放弃纠错: {v_err[:80]}"
                    from core.logger import info as _li2
                    _li2(_log_ooda)
                    break
                result = safe_call(execute_tool, tool_name, **filtered)
                msg = _truncate_result(result)

        if msg.data.get("reason") == "need_force":
            from core.context import get_context
            get_context().save("force_pending", {"_tool": tool_name, "_kwargs": dict(filtered)})
        from core.context import get_context; tid = get_context().get_trace_id()
        if "[trace:" not in msg.text and msg.text:
            msg = ToolResult(f"{msg.text} [trace:{tid}]", msg.data)
        return msg

    def execute_single(
        self,
        instruction: str,
        behavior_key: str = "",
        db_category_key: str = "",
        constraint: str = "",
        structured_args: dict | None = None,
    ) -> "ToolResult":
        """公开入口：执行单条指令，走完整的 P1→树→P2 流程

        直接委托 _execute_single（签名一致），供 agent.open_layer.executor 等
        外部编排层调用，避免外部依赖私有方法。
        返回 ToolResult：text 给用户看，data.code/reason/effects 给机器判断。
        """
        # 改/删人审确认（方案C 统一语义）：上一步多候选挂起了 mutation_pending，
        # 本句「确认」执行、「其他内容」取消——在一切路由之前结算挂起态。
        # 确认判定用整句精确匹配（is_pure_confirm，与图主链同款）：子串匹配会把
        # "不要执行"（含"执行"）"不对"（含"对"）误判为确认——人审闸反向触发即事故
        from core.context import get_context
        from agent.open_layer.graph._shared import _is_pure_confirm
        pending = get_context().consume("mutation_pending")
        if pending is not None:
            text = (instruction or "").strip()
            if _is_pure_confirm(text):
                sel = get_context().get_selection(pending.get("selection_id", 0))
                if not sel:
                    return ToolResult.fail("选择集已失效，请重新发起操作",
                                           code="VALIDATION", reason="selection_expired")
                # 类型驱动字面量拼装（ids_in_clause 唯一实现）：文本主键表的
                # 脏 id 不会变成注入文本；整型主键不受影响
                from core.contract.security_contract import ids_in_clause
                ids = sel["ids"]
                where = ids_in_clause(ids)
                from core.data_ops import update_rows, delete_rows
                if pending.get("action") == "UPDATE":
                    r = update_rows(pending["table"], pending.get("set_clause", ""), where)
                else:
                    r = delete_rows(pending["table"], where)
                # 人审确认路径补 effects（含目标 id 集），供 goal_verify 独立复查
                if r.data.get("ok"):
                    r.data["effects"] = {
                        "table": pending["table"],
                        "action": pending.get("action", ""),
                        "affected": r.data.get("affected", 0),
                        "affected_ids": ids,
                    }
                return r
            return ToolResult.ok("已取消该操作（数据未改动）", action="cancelled")

        return self._execute_single(
            instruction,
            behavior_key=behavior_key,
            db_category_key=db_category_key,
            constraint=constraint,
            structured_args=structured_args,
        )

    @staticmethod
    def _build_args_from_structured(sargs: dict, tool_def, all_tables, all_columns) -> dict:
        """从 LangGraph 输出的 structured_args 构造工具参数（跳过 FC AI 调用）

        根据 tool_def.params 定义的字段名和类型，从 structured_args 中提取对应值。
        - data/definitions 等复杂类型转为 JSON 字符串（工具 handler 期望 str）
        - conditions 列表转为 JSON 字符串
        - 布尔值/数值做类型转换
        - set_data 如果是 dict 自动转为 "key=value" 格式
        - 只返回工具定义中存在的参数（自动过滤无关字段）
        """
        args = {}

        # 特殊处理：edit_data 的 set_data
        # LangGraph 应输出 set_data="phone=13900000999"，但可能输出 data={"phone":"13900000999"}
        # 此时将 data dict 转为 "key=value" 格式字符串
        param_names = {p.name for p in tool_def.params}
        if "set_data" in param_names and not sargs.get("set_data"):
            data_val = sargs.get("data")
            if isinstance(data_val, dict) and data_val:
                sargs = dict(sargs)
                sargs["set_data"] = ",".join(f"{k}={v}" for k, v in data_val.items())

        for p in tool_def.params:
            val = sargs.get(p.name)
            if val is None or val == "":
                continue

            # 类型转换
            if p.type == "int":
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    continue
            elif p.type == "bool":
                if isinstance(val, str):
                    val = val.lower() in ("true", "1", "yes", "是")
                else:
                    val = bool(val)
            elif p.type == "str":
                # data/definitions/conditions 等复杂类型转 JSON 字符串
                if isinstance(val, (dict, list)):
                    val = json.dumps(val, ensure_ascii=False)
                else:
                    val = str(val)

            # 表名/字段名白名单校验
            if p.name == "table" and val not in all_tables and val in all_columns:
                continue  # AI 把字段名填进了 table → 跳过

            args[p.name] = val

        return args


    # ========== 体系A：异常信号采集 + 轻量OODA纠错 ==========
    #
    # 架构设计（信号 vs 规则）：
    # - 代码层只采集"事实"（信号），不做"判断"
    # - 信号采集器正交独立，可无限扩展（新增异常类型 = 新增一个采集器）
    # - 所有信号汇总后交给AI做综合判断（_ooda_regenerate）
    # - AI看到原始指令，能区分"解析错误"vs"用户明确意图"
    #
    # 设计原则：
    # - 信号是数据特征（事实），不是模式匹配（判断）
    # - 例："P999不在值域内"是事实，"P999是解析错误"是判断
    # - 代码采集事实，AI做判断 → 通用性来自AI判断，不来自规则穷举

    _OODA_MAX_ROUNDS = 2  # 最多纠错轮次

    def _collect_anomaly_signals(self, sargs: dict, tool_name: str, result,
                                  all_tables: set, schemas: list) -> list[dict]:
        """采集异常信号——只采集事实，不做判断

        每个信号采集器只输出"事实"（如"值X不在字段Y的值域内"），
        不输出"判断"（如"值X是解析错误"）。判断由 _ooda_regenerate 中的AI完成。

        信号之间正交独立，新增异常类型只需新增一个采集器，不影响其他信号。

        Returns:
            信号列表，空列表=无异常信号（99%的查询走这条路径）
        """
        signals = []

        # 仅对查询类工具采集信号（增删改DDL等操作不采集）
        if tool_name not in ("query", "join_query", "aggregate_query"):
            return signals

        # === 信号1：条件值不在字段值域内（事实） ===
        signals.extend(self._signal_value_domain(sargs, all_tables))

        # === 信号2：条件值类型与字段类型不匹配（事实） ===
        signals.extend(self._signal_type_mismatch(sargs, schemas))

        # === 信号3：执行返回错误（事实） ===
        err_signal = self._signal_execution_error(result)
        if err_signal:
            signals.append(err_signal)

        # === 信号4：空结果+有具体条件（事实，需AI判断是否合理） ===
        empty_signal = self._signal_empty_with_condition(sargs, result)
        if empty_signal:
            signals.append(empty_signal)

        # 未来可扩展：信号5 范围异常、信号6 关系不一致、信号7 聚合值偏离...
        return signals

    def _signal_value_domain(self, sargs: dict, all_tables: set) -> list[dict]:
        """信号1采集器：条件值不在字段实际值域内

        事实：执行 SELECT DISTINCT 获取字段实际值，比对条件值是否在内
        场景：LangGraph把"轻度严重程度"切分为"轻度严重"（错误，应为"轻度"）
        """
        signals = []

        conditions, table = _parse_conditions(sargs)
        if not conditions:
            return signals
        if not table or table not in all_tables:
            return signals

        from core.data_ops import _get_driver
        drv = _get_driver()
        for cond in conditions:
            field = cond.get("field", "")
            op = cond.get("op", "=")
            value = str(cond.get("value", ""))
            # 仅校验精确匹配条件（=、!=），LIKE/IN等不校验
            if not field or not value or op not in ("=", "!="):
                continue
            try:
                rows = drv.query(
                    f'SELECT DISTINCT {safe_column_sql(field)} FROM {safe_table_sql(table)} WHERE {safe_column_sql(field)} IS NOT NULL LIMIT 100'
                )
                valid_values = {str(r[field]) for r in rows if r.get(field) is not None}
                if valid_values and value not in valid_values:
                    signals.append({
                        "type": "value_not_in_domain",
                        "field": field,
                        "value": value,
                        "valid_values": sorted(valid_values)[:20],
                        "table": table,
                        "fact": f"条件值 '{value}' 不在字段 {table}.{field} 的实际值域内（共{len(valid_values)}个有效值）",
                    })
            except Exception:
                continue
        return signals

    def _signal_type_mismatch(self, sargs: dict, schemas: list) -> list[dict]:
        """信号2采集器：条件值类型与字段声明类型不匹配

        事实：从schema读取字段声明类型，检查条件值是否符合该类型
        场景：字段是INTEGER但条件值是"张三"（类型不匹配）
        """
        signals = []

        conditions, table = _parse_conditions(sargs)
        if not conditions or not table:
            return signals

        # 查找表的字段类型映射
        field_types = {}
        for s in schemas:
            if s.get("name") == table:
                for c in s.get("columns", []):
                    field_types[c["name"]] = c.get("type", "").upper()
                break

        for cond in conditions:
            field = cond.get("field", "")
            value = str(cond.get("value", ""))
            if not field or not value:
                continue
            ftype = field_types.get(field, "")
            if not ftype:
                continue

            # 数值类型字段：条件值应该是数字
            if ftype in ("INTEGER", "INT", "FLOAT", "REAL", "NUMERIC", "DOUBLE"):
                try:
                    float(value)
                except ValueError:
                    signals.append({
                        "type": "type_mismatch",
                        "field": field,
                        "value": value,
                        "field_type": ftype,
                        "table": table,
                        "fact": f"字段 {table}.{field} 声明类型为 {ftype}，但条件值 '{value}' 不是数字",
                    })
        return signals

    def _signal_execution_error(self, result) -> dict | None:
        """信号3采集器：执行返回错误（读 ToolResult 结构化 code，不做文本匹配）

        事实：工具双轨结果 data.ok=False（code 标明错误类别）
        场景：SQL语法错误、表/字段不存在、执行异常
        """
        if not result:
            return None
        data = getattr(result, "data", None) or {}
        if data.get("ok") is False:
            return {
                "type": "execution_error",
                "code": data.get("code"),
                "fact": f"执行返回错误（code={data.get('code')}）：{str(result)[:200]}",
            }
        return None

    def _signal_empty_with_condition(self, sargs: dict, result) -> dict | None:
        """信号4采集器：空结果+有具体条件（读 ToolResult 结构化 row_count）

        事实：查询带了条件但 row_count=0
        说明：这个信号较模糊——可能是用户查不存在的值（合理），也可能是解析错误（需纠错）
        交给AI判断
        """
        conditions, _table = _parse_conditions(sargs)
        if not conditions:
            return None

        # 结构化行数（query/join/aggregate 双轨结果均携带 row_count）；
        # 非查询类工具无此字段 → 不发空结果信号（unknown 不当作 0）
        data = getattr(result, "data", None) or {}
        if data.get("ok") is not True or data.get("row_count") != 0:
            return None

        return {
            "type": "empty_with_condition",
            "fact": f"查询带了 {len(conditions)} 个条件，但返回 0 条结果",
        }

    def _ooda_regenerate(self, instruction: str, signals: list[dict], sargs: dict,
                         all_tables: set, all_columns: set) -> dict | None:
        """OODA综合判断+纠错：AI看到原始指令+所有信号后做综合判断

        流程：
        - Observe: 收集所有信号 + 原始指令 + structured_args
        - Orient: AI综合分析信号是"解析错误"还是"用户明确意图"或"合理空结果"
        - Decide: AI决定是否修正structured_args
        - Act: 返回新的structured_args（或None表示不修正）

        关键设计：AI看到原始指令，能区分：
        - "用户说的P999"（用户明确意图，不修正）
        - "解析产生的轻度严重"（解析错误，需修正为"轻度"）

        Returns:
            新的structured_args dict，或None表示不修正
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        signals_desc = json.dumps(signals, ensure_ascii=False, indent=2)
        sargs_desc = json.dumps(sargs, ensure_ascii=False)

        prompt = f"""你是数据查询参数纠错专家。请综合分析所有异常信号，判断是解析错误还是用户明确意图。

用户原始指令：{instruction}
当前 structured_args：{sargs_desc}
采集到的异常信号：
{signals_desc}

可用表：{', '.join(sorted(all_tables))}
可用字段：{', '.join(sorted(list(all_columns)[:40]))}

判断规则：
1. **解析错误**（需修正）：信号中的值是LangGraph解析过程中产生的错误
   - 典型：用户说"轻度严重程度"，被切分为 value="轻度严重"（错误），应为"轻度"
   - 典型：用户说"心内科的就诊"，被提取为 value="心内"（截断）
   - 修正方式：从 valid_values 中选择最匹配的值，返回新的 structured_args

2. **用户明确意图**（不修正）：信号中的值是用户在原指令中明确指定的
   - 典型：用户说"查询code为X999的记录"，X999是用户明确指定（即使不存在）
   - 典型：用户说"department为不存在科室"，"不存在科室"是用户原话
   - 处理方式：保持原 structured_args 不变，返回 null

3. **类型不匹配**（需修正）：用户指令的语义与字段类型不符
   - 典型：字段是INTEGER，但条件值是"张三"
   - 修正方式：从原指令中重新提取正确的值，或移除该条件

4. **空结果+有条件**（需AI判断）：
   - 如果用户原话明确指定了不存在的值（如P999）→ 不修正
   - 如果是解析错误导致的空结果 → 修正

关键判断依据：对比"用户原始指令"与"structured_args中的值"：
- 如果用户原话明确包含了该值（如"P999"、"不存在科室"）→ 不修正
- 如果用户原话没有该值，是解析过程产生的（如"轻度严重"是"轻度"+"严重程度"的切分）→ 修正

请返回 JSON：
- 需要修正：返回新的 structured_args 完整 JSON
- 不需要修正：返回 null

格式：
{{"table": "...", "conditions": [...], ...}}  或  null"""

        try:
            from agent.open_layer.graph import _get_llm
            from core.llm_usage import set_role as _usage_role
            llm = _get_llm(role="ooda_correct")
            with _usage_role("ooda_correct"):
                response = llm.invoke([
                    SystemMessage(content="你是数据查询参数纠错专家。综合分析信号，判断是否需要修正structured_args。"),
                    HumanMessage(content=prompt),
                ])
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            # 处理null响应（AI判断不需要修正）
            if content.lower() in ("null", "none", "不需修正", "不需要修正", "不修正"):
                return None

            new_sargs = json.loads(content)
            if isinstance(new_sargs, dict) and new_sargs != sargs:
                return new_sargs
        except Exception:
            pass
        return None



    def run(self, user_input: str) -> str:

        from industries.base import discover_industries, get_industry
        from core.context import get_context
        get_context().set_trace_id()  # 生成请求追踪 ID
        discover_industries()
        cfg = get_industry(settings.INDUSTRY)

        # 开放式 AI 模式：走 LangGraph 编排器，子任务仍走 P1→树→P2
        # （open_ai_mode 恒为 True，原 else legacy 对话状态机死分支已删，R4）
        if self.open_ai_mode:
            from agent.open_layer.graph import run_open_agent
            return run_open_agent(user_input)
