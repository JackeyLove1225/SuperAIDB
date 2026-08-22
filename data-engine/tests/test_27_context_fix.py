"""层 36：上下文修复回归网（20260805 上下文修复 A1/A2/C1/B1）

mock 脚本式测试（不依赖真实 LLM）：
1. 多轮指代：build_chat_history 窗口/剔除/截断规则 + run_agent 消息组装顺序
   （System 稳定 → 历史只追加 → 当前输入尾部，前缀缓存友好的唯一布局）
2. 误判回归：_is_pure_confirm 整句精确匹配——"帮我看看对不对"不再被"对"误判
3. 模板顺序：build_decompose_prompt 稳定段前置、变化段沉底、指令最后，
   不同历史/指令的两次调用共享长公共前缀（DeepSeek 前缀缓存命中的前提）
4. 会话状态便签：build_session_note 无选择集不占 token、有选择集如实告知

测试网目的：上下文链路（历史→注入→缓存布局）后续再动时，本层兜底防回退。
"""
import sys; sys.path.insert(0, ".")

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage


# ── 公共桩 ──

class _RecordingLLM:
    """捕获首次 invoke 收到的完整消息列表（验证组装顺序），随后按脚本收口。"""

    def __init__(self, script=("兜底答复",)):
        self.script = list(script)
        self.first_msgs = None

    def bind_tools(self, schemas):
        return self

    def invoke(self, msgs):
        if self.first_msgs is None:
            self.first_msgs = list(msgs)
        item = self.script.pop(0) if self.script else "（脚本耗尽）"
        if isinstance(item, str):
            return AIMessage(content=item)
        name, args = item
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": args, "id": "call_x", "type": "tool_call"}])


def _make_conversation(turns: int, with_tool_noise: bool = True):
    """造 turns 轮对话 + 最新一条用户消息；中间夹工具噪声（应被历史剔除）"""
    msgs = []
    for i in range(turns):
        msgs.append(HumanMessage(content=f"第{i+1}轮问题"))
        msgs.append(AIMessage(content=f"第{i+1}轮回答"))
        if with_tool_noise:
            msgs.append(AIMessage(content="", tool_calls=[{
                "name": "query", "args": {"query": "x"}, "id": f"tc{i}"}]))
            msgs.append(ToolMessage(content="工具内部结果", tool_call_id=f"tc{i}"))
    msgs.append(HumanMessage(content="再查一下"))
    return msgs


# ── 1. 多轮指代：历史组装器 ──

def test_history_excludes_latest_user_and_tool_noise():
    """窗口规则：剔除最新用户消息（当前指令调用方单传）；工具轨迹一律不进历史"""
    from agent.open_layer.prompts import build_chat_history
    msgs = _make_conversation(3)
    hist = build_chat_history(msgs)
    # 3 轮 = 6 条；最新"再查一下"不在历史中
    assert len(hist) == 6, f"应恰 6 条: {len(hist)}"
    assert all(isinstance(m, (HumanMessage, AIMessage)) for m in hist)
    assert not any(getattr(m, "tool_calls", None) for m in hist), "带 tool_calls 的 AI 消息必须剔除"
    assert not any(isinstance(m, ToolMessage) for m in hist), "ToolMessage 必须剔除"
    assert hist[-1].content == "第3轮回答", f"历史最后一条应是上轮回答: {hist[-1].content}"
    print("OK - 历史窗口：剔除最新用户消息 + 工具轨迹")


def test_history_max_turns_window():
    """超出窗口只留最近 N 轮（防历史无限膨胀吃 token）"""
    from agent.open_layer.prompts import build_chat_history, HISTORY_MAX_TURNS
    msgs = _make_conversation(HISTORY_MAX_TURNS + 5, with_tool_noise=False)
    hist = build_chat_history(msgs)
    assert len(hist) == HISTORY_MAX_TURNS * 2, f"应截到 {HISTORY_MAX_TURNS * 2} 条: {len(hist)}"
    # 保留的是最近的轮次
    assert hist[0].content == f"第6轮问题", f"应丢弃最早轮次: {hist[0].content}"
    print("OK - 历史窗口：超出只留最近 N 轮")


