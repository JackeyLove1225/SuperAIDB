"""AI 结构提取（编排层职责，自 core/data_ops 上移）

AI 只做"自然语言 → 结构化操作"的提取，不碰 SQL——确定性拼装与执行
（_build_set_where / 契约层 / 驱动）全部留在 core。本模块经
core.data_ops.register_mutation_extractor 依赖倒置注册（方向铁律：
core 不 import agent，能力由编排层启动时注入）。

硬路由时代（20260824）新增 P1/P2：
- extract_intent：确定性优先的意图标签提取（文本铁证零 LLM 先行，
  LLM 兜底后再经铁证纠偏）——execute_instruction 的 P1
- extract_tool_args：按工具参数 schema 的 FC 提参——execute_instruction 的 P2
"""
from core.logger import get_logger

logger = get_logger(__name__)


def extract_mutation_ops(instruction: str) -> list:
    """自然语言 → 结构化改/删操作列表（AI 只提取结构，不碰 SQL）

    返回 operations 列表（供 core.data_ops.mutate_natural / parse_instruction
    经 DI 分发点消费）。
    """
    from core.data_ops import get_driver
    from core.ai_runtime.ai_client import AIClient

    drv = get_driver()
    table_lines = []
    for tname in drv.list_tables():
        cols = [c["name"] for c in drv.get_columns(tname)]
        table_lines.append(f"{tname}: {', '.join(cols)}")
    tables_desc = "\n".join(table_lines)

    ai = AIClient.get_instance()

    # 构建可用字段名提示（字段字典走行业加载器单源，与 core.data_ops.resolve_field 同一货源）
    from industries.base import get_current_industry
    fd = get_current_industry().field_dict or {}
    field_aliases = {}
    for fname, finfo in fd.items():
        field_aliases[fname] = fname
        for alias in (finfo or {}).get("alias", []):
            field_aliases[alias] = fname
    fields_hint = ", ".join(sorted(field_aliases.keys())[:20])

    functions = [{
        "type": "function",
        "function": {
            "name": "edit_data",
            "description": "修改或删除数据",
            "parameters": {
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "table": {"type": "string", "description": "表名"},
                                "action": {"type": "string", "enum": ["UPDATE", "DELETE"]},
                                "set_fields": {
                                    "type": "array",
                                    "description": f"UPDATE 时设置的值。支持字段: {fields_hint}",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "field": {"type": "string", "description": "字段名"},
                                            "value": {"type": "string", "description": "新值"},
                                        },
                                        "required": ["field", "value"],
                                    },
                                },
                                "where_conditions": {
                                    "type": "array",
                                    "description": f"WHERE 条件（多个条件用 link 连接）。多个值用 OR 连接，不要用 IN。支持字段: {fields_hint}",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "field": {"type": "string", "description": "字段名"},
                                            "op": {"type": "string", "enum": ["=", "!=", "<>", ">", "<", ">=", "<=", "LIKE", "NOT LIKE", "IN", "NOT IN", "BETWEEN", "NOT BETWEEN", "IS NULL", "IS NOT NULL"], "description": "运算符"},
                                            "value": {"type": "string", "description": "值"},
                                            "link": {"type": "string", "enum": ["AND", "OR"], "description": "与上个条件的连接符"},
                                        },
                                        "required": ["field", "op", "value"],
                                    },
                                },
                            },
                        },
                    }
                },
                "required": ["operations"],
            },
        },
    }]
    from core.llm_usage import set_role as _usage_role
    with _usage_role("extract_param"):
        fn_name, fn_args = ai.call_function(functions, instruction, system_prompt=f"你是一个数据操作助手。当前表结构：\n{tables_desc}")
    return fn_args.get("operations", [])


# 启动即注册（DI）：core.data_ops._extract_mutation_ops 分发到本实现。
# 调用方链：agent/__init__ 导入本模块 → 注册发生；未注册时分发点显式报错（不静默）。
from core.data_ops import register_mutation_extractor

register_mutation_extractor(extract_mutation_ops)


# ═══════════════════════════════════════════════════════════════
# P1：意图标签提取（确定性优先，20260824 硬路由）
# ═══════════════════════════════════════════════════════════════

# P1 标签空间（封闭枚举——唯一真源；FC schema 与散文提示词同用，
# 层 39 断言与决策树路由标签集同源，手抄漂移即红）
_BEHAVIOR_KEYS = ("改", "查", "增", "删", "导入", "上传", "导出")
_DB_KEYS = ("数据库", "模板", "会话", "表", "记录", "选择集", "结构", "字段",
            "外键", "索引", "类型", "精度", "文件", "关联", "统计")

_INTENT_FC = [{
    "type": "function",
    "function": {
        "name": "parse",
        "description": "parse db command to structured fields",
        "parameters": {
            "type": "object",
            "properties": {
                "behavior_key": {"type": "string",
                                 "enum": list(_BEHAVIOR_KEYS)},
                "constraint": {"type": "string",
                               "description": "批量/第N页/所有/前N条/标准/自定义/单条/非空"},
                "db_category_key": {"type": "string",
                                    "enum": list(_DB_KEYS)},
            },
            "required": ["behavior_key", "db_category_key"],
        },
    }
}]


