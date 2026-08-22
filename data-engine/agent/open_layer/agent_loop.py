"""智能体循环（观察-调整 / ReAct）：统一执行器

设计原则（与全项目同一套白盒哲学）：
- 先判断再动手：AI 先观察环境（有哪些表/结构/工具），判断指令合理性与
  可行路径，缺信息先查再执行——不预设立场，不盲目动手
- 思考灵活：AI 在循环里自主决定调哪个工具、观察结果、调整策略，
  直到完成任务（或步数用尽如实汇报已尝试路径，不编造）
- 动作受控：动作空间 = 注册表全量（20260806 方案B：差距1"工具空间分裂"收口），
  一律经 execute_tool 统一入口——核武人审闸（drop_table 等 DDL）/选择集闸
  （edit/delete_data）/契约校验/权限/审计全在底层生效，与决策树路径同一套护栏；
  放权不放大风险
- 全程可追踪：每一步（工具/参数/结果摘要）落日志并汇入执行轨迹，
  附在最终答案末尾——路径不再预先确定，但事后完全可见
"""
import json
from core.logger import get_logger

from agent.open_layer.prompts import MAX_RESULT_CHARS, truncate_result

logger = get_logger(__name__)

# 核心工具面：prompt 展示顺序与心智模型（高频查询+记录写）。
# 运行时可用集 = 注册表全量（unsupported_op 除外——路由失败占位，不是真工具）。
AGENT_TOOLS = (
    # 只读
    "query", "describe_schema", "join_query", "aggregate_query",
    "list_databases", "search_documents",
    # 记录级写
    "insert_data", "batch_insert_data", "mutate_data", "edit_data", "delete_data",
)

MAX_AGENT_STEPS = 10

# ── 循环韧性护栏（20260807 统一循环内核，移植 Reasonix run_loop 工程）──
# 模型打转是真实事故形态：同参数原地重试 / 连续全失败还反复冲 / 空答复 /
# 没动手就声称已执行。四道护栏均为"nudge 一次纠偏 → 再犯熔断如实汇报"，
# 熔断不再消耗 LLM 调用（省钱），宁停不编。
MAX_SAME_CALL_NUDGE = 2   # 同工具同参数连续重复达到此次数 → 注入纠偏
MAX_SAME_CALL_FUSE = 3    # 再犯 → 熔断
MAX_STALL_NUDGE = 2       # 连续零进展轮（全失败/全拦截/无新工具面）→ 注入纠偏
MAX_STALL_FUSE = 3        # 再犯 → 熔断
MAX_EMPTY_FINAL = 2       # 空答复重试上限

_NUDGE_SAME_CALL = ("你连续以完全相同的参数调用同一工具，结果不会有变化。"
                    "停下来分析：表名/字段名不对（先 describe_schema 核对真实结构）、"
                    "条件太严（放宽再试），还是目标不存在（如实汇报）？禁止原地重试。")
_NUDGE_STALL = ("连续多步操作没有任何实质进展（全部失败/被拦截/被拒绝）。"
                "停止重试轰炸，先总结失败的共同原因，换一条可行路径；"
                "确实做不到就如实汇报已尝试路径。")
_NUDGE_EMPTY = "你的上一条回答没有任何内容。请直接给出答案，或调用工具获取信息。"
_NUDGE_FABRICATED = ("你的回答声称完成了写操作，但本轮你并未实际调用任何写工具，"
                     "这是编造执行结果。立即纠正：真正调用工具执行，或如实说明尚未执行。")

# 完成态宣称词（反编造门用；命中且全程无写工具调用 → 疑似幻觉汇报）
_WRITE_CLAIM_RE = None  # 惰性编译（见 _claims_write）


def _claims_write(text: str) -> bool:
    """答复文本是否声称完成了写操作（完成态动词 + 数据对象）"""
    global _WRITE_CLAIM_RE
    if _WRITE_CLAIM_RE is None:
        import re
        _WRITE_CLAIM_RE = re.compile(
            r"(已(经)?(删除|修改|更新|插入|添加|创建|新建|清空|写入|导入|入库)|"
            r"(删除|修改|更新|插入|添加|创建|新建|清空|写入|导入|入库)(完成|成功|完毕))")
    return bool(_WRITE_CLAIM_RE.search(text))


def _is_write_tool(name: str) -> bool:
    """按注册表元数据判定写工具（risk_level 非 readonly；单一事实源）"""
    from core.tool_registry import _tools
    t = _tools.get(name)
    return bool(t) and t.risk_level not in ("readonly", "", None)


