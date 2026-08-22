"""层 31：失败重规划回路（方案C，20260806）

agent_run 循环步数耗尽（路径走不通）→ route_after_agent 判 replan →
replan 节点携失败证据（轨迹摘要）写 replan_note 回 understand 重拆 →
换路径执行。上限 1 次（replan_count<1）防无限循环；replan 轮跳过短路
（短路看不见失败证据，命中只会重蹈覆辙同一路径）。

覆盖：
1. 路由判定 route_after_agent：exhausted+有额度→replan；exhausted+额度用尽→synthesize；
   ok→synthesize；缺省字段（老 state 无 replan 键）→synthesize
2. replan 节点：replan_note 含轨迹证据、replan_count+1、任务态全重置、agent_status 清空
3. understand 消费 replan_note：跳过短路 + 失败证据拼进拆解 prompt + 消费即清
4. 图级闭环（mock decompose LLM + mock run_agent）：
   agent_run(exhausted) → replan → understand（带证据重拆）→ agent_run(ok) → synthesize，
   一轮 invoke 内完成，run_agent 恰被调 2 次，第二轮拆解 prompt 携带失败证据
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import HumanMessage

from agent.open_layer import graph as g

_TRACE = [{"step": 1, "tool": "query", "args": {"query": "q1"}, "result_head": "（无结果）"}]
_PLAN_JSON = ('{"sub_tasks": [{"type": "db", "query": "查 A1-6 的价格", '
              '"behavior_key": "查", "db_category_key": "记录", "constraint": "", '
              '"structured_args": {}}], "is_complex": false, "task_type": "basic"}')


def test_route_after_agent():
    """路由判定：只在「步数耗尽 + 重规划额度未用」时进 replan，其余一律 synthesize"""
    assert g.route_after_agent({"agent_status": "exhausted", "replan_count": 0}) == "replan"
    assert g.route_after_agent(
        {"agent_status": "exhausted", "replan_count": 1}) == "synthesize", "上限 1 次"
    assert g.route_after_agent({"agent_status": "ok", "replan_count": 0}) == "synthesize"
    assert g.route_after_agent({}) == "synthesize", "缺省字段（老 state）必须安全落 synthesize"
    print("OK - route_after_agent：耗尽+额度→replan，额度用尽/正常/缺省→synthesize")


def test_replan_node_evidence_and_reset():
    """replan 节点：失败证据（轨迹摘要）写入 replan_note，任务态全重置，计数+1"""
    out = g.replan({"agent_trace_note": "query → query", "replan_count": 0,
                    "sub_tasks": [{"type": "db", "query": "旧任务"}],
                    "results": ["旧结果"], "failed_tasks": ["旧失败"],
                    "current_step": 3, "agent_status": "exhausted"})
    note = out["replan_note"]
    assert "步数耗尽" in note and "query → query" in note, f"证据未入 replan_note: {note}"
    assert out["replan_count"] == 1
    # 任务态全重置：防旧子任务/旧结果残留污染重拆
    assert out["sub_tasks"] == [] and out["results"] == [] and out["failed_tasks"] == []
    assert out["current_step"] == 0 and out["agent_status"] == "" and out["task_type"] == "basic"
    print("OK - replan 节点：证据入 replan_note、计数+1、任务态全重置")


def test_understand_consumes_replan_note():
    """understand 消费 replan_note：跳过短路、证据拼进拆解 prompt、消费即清"""
    note = "（系统提示：上一轮执行失败——智能体循环步数耗尽仍未完成。已尝试路径：query。）"
    state = {"messages": [HumanMessage(content="查 A1-6 的价格")], "replan_note": note}
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = MagicMock(content=_PLAN_JSON)
    sc = MagicMock(return_value={"sub_tasks": [], "is_complex": False, "task_type": "basic"})
    with patch.object(g, "_get_llm", return_value=mock_llm), \
         patch("agent.open_layer.shortcircuit.try_shortcircuit", sc), \
         patch.object(g, "list_document_collections", return_value=[]):
        out = g.understand_and_decompose(state)
    sc.assert_not_called(), "replan 轮必须跳过短路（短路看不见失败证据，命中只会重蹈覆辙）"
    mock_llm.invoke.assert_called_once()
    prompt_text = str(mock_llm.invoke.call_args[0][0])
    assert "步数耗尽" in prompt_text and "已尝试路径" in prompt_text, "失败证据未进拆解 prompt"
    assert out["replan_note"] == "", "replan_note 消费后必须清空（防残留污染后续正常轮次）"
    assert out["sub_tasks"][0]["query"] == "查 A1-6 的价格"
    print("OK - understand 消费 replan_note：跳过短路、证据入 prompt、消费即清")


def test_replan_circuit_end_to_end():
    """图级闭环：exhausted → replan → 带证据重拆 → 循环成功 → synthesize（一轮 invoke）"""
    decompose_llm = MagicMock()
    decompose_llm.invoke.side_effect = [MagicMock(content=_PLAN_JSON),
                                        MagicMock(content=_PLAN_JSON)]
    mock_run_agent = MagicMock(side_effect=[
        ("（步数耗尽，未能完成）", _TRACE, "exhausted"),   # 第一轮：路径走不通
        ("A1-6 的价格是 88 元。", _TRACE, "ok"),           # 重拆后第二轮：成功
    ])
    app = g.build_graph()
    with patch.object(g, "_get_llm", return_value=decompose_llm), \
         patch("agent.open_layer.shortcircuit.try_shortcircuit", return_value=None), \
         patch.object(g, "list_document_collections", return_value=[]), \
         patch("agent.open_layer.agent_loop.run_agent", mock_run_agent):
        out = app.invoke({"messages": [HumanMessage(content="查 A1-6 的价格")]})

    assert mock_run_agent.call_count == 2, "exhausted 后必须经 replan 重拆再进一次循环"
    assert decompose_llm.invoke.call_count == 2, "首拆 + 重拆恰两次拆解"
    second_prompt = str(decompose_llm.invoke.call_args_list[1][0][0])
    assert "步数耗尽" in second_prompt, "重拆 prompt 必须携带上轮失败证据"
    assert out["replan_count"] == 1, "replan 恰触发 1 次"
    final_reply = out["messages"][-1].content
    assert "88" in final_reply, f"最终答复应来自重拆后的成功路径: {final_reply!r}"
    print("OK - 图级闭环：exhausted→replan→带证据重拆→循环成功→synthesize")


if __name__ == "__main__":
    test_route_after_agent()
    test_replan_node_evidence_and_reset()
    test_understand_consumes_replan_note()
    test_replan_circuit_end_to_end()
    print("\n层 31 全绿：失败重规划回路（路由判定/节点重置/证据消费/图级闭环）")
