"""层 30：契约层 force 确认卡（方案F 20260805）

对话模式下，契约校验层"需人工确认"的点（危险类型转换/精度收紧/外键删除确认等）
与人工闸同款卡片确认，替代"请回复 force=true"文字交互。

测试网覆盖：
1. need_force + 卡片批准 → 内部带 force=True 重试并成功，卡片含风险详情+折叠报告
2. need_force + 卡片拒绝 → 原样返回失败，handler 未被重试
3. 硬阻断（RiskError forceable=False）→ 不弹卡，原样返回指引
4. 非 graph 环境（interrupt 无上下文）→ 安全拒绝，handler 未被重试
5. _msg_result：confirm:True 历史标记同口径映射 need_force
6. force 链修复：schema_manager.modify_column/alter_precision/add_foreign_key
   自报 need_force 且 force=True 时传到底层 driver
"""
import sys; sys.path.insert(0, ".")
from unittest.mock import MagicMock, patch


def _register_tmp_tool(name, handler, params):
    """注册临时工具（用例结束移除，不污染注册表）"""
    from core.tool_registry import Tool, Param, register_tool
    ps = [Param(p, "bool", "", default=False) if p == "force"
          else Param(p, "str", "", default="") for p in params]
    register_tool(Tool(name=name, description="tmp", handler=handler, params=ps))


def _unregister(name):
    from core.tool_registry import _tools
    _tools.pop(name, None)


def test_force_card_approve_retries_with_force():
    """need_force + 批准：第二次调用带 force=True；卡片含风险详情+__fold__报告"""
    from core.tool_registry import execute_tool
    from core.tool_result import ToolResult
    calls: list[bool] = []

    def handler(table="", force=False):
        calls.append(force)
        if not force:
            return ToolResult.fail(
                "将 FLOAT 改为 TEXT 可能导致非数字文本丢失，确认请使用 force=True",
                code="VALIDATION", reason="need_force",
                report={"risk": {"level": "high", "message": "FLOAT→TEXT"}})
        return ToolResult.ok("已修改 t.c: FLOAT -> TEXT")

    _register_tmp_tool("tmp_force_ok", handler, ["table", "force"])
    captured: dict = {}

    def _approve(payload):
        captured.update(payload)
        return {"decisions": [{"type": "approve"}]}

    try:
        with patch("langgraph.types.interrupt", _approve):
            r = execute_tool("tmp_force_ok", table="t")
        assert r.data.get("ok") is True, r.data
        assert calls == [False, True], calls  # 重试带 force=True
        ar = captured["action_requests"][0]
        assert "风险确认" in ar["name"]
        assert "FLOAT" in ar["args"]["风险详情"]
        assert ar["args"]["风险报告"]["__fold__"]  # 报告走折叠协议
        assert "force=True" in ar["description"]
    finally:
        _unregister("tmp_force_ok")
    print("OK - force卡批准：带 force=True 重试成功，卡片含风险详情+折叠报告")


def test_force_card_reject_no_retry():
    """need_force + 拒绝：原样返回失败，handler 未被重试"""
    from core.tool_registry import execute_tool
    from core.tool_result import ToolResult
    calls: list[bool] = []

    def handler(force=False):
        calls.append(force)
        return ToolResult.fail("精度收紧可能截断", code="VALIDATION",
                               reason="need_force")

    _register_tmp_tool("tmp_force_no", handler, ["force"])
    try:
        with patch("langgraph.types.interrupt",
                   lambda _p: {"decisions": [{"type": "reject"}]}):
            r = execute_tool("tmp_force_no", force=False)
        assert r.data.get("ok") is False
        assert r.data.get("reason") == "need_force", r.data
        assert calls == [False], calls  # 未重试
    finally:
        _unregister("tmp_force_no")
    print("OK - force卡拒绝：不重试，原样返回 need_force")


def test_hard_block_no_card():
    """硬阻断（forceable=False）：handler 有 force 参也不弹卡，原样返回指引"""
    from core.tool_registry import execute_tool
    from core.exceptions import RiskError
    interrupt_calls: list = []

    def handler(force=False):
        raise RiskError("字段被外键引用，无法删除", report={"referenced_by": []},
                        forceable=False)

    _register_tmp_tool("tmp_hard", handler, ["force"])
    try:
        with patch("langgraph.types.interrupt",
                   side_effect=lambda p: interrupt_calls.append(p)
                   or {"decisions": [{"type": "approve"}]}):
            r = execute_tool("tmp_hard")
        assert interrupt_calls == [], "硬阻断不得弹卡"
        assert r.data.get("ok") is False
        assert r.data.get("reason") == "need_force"
        assert r.data.get("forceable") is False, r.data
    finally:
        _unregister("tmp_hard")
    print("OK - 硬阻断：forceable=False 不弹卡，指引原样返回")