def _call_sig(name: str, args: dict) -> str:
    """工具调用签名（同工具+同参数=同签名，重复检测用）"""
    return name + "|" + json.dumps(args or {}, sort_keys=True, ensure_ascii=False)


def _is_fail_out(text: str) -> bool:
    """工具结果是否为失败/拦截/拒绝/挂起（零进展判定；文本级，尽力识别）"""
    head = (text or "").lstrip()[:30]
    return any(k in head for k in (
        "错误", "失败", "不存在", "未批准", "被拒绝", "拒绝", "挂起", "不支持", "无此工具"))


def _agent_tool_names() -> tuple:
    """运行时可用工具集：注册表全量（核心面优先，其余按注册顺序追加）"""
    from core.tool_registry import _tools
    core = [n for n in AGENT_TOOLS if n in _tools]
    rest = [n for n in _tools if n not in AGENT_TOOLS and n != "unsupported_op"]
    return tuple(core) + tuple(rest)


def _build_system_prompt() -> str:
    """系统 prompt：工作方式纪律 + 全量工具面按风险级分组清单。

    全量可见 = AI 有"先判断"的完整信息（能不能做、该用哪个、代价是什么）；
    核武级工具明确标注"调用即弹人工确认卡"——AI 据此预判路径成本与用户干预点。
    分组清单从注册表元数据动态生成（单一事实源，工具增删零维护）。
    """
    from core.tool_registry import _tools
    groups = {"readonly": [], "record_write": [], "ddl": [], "file": [], "admin": []}
    for name in _agent_tool_names():
        t = _tools.get(name)
        if t is None:
            continue
        desc = t.description.split("。")[0] if t.description else ""
        groups.setdefault(t.risk_level or "file", []).append(
            f"{name}（{desc}）" if desc else name)
    def _fmt(key):
        return "、".join(groups.get(key, [])) or "（无）"
    return f"""你是数据操作智能体。理解用户意图后，先观察判断再动手，用手头的工具逐步完成任务。

工作方式（先判断 → 摸底 → 执行 → 观察 → 调整）：
0. **先判断**：读完指令先想——用户要达成什么？我需要先了解什么信息（有哪些表/结构/现有数据）？
   我的工具面里哪个工具能达成它？缺信息先查（describe_schema/query），路径不明先说明再动手
1. **先摸底**：不确定表结构时，先 describe_schema 看真实表名/字段/外键；
   写操作前必须先确认目标真实存在（表/字段/条件命中的记录），再动手
2. 只读工具：{_fmt("readonly")}
3. 记录级写（硬闸纪律，不得绕过）：
   - 插入 → insert_data / batch_insert_data（唯一键冲突会被契约拒绝，如实汇报）
   - 修改/删除记录 → **一律用 mutate_data**（内置人审闸：候选多条时挂起等用户确认）
   - 仅当用户明确说"删除/修改**查询到的这些**记录"时，才可用 delete_data/edit_data（选择集即用户确认的批次）
4. 结构与库级工具（{_fmt("ddl")}；{_fmt("admin")}）：
   在工具面内、可直接调用——**带人审闸的工具调用即弹人工确认卡**：
   用户批准后执行；被拒绝就如实汇报"用户未批准该操作"，禁止换工具变相达成、禁止重复调用轰炸
5. 文件工具：{_fmt("file")}
6. 工具报错或结果为空时：分析原因（表名错？字段错？条件太严？），**调整后重试**——
   例如中文字段名被拒，就换 describe_schema 返回的真实英文字段名再来；禁止编造数据
7. **每一步工具返回后先自问：现在的信息够不够回答/完成任务？够了就立即收尾**——
   不要因为想做得更全而继续调用工具（步数有限，够用即止）
8. 答案用中文，表格用 markdown；写操作如实汇报影响行数与结果（含被拦截/挂起/被拒绝）
9. 确实做不到：如实说明，并简述你试过哪些路径"""


def _tool_schemas() -> list[dict]:
    """从工具注册表自动生成 function-calling 参数模式（单一事实源，不重复维护）"""
    from core.tool_registry import _tools
    schemas = []
    for name in _agent_tool_names():
        t = _tools.get(name)
        if not t:
            continue
        props, required = {}, []
        for p in t.params:
            jtype = {"str": "string", "int": "integer", "bool": "boolean",
                     "float": "number"}.get(p.type, "string")
            props[p.name] = {"type": jtype, "description": p.description}
            if p.required:
                required.append(p.name)
        schemas.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        })
    return schemas