def _router_prompt() -> str:
    """语义路由提示词——行业示例/术语映射从 prompts.yml 注入（配置即代码）"""
    base = (
        "你是数据库自然语言接口的语义解析器。任务：从用户指令提取两个信息——"
        "1. behavior_key：用户想做什么（7选1）"
        "2. db_category_key：用户在什么对象上操作（15选1）。"
        "一、7种标准行为：改(修改、改成、改为、设置、设为、重命名)、查(查询、查看、列出、显示、有没有、哪些)、"
        "增(加、新增、添加、新建、创建、插入、录入)、删(删除、删掉、去掉、清空、清除、移除)、"
        "导入(导入、加载、读入)、上传(上传、传文件)、导出(导出、保存、另存为)。"
        "二、15种对象：数据库(数据库、库、引擎、类型、文件路径)、模板(模板、表模板、结构模板)、"
        "会话(对话、历史、聊天记录、会话)、表(建表、创建表、新建表、删表、标准表)、记录(数据、记录、行、内容、字段值)、"
        "选择集(选择集、暂存数据、暂存、筛选结果)、结构(表结构、结构、字段列表、列信息、外键关系)、"
        "文件(文件、文档、PDF、Excel、扫描件)、"
        "字段(加字段、删字段、字段、列)、外键(外键、FK、引用、关联、指向)、索引(索引、建索引、删除索引)、"
        "类型(数据类型、改为INTEGER、改为TEXT)、精度(精度、小数位、DECIMAL、精度设置)、"
        "关联(多表联合、JOIN、跨表查询、关联查询、连接查询、对比)、"
        "统计(统计、计数、求和、平均值、最大值、最小值、分组统计、聚合、总数)。"
        "三、关键原则：1.设为非空/索引→改行为，非空/索引是对象，constraint=非空。"
        "2.模糊动词没对象→默认dk=记录。3.多表关联查询→dk=关联。4.计数/求和/平均值/分组统计→dk=统计。"
        "5.问'有哪些表/几张表/有什么表'→dk=表（即使句中出现'数据库'字样）；"
        "查→数据库只用于问数据库本身（有哪些数据库、库的类型、路径）。"
        "6.只输出 behavior_key 和 db_category_key。"
    )
    try:
        from industries.base import get_current_industry
        cfg = get_current_industry()
        if cfg.router_examples:
            exs = "；".join(f'{e["input"]}→{{{e["behavior_key"]},{e["db_category_key"]}}}'
                            for e in cfg.router_examples)
            base += f"行业示例：{exs}。"
        term = cfg.terminology or {}
        parts = []
        for std, aliases in (term.get("behavior_aliases") or {}).items():
            if aliases:
                parts.append(f'"{"，".join(aliases)}"→behavior_key={std}')
        for std, aliases in (term.get("object_aliases") or {}).items():
            if aliases:
                parts.append(f'"{"，".join(aliases)}"→db_category_key={std}')
        for std_t, aliases in (term.get("table_aliases") or {}).items():
            if aliases:
                parts.append(f'"{"，".join(aliases)}"→表={std_t}')
        if parts:
            base += "术语映射（行业/个人表达→标准值）：" + "；".join(parts) + "。"
    except Exception:
        pass  # 配置缺失时用纯通用提示词（fail-open，行业示例是增强非必需）
    return base


def extract_intent(instruction: str) -> dict:
    """自然语言 → 意图标签（行为/对象/约束）——确定性优先

    流程：
    1. 文本铁证先行（text_behavior_override/text_db_override，纯代码零 LLM）：
       双命中即定案，不调 LLM——大部分常见句式零成本零犯错面
    2. 任一缺失 → LLM 解析（封闭枚举 FC），产出再经铁证纠偏
       （LLM 标签与文本冲突时以文本为准）
    3. canonicalize_intent 归一（别名→canonical）后返回

    Returns:
        {"behavior": str, "db_category": str, "constraint": str,
         "deterministic": bool（零 LLM 定案）, "llm_used": bool}
    """
    from agent.router import (text_behavior_override, text_db_override,
                              canonicalize_intent)
    tbk, tdk = text_behavior_override(instruction), text_db_override(instruction)
    if tbk and tdk:
        bk, dk, ct = canonicalize_intent(tbk, tdk, "")
        return {"behavior": bk, "db_category": dk, "constraint": ct,
                "deterministic": True, "llm_used": False}

    # LLM 兜底（封闭枚举）
    from core.ai_runtime.ai_client import AIClient
    from core.llm_usage import set_role as _usage_role
    bk = dk = ct = ""
    try:
        ai = AIClient.get_instance()
        with _usage_role("extract_param"):
            _, args = ai.call_function(_INTENT_FC, instruction,
                                       system_prompt=_router_prompt())
        bk = (args.get("behavior_key") or "").strip()
        dk = (args.get("db_category_key") or "").strip()
        ct = (args.get("constraint") or "").strip()
    except Exception as e:
        logger.warning("P1 LLM 意图解析失败（按空标签进树）: %s", str(e)[:120])
    # 铁证纠偏：文本与 LLM 标签冲突时以文本为准
    if tbk and tbk != bk:
        logger.info("P1 意图标签纠偏：以文本关键词为准（%s → %s）", bk, tbk)
        bk = tbk
    if tdk and tdk != dk:
        logger.info("P1 对象标签纠偏：以文本关键词为准（%s → %s）", dk, tdk)
        dk = tdk
    bk, dk, ct = canonicalize_intent(bk, dk, ct)
    return {"behavior": bk, "db_category": dk, "constraint": ct,
            "deterministic": False, "llm_used": True}


