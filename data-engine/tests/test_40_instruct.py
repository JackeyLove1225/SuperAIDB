"""层 40：硬路由元工具 execute_instruction 全链（20260824）

锁定硬路由的每一节链条：
1. 零 LLM 直达：文本铁证双命中 → 不调 LLM → 树路由 → 执行（确定性优先）
2. LLM 兜底 + 铁证纠偏：标签缺失调 LLM，文本冲突以文本为准
3. cannot_route → 未识别池记录 + 如实报（映射自学习原料）
4. 改/删+记录无选择集 → mutate_natural 统一语义回退
5. 人审闸在通道内全程在位（drop_table 裸上下文 fail-closed）
6. 纯确认语 → 指引管理端审批中心（AI 通道不结算人审闸）
7. 路由轨迹随结果返回（白盒审计）
"""
import sys; sys.path.insert(0, ".")
import json
from unittest.mock import patch


def _fake_ai(labels=None, tool_args=None, fail=False):
    """按调用内容分发的假 AIClient 实例：labels 给意图解析，tool_args 给 FC 提参"""
    calls = []

    class _AI:
        def call_function(self, functions, text, system_prompt=""):
            calls.append(text)
            if fail:
                raise AssertionError("确定性路径不得调用 LLM")
            fname = functions[0]["function"]["name"]
            if fname == "parse":
                return "parse", dict(labels or {})
            return fname, dict(tool_args or {})
    inst = _AI()
    inst.calls = calls
    return inst


def _patch_ai(inst):
    """统一 patch 面：get_instance 返回假实例（函数内 from-import 同源生效）"""
    from core.ai_runtime.ai_client import AIClient
    return patch.object(AIClient, "get_instance", classmethod(lambda cls: inst))


def _capture_execute():
    """捕获 execute_tool 调用（不落库）"""
    from core.tool_result import ToolResult
    calls = []

    def _exec(name, **kwargs):
        calls.append((name, kwargs))
        return ToolResult.ok(f"[FAKE {name} 结果]")
    return calls, _exec


def test_zero_llm_deterministic_route():
    """1. 零 LLM 直达：'现在数据库里有哪些表？' → 查+表 → describe_schema"""
    calls, _exec = _capture_execute()
    ai = _fake_ai(fail=True)  # 调 LLM 即炸——确定性路径必须零 LLM
    with _patch_ai(ai), \
         patch("core.tool_registry.execute_tool", _exec), \
         patch("core.tool_registry.execute_tool", _exec):
        from agent.tools.instruct import execute_instruction
        r = execute_instruction("现在数据库里有哪些表？")
    assert calls and calls[0][0] == "describe_schema", f"路由错: {calls}"
    assert "零LLM" in r.data.get("route", ""), f"应标零LLM轨迹: {r.data}"
    assert "[路由:" in r.text, "文本应带路由轨迹"
    print("OK - 零LLM直达：铁证双命中→树→describe_schema（未调 LLM）")


def test_llm_fallback_and_evidence_correction():
    """2. LLM 兜底 + 铁证纠偏（分支按真实可达性设计）：
    a) 行为铁证命中、对象铁证落空 → LLM 补对象（兜底真实发生）；
    b) LLM 标签与文本铁证冲突时以文本为准（patch 覆盖层直接打纠偏分支）"""
    # 2a. 兜底：对象铁证落空 → LLM 补对象 → 增+记录 → insert_data
    calls, _exec = _capture_execute()
    ai = _fake_ai(labels={"behavior_key": "增", "db_category_key": "记录", "constraint": ""},
                  tool_args={"table": "quota_items", "data": "{\"quota_code\": \"T1\"}"})
    with _patch_ai(ai),          patch("core.tool_registry.execute_tool", _exec):
        from agent.tools.instruct import execute_instruction
        execute_instruction("往 quota_items 插入一条记录 quota_code=T1")
    assert ai.calls, "对象铁证落空应走 LLM 兜底"
    assert calls and calls[0][0] == "insert_data", f"路由错: {calls}"
    assert calls[0][1].get("table") == "quota_items", f"P2 提参错: {calls[0][1]}"

    # 2b. 纠偏分支：LLM 把对象打成"数据库"，文本铁证为"表" → 以文本为准
    import agent.ai_extract as ax
    ai3 = _fake_ai(labels={"behavior_key": "查", "db_category_key": "数据库", "constraint": ""})
    with _patch_ai(ai3),          patch("agent.router.text_behavior_override", lambda _t: ""),          patch("agent.router.text_db_override", lambda _t: "表"):
        intent = ax.extract_intent("数据库中有哪些表？")
    assert intent["db_category"] == "表",         f"LLM 误标'数据库'应被文本纠偏为'表': {intent}"
    print("OK - LLM兜底补对象 + 纠偏分支：文本铁证优先于 LLM 标签")


