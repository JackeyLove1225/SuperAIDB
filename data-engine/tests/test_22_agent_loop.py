"""层 22：agent_run 统一循环可测试化（迭代 2.3 / 20260806 方案B/C 更新）

mock LLM 脚本式测试网（不依赖真实 LLM）：
1. 意图→预期工具序列断言（查询单步收口 / 写操作摸底三步）
2. 幻觉工具名拦截（LLM 编造注册表不存在的工具 → 如实记录不执行）；
   DDL 工具在工具面内经真 execute_tool → 核武闸拒绝 → 表未删（方案B 语义：
   工具面=注册表全量，护栏在 execute_tool 核武闸，不在白名单）
3. 步数用尽收口路径（强制 max_steps 截断 → 无工具收口调用，status=exhausted）
4. trace 结构化断言（step/tool/args/result_head 四字段完整，可回放）
5. mutate_data 端到端（真实刮削库 + mock LLM：查→改→库内断言生效）

测试网目的：迭代 4 双轨契约大改时，本层是工具行为回归的兜底网。
"""
import sys; sys.path.insert(0, ".")
from core.crypto.connection import open_db
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage


class _MockLLM:
    """预设脚本式 LLM：bind_tools 返回自身；invoke 按脚本逐条吐响应。

    script 元素：
    - str                    → 最终文本答复（无 tool_calls，循环收口）
    - (tool_name, args:dict) → 一条 tool_call
    脚本耗尽仍被调用 → 返回兜底文本（模拟模型自发收口）
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def bind_tools(self, schemas):
        return self

    def invoke(self, msgs):
        self.calls += 1
        if not self.script:
            return AIMessage(content="（脚本耗尽，兜底收口）")
        item = self.script.pop(0)
        if isinstance(item, str):
            return AIMessage(content=item)
        name, args = item
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": args, "id": f"call_{self.calls}",
            "type": "tool_call"}])


def _fake_exec_factory(log: list):
    """假 execute_tool：记录调用并返回可辨识的固定串（循环逻辑测试用）"""
    def _fake(name, **kwargs):
        log.append((name, kwargs))
        return f"[FAKE {name} 结果]"
    return _fake


def test_query_intent_single_step():
    """查询意图：一步 query 拿到结果即收口——工具序列恰为 [query]"""
    from agent.open_layer.agent_loop import run_agent
    exec_log = []
    llm = _MockLLM([
        ("query", {"query": "A1-6 的价格"}),
        "A1-6 的价格是 88 元。",
    ])
    with patch("core.tool_registry.execute_tool", _fake_exec_factory(exec_log)):
        final, trace, status = run_agent("查一下 A1-6 的价格", llm)
    assert [t["tool"] for t in trace] == ["query"], trace
    assert final == "A1-6 的价格是 88 元。"
    assert status == "ok", status
    assert exec_log[0][0] == "query"
    print("OK - 查询意图单步收口，工具序列=[query]")


def test_write_intent_recon_then_act():
    """写意图：先摸底（describe_schema→query）再写（mutate_data）——序列完整"""
    from agent.open_layer.agent_loop import run_agent
    exec_log = []
    llm = _MockLLM([
        ("describe_schema", {"table": "quota"}),
        ("query", {"query": "编号 901 的记录", "table": "quota"}),
        ("mutate_data", {"instruction": "把 quota 表中编号为 901 的记录的 price 改成 200"}),
        "已修改 1 条记录。",
    ])
    with patch("core.tool_registry.execute_tool", _fake_exec_factory(exec_log)):
        final, trace, _s = run_agent("把编号 901 的 price 改成 200", llm)
    assert [t["tool"] for t in trace] == ["describe_schema", "query", "mutate_data"], trace
    assert "已修改" in final
    print("OK - 写意图摸底→执行三步序列完整")


def test_hallucinated_tool_blocked():
    """幻觉工具名（注册表不存在）→ 不进 execute_tool，trace 如实记录拦截"""
    from agent.open_layer.agent_loop import run_agent
    exec_log = []
    llm = _MockLLM([
        ("drop_everything", {"table": "quota"}),  # LLM 编造的幻觉工具
        "该工具不存在，无法执行。",
    ])
    with patch("core.tool_registry.execute_tool", _fake_exec_factory(exec_log)):
        final, trace, _s = run_agent("删掉所有东西", llm)
    assert exec_log == [], "幻觉工具不得进入 execute_tool"
    assert trace[0]["tool"] == "drop_everything"
    assert "不存在" in trace[0]["result_head"]
    print("OK - 幻觉工具名拦截且如实记录")


def test_ddl_tool_gated_by_nuke():
    """方案B 新语义：drop_table 在工具面内（注册表全量），护栏在核武闸——
    真 execute_tool + 闸拒绝 → 不执行，刮削库表仍在（护栏从白名单移到 execute_tool）"""
    from agent.open_layer.agent_loop import run_agent
    _setup_scratch_db()
    try:
        llm = _MockLLM([
            ("drop_table", {"table": "t_demo"}),
            "用户未批准删表操作。",
        ])
        # 裸测试环境无 graph runtime：patch 闸为拒绝（安全默认路径）
        with patch("core.tool_registry._nuke_confirmed", return_value=False):
            final, trace, _s = run_agent("删除表格t_demo", llm)
        assert trace[0]["tool"] == "drop_table"
        # 库内断言：表未被删（核武闸拦住）
        from core.data_ops import _get_driver
        assert "t_demo" in _get_driver().list_tables(), "闸拒绝后表必须仍在"
    finally:
        _teardown_scratch_db()
    print("OK - drop_table 在工具面内，核武闸拒绝 → 表未删")


def test_max_steps_exhausted_graceful_close():
    """步数用尽：追加一次无工具收口调用，仍返回文本而非裸 trace"""
    from agent.open_layer.agent_loop import run_agent
    exec_log = []
    llm = _MockLLM([
        ("query", {"query": "q1"}),
        ("query", {"query": "q2"}),
        # 步数用尽后的收口 invoke 拿到这个文本
        "根据目前信息：答案是 X。",
        ("query", {"query": "q3-never-reached"}),
    ])
    with patch("core.tool_registry.execute_tool", _fake_exec_factory(exec_log)):
        final, trace, status = run_agent("复杂问题", llm, max_steps=2)
    assert len(trace) == 2, f"应恰走 2 步: {trace}"
    assert final == "根据目前信息：答案是 X。", final
    assert status == "exhausted", f"步数用尽应标 exhausted（replan 信号）: {status}"
    # 收口调用后脚本剩 1 条未消费（q3 未执行）
    assert len(llm.script) == 1
    print("OK - 步数用尽收口：追加无工具 invoke，如实收尾")


def test_trace_structure_replayable():
    """trace 四字段完整（step/tool/args/result_head），可结构化回放"""
    from agent.open_layer.agent_loop import run_agent
    exec_log = []
    llm = _MockLLM([
        ("query", {"query": "库存", "table": "stock", "page": 1}),
        ("aggregate_query", {"table": "stock", "agg_func": "COUNT"}),
        "共 42 条。",
    ])
    with patch("core.tool_registry.execute_tool", _fake_exec_factory(exec_log)):
        _final, trace, _s = run_agent("库存有多少条", llm)
    for i, t in enumerate(trace, 1):
        assert set(t.keys()) == {"step", "tool", "args", "result_head"}, t
        assert t["step"] == i
        assert isinstance(t["args"], dict) and isinstance(t["result_head"], str)
    # 可序列化（落盘回放的前提）
    json.dumps(trace, ensure_ascii=False)
    # 回放：args 与 exec_log 逐条一致
    assert [t["tool"] for t in trace] == [n for n, _ in exec_log]
    assert [t["args"] for t in trace] == [a for _, a in exec_log]
    print("OK - trace 四字段完整可回放")


# ── 5. mutate_data 端到端（真实刮削库 + mock LLM）──

DS_FIXTURE = os.path.join("tests", "fixtures", "datasources_agent_loop.yml")
DB_PATH = os.path.join("db", "test_agent_loop.db")


def _setup_scratch_db():
    """独立刮削库（与层 19 同 fixture 模式，不同库文件防交叉）"""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    import sqlite3
    conn = open_db(DB_PATH)
    conn.execute("""CREATE TABLE t_demo(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE, name TEXT, price FLOAT, amount INTEGER)""")
    conn.execute("INSERT INTO t_demo(code,name,price,amount) VALUES('M1','螺母',5.0,100)")
    conn.commit()
    conn.close()
    from core.datasource_manager import DataSourceManager
    DataSourceManager.reset_instance()
    DataSourceManager().load_config(DS_FIXTURE)
    import core.data_ops as _ops
    _ops._federated_driver = None


def _teardown_scratch_db():
    try:
        import core.data_ops as _ops
        if _ops._federated_driver is not None:
            _ops._federated_driver.close()
    except Exception:
        pass
    from core.datasource_manager import DataSourceManager
    DataSourceManager.reset_instance()
    import core.data_ops as _ops
    _ops._federated_driver = None
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


def test_mutate_data_end_to_end():
    """mock LLM 驱动真实链路：query 摸底 → mutate_data 单候选核武闸批准 → 库内断言

    方案D/E（20260804/05）：单候选统一走核武闸（interrupt 人审），不再直执行。
    裸测试环境无 graph runtime，interrupt 抛 RuntimeError→安全默认拒绝执行；
    此处 patch 批准，验证"用户批准 → 真实执行到库"的完整链路。
    注意：mutate_data 内部的 NL→结构化解析（_extract_mutation_ops）走真实 AI——
    照 test_24 先例 mock 掉，本层才真正离线。
    """
    from agent.open_layer.agent_loop import run_agent
    import core.data_ops as _ops
    _setup_scratch_db()
    ops_parsed = [{
        "table": "t_demo", "action": "UPDATE",
        "set_fields": [{"field": "price", "value": "88"}],
        "where_conditions": [{"field": "code", "op": "=", "value": "M1"}],
    }]
    try:
        llm = _MockLLM([
            ("query", {"query": "code 为 M1 的记录", "table": "t_demo"}),
            ("mutate_data", {"instruction": "把 t_demo 表中 code 为 M1 的记录的 price 改成 88"}),
            "已将 M1 的 price 改为 88。",
        ])
        with patch("core.tool_registry._nuke_confirmed", return_value=True), \
             patch.object(_ops, "_extract_mutation_ops", lambda _i: ops_parsed):
            final, trace, _s = run_agent("把 M1 的 price 改成 88", llm)
        assert [t["tool"] for t in trace] == ["query", "mutate_data"], trace
        # 库内断言：price 真实变更
        from core.data_ops import _get_driver
        row = _get_driver().query("SELECT price FROM t_demo WHERE code='M1'")[0]
        assert row["price"] == 88.0, f"库内 price 应为 88: {row}"
        assert "88" in final
    finally:
        _teardown_scratch_db()
    print("OK - mutate_data 端到端：mock LLM→核武闸批准→真实执行→库内断言生效")


if __name__ == "__main__":
    test_query_intent_single_step()
    test_write_intent_recon_then_act()
    test_hallucinated_tool_blocked()
    test_ddl_tool_gated_by_nuke()
    test_max_steps_exhausted_graceful_close()
    test_trace_structure_replayable()
    test_mutate_data_end_to_end()
    print("\n层 22 全绿")