def build_session_note() -> str:
    """会话状态便签（20260805 上下文修复 C1）：注入当前输入尾部——

    选择集等会话态只存进程内 context，AI 原本完全看不到
    （"删除查询到的这些记录"的"这些"指代无着落）。便签把可见状态如实告知。
    无状态时返回空串：不占 token；便签只出现在消息尾部，不破坏前缀缓存。
    """
    from core.context import get_context
    sels = get_context().list_selections()
    if not sels:
        return ""
    # 最多展示最近 3 个选择集，防便签膨胀
    lines = [f"- #{s['id']} 表 {s['table']}（{s['count']} 条，来自「{s.get('query', '')[:40]}」）"
             for s in sels[-3:]]
    return ("【会话状态（系统注入，供理解上下文，非用户输入）】\n"
            "当前可用选择集：\n" + "\n".join(lines) +
            "\n用户若说「删除/修改查询到的这些记录」，用 delete_data/edit_data 并传对应 selection_id。")


def _log_usage(ai, step: int) -> None:
    """token 用量与缓存命中落账（B3）——DeepSeek 前缀缓存命中部分低价计费，
    命中率是迭代 3.0 成本统计的输入。尽力提取，缺字段不报错。"""
    try:
        um = getattr(ai, "usage_metadata", None) or {}
        inp = um.get("input_tokens")
        out = um.get("output_tokens")
        hit = (um.get("input_token_details") or {}).get("cache_read")
        if inp is None:
            tu = (getattr(ai, "response_metadata", None) or {}).get("token_usage") or {}
            inp = tu.get("prompt_tokens")
            out = tu.get("completion_tokens")
            hit = tu.get("prompt_cache_hit_tokens")
        if inp is not None:
            logger.info("agent_run 第%d步 token: 输入=%s 输出=%s 缓存命中=%s",
                        step, inp, out, hit if hit is not None else "-")
    except Exception:
        pass