def test_cannot_route_records_pool(tmp_pool):
    """3. cannot_route → 未识别池记录 + 如实报（自学习原料）"""
    class _UnsupTree:
        def route(self, *a):
            return "unsupported_op"
    with patch("agent.router.get_tree", return_value=_UnsupTree()), \
         _patch_ai(_fake_ai(labels={"behavior_key": "改", "db_category_key": "精度"})):
        from agent.tools.instruct import execute_instruction
        r = execute_instruction("把 a 的精度改成 9,9")
    assert r.data.get("reason") == "cannot_route", r.data
    from core.unrecognized import list_unrecognized
    pool = list_unrecognized()
    assert any("精度" in p["q"] for p in pool), f"未入池: {pool}"
    print("OK - cannot_route：入池+如实报+意图标签随池记录")


def test_mutate_natural_fallback():
    """4. 改/删+记录无选择集 → mutate_natural 回退（统一候选语义）"""
    seen = {}
    from core.tool_result import ToolResult
    def _fake_mutate(instruction, action=""):
        seen["instruction"] = instruction
        seen["action"] = action
        return ToolResult.ok("已更新 1 条")
    ai = _fake_ai(labels={"behavior_key": "改", "db_category_key": "记录", "constraint": ""})
    with _patch_ai(ai), \
         patch("core.data_ops.mutate_natural", _fake_mutate):
        from agent.tools.instruct import execute_instruction
        r = execute_instruction("把 TEST-901 的 base_price 改成 200")
    assert seen.get("action") == "update", f"应回退 mutate_natural(update): {seen}"
    assert r.data.get("ok"), r.data
    print("OK - 改+记录无选择集：回退 mutate_natural 统一语义")


def test_nuke_gate_inside_channel():
    """5. 人审闸在通道内：删+表 → drop_table → 裸上下文 fail-closed（不执行）"""
    ai = _fake_ai(labels={"behavior_key": "删", "db_category_key": "表", "constraint": ""},
                  tool_args={"table": "t_probe"})
    with _patch_ai(ai):
        from agent.tools.instruct import execute_instruction
        r = execute_instruction("删除 t_probe 表")
    assert r.data.get("reason") == "nuke_rejected", \
        f"裸上下文人审闸应安全拒绝: {r.data}"
    print("OK - 人审闸在通道内：drop_table 裸上下文 fail-closed")


def test_pure_confirm_guides_to_console(tmp_pending):
    """6. 纯确认语 → 指引管理端审批中心（AI 通道不结算人审闸）"""
    from agent.tools.instruct import execute_instruction
    r = execute_instruction("确认")
    assert "待审批" in r.text or "审批" in r.text, f"应指引审批中心: {r.text[:120]}"
    assert "不得" in r.text or "只能" in r.text, "应明示 AI 通道不可结算"
    print("OK - 纯确认语：指引管理端审批中心，不在 AI 通道结算")


def test_nl_to_batch_create_tables():
    """7b. NL→多表 DDL 提取（演示旗舰场景回归锁）：增+表 → batch_create_tables，
    P2 的 definitions 提取（2 张表/字段/外键）落参正确"""
    calls, _exec = _capture_execute()
    defs = [{"name": "books", "business_name": "图书",
             "columns": [{"name": "book_code", "type": "TEXT", "business_name": "编号"},
                         {"name": "title", "type": "TEXT", "business_name": "书名"}],
             "foreign_keys": []},
            {"name": "borrow_log", "business_name": "借阅记录",
             "columns": [{"name": "book_code", "type": "TEXT", "business_name": "图书编号"}],
             "foreign_keys": [{"columns": ["book_code"], "references": "books",
                               "ref_columns": ["id"]}]}]
    ai = _fake_ai(labels={"behavior_key": "增", "db_category_key": "表", "constraint": ""},
                  tool_args={"definitions": defs})
    with _patch_ai(ai), \
         patch("core.tool_registry.execute_tool", _exec):
        from agent.tools.instruct import execute_instruction
        r = execute_instruction("创建图书和借阅记录两张表")
    assert calls and calls[0][0] == "batch_create_tables", f"建表路由错: {calls}"
    got = calls[0][1].get("definitions")
    got = json.loads(got) if isinstance(got, str) else got
    assert isinstance(got, list) and len(got) == 2 and got[0]["name"] == "books", \
        f"definitions 落参错: {str(got)[:120]}"
    assert r.data.get("ok"), r.data
    print("OK - NL→batch_create_tables：增+表路由+definitions 提取落参正确")


