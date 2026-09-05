"""硬路由元工具——execute_instruction（20260824）

唯一自然语言数据通道：一切 NL 数据操作（查/增/删/改/导入/导出/DDL）
在本处理器内走完整确定性链路，上层 AI 客户端不得也无法绕过：

  原话 → P1 意图标签（文本铁证零 LLM 优先，LLM 兜底+铁证纠偏）
       → 决策树确定性路由（agent/router，7 行为 × 15 对象）
       → P2 FC 提参（封闭 schema，假想名由边界闸兜底）
       → execute_tool（边界闸/高危人审闸/force 闸全程在位）
       → 结果 + 路由轨迹（白盒：每次执行的路由路径可审计）

改/删+记录的特殊语义：edit_data/delete_data 需要选择集——无选择集时
回退 mutate_natural 统一改/删语义（候选 0 如实报/1 走人审闸/N 挂起
管理端审批中心），与历史 execute_single 同口径。
"""
from core.logger import get_logger
from core.tool_result import ToolResult

logger = get_logger(__name__)


def _confirm_guidance() -> "ToolResult | None":
    """纯确认语（"确认/执行/好"）→ 指引到管理端审批中心（人审挂起不在此结算）"""
    from core.pending_ops import list_pending
    pend = list_pending()
    if not pend:
        return None
    lines = [f"当前有 {len(pend)} 项待批准的高危操作（10 分钟有效期）："]
    for p in pend[:5]:
        lines.append(f"- {p['name']}（影响面 {len(p.get('impact',''))} 字）")
    lines.append("批准/拒绝只能在 Web 管理台的「权限管理 → 待审批」中进行——"
                 "AI 通道不得也无法自行结算人审闸。")
    return ToolResult.ok("\n".join(lines), action="approval_guide")


def _p2_extract_args(tool_def, instruction, intent, route_trace):
    """P2：FC 提参（有必需参数或意图来自 LLM 兜底时才调 LLM）。
    确定性直达（铁证双命中）且参数全可选（list_databases/describe_schema 类）
    → 空调用：AI 无参数可提，调 LLM 是纯浪费——全链零 LLM 才名副其实。
    返回 (args, err)"""
    args = {}
    need_p2 = bool(tool_def.params) and (
        any(p.required for p in tool_def.params) or not intent["deterministic"])
    if need_p2:
        try:
            from agent.ai_extract import extract_tool_args
            args = extract_tool_args(tool_def, instruction)
        except Exception as e:
            # LLM 提参失败按临时故障如实报（不静默带空参执行）
            return None, ToolResult.fail(f"参数解析失败（AI 提参异常）: {e}",
                                         code="TRANSIENT", reason="fc_ai_failed",
                                         route=route_trace)
    return args, None


def execute_instruction(instruction: str = "", database: str = "") -> "ToolResult":
    """硬路由唯一 NL 入口：P1 → 树 → P2 → execute_tool（含路由轨迹）

    Args:
        instruction: 自然语言指令（如"查 t 表有多少条记录"）
        database: 目标数据源（可选，缺省默认库）
    """
    instruction = (instruction or "").strip()
    if not instruction:
        return ToolResult.fail("请给出自然语言指令", code="VALIDATION",
                               reason="missing_params")

    # 纯确认语：人审挂起不在 AI 通道结算（token 不出管理通道），如实指引
    from agent.router import get_tree
    if instruction in ("确认", "执行", "确认执行", "好", "好的", "批准"):
        g = _confirm_guidance()
        if g is not None:
            return g

    # ── P1：意图标签（确定性优先）──
    from agent.ai_extract import extract_intent
    intent = extract_intent(instruction)
    bk, dk, ct = intent["behavior"], intent["db_category"], intent["constraint"]

    # 空行为标签（LLM 故障/无命中）不进树——空标签经树右链
    # 会落到具体工具（fail-closed 前的历史兜底），写工具被执行而用户无意。
    # 行为缺省=无法识别，如实报 + 入未识别池（自学习原料）
    if not bk:
        from core.unrecognized import record_unrecognized
        record_unrecognized(instruction, reason="行为意图未识别（LLM 兜底失败或无命中）",
                            intent=f"{bk}/{dk}")
        return ToolResult.fail(
            "未能理解您的意图（请换一种说法，如明确说'查询/插入/修改/删除'）"
            "——已记入未识别池，可在管理端确认后自动学习该问法",
            code="VALIDATION", reason="cannot_route", route=f"?+{dk or '?'}")
    # ── 树：确定性路由 ──
    tool_name = get_tree().route(bk, dk, ct)
    from core.tool_registry import get_tool, execute_tool
    tool_def = get_tool(tool_name)
    route_trace = f"{bk or '?'}+{dk or '?'} → {tool_name}"
    if intent["deterministic"]:
        route_trace += "（零LLM）"
    if tool_def is None or tool_name == "unsupported_op":
        # cannot_route：未识别问法入池（映射自学习的原料），如实报
        from core.unrecognized import record_unrecognized
        record_unrecognized(instruction,
                            reason=f"意图 {bk or '?'}+{dk or '?'} 无路由",
                            intent=f"{bk}/{dk}")
        return ToolResult.fail(
            f"暂不支持的操作（意图：{bk or '?'}+{dk or '?'})——已记入未识别池，"
            "可在管理端确认后自动学习该问法", code="VALIDATION",
            reason="cannot_route", route=route_trace)

    logger.info("硬路由: %s ← %s", route_trace, instruction[:60])

    # ── P2：FC 提参 ──
    args, err = _p2_extract_args(tool_def, instruction, intent, route_trace)
    if err:
        return err
    if database and not args.get("database"):
        args["database"] = database

    # 改/删+记录的特殊语义：edit_data/delete_data 无选择集时回退
    # mutate_natural 统一改/删语义（候选分流+人审闸内置）
    if tool_name in ("edit_data", "delete_data") and not args.get("selection_id"):
        from core.data_ops import mutate_natural
        r = mutate_natural(instruction,
                           action=("update" if tool_name == "edit_data" else "delete"))
        return _with_trace(r, route_trace)

    r = execute_tool(tool_name, **args)
    # 边界闸拦下（假想表名/字段名）也入池——映射缺口的另一半来源
    #（自学习叙事与实际接线同宽）
    if isinstance(r, ToolResult) and r.data.get("reason") == "arg_validation":
        from core.unrecognized import record_unrecognized
        record_unrecognized(instruction,
                            reason=str(r.text)[:100], intent=route_trace)
    return _with_trace(r, route_trace)


def _with_trace(r: "ToolResult", trace: str) -> "ToolResult":
    """结果附路由轨迹（白盒：每次执行的意图→工具路径可审计）"""
    if isinstance(r, ToolResult):
        data = dict(r.data)
        data["route"] = trace
        text = r.text
        if text and "[路由:" not in text:
            text = f"{text}\n[路由: {trace}]"
        return ToolResult(text, data)
    return r