def test_history_truncation():
    """单条截断：AI 500 / 用户 1000 字符上限，超长加省略号"""
    from agent.open_layer.prompts import (
        build_chat_history, HISTORY_AI_MAX_CHARS, HISTORY_USER_MAX_CHARS)
    msgs = [
        HumanMessage(content="用" * (HISTORY_USER_MAX_CHARS + 100)),
        AIMessage(content="答" * (HISTORY_AI_MAX_CHARS + 100)),
        HumanMessage(content="当前指令"),
    ]
    hist = build_chat_history(msgs)
    assert len(hist[0].content) == HISTORY_USER_MAX_CHARS + 3  # "..."
    assert len(hist[1].content) == HISTORY_AI_MAX_CHARS + 3
    assert hist[0].content.endswith("...") and hist[1].content.endswith("...")
    print("OK - 历史截断：用户 1000 / AI 500 上限")


def test_text_and_object_forms_same_window():
    """文本形态（decompose 用）与对象形态（agent 循环用）同一窗口规则（A1 单一事实源）"""
    from agent.open_layer.prompts import build_chat_history, format_conversation_history
    msgs = _make_conversation(2, with_tool_noise=False)
    hist_objs = build_chat_history(msgs)
    hist_text = format_conversation_history(msgs)
    for obj in hist_objs:
        assert obj.content in hist_text, f"对象形态与文本形态窗口不一致: {obj.content}"
    # 无历史时文本形态返回空串（调用方据此决定是否追加历史段）
    assert format_conversation_history([]) == ""
    assert build_chat_history([]) == []
    print("OK - 两种形态同一窗口；空历史返回空")


def test_run_agent_injects_history_and_note():
    """run_agent 消息组装（A2）：System 稳定 → 历史原样 → 当前输入（便签并入尾部）"""
    from agent.open_layer.agent_loop import run_agent
    from agent.open_layer.prompts import build_chat_history
    convo = _make_conversation(2, with_tool_noise=False)
    history = build_chat_history(convo)
    llm = _RecordingLLM()
    final, _trace, _status = run_agent("再查一下", llm, history=history, state_note="【会话状态】测试便签")
    msgs = llm.first_msgs
    # 布局：System + 4 条历史 + 当前输入
    assert len(msgs) == 1 + 4 + 1, f"消息数不符: {len(msgs)}"
    assert msgs[0].type == "system", "首条必须是稳定 System"
    assert [m.content for m in msgs[1:-1]] == [m.content for m in history], \
        "历史必须原样进入（只追加不改写是前缀缓存命中前提）"
    assert msgs[-1].type == "human"
    assert msgs[-1].content.startswith("【会话状态】测试便签"), "便签并入当前输入尾部"
    assert msgs[-1].content.endswith("再查一下")
    # 无便签时 content 就是原始输入（不多占 token）
    llm2 = _RecordingLLM()
    run_agent("再查一下", llm2, history=history)
    assert llm2.first_msgs[-1].content == "再查一下"
    print("OK - run_agent 组装：System→历史→当前输入；便签只入尾部")


# ── 2. 误判回归：纯确认词整句精确匹配 ──

def test_pure_confirm_exact_match():
    """整句归一后精确命中确认词表 → True"""
    from agent.open_layer.graph import _is_pure_confirm
    for t in ("确认", "执行", "好的", "可以", "对", "是", "yes", "OK", "确认执行", "确定",
              " 确认 ", "对。", "好的！", "ok "):
        assert _is_pure_confirm(t), f"应判纯确认: {t!r}"
    print("OK - 纯确认词：正常命中（含空白/语气标点归一）")


def test_pure_confirm_no_substring_false_positive():
    """含确认词子串的自由表述 → False（回归：原子串匹配把"对不对"的"对"误判确认）"""
    from agent.open_layer.graph import _is_pure_confirm
    for t in ("帮我看看对不对", "确认删除所有记录", "是不是这样", "好的，先查一下库存",
              "可以的话把价格改了", "执行计划是什么", "对不起", "确认一下有哪些表"):
        assert not _is_pure_confirm(t), f"不得误判: {t!r}"
    print("OK - 误判回归：自由表述不再被子串拦截")