def run_agent(user_input: str, llm, history: list | None = None,
              state_note: str = "", max_steps: int = MAX_AGENT_STEPS):
    """观察-调整循环：AI 自主调用白名单工具直到完成任务。

    Args:
        user_input: 用户原始问题
        llm: 规划级 LLM（bind_tools 后使用）
        history: 最近 N 轮对话历史（prompts.build_chat_history 产出的消息对象）——
                 多轮指代（"再查一下"/"把它删掉"）的上下文来源；逐轮只追加不改写，
                 前缀缓存友好（20260805 前此参数不存在，循环零历史）
        state_note: 会话状态便签（build_session_note 产出），并入当前输入尾部
        max_steps: 最大步数（防死循环；用尽如实汇报）

    Returns:
        (final_text, trace, status)——最终答案 + 执行轨迹 [{step, tool, args, result_head}]
        + 结局状态：ok=正常收口；exhausted=步数用尽（宽限轮收口后仍无答案）；
        stalled=打转熔断（同调用重复/连续零进展，如实汇报不编造）
    """
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    from core.tool_registry import execute_tool

    tool_names = set(_agent_tool_names())
    bound = llm.bind_tools(_tool_schemas())
    content = f"{state_note}\n\n{user_input}" if state_note else user_input
    # 消息布局（缓存友好）：稳定 System → 历史（只追加）→ 当前输入（尾部）
    msgs = [SystemMessage(content=_build_system_prompt()),
            *(history or []),
            HumanMessage(content=content)]
    trace: list[dict] = []
    final = ""
    status = "ok"
    # 护栏计数器（per-run，一次性——Reasonix perTurnState 纪律：字段随轮次显式重置，
    # 不加进状态结构就不会忘记重置；nudged 标记防 nudge 重复轰炸）
    same_call_streak = 0      # 同签名调用连续重复计数
    last_sigs: frozenset = frozenset()
    stall_streak = 0          # 连续零进展轮计数
    empty_final = 0           # 空答复计数
    fabricated_nudged = False # 反编造门只 nudge 一次（防纠偏本身成循环）
    fused = ""                # 熔断原因（非空即已熔断）

    for step in range(1, max_steps + 1):
        ai = bound.invoke(msgs)
        _log_usage(ai, step)
        msgs.append(ai)
        calls = getattr(ai, "tool_calls", None) or []
        text = ai.content if isinstance(ai.content, str) else str(ai.content or "")

        if not calls:
            # 空答复重试（DeepSeek 偶发 content 空块）：超限熔断，宁停不编
            if not text.strip():
                empty_final += 1
                if empty_final > MAX_EMPTY_FINAL:
                    fused = "模型连续返回空答复"
                    break
                msgs.append(HumanMessage(content=_NUDGE_EMPTY))
                continue
            # 反编造门：声称完成写操作但全程无写工具调用 → 纠偏一次
            if not fabricated_nudged and _claims_write(text) \
                    and not any(_is_write_tool(t["tool"]) for t in trace):
                fabricated_nudged = True
                logger.info("反编造门：答复宣称写完成但 trace 无写工具 → 纠偏")
                msgs.append(HumanMessage(content=_NUDGE_FABRICATED))
                continue
            final = text
            break

        # 工具轮：执行 + 停滞/风暴检测
        round_sigs = frozenset(_call_sig(tc.get("name", ""), tc.get("args", {}) or {})
                               for tc in calls)
        round_all_fail = True
        for tc in calls:
            name = tc.get("name", "")
            args = tc.get("args", {}) or {}
            if name not in tool_names:
                # 幻觉工具名拦截（LLM 编造注册表不存在的工具）——如实记录不执行
                out = f"工具 {name} 不存在（注册表无此工具）"
            else:
                out = execute_tool(name, **args)
                # 目标达成检测钩子（4.5）：写操作 effects 独立复查，
                # 报告附加到 text/data（单向依赖，工具层零感知，异常不阻断）
                from core.tool_result import ToolResult as _TR
                if isinstance(out, _TR):
                    from core.goal_verify.hooks import attach as _gv_attach
                    _gv_attach(out)
            out = truncate_result(str(out), MAX_RESULT_CHARS)
            if not _is_fail_out(out):
                round_all_fail = False
            trace.append({"step": step, "tool": name, "args": args,
                          "result_head": out[:80]})
            logger.info("agent_run 第%d步: %s(%s) → %s", step, name,
                        json.dumps(args, ensure_ascii=False)[:150],
                        out[:80].replace("\n", " "))
            msgs.append(ToolMessage(content=out, tool_call_id=tc.get("id", "")))

        # 停滞判定：同签名重复 或 整轮零进展（全失败/拦截/拒绝/挂起）
        if round_sigs and round_sigs == last_sigs:
            same_call_streak += 1
        else:
            same_call_streak = 0
        last_sigs = round_sigs
        stall_streak = stall_streak + 1 if round_all_fail else 0

        if same_call_streak >= MAX_SAME_CALL_FUSE:
            fused = "同一调用原地重复（换路径纠偏无效）"
        elif stall_streak >= MAX_STALL_FUSE:
            fused = "连续多步零进展（全部失败/被拦截）"
        elif same_call_streak >= MAX_SAME_CALL_NUDGE:
            logger.info("停滞护栏：同签名调用连续 %d 轮 → 注入纠偏", same_call_streak + 1)
            msgs.append(HumanMessage(content=_NUDGE_SAME_CALL))
        elif stall_streak >= MAX_STALL_NUDGE:
            logger.info("风暴护栏：连续 %d 轮零进展 → 注入纠偏", stall_streak)
            msgs.append(HumanMessage(content=_NUDGE_STALL))
        if fused:
            logger.info("循环熔断：%s（已用 %d 步）", fused, step)
            break

    if fused:
        status = "stalled"
        tried = " → ".join(t["tool"] for t in trace) or "（未调用任何工具）"
        final = (f"我在执行中停了下来：{fused}。已尝试路径：{tried}。"
                 "请补充更具体的信息（如表名/字段/筛选条件），或换个说法我再试。")
    elif not final:
        # 步数用尽：宽限轮（给一次无工具的收口机会——基于已获得信息尽力回答，
        # 常见情形：最后一步工具已经拿到答案，只是没来得及说）；真答不了再如实汇报
        status = "exhausted"
        msgs.append(HumanMessage(
            content="步数已达上限。请基于目前已经获得的信息直接回答用户的问题；"
                    "信息确实不足就如实说明缺什么，禁止编造。"))
        ai = llm.invoke(msgs)
        final = ai.content if isinstance(ai.content, str) else str(ai.content)
        if not final.strip():
            tried = " → ".join(t["tool"] for t in trace) or "（未调用任何工具）"
            final = (f"我在 {max_steps} 步内没能得到确定答案（已尝试：{tried}）。"
                     "请把问题说得更具体些（如指明表名/字段），或拆成几个小问题问我。")
    return final, trace, status