# ═══════════════════════════════════════════════════════════════
# P2：按工具参数 schema 的 FC 提参（20260824 硬路由）
# ═══════════════════════════════════════════════════════════════

def _build_tool_fc(tool_def, all_tables, all_columns) -> list:
    """按工具参数定义构建 FC schema（已知表/列清单注入提示，压假想名犯错面）"""
    type_map = {"str": "string", "int": "integer", "bool": "boolean", "file": "string"}
    props = {}
    for p in tool_def.params:
        if p.internal:
            continue  # 内部保留参数不进 FC schema——AI 不可见即不可注入
        if p.schema:
            props[p.name] = {**p.schema, "description": p.description or ""}
            continue
        item = {"type": type_map.get(p.type, "string"),
                "description": p.description or ""}
        if p.name == "table":
            item["description"] += f"（已知表：{', '.join(all_tables) or '无'}）"
        elif p.name in ("main_table", "ref_table"):
            item["description"] += f"（已知表：{', '.join(all_tables) or '无'}）"
        elif p.name == "join_tables":
            item["description"] += "（多个用逗号分隔）"
        elif p.name == "column":
            item["description"] += f"（已知列：{', '.join(all_columns[:15])}）"
        elif p.name == "conditions":
            item["description"] += '。JSON数组：[{"field":"列名","op":"=,!=,<,>,<=,>=,LIKE 之一","value":"值"}]'
        elif p.name == "agg_func":
            item["enum"] = ["COUNT", "SUM", "AVG", "MIN", "MAX", "DISTINCT"]
        elif p.name == "selection_id":
            from core.context import get_context
            sels = get_context().list_selections()
            if sels:
                desc = ", ".join(f"#{s['id']}[{s['table']},{s['count']}条]" for s in sels)
                item["description"] += f"（当前可用：{desc}；未指定默认最近查询）"
            else:
                item["description"] += "（当前无选择集，需先查询创建）"
        props[p.name] = item
    return [{"type": "function", "function": {
        "name": tool_def.name,
        "description": tool_def.description,
        "parameters": {"type": "object", "properties": props,
                       "required": [p.name for p in tool_def.params if p.required]},
    }}]


def extract_tool_args(tool_def, instruction: str) -> dict:
    """自然语言 → 工具参数（FC 提参，只取用户明确提到的）

    产出按参数类型归一（int/bool 强转、复杂类型 JSON 化），假想表/字段名
    由下游 tool_arg_guard 边界闸兜底校验——本函数不做存在性判断。
    """
    from core.tool_arg_guard import enumerate_objects
    _, _, all_tables, all_columns, _ = enumerate_objects()
    from core.ai_runtime.ai_client import AIClient
    from core.llm_usage import set_role as _usage_role
    import json as _json

    fc = _build_tool_fc(tool_def, all_tables, all_columns)
    ai = AIClient.get_instance()
    with _usage_role("extract_param"):
        _, args = ai.call_function(
            fc, instruction,
            system_prompt=(
                "从用户指令中提取参数。只提取用户明确提到的，没提到的留空。"
                "表名/字段名必须从已知列表中选择，严禁猜测编造（如把 name 猜成 name_id）。"
                "如不确定就留空让系统自动补全。"
                "建表（batch_create_tables）时：definitions 的字段 name 必须是英文 "
                "snake_case，严禁中文；中文业务名放 business_name。"))
    out = {}
    for p in tool_def.params:
        val = args.get(p.name)
        if val is None or val == "":
            continue
        if p.type == "int":
            try:
                val = int(val)
            except (TypeError, ValueError):
                continue
        elif p.type == "bool":
            val = val if isinstance(val, bool) else str(val).lower() in ("true", "1", "yes", "是")
        elif p.type == "str" and isinstance(val, (dict, list)):
            val = _json.dumps(val, ensure_ascii=False)
        # 表名/字段名混淆纠偏：AI 把字段名填进了 table → 跳过
        if p.name == "table" and val not in all_tables and val in all_columns:
            continue
        out[p.name] = val
    return out