def test_route_trace_attached():
    """7. 路由轨迹随结果返回（白盒审计）"""
    calls, _exec = _capture_execute()
    ai = _fake_ai(labels={"behavior_key": "查", "db_category_key": "统计", "constraint": ""},
                  tool_args={"table": "quota_items", "agg_func": "COUNT"})
    with _patch_ai(ai), \
         patch("core.tool_registry.execute_tool", _exec):
        from agent.tools.instruct import execute_instruction
        r = execute_instruction("quota_items 有多少条记录")
    assert calls and calls[0][0] == "aggregate_query", f"统计应路由 aggregate_query: {calls}"
    assert calls[0][1].get("table") == "quota_items" and calls[0][1].get("agg_func") == "COUNT", \
        f"P2 提参错: {calls[0][1]}"
    assert r.data.get("route", "").startswith("查+统计 → aggregate_query"), r.data
    print("OK - 统计路由：查+统计→aggregate_query，P2 提参正确，轨迹完整")


# ── fixtures（池/挂起隔离，不碰生产文件）──

import pytest  # noqa: E402


@pytest.fixture()
def tmp_pool(tmp_path, monkeypatch):
    from core.file_contract import JsonContract
    import core.unrecognized as u
    monkeypatch.setattr(u, "_POOL", JsonContract(tmp_path / "pool.json", default_factory=list))
    yield tmp_path


@pytest.fixture()
def tmp_pending(tmp_path, monkeypatch):
    # 挂起表指向临时文件（直写真实 config/pending_approvals.json 会
    # 中途崩溃留尸污染生产面）
    import core.pending_ops as po
    monkeypatch.setattr(po, "_STORE", tmp_path / "pending_approvals.json")
    from core.pending_ops import register_pending
    token = register_pending("probe_tool", {"table": "t"}, "影响面")
    yield token
    from core.pending_ops import pop_pending
    pop_pending(token)


def test_real_tree_fail_closed():
    """8. 真实树兜底 fail-closed（阻断回归锁，禁用 mock 树冒充）：
    空标签/未知分类经真实树右链尾全部落 unsupported_op（不得落
    save_template/add_column/alter_precision 写工具）；空行为标签不进树
    （直接 unroutable + 入池）；合法路由零误伤。"""
    from agent.router import get_tree
    t = get_tree()
    for bk in ("", "增", "改", "删", "导出"):
        got = t.route(bk, "", "")
        assert got == "unsupported_op", f"{bk or '空'}+未知 应 fail-closed: {got}"
    # 合法路由不受影响（钉死防误伤）
    assert t.route("改", "精度", "") == "alter_precision"
    assert t.route("改", "字段", "") == "modify_column"
    assert t.route("增", "记录", "") == "insert_data"
    # 空行为标签不进树（直接 unroutable）
    with _patch_ai(_fake_ai(fail=True)):
        from agent.tools.instruct import execute_instruction
        r = execute_instruction("……")
    assert r.data.get("reason") == "cannot_route", r.data
    print("OK - 真实树兜底 fail-closed + 空意图不进树 + 合法路由零误伤")


def run():
    """run_all 脚本制入口（无 pytest 收集，手动传 fixtures 等价物）"""
    import tempfile
    from pathlib import Path
    test_zero_llm_deterministic_route()
    test_llm_fallback_and_evidence_correction()
    # cannot_route（手动 fixture）
    with tempfile.TemporaryDirectory() as tmp:
        from core.file_contract import JsonContract
        import core.unrecognized as u
        orig = u._POOL
        u._POOL = JsonContract(Path(tmp) / "pool.json", default_factory=list)
        try:
            test_cannot_route_records_pool(tmp)
        finally:
            u._POOL = orig
    test_mutate_natural_fallback()
    test_nuke_gate_inside_channel()
    # pure confirm（手动 fixture：登记一个挂起）
    import tempfile as _tf
    from pathlib import Path as _P2
    with _tf.TemporaryDirectory() as _tp:
        import core.pending_ops as po
        _orig_store = po._STORE
        po._STORE = _P2(_tp) / "pending_approvals.json"
        try:
            from core.pending_ops import register_pending, pop_pending
            token = register_pending("probe_tool", {"table": "t"}, "影响面")
            try:
                test_pure_confirm_guides_to_console(token)
            finally:
                pop_pending(token)
        finally:
            po._STORE = _orig_store
    test_route_trace_attached()
    test_nl_to_batch_create_tables()
    test_real_tree_fail_closed()
    print("\n✅ 层 40 全部通过：硬路由 execute_instruction 全链")


if __name__ == "__main__":
    run()