# ── 3. 模板顺序：B1 缓存布局 ──

def test_decompose_prompt_layout_stable_first():
    """布局断言：规则/JSON 稳定段 → 表清单 → 历史 → 文件清单 → 用户指令（最后）"""
    from agent.open_layer.prompts import build_decompose_prompt
    p = build_decompose_prompt(
        "查一下库存", "集合A", 10,
        tables_str="表: t_stock",
        history_text="用户: 你好\n助手: 你好",
        manifest_text="📂 工作区已加载文件：x.xlsx")
    i_rule = p.find("请返回 JSON")
    i_tables = p.find("数据库当前可用表")
    i_hist = p.find("对话历史")
    i_manifest = p.find("📂")
    i_instr = p.find("用户指令：")
    assert -1 not in (i_rule, i_tables, i_hist, i_manifest, i_instr), "各段都必须存在"
    assert i_rule < i_tables < i_hist < i_manifest < i_instr, \
        f"布局必须为 稳定段→表→历史→清单→指令: {(i_rule, i_tables, i_hist, i_manifest, i_instr)}"
    assert p.rstrip().endswith("用户指令：查一下库存"), "用户指令必须沉底"
    print("OK - 模板布局：稳定段前置，历史/清单/指令沉底")


def test_decompose_prompt_shared_prefix_across_turns():
    """缓存命中前提：不同历史/指令的两次调用，稳定头部逐字节一致"""
    from agent.open_layer.prompts import build_decompose_prompt
    common = dict(collections="集合A", max_tasks=10, tables_str="表: t_stock")
    p1 = build_decompose_prompt("查库存", history_text="用户: 甲\n助手: 乙", **common)
    p2 = build_decompose_prompt("删掉它", history_text="用户: 丙\n助手: 丁", **common)
    # 公共前缀必须至少覆盖到"对话历史"段之前（即稳定段+表清单+术语段全量命中）
    cut = p2.find("对话历史")
    assert cut > 0
    assert p1[:cut] == p2[:cut], "历史段之前的稳定头部必须逐字节一致（前缀缓存命中前提）"
    # 且稳定头部长度占比可观（>50%），缓存命中率才有成本意义
    assert cut > len(p2) * 0.5, f"稳定头部过短: {cut}/{len(p2)}"
    print("OK - 前缀缓存：跨轮稳定头部逐字节一致且占比过半")


# ── 4. 会话状态便签：C1 ──

def test_session_note_empty_without_selections():
    """无选择集 → 空串（不占 token）"""
    from core.context import get_context
    from agent.open_layer.agent_loop import build_session_note
    get_context().clear_all()
    assert build_session_note() == ""
    print("OK - 便签：无选择集返回空串")


def test_session_note_lists_selections():
    """有选择集 → 如实列出 id/表/条数（"删除查询到的这些记录"的指代着落）"""
    from core.context import get_context
    from agent.open_layer.agent_loop import build_session_note
    ctx = get_context()
    ctx.clear_all()
    try:
        sid = ctx.save_selection("t_demo", [{"id": 1, "code": "M1"}, {"id": 2, "code": "M2"}],
                                 query="查螺母", datasource="test_ds")
        note = build_session_note()
        assert f"#{sid}" in note and "t_demo" in note and "2 条" in note, note
        assert "selection_id" in note, "必须告知 AI 用 selection_id 传参"
    finally:
        ctx.clear_all()
    print("OK - 便签：选择集如实注入")


if __name__ == "__main__":
    test_history_excludes_latest_user_and_tool_noise()
    test_history_max_turns_window()
    test_history_truncation()
    test_text_and_object_forms_same_window()
    test_run_agent_injects_history_and_note()
    test_pure_confirm_exact_match()
    test_pure_confirm_no_substring_false_positive()
    test_decompose_prompt_layout_stable_first()
    test_decompose_prompt_shared_prefix_across_turns()
    test_session_note_empty_without_selections()
    test_session_note_lists_selections()
    print("\n层 36 全绿")
