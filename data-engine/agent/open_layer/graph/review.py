"""graph 子包·未识别问法审核节点（映射层自学习）

由原 graph.py 拆分而来（facade 模式，纯搬家不改逻辑）。
"""

from core.logger import get_logger

logger = get_logger(__name__)

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from agent.open_layer.state import AgentState, FileContext

# 经 facade 调用时取值：_get_llm 被测试 patch.object(graph, "_get_llm") 遮蔽，
# 节点内必须读 facade 上的当前值（循环导入安全：见 graph/__init__.py 注释）
from agent.open_layer import graph as _g


def unrecognized_review(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """未识别问法审核：AI 提议映射样例 → interrupt 人工确认 → 纳入 prompts.yml

    边界：树结构不动，只追加 decompose_examples（映射层自学习）；
    out_of_scope（需要新行为/对象）的条目不纳入，如实汇报由人来定结构。
    """
    from langgraph.types import interrupt
    from core.unrecognized import (
        load_pool, propose_examples, archive_examples, clear_pool)

    llm = _g._get_llm(role="review")  # 映射提议需要推理（未识别问法→标准映射）
    from config.settings import settings
    industry = settings.INDUSTRY

    pool = load_pool(industry)
    if not pool:
        msg = "目前没有采集到未识别的问法。等攒到 3 条以上路由失败的问法，再来找我审核。"
        return {**state, "messages": list(state.get("messages", [])) + [AIMessage(content=msg)],
                "results": [msg]}

    try:
        from core.llm_usage import set_role as _usage_role
        with _usage_role("review"):
            proposals = propose_examples(industry, llm, max_items=10)
    except Exception as e:
        msg = f"样例提议失败（AI 暂不可用）：{str(e)[:100]}。池里有 {len(pool)} 条待审核，稍后再试。"
        return {**state, "messages": list(state.get("messages", [])) + [AIMessage(content=msg)],
                "results": [msg]}

    if not proposals:
        msg = f"池里有 {len(pool)} 条未识别问法，但 AI 未能给出可用样例。可以让我重试，或清空重来。"
        return {**state, "messages": list(state.get("messages", [])) + [AIMessage(content=msg)],
                "results": [msg]}

    lines = []
    for i, p in enumerate(proposals, 1):
        if p.get("out_of_scope"):
            lines.append(f"{i}. \"{p.get('query')}\"——⚠️ 超出树能力：{p.get('note', '')}（不纳入，需结构级扩展）")
        else:
            st = (p.get("sub_tasks") or [{}])[0]
            lines.append(f"{i}. \"{p.get('query')}\" → {st.get('behavior_key', '?')}×{st.get('db_category_key', '?')}"
                         f"（{st.get('query', '')[:40]}）")
    desc = (f"共 {len(pool)} 条未识别问法，AI 提议了 {len(proposals)} 条映射样例：\n" + "\n".join(lines)
            + "\n\n批准后纳入行业路由样例（decompose_examples，树结构不变）；"
              "拒绝则保留在池中下次再审；也可直接编辑样例。")

    decision = interrupt({
        "action_requests": [{
            "name": "confirm_unrecognized",
            "args": {"proposals": proposals, "pool_size": len(pool)},
            "description": desc,
        }],
        "review_configs": [{
            "action_name": "confirm_unrecognized",
            "allowed_decisions": ["approve", "reject", "edit"],
        }],
    })

    d = {}
    if isinstance(decision, dict):
        decisions = decision.get("decisions") or []
        d = decisions[0] if decisions else {}
    dtype = d.get("type", "approve")

    if dtype == "reject":
        msg = f"已保留 {len(pool)} 条未识别问法在池中，未做任何纳入。随时说「看看没识别的问题」再审。"
        return {**state, "messages": list(state.get("messages", [])) + [AIMessage(content=msg)],
                "results": [msg]}

    final_proposals = proposals
    if dtype == "edit":
        edited = d.get("edited_action", {}).get("args", {})
        if isinstance(edited.get("proposals"), list):
            final_proposals = edited["proposals"]

    added = archive_examples(industry, final_proposals)
    # 已提议的从池中移除（只保留未提议/超范围的）
    proposed_queries = {p.get("query") for p in final_proposals if not p.get("out_of_scope")}
    remaining = [it for it in pool if it.get("query") not in proposed_queries]
    from core.unrecognized import _write_pool
    _write_pool(industry, remaining)
    oos = [p for p in final_proposals if p.get("out_of_scope")]

    msg_parts = [f"已纳入 {added} 条路由样例到行业配置（decompose_examples，立即生效）。"]
    if oos:
        msg_parts.append(f"{len(oos)} 条超出树能力未纳入，需要你设计结构级扩展：" +
                         "；".join(f"\"{p.get('query')}\"" for p in oos[:3]))
    msg_parts.append("现在可以把这些问法再问一遍验证效果。")
    msg = "\n".join(msg_parts)
    return {**state, "messages": list(state.get("messages", [])) + [AIMessage(content=msg)],
            "results": [msg]}