def test_non_graph_context_safe_deny():
    """非 graph 环境（interrupt 无上下文抛异常）：安全拒绝，handler 未被重试"""
    from core.tool_registry import execute_tool
    from core.tool_result import ToolResult
    calls: list[bool] = []

    def handler(force=False):
        calls.append(force)
        return ToolResult.fail("危险变更需确认", code="VALIDATION",
                               reason="need_force")

    _register_tmp_tool("tmp_force_env", handler, ["force"])
    try:
        with patch("langgraph.types.interrupt",
                   side_effect=RuntimeError("no graph context")):
            r = execute_tool("tmp_force_env")
        assert r.data.get("ok") is False
        assert r.data.get("reason") == "need_force"
        assert calls == [False], calls  # 未重试——宁可不执行
    finally:
        _unregister("tmp_force_env")
    print("OK - 非graph环境：安全拒绝，不重试，need_force 原文返回")


def test_msg_result_maps_confirm_to_need_force():
    """confirm:True 历史标记（drop_column/drop_foreign_key）同口径映射 need_force"""
    from agent.tools import _msg_result
    tr = _msg_result({"ok": False, "confirm": True,
                      "message": "字段是外键字段，确认删除请 force=true"})
    assert tr.data.get("code") == "VALIDATION"
    assert tr.data.get("reason") == "need_force", tr.data
    # need_force 标记本身不受影响
    tr2 = _msg_result({"ok": False, "need_force": True, "message": "m"})
    assert tr2.data.get("reason") == "need_force"
    # 普通失败不误标
    tr3 = _msg_result({"ok": False, "message": "m"})
    assert tr3.data.get("reason") is None
    print("OK - _msg_result：confirm/need_force 同口径，普通失败不误标")


def _sm_patches(cfg, drv):
    """schema_manager 单元测试公共打桩：绕过一致性守卫/配置/落盘，注入 mock driver"""
    return (patch("core.schema_manager._preflight_check", return_value=""),
            patch("core.schema_manager._load_config", return_value=cfg),
            patch("core.schema_manager.get_driver", return_value=drv),
            patch("core.schema_manager._save_config"),
            patch("core.schema_manager._save_with_rollback", lambda *a, **k: None))


def test_schema_manager_force_chain():
    """force 链修复：三函数自报 need_force + force=True 传到底层 driver"""
    import copy
    import core.schema_manager as sm

    # ── alter_precision：收紧 12,2 → 5,1 ──
    cfg = {"tables": [{"name": "t", "columns": [
        {"name": "id", "type": "INTEGER", "pk": True},
        {"name": "c", "type": "FLOAT", "precision": [12, 2]}]}]}
    drv = MagicMock()
    ps = _sm_patches(cfg, drv)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        r1 = sm.alter_precision("t", "c", "5,1")
        assert r1.get("need_force") is True, r1  # 自报（修复前函数无 force 参）
        drv.alter_precision.assert_not_called()
        r2 = sm.alter_precision("t", "c", "5,1", force=True)
        assert r2.get("ok") is True, r2
        drv.alter_precision.assert_called_once_with("t", "c", (5, 1), force=True)

    # ── modify_column：FLOAT → TEXT 危险转换 ──
    cfg2 = {"tables": [{"name": "t", "columns": [
        {"name": "id", "type": "INTEGER", "pk": True},
        {"name": "c", "type": "FLOAT"}]}]}
    drv2 = MagicMock()
    drv2.get_referencing_tables.return_value = []
    ps = _sm_patches(cfg2, drv2)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        r3 = sm.modify_column("t", "c", "TEXT")
        assert r3.get("need_force") is True, r3  # 补标记修复验证（修复前无此键）
        drv2.modify_column.assert_not_called()
        r4 = sm.modify_column("t", "c", "TEXT", force=True)
        assert r4.get("ok") is True, r4
        drv2.modify_column.assert_called_once_with("t", "c", "TEXT", force=True)

    # ── add_foreign_key：TEXT 列指向 INTEGER 主键（类型族不一致）──
    cfg3 = {"tables": [
        {"name": "t", "columns": [
            {"name": "id", "type": "INTEGER", "pk": True},
            {"name": "c", "type": "TEXT"}], "foreign_keys": []},
        {"name": "r", "columns": [{"name": "id", "type": "INTEGER"}]}]}
    drv3 = MagicMock()
    ref_drv = MagicMock()
    ref_drv.query.return_value = [{"name": "id", "type": "INTEGER"}]
    dsm = MagicMock()
    dsm.get_driver_for_table.return_value = ref_drv
    ps = _sm_patches(cfg3, drv3)
    with ps[0], ps[1], ps[2], ps[3], ps[4], \
         patch("core.datasource_manager.DataSourceManager", return_value=dsm):
        r5 = sm.add_foreign_key("t", "c", "r")
        assert r5.get("need_force") is True, r5  # 类型不一致可确认放行（原硬阻断文案改自报）
        drv3.add_foreign_key.assert_not_called()
        r6 = sm.add_foreign_key("t", "c", "r", force=True)
        assert r6.get("ok") is True, r6
        drv3.add_foreign_key.assert_called_once_with("t", "c", "r", force=True)
    print("OK - force链修复：三函数自报 need_force，force=True 传到底层 driver")


if __name__ == "__main__":
    test_force_card_approve_retries_with_force()
    test_force_card_reject_no_retry()
    test_hard_block_no_card()
    test_non_graph_context_safe_deny()
    test_msg_result_maps_confirm_to_need_force()
    test_schema_manager_force_chain()
    print("\n层 30 全绿")
