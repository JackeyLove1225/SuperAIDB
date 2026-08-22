"""层 24：3.1 规划层确定性短路（agent/open_layer/shortcircuit.py + graph 接线）

短路只产意图标签（behavior_key/db_category_key/constraint），不产参数——
参数提取仍归 FC AI / agent_run 循环，执行护栏（P0 校验/人审闸/选择集分流）全在位。
词表单一事实源 config/shortcircuit.yml（模拟器 stats_shortcircuit.py 同读）。

覆盖三态 + 接线：

1. 命中态：统计/表/模板/数据库/字段/索引/查选择集 → 返回与 LLM 拆解同构的 plan
   （sub_tasks 单子任务、标签正确、structured_args 为空、is_complex=False、task_type=basic）
2. 未命中态（fail-open → 交 LLM 拆解）：复合指令 / 多轮指代无表 /
   改×选择集 / 删×选择集（选择集条件语境漂移，误路由 alter_precision 的真事故防线）
3. 关闭态：SHORTCIRCUIT_ENABLED=False → 任何问句一律 None（整体回退 LLM 拆解）
4. graph 接线：短路命中时 understand_and_decompose 直接返回 plan、不触发 llm.invoke；
   短路未命中时落回 LLM 拆解路径
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.open_layer.shortcircuit import try_shortcircuit
from config.settings import settings


def _assert_plan(plan, bk, dk, ct=""):
    """plan 与 LLM 拆解产出同构校验（下游 route_by_task_type/executor 消费的字段全集）"""
    assert plan is not None, "应命中短路"
    assert set(plan) >= {"sub_tasks", "is_complex", "task_type"}, \
        f"plan 缺顶层键: {plan.keys()}"
    assert plan["is_complex"] is False and plan["task_type"] == "basic"
    tasks = plan["sub_tasks"]
    assert len(tasks) == 1, f"短路只产单子任务: {tasks}"
    t = tasks[0]
    assert t["type"] == "db" and t["query"], f"子任务缺 type/query: {t}"
    assert (t["behavior_key"], t["db_category_key"], t["constraint"]) == (bk, dk, ct), \
        f"标签不符: 期望 ({bk},{dk},{ct!r}) 实际 ({t['behavior_key']},{t['db_category_key']},{t['constraint']!r})"
    assert t["structured_args"] == {}, "短路不产参数（参数归 FC AI / agent_run 循环）"


def test_hit_routes():
    """命中态：各对象大类确定性路由到正确意图标签"""
    cases = [
        ("统计quota_materials有多少条记录", "查", "统计", ""),
        ("查询有哪些表", "查", "表", ""),
        ("列出所有模板", "查", "模板", ""),
        ("有哪些数据库", "查", "数据库", ""),
        ("把quota_materials的price字段改成FLOAT", "改", "字段", ""),
        ("删除索引idx_quota_materials_price", "删", "索引", ""),
        ("查询选择集", "查", "选择集", ""),
    ]
    for q, bk, dk, ct in cases:
        _assert_plan(try_shortcircuit(q), bk, dk, ct)
    print(f"OK - 命中态：{len(cases)} 类问句标签全部正确且 plan 同构")


def test_miss_fail_open():
    """未命中态：复合/无表/选择集条件语境 → None（fail-open 交 LLM）"""
    cases = [
        "查询并删除quota_materials的数据",   # 真复合指令（查+删两族）
        "查一下",                            # 多轮指代、表落空
        "修改选择集#1的name为xx",            # 改×选择集：选择集是条件不是对象
        "删除选择集#1",                      # 删×选择集：同上（防 alter_precision 误路由）
        "",                                  # 空输入
    ]
    for q in cases:
        r = try_shortcircuit(q)
        assert r is None, f"{q!r} 应 fail-open 返回 None，实际: {r}"
    print(f"OK - 未命中态：{len(cases)} 类问句全部 fail-open")


def test_disabled_switch():
    """关闭态：总开关 False 时任何问句一律 None（整体回退 LLM 拆解）"""
    original = settings.SHORTCIRCUIT_ENABLED
    try:
        settings.SHORTCIRCUIT_ENABLED = False
        assert try_shortcircuit("统计quota_materials有多少条记录") is None
        assert try_shortcircuit("查询有哪些表") is None
    finally:
        settings.SHORTCIRCUIT_ENABLED = original
    # 恢复后命中能力复原（开关是运行时读取，非 import 时固化）
    _assert_plan(try_shortcircuit("查询有哪些表"), "查", "表")
    print("OK - 关闭态：开关关闭整体回退，恢复后命中能力复原")


def test_graph_wiring():
    """graph 接线：命中走短路（不触发 llm.invoke），未命中落回 LLM 拆解"""
    from langchain_core.messages import HumanMessage
    from agent.open_layer import graph as g

    state = {"messages": [HumanMessage(content="查询有哪些表")]}
    fake_plan = {
        "sub_tasks": [{"type": "db", "query": "查询有哪些表",
                       "behavior_key": "查", "db_category_key": "表",
                       "constraint": "", "structured_args": {}}],
        "is_complex": False, "task_type": "basic",
    }
    mock_llm = MagicMock()

    # 命中：try_shortcircuit 返回 plan → 直接返回，llm.invoke 零调用
    with patch.object(g, "_get_llm", return_value=mock_llm), \
         patch("agent.open_layer.shortcircuit.try_shortcircuit", return_value=fake_plan):
        out = g.understand_and_decompose(state)
    assert out["sub_tasks"] == fake_plan["sub_tasks"]
    assert out["is_complex"] is False and out["task_type"] == "basic"
    assert out["current_step"] == 0 and out["results"] == [] and out["failed_tasks"] == []
    mock_llm.invoke.assert_not_called()

    # 未命中：try_shortcircuit 返回 None → 落回 LLM 拆解（mock invoke 产 JSON）
    mock_llm2 = MagicMock()
    mock_llm2.invoke.return_value = MagicMock(
        content='{"sub_tasks": [{"type": "db", "query": "查一下"}], '
                '"is_complex": false, "task_type": "basic"}')
    with patch.object(g, "_get_llm", return_value=mock_llm2), \
         patch("agent.open_layer.shortcircuit.try_shortcircuit", return_value=None), \
         patch.object(g, "list_document_collections", return_value=[]):
        out2 = g.understand_and_decompose({"messages": [HumanMessage(content="查一下")]})
    mock_llm2.invoke.assert_called_once()
    assert out2["sub_tasks"][0]["query"] == "查一下"
    print("OK - graph 接线：命中零 LLM 调用，未命中落回 LLM 拆解")


if __name__ == "__main__":
    # 本层测短路功能本身，开关状态由测试自控（关闭态由 test_disabled_switch 覆盖）；
    # 进程级 SHORTCIRCUIT_ENABLED=false 是"回退态验收"场景——既有 16 层全绿即证明
    # 关闭短路不影响既有功能，本层固定开启以保证功能用例可运行
    settings.SHORTCIRCUIT_ENABLED = True
    test_hit_routes()
    test_miss_fail_open()
    test_disabled_switch()
    test_graph_wiring()
    print("\n✅ 层 24 全部通过：3.1 确定性短路（命中/未命中 fail-open/关闭回退/graph 接线）")
