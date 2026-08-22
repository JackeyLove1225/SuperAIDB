"""层 28：MCP 能力面（mcp_server.py + 高危人审闸 MCP 通道桥接，20260807）

形态①（原生 Reasonix 作为上层 AI）的协议地基：
- 注册表全量工具经 MCP stdio 暴露（execute_tool 直达，P1→树→P2 + 护栏全在位）
- 高危人审闸双通道：graph=interrupt 人审卡（原样）；mcp=挂起表回执
  （PendingApproval → confirm_action 结算，复用批量预批准通道放行）
- 安全语义：fail-closed（未确认不执行）、token 一次性、10 分钟有期

覆盖：
1. confirm_action 注册与元数据（admin 级）
2. MCP 通道闸：drop_table → 待批准 token，表未删
3. approve=false → 拒绝，表仍在
4. approve=true → 经批量预批准通道放行，穿透闸到达 handler 业务层
5. token 一次性（重放被拒）
6. graph 通道裸调仍安全拒绝（fail-closed 不变）
7. MCP server 工具面/schema 转换正确
8. 端到端：真实 stdio 握手 → list_tools → call_tool(list_databases)
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("INDUSTRY", "construction_engineering")

import agent.tools  # noqa: F401 触发全量注册
from core.tool_registry import _tools, execute_tool, NUKE_TOOLS
from core.context import get_context


def _scratch_table(name: str):
    execute_tool("batch_create_tables", definitions=(
        f'[{{"name":"{name}","columns":[{{"name":"c1","type":"TEXT"}}]}}]'))
    from core.data_ops import _get_driver
    assert name in _get_driver().list_tables(), f"建 scratch 表失败: {name}"


def _drop_direct(name: str):
    """scratch 清理：db 表 + 自动落盘的 schema yaml（batch_create_tables 副作用，
    不清会污染行业配置目录——层 15/16/20 真事故 20260807）"""
    from core.data_ops import _get_driver
    _get_driver().execute(f"DROP TABLE IF EXISTS {name}")
    # 动态取当前行业目录（20260809：行业改为单行业 construction_engineering，
    # 不再硬编码 engineering；读 settings.INDUSTRY 定位 schemas 路径）
    from config.settings import settings
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ind = getattr(settings, "INDUSTRY", "construction_engineering") or "construction_engineering"
    yaml_path = os.path.join(root, "industries", ind, "schemas", f"{name}.yaml")
    if os.path.exists(yaml_path):
        os.remove(yaml_path)


def test_registry_and_metadata():
    """1. confirm_action 注册 + admin 元数据 + NUKE_TOOLS 完整"""
    t = _tools.get("confirm_action")
    assert t is not None, "confirm_action 未注册"
    assert t.risk_level == "admin", f"元数据风险级错: {t.risk_level}"
    assert {"drop_table", "edit_data", "delete_data", "clear_db"} <= NUKE_TOOLS
    print(f"OK 1 - 注册表 {len(_tools)} 工具，confirm_action 在位（admin）")


def test_mcp_channel_approval_flow():
    """2-5. MCP 通道人审全链路（20260822 安全语义）：待批准（token 不回传 AI）→
    AI 通道结算被拒 → 管理端批准放行 → token 一次性"""
    from core.data_ops import _get_driver
    from core.pending_ops import list_pending, pop_pending
    ctx = get_context()
    ctx.set_channel("mcp")
    try:
        # 挂起表是跨进程全局文件：清场，避免历史残留干扰计数断言
        from core.pending_ops import _STORE as _PSTORE
        if _PSTORE.exists():
            _PSTORE.unlink()
        _scratch_table("t_mcp_t32")
        # 2. 闸拦截 → 待批准，且 token 不出现在 AI 可见文本
        r = execute_tool("drop_table", table="t_mcp_t32")
        s = str(r)
        assert "待批准" in s, f"应返回待批准: {s[:120]}"
        assert "token=P-" not in s, f"token 不得回传 AI 通道（防自助结算）: {s[:120]}"
        assert "t_mcp_t32" in _get_driver().list_tables(), "待批准期间表必须仍在"
        # 管理台审批中心可见（含 token 与影响面）——按本测试的表名过滤（挂起表是全局文件，
        # 历史残留不清空也不影响判定）
        pend = [p for p in list_pending() if "t_mcp_t32" in p.get("impact", "")]
        assert len(pend) == 1 and pend[0]["name"] == "drop_table", pend
        token = pend[0]["token"]
        # 3. AI 通道 confirm_action 一律不再结算（防自助人审）
        r2 = execute_tool("confirm_action", token=token, approve=True)
        assert "不再结算" in str(r2) or "管理台" in str(r2), f"AI 通道结算应被拒: {str(r2)[:100]}"
        assert "t_mcp_t32" in _get_driver().list_tables(), "AI 自助结算后表必须仍在"
        assert len([p for p in list_pending() if "t_mcp_t32" in p.get("impact", "")]) == 1, \
            "AI 通道尝试不得消费 token"
        # 4. 管理端批准（审批中心同逻辑：pop + 批量预批准通道放行）
        op = pop_pending(token)
        assert op and op["kwargs"].get("table") == "t_mcp_t32", f"登记时表名应已解析进 kwargs: {op}"
        ctx.set_nuke_batch(tables={op["kwargs"]["table"]}, ops={op["name"]})
        try:
            r4 = execute_tool(op["name"], **op["kwargs"])
        finally:
            ctx.clear_nuke_batch()
        s4 = str(r4)
        assert "待批准" not in s4 and "需用户在前端确认" not in s4, f"批准后不应再是闸文案: {s4[:150]}"
        # 穿透到 handler：scratch 表由 batch_create_tables 创建（有 YAML 配置），正常删除成功
        assert "已删除表" in s4 or "配置与数据库不一致" in s4, f"应穿透到 handler 业务契约层: {s4[:150]}"
        # 5. token 一次性
        assert pop_pending(token) is None, "重放应失败"
        print("OK 2-5 - MCP 通道：token 不回传 AI→AI 结算被拒→管理端批准放行→token 一次性")
    finally:
        _drop_direct("t_mcp_t32")
        ctx.set_channel("graph")


def test_graph_channel_unchanged():
    """6. graph 通道裸调仍 fail-closed（无 runtime 安全拒绝）"""
    from core.data_ops import _get_driver
    assert get_context().get_channel() == "graph"
    _scratch_table("t_mcp_t32b")
    try:
        r = execute_tool("drop_table", table="t_mcp_t32b")
        assert "操作未执行" in str(r), f"graph 裸调应安全拒绝: {str(r)[:120]}"
        assert "t_mcp_t32b" in _get_driver().list_tables(), "拒绝后表必须仍在"
        print("OK 6 - graph 通道裸调仍安全拒绝（fail-closed 语义不变）")
    finally:
        _drop_direct("t_mcp_t32b")


def test_server_tool_face():
    """7. mcp_server 可导入；工具面排除占位工具；schema 转换正确"""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "mcp_server.py")
    spec = importlib.util.spec_from_file_location("mcp_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = [n for n in _tools if n not in mod._EXCLUDE]
    assert "unsupported_op" in mod._EXCLUDE
    assert {"query", "mutate_data", "drop_table", "confirm_action"} <= set(names)
    s1 = mod._input_schema(_tools["confirm_action"])
    # 20260822 起 confirm_action 不再结算（防 AI 自助人审），token 参数已废弃为可选
    assert s1["properties"]["token"]["type"] == "string" \
        and "token" not in s1.get("required", [])
    s2 = mod._input_schema(_tools["drop_table"])
    assert s2["properties"]["table"]["type"] == "string" \
        and s2["properties"]["all"]["type"] == "boolean" \
        and s2["properties"]["all"].get("default") is False
    assert mod._tool_brief(_tools["drop_table"]).startswith("[结构变更]")
    print(f"OK 7 - MCP 工具面 {len(names)} 个，schema/风险标注转换正确")


def test_e2e_stdio_handshake():
    """8. 端到端：真实 stdio 握手 → list_tools → call_tool(list_databases)"""
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run():
        env = dict(os.environ)
        env.setdefault("INDUSTRY", "construction_engineering")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.join(root, "mcp_server.py")], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                assert {"query", "drop_table", "confirm_action"} <= set(names)
                assert "unsupported_op" not in names
                dt = next(t for t in tools.tools if t.name == "drop_table")
                assert dt.inputSchema["properties"]["table"]["type"] == "string"
                assert dt.description.startswith("[结构变更]")
                r = await session.call_tool("list_databases", {})
                text = r.content[0].text
                assert "数据库" in text, text[:200]
                return len(names)

    n = asyncio.run(_run())
    print(f"OK 8 - 端到端 stdio 握手成功，{n} 工具经协议可见，call_tool 真实返回")


def test_pending_pop_concurrency():
    """挂起表结算互斥锁（评审五轮 A10 回归锁）：两线程并发结算同一 token，
    恰一个拿到（另一个 None）——双执行面闭合"""
    import threading
    from core.pending_ops import register_pending, pop_pending, _STORE
    token = register_pending("probe_tool", {"x": 1}, "测试影响面")
    got = []
    barrier = threading.Barrier(2)

    def _pop():
        barrier.wait()  # 同步起跑，最大化竞争
        got.append(pop_pending(token))

    ts = [threading.Thread(target=_pop) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(1 for g in got if g is not None) == 1, \
        f"同一 token 必须恰被结算一次: {got}"
    assert pop_pending(token) is None, "已结算 token 不得复活"
    try:
        _STORE.with_suffix(".lock").unlink(missing_ok=True)
    except OSError:
        pass
    print("OK - 挂起表结算互斥：并发 pop 恰一次成功，无复活")


if __name__ == "__main__":
    test_registry_and_metadata()
    test_mcp_channel_approval_flow()
    test_graph_channel_unchanged()
    test_server_tool_face()
    test_pending_pop_concurrency()
    test_e2e_stdio_handshake()
    print("\n✅ 层 28 全部通过：MCP 能力面 + 高危人审闸双通道桥接")
