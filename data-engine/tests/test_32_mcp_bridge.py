"""层 28：MCP 能力面（mcp_server.py + 高危人审闸 MCP 通道桥接）

形态①（原生 Reasonix 作为上层 AI）的协议地基：
- MCP stdio 面仅白名单 5 工具（execute_structured 结构化契约元工具 + 人审
  机制三件套 + 只读辅助）；写/DDL/查询等 33 个具体工具经 execute_structured
  的树路由到达，按名直调在面边界封死
- 高危人审闸：mcp=挂起表回执（PendingApproval → 管理端审批中心单点结算，
  token 不回传 AI 通道，防 AI 自助结算）；graph=interrupt 兜底 fail-closed
- 安全语义：fail-closed（未确认不执行）、token 一次性、10 分钟有期、
  落盘条目 HMAC 验签（篡改/伪造拒认）

覆盖：
1. confirm_action 注册与元数据（admin 级）
2. MCP 通道闸：drop_table → 待批准 token，表未删
3. approve=false → 拒绝，表仍在
4. approve=true → 经批量预批准通道放行，穿透闸到达 handler 业务层
5. token 一次性（重放被拒）
6. graph 通道裸调仍安全拒绝（fail-closed 不变）
7. MCP server 工具面/schema 转换正确
8. 端到端：真实 stdio 握手 → list_tools → call_tool(list_databases)
15. 结构化指令契约（execute_structured）：服务端 fail-closed ×4 + happy path ×4
16. 首连首启回归锁：全新库自举建管理员，协议通道（stdout）零裸写——
    任何 print 落管道即毒死会话（auth 自举 print 病型的确定性复现）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("INDUSTRY", "construction_engineering")

import agent.tools  # noqa: F401 触发全量注册
from unittest.mock import patch
from core.tool_registry import execute_tool, get_tools, find_tools
from core.context import get_context

_TOOLS = get_tools()  # 注册表快照（公开门面；本层只读枚举）
_NUKE = {t.name for t in find_tools(gate="nuke")}  # 元数据单源


def _scratch_table(name: str):
    # batch_create_tables 已升 nuke 闸——测试走批量预批准通道
    ctx = get_context()
    ctx.set_nuke_batch(tables={"*"}, ops={"batch_create_tables"})
    try:
        execute_tool("batch_create_tables", definitions=(
            f'[{{"name":"{name}","columns":[{{"name":"c1","type":"TEXT"}}]}}]'))
    finally:
        ctx.clear_nuke_batch()
    from core.data_ops import get_driver
    assert name in get_driver().list_tables(), f"建 scratch 表失败: {name}"


def _drop_direct(name: str):
    """scratch 清理：db 表 + 自动落盘的 schema yaml（batch_create_tables 副作用，
    不清会污染行业配置目录——层 15/16/20 回归锁 20260807）"""
    from core.data_ops import get_driver
    get_driver().execute(f"DROP TABLE IF EXISTS {name}")
    # 动态取当前行业目录（20260809：行业改为单行业 construction_engineering，
    # 不再硬编码 engineering；读 settings.INDUSTRY 定位 schemas 路径）
    from config.settings import settings
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ind = getattr(settings, "INDUSTRY", "construction_engineering") or "construction_engineering"
    yaml_path = os.path.join(root, "industries", ind, "schemas", f"{name}.yaml")
    if os.path.exists(yaml_path):
        os.remove(yaml_path)


def test_registry_and_metadata():
    """1. confirm_action 注册 + admin 元数据 + 人审闸集合（元数据单源）完整"""
    t = _TOOLS.get("confirm_action")
    assert t is not None, "confirm_action 未注册"
    assert t.risk_level == "admin", f"元数据风险级错: {t.risk_level}"
    assert {"drop_table", "edit_data", "delete_data", "clear_db"} <= _NUKE
    # 提权两工具必须已登记元数据（漏登记回归锁）
    for sudo in ("escalate_permission", "deescalate_permission"):
        st = _TOOLS.get(sudo)
        assert st is not None and st.risk_level == "admin", f"{sudo} 元数据缺失/漂移"
    print(f"OK 1 - 注册表 {len(_TOOLS)} 工具，confirm_action 在位（admin），人审闸集合 {_NUKE and len(_NUKE)} 个")


def test_mcp_channel_approval_flow():
    """2-5. MCP 通道人审全链路（20260822 安全语义）：待批准（token 不回传 AI）→
    AI 通道结算被拒 → 管理端批准放行 → token 一次性"""
    from core.data_ops import get_driver
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
        assert "t_mcp_t32" in get_driver().list_tables(), "待批准期间表必须仍在"
        # 管理台审批中心可见（含 token 与影响面）——按本测试的表名过滤（挂起表是全局文件，
        # 历史残留不清空也不影响判定）
        pend = [p for p in list_pending() if "t_mcp_t32" in p.get("impact", "")]
        assert len(pend) == 1 and pend[0]["name"] == "drop_table", pend
        token = pend[0]["token"]
        # 3. AI 通道 confirm_action 一律不再结算（防自助人审）
        r2 = execute_tool("confirm_action", token=token, approve=True)
        assert "不再结算" in str(r2) or "管理台" in str(r2), f"AI 通道结算应被拒: {str(r2)[:100]}"
        assert "t_mcp_t32" in get_driver().list_tables(), "AI 自助结算后表必须仍在"
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
    from core.data_ops import get_driver
    assert get_context().get_channel() == "graph"
    _scratch_table("t_mcp_t32b")
    try:
        r = execute_tool("drop_table", table="t_mcp_t32b")
        assert "操作未执行" in str(r), f"graph 裸调应安全拒绝: {str(r)[:120]}"
        assert "t_mcp_t32b" in get_driver().list_tables(), "拒绝后表必须仍在"
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
    names = [n for n in _TOOLS if n in mod._INCLUDE]
    # 硬路由白名单（20260905 结构化契约）：MCP 面 = 结构化契约元工具 +
    # 人审机制 + 只读辅助（5 个）；新工具默认不上面（白名单默认方向=
    # 安全边界），写/DDL/查询等 33 个具体工具只能经 execute_structured
    # 的树路由到达（execute_instruction 自然语言链保留仓内，不上 MCP 面）
    assert set(names) == {"execute_structured", "confirm_action",
                          "escalate_permission", "deescalate_permission",
                          "list_vector_collections"}, f"工具面漂移: {sorted(names)}"
    assert "unsupported_op" not in mod._INCLUDE
    assert "execute_instruction" not in mod._INCLUDE, \
        "自然语言通道不得留在 MCP 面（结构化契约唯一数据通道）"
    for hidden in ("drop_table", "insert_data", "query",
                   "batch_create_tables", "alter_precision"):
        assert hidden not in mod._INCLUDE, f"{hidden} 不应在 MCP 面（白名单）"
    # 反向锁：注册表新工具未显式声明暴露即不得上面
    from core.tool_registry import get_tools as _gt
    _hidden_all = set(_gt()) - mod._INCLUDE
    assert len(_hidden_all) == 34, f"隐藏工具数漂移（应 34=33 具体+自然语言链）: {len(_hidden_all)}"
    s1 = mod._input_schema(_TOOLS["confirm_action"])
    # 20260822 起 confirm_action 不再结算（防 AI 自助人审），token 参数已废弃为可选
    assert s1["properties"]["token"]["type"] == "string" \
        and "token" not in s1.get("required", [])
    s2 = mod._input_schema(_TOOLS["drop_table"])
    assert s2["properties"]["table"]["type"] == "string" \
        and s2["properties"]["all"]["type"] == "boolean" \
        and s2["properties"]["all"].get("default") is False
    # 结构化契约 schema：behavior/object 枚举暴露给上层 AI（真源=决策树），
    # args 声明为 object（客户端可直传 JSON 对象）
    s3 = mod._input_schema(_TOOLS["execute_structured"])
    assert "查" in s3["properties"]["behavior"].get("enum", []), s3["properties"]["behavior"]
    assert "记录" in s3["properties"]["object"].get("enum", []), s3["properties"]["object"]
    assert s3["properties"]["args"]["type"] == "object"
    assert "execute_structured" in _TOOLS and _TOOLS["execute_structured"].risk_level == "ddl"
    assert mod._tool_brief(_TOOLS["drop_table"]).startswith("[结构变更]")
    print(f"OK 7 - MCP 工具面 {len(names)} 个，schema/风险标注转换正确")


def test_structured_contract():
    """15. 结构化指令契约（execute_structured，20260905 MCP 面唯一数据通道）：
    服务端侧 fail-closed——非法枚举（报错附合法值清单）/ 非法 args JSON /
    枚举合法但树不可达 / 改删记录两步流程强制（无 selection_id 拒）；
    happy path——增+记录（dict 参数按工具签名适配）、查+记录（纯结构化条件，
    不带自然语言 query）、查+统计、查+表，全部零仓内 LLM 且带路由轨迹"""
    ctx = get_context()
    _scratch_table("t_mcp_t32d")
    try:
        # 1. 服务端枚举 fail-closed（客户端侧 schema enum 拒见用例 8）
        r = execute_tool("execute_structured", behavior="看看", object="表", args="{}")
        s = str(r)
        assert "不合法" in s and "查" in s, f"枚举报错应附合法值清单: {s[:150]}"
        # 2. args 非法 JSON → fail-closed
        r = execute_tool("execute_structured", behavior="查", object="记录",
                         args="{not-json")
        assert "args 不是合法 JSON" in str(r), str(r)[:150]
        # 3. 枚举合法但树不可达（增+库）→ 直接报不支持（无问法学习语义）
        r = execute_tool("execute_structured", behavior="增", object="库", args="{}")
        assert "暂不支持" in str(r), str(r)[:150]
        # 4. 改+记录两步流程强制：无 selection_id 拒（影响面显式化）
        r = execute_tool("execute_structured", behavior="改", object="记录",
                         args={"table": "t_mcp_t32d"})
        s = str(r)
        assert "selection_id" in s and "两步" in s, s[:200]
        # 5. 增+记录：args.data dict 按工具签名（str 型）适配序列化。
        #    insert_data 属 nuke 闸（不可逆写需人审）——测试走批量预批准通道
        #    （与管理端批准后结算同路径），未预批准时 fail-closed 见用例 6 语义
        ctx.set_nuke_batch(tables={"t_mcp_t32d"}, ops={"insert_data"})
        try:
            r = execute_tool("execute_structured", behavior="增", object="记录",
                             args={"table": "t_mcp_t32d", "data": {"c1": "x1"}})
        finally:
            ctx.clear_nuke_batch()
        assert r.data.get("ok"), str(r)[:150]
        assert "[路由:" in r.text, f"应带路由轨迹: {r.text[-120:]}"
        # 6. 查+记录：纯结构化条件（无自然语言 query 也能查——结构化全场景）
        r = execute_tool("execute_structured", behavior="查", object="记录",
                         args={"table": "t_mcp_t32d",
                               "conditions": [{"field": "c1", "op": "=", "value": "x1"}]})
        assert "x1" in r.text, r.text[:150]
        assert r.data.get("selection_id"), "查询应建选择集（改/删两步流程的地基）"
        # 7. 查+统计：agg 参数直达聚合工具（1 行 → COUNT=1）
        r = execute_tool("execute_structured", behavior="查", object="统计",
                         args={"table": "t_mcp_t32d", "agg_func": "COUNT"})
        assert r.data.get("ok"), r.text[:150]
        # 8. 查+表（带表名）：结构查询
        r = execute_tool("execute_structured", behavior="查", object="表",
                         args={"table": "t_mcp_t32d"})
        assert "c1" in r.text, r.text[:150]
        print("OK 15 - 结构化契约：fail-closed×4 + happy path×4（增查改删统计表全覆盖）")
    finally:
        _drop_direct("t_mcp_t32d")


def test_e2e_stdio_handshake():
    """8. 端到端：真实 stdio 握手 → list_tools → call_tool(list_databases)"""
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run():
        env = dict(os.environ)
        env.setdefault("INDUSTRY", "construction_engineering")
        # MCP 未绑定=拒起（拒绝服务模型）：测试子进程绑定 admin（live users 表必在）
        env["MCP_USER"] = "admin"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.join(root, "mcp_server.py")], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                assert {"execute_structured", "confirm_action"} <= set(names)
                for hidden in ("query", "drop_table", "unsupported_op"):
                    assert hidden not in names, f"{hidden} 不应出现在工具面"
                # 按名直调隐藏工具 → 面边界在调用点封死（硬路由不可绕过）
                blocked = await session.call_tool("drop_table", {"table": "x"})
                assert "不在 MCP 能力面" in blocked.content[0].text, blocked.content[0].text[:100]
                # 全量封死（抽验 1 个 → 全量 34 名逐一断言——
                # 未来若有人把调用点判断拆成独立且更窄的集合，静默失效必红）
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location(
                    "mcp_server_mod", os.path.join(root, "mcp_server.py"))
                _m = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_m)
                from core.tool_registry import get_tools as _gt2
                for _h in sorted(set(_gt2()) - _m._INCLUDE):
                    _b = await session.call_tool(_h, {})
                    assert "不在 MCP 能力面" in _b.content[0].text, \
                        f"隐藏工具 {_h} 未被调用点封死"
                # 走结构化契约：零 LLM 直达（查+表 → describe_schema），
                # 契约字段即路由输入，token 不回传 AI 文本通道
                r = await session.call_tool(
                    "execute_structured",
                    {"behavior": "查", "object": "表", "args": {}})
                text = r.content[0].text
                assert "quota_items" in text or "表" in text, text[:200]
                assert "[路由:" in text, f"应带路由轨迹: {text[-200:]}"
                # 非法枚举 fail-closed 双层：MCP schema enum 客户端侧即拒
                #（合法值清单在报错里，上游 AI 可自纠；服务端侧校验另见
                # test_structured_contract 直调用例）
                bad = await session.call_tool(
                    "execute_structured",
                    {"behavior": "看看", "object": "表", "args": {}})
                assert "is not one of" in bad.content[0].text \
                    and "查" in bad.content[0].text, bad.content[0].text[:150]
                return len(names)

    n = asyncio.run(_run())
    print(f"OK 8 - 端到端 stdio 握手成功，{n} 工具经协议可见，call_tool 真实返回")


def test_e2e_first_run_poison_regression():
    """16. 首连首启回归锁：全新库首启自举建管理员，通知必须走 stderr
    （logger，SUPERAIDB_ROLE=mcp）——stdout 是 JSON-RPC 协议通道，裸 print
    落管道即毒死会话（客户端 reader 解析非 JSON 行即崩）。修前病型：auth
    自举 print 在 _main 启动期落管道，首连必挂；本用例确定性复现该路径
    （全新库 + 首次握手），修后全链路走 stderr，管道零裸写。"""
    import asyncio
    import tempfile
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    tmp = tempfile.mkdtemp(prefix="t32_firstrun_")
    fresh_db = os.path.join(tmp, "fresh.db")

    async def _run():
        env = dict(os.environ)
        env.setdefault("INDUSTRY", "construction_engineering")
        # MCP 未绑定=拒起；首启自举先于身份解析（_main 顺序），admin 必在
        env["MCP_USER"] = "admin"
        env["SQLITE_DB_PATH"] = fresh_db  # 全新库：_main 自举建管理员
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.join(root, "mcp_server.py")], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                # 修前：自举 print 落管道 → 首行非 JSON → 此处即挂（无竞速）
                await session.initialize()
                tools = await session.list_tools()
                r = await session.call_tool(
                    "execute_structured",
                    {"behavior": "查", "object": "库", "args": {}})
                assert r.content and r.content[0].text.strip(), \
                    "查+库 应真实返回库清单"
                return len(tools.tools)

    n = asyncio.run(_run())
    # 首启路径真实走到（防空转）：全新库内 admin 已自举建立
    from core.crypto.connection import open_db
    conn = open_db(fresh_db)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM users WHERE username='admin'"
        ).fetchone()[0]
        assert cnt == 1, "首启自举应建 admin（回归对象未真实执行）"
    finally:
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            p = fresh_db + suffix
            if os.path.exists(p):
                os.remove(p)
    print(f"OK 16 - 首连首启回归：协议通道零裸写，admin 自举真实执行，{n} 工具可见")


def test_pending_pop_concurrency():
    """挂起表结算互斥锁（回归锁）：两线程并发结算同一 token，
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


def test_pending_expired_cannot_settle():
    """过期 token 不得结算（回归锁：旧顺序先 pop 后 sweep，
    '防迟来确认'形同虚设）——把 ts 改到 TTL 之外，pop_pending 必须返回 None"""
    import json as _json
    from core.pending_ops import (register_pending, pop_pending, _STORE,
                                  _TTL_SECONDS, _sign_op)
    token = register_pending("probe_tool", {"x": 1}, "测试影响面")
    # 把时间戳改到 TTL 之外（模拟"批准来迟"）——改完重签，让拒绝理由落在
    # TTL 而非验签（挂起表条目落盘即签，篡改 ts 不补签会先被撞签拒认）
    from core.file_contract import FileLock
    with FileLock(_STORE.with_suffix(".lock")):
        d = _json.loads(_STORE.read_text(encoding="utf-8"))
        d[token]["ts"] = d[token]["ts"] - _TTL_SECONDS - 10
        d[token]["sig"] = _sign_op(d[token])
        _STORE.write_text(_json.dumps(d), encoding="utf-8")
    # 强制推进 mtime（确定性失效 _load 的 mtime 缓存——同 tick 写入在
    # 高负载机器上会让 mtime 相等吃到陈旧缓存，20260823）
    import os as _os
    t = time.time() + 2
    _os.utime(_STORE, (t, t))
    try:
        assert pop_pending(token) is None, "过期 token 不得结算出操作"
    finally:
        pop_pending(token)  # 兜底清场（若 bug 回退被结算出来了，至少清掉）
    print("OK - 挂起表 TTL：过期 token 不可结算（防迟来确认真实生效）")


def test_pending_tamper_rejected():
    """挂起表落盘完整性锁：登记即 HMAC 签名，落盘后篡改 kwargs/ts、
    或整条伪造 → 结算端验签一律拒认（pop 返回 None），批准对象≠执行对象
    的注入面在落盘层封死（与提权契约同签名通道）。"""
    import json as _json
    import os as _os
    from core.pending_ops import register_pending, pop_pending, _STORE
    from core.file_contract import FileLock

    def _bump_mtime():
        # 强制推进 mtime（确定性失效 _load 的 mtime 缓存——同 tick 篡改在高
        # 负载/CI 机器上 mtime 相等会吃到陈旧缓存，生产篡改天然带新 mtime）
        t = time.time() + 2
        _os.utime(_STORE, (t, t))

    # 1) 篡改 kwargs（提权 ttl 改大）不补签 → 拒认
    t1 = register_pending("__escalate__", {"role": "admin", "ttl": 60}, "提权请求")
    with FileLock(_STORE.with_suffix(".lock")):
        d = _json.loads(_STORE.read_text(encoding="utf-8"))
        d[t1]["kwargs"]["ttl"] = 999999
        _STORE.write_text(_json.dumps(d), encoding="utf-8")
    _bump_mtime()
    assert pop_pending(t1) is None, "篡改 kwargs 的挂起必须验签拒认"
    # 2) 整条伪造（无签）→ 拒认
    t2 = "P-forged0001"
    with FileLock(_STORE.with_suffix(".lock")):
        d = _json.loads(_STORE.read_text(encoding="utf-8"))
        d[t2] = {"name": "drop_table", "kwargs": {"table": "users"},
                 "impact": "删除 users（无害描述诱批）", "ts": time.time()}
        _STORE.write_text(_json.dumps(d), encoding="utf-8")
    _bump_mtime()
    assert pop_pending(t2) is None, "伪造挂起（无签）必须拒认"
    # 3) 正常登记 → 验签通过照常结算
    t3 = register_pending("probe_tool", {"x": 1}, "正常挂起")
    op = pop_pending(t3)
    assert op and op["name"] == "probe_tool", "正常挂起不得被验签误伤"
    print("OK - 挂起表验签：篡改/伪造拒认，正常登记照常结算")


def test_register_dedup_atomic():
    """register_pending_dedup 原子锁：并发同风险登记恰一条（find+register 同
    FileLock 临界区，TOCTOU 窗口为零）；is_dup 复用既有 token；不同 kwargs 各立一条"""
    import threading as _th
    from core.pending_ops import register_pending_dedup, pending_count, pop_pending
    results = []
    def _worker():
        results.append(register_pending_dedup("probe_tool", {"x": 1}, "并发探针"))
    threads = [_th.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    tokens = {tok for tok, _ in results}
    assert len(tokens) == 1, f"并发同风险应恰登记一条: {len(tokens)}"
    assert sum(1 for _, d in results if d) == 7, f"其余 7 个应标 is_dup: {results}"
    # 不同 kwargs 各立一条
    t2, d2 = register_pending_dedup("probe_tool", {"x": 2}, "不同参")
    assert not d2 and t2 != tokens.pop()
    before = pending_count()
    assert before >= 2, f"两条独立挂起应在册: {before}"
    for tok, _ in results + [(t2, d2)]:
        pop_pending(tok)
    print("OK - dedup 原子锁：并发恰一条/is_dup 复用/不同 kwargs 各立一条")


def test_stdio_survives_logging():
    """9. 真实 stdio 下调用必产日志的工具，MCP 会话不被日志毒死（20260824 回归锁）

    高危工具命中 MCP 通道人审闸时进程会写 INFO 日志——若日志落 stdout
    （=JSON-RPC 协议通道），官方 SDK 解析失败会把异常注入读流、整个会话死亡
    （daemon 自动拉起的两行 INFO 即可杀死会话）。
    修复：MCP 进程日志整体切 stderr。本用例走面边界拦截（必写审计 INFO 日志
    且零 LLM、CI 离线确定性可达）后再做一次普通调用——会话必须完好。"""
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run():
        env = dict(os.environ)
        env.setdefault("INDUSTRY", "construction_engineering")
        # MCP 未绑定=拒起（拒绝服务模型）：测试子进程绑定 admin（live users 表必在）
        env["MCP_USER"] = "admin"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.join(root, "mcp_server.py")], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # 面边界拦截路径（必写审计 INFO 日志，且全程零 LLM——CI 离线
                # 环境确定性可达；若日志落 stdout 即毒死会话）
                r1 = await session.call_tool("drop_table", {"table": "x"})
                t1 = r1.content[0].text
                assert "不在 MCP 能力面" in t1, \
                    f"面边界拦截回执应可达（会话若被日志毒死此处已异常）: {t1[:120]}"
                # 会话仍完好：再做一次普通调用（日志毒死会话时此调用必抛异常）
                r2 = await session.call_tool(
                    "execute_structured",
                    {"behavior": "查", "object": "表", "args": {}})
                assert "表" in r2.content[0].text, "日志后会话必须仍可用"
                # 清理挂起（防测试间串台）
                from core.pending_ops import list_pending, pop_pending
                for p in list_pending():
                    if p["name"] == "drop_table":
                        pop_pending(p["token"])
                return True

    assert asyncio.run(_run())
    print("OK 9 - 真实 stdio 下产日志工具调用后会话完好（日志全走 stderr）")


def test_frozen_ids_cross_owner_settlement():
    """10. 单表改/删审批跨主体结算（N9 回归锁）：登记进程存选择集（owner=A），
    结算进程（owner=B）凭冻结 ids 直执——不再 selection_owner_mismatch 打断；
    冻结 id 集使批准对象=执行对象精确同一（行级 TOCTOU 无面）。"""
    import json as _json
    from core.data_ops import get_driver, insert_rows
    from core.pending_ops import list_pending, pop_pending
    ctx = get_context()
    _scratch_table("t_mcp_n9")  # 自动带 id 主键
    insert_rows("t_mcp_n9", [{"c1": "r1"}, {"c1": "r2"}])
    get_driver().commit()
    ctx.set_channel("mcp")
    try:
        # A 主体（MCP 进程）：查成选择集 → 发起删除 → 挂起（登记冻结 ids）
        sid = ctx.save_selection("t_mcp_n9", [{"id": 1, "c1": "r1"}, {"id": 2, "c1": "r2"}],
                                 query="probe")
        r = execute_tool("delete_data", selection_id=sid, table="t_mcp_n9")
        assert "待批准" in str(r), str(r)[:120]
        pend = [p for p in list_pending() if "t_mcp_n9" in p.get("impact", "")]
        assert len(pend) == 1
        op = pop_pending(pend[0]["token"])
        frozen = op["kwargs"].get("frozen_ids")
        assert frozen and set(_json.loads(frozen)) == {1, 2}, \
            f"登记应冻结选择集 id 集: {op['kwargs']}"
        # B 主体（管理端进程）：owner 不同——冻结载荷直执不受主体隔离影响
        with patch("core.context.ContextManager._current_owner",
                   return_value="graph:99999:system"):
            ctx.set_nuke_batch(tables={"t_mcp_n9"}, ops={"delete_data"})
            try:
                r2 = execute_tool("delete_data", table="t_mcp_n9", frozen_ids=frozen)
            finally:
                ctx.clear_nuke_batch()
        assert r2.data.get("ok"), f"冻结结算应直执: {str(r2)[:120]}"
        left = get_driver().query("SELECT COUNT(*) c FROM t_mcp_n9")[0]["c"]
        assert left == 0, f"冻结 id 集应全删: 剩 {left}"
        # dict 形态 set_data 的冻结结算（归一两路径同命锁）：登记/结算走
        # 与选择集分支同一归一，不再"直执成功、批准后 TypeError"
        insert_rows("t_mcp_n9", [{"c1": "e1"}])
        get_driver().commit()
        ctx.set_nuke_batch(tables={"t_mcp_n9"}, ops={"edit_data"})
        try:
            r3 = execute_tool("edit_data", table="t_mcp_n9", frozen_ids="[3]",
                              set_data={"c1": "e2"})
        finally:
            ctx.clear_nuke_batch()
        assert r3.data.get("ok"), f"dict set_data 冻结结算应直执: {str(r3)[:160]}"
        # 冻结路径 effects 挂账与选择集路径同构（结算回执/对账同宽）
        eff = r3.data.get("effects") or {}
        assert eff.get("affected_ids") == [3] and eff.get("changed_fields") == ["c1"], \
            f"冻结结算 effects 挂账缺失: {eff}"
        assert eff.get("expected_values") == {"c1": "e2"}, f"期望值挂账缺失: {eff}"
        row = get_driver().query("SELECT c1 FROM t_mcp_n9 WHERE id = 3")[0]
        assert row["c1"] == "e2", f"dict set_data 应已归一执行: {row}"
    finally:
        _drop_direct("t_mcp_n9")
        ctx.set_channel("graph")
    print("OK 10 - 冻结 ids 跨主体结算直执（N9 回归锁），批准对象=执行对象同一；dict set_data 归一同命")


def test_frozen_ids_injection_stripped():
    """11. 内部参数注入面锁：AI 侧自供 frozen_ids（无批量预批准上下文）一律剥离——
    '批准对象=执行对象同一'不变量不得被注入载荷架空；且 FC schema 不含 internal 参数
    （AI 不可见即不可注入）；MCP_ROLE 解析 fail-closed（空/非法→readonly）。"""
    from core.data_ops import get_driver, insert_rows
    ctx = get_context()
    _scratch_table("t_mcp_n11")
    insert_rows("t_mcp_n11", [{"c1": "keep1"}, {"c1": "keep2"}])
    get_driver().commit()
    try:
        # 无前序选择集污染（层内前序用例留过选择集，last_selection 回退会让
        # 人审闸自注入冻结 ids——那是设计内行为；本用例测的是 AI 直传注入）
        ctx.clear_all()
        # 无 nuke_batch、无选择集：AI 直传 frozen_ids 企图绕开选择集直执。
        # edit/delete 是 nuke 闸——走 MCP 通道看挂起登记载荷：注入的 frozen_ids
        # 必须在入口被剥离，不得混入挂起表（否则管理端结算重放时载荷存活）
        from core.pending_ops import list_pending, pop_pending
        ctx.set_channel("mcp")
        ctx.clear_nuke_batch()
        before = {p["token"] for p in list_pending()}
        try:
            r = execute_tool("delete_data", table="t_mcp_n11", frozen_ids="[1,2]")
        finally:
            ctx.set_channel("graph")
        assert "待批准" in str(r), str(r)[:120]
        new_pend = [p for p in list_pending() if p["token"] not in before]
        assert len(new_pend) == 1, f"nuke 闸应恰好登记一条挂起: {len(new_pend)}"
        op = pop_pending(new_pend[0]["token"])
        assert not op["kwargs"].get("frozen_ids"), \
            f"AI 注入的 frozen_ids 必须被入口剥离: {op['kwargs']}"
        left = get_driver().query("SELECT COUNT(*) c FROM t_mcp_n11")[0]["c"]
        assert left == 2, f"注入载荷不得改动任何行: 剩 {left}"
        # 畸形冻结载荷在合法上下文里也只得到 VALIDATION，不裸抛
        ctx.set_nuke_batch(tables={"t_mcp_n11"}, ops={"delete_data"})
        try:
            r2 = execute_tool("delete_data", table="t_mcp_n11",
                              frozen_ids="{not-json")
            assert r2.data.get("code") == "VALIDATION", str(r2)[:120]
        finally:
            ctx.clear_nuke_batch()
        # FC schema 剔除 internal 参数
        from agent.ai_extract import _build_tool_fc
        from core.tool_registry import get_tools as _gt
        spec = _build_tool_fc(_gt()["delete_data"], ["t_mcp_n11"], ["id", "c1"])
        props = spec[0]["function"]["parameters"]["properties"]
        assert "frozen_ids" not in props, f"internal 参数不得进 FC schema: {list(props)}"
        # MCP_USER 身份解析：未绑定/绑错一律未绑定（拒绝服务模型——只读都不给）
        import mcp_server as _ms
        assert _ms._resolve_mcp_identity(None) == ("", "")
        assert _ms._resolve_mcp_identity("") == ("", "")
        assert _ms._resolve_mcp_identity("ghost_no_such_user") == ("", "")
        # 绑定真实用户：角色从 users 表取（admin 是默认管理员，必在）
        u, r = _ms._resolve_mcp_identity("admin")
        assert u == "admin" and r == "admin", (u, r)
    finally:
        _drop_direct("t_mcp_n11")
    print("OK 11 - 内部参数注入剥离 + FC schema 剔除 + MCP_ROLE fail-closed")


def test_settlement_hub_roundtrip():
    """12. 结算回执中继站（settlement_hub，20260903 同步人审桥的回执通道）：
    record→wait 取回；peek 语义（读到不删，dedup 多等待者共用）；TTL 过期清扫"""
    import json as _json
    import os as _os
    from core.settlement_hub import (record_settlement, wait_settlement,
                                     _STORE, _TTL_SECONDS)
    from core.file_contract import FileLock
    if _STORE.exists():
        _STORE.unlink()
    try:
        # record → wait 取回
        record_settlement("P-hub-a", "approved", "执行结果文本")
        r = wait_settlement("P-hub-a", 0.5)
        assert r and r["status"] == "approved" and "执行结果文本" in r["result"], r
        # 未记录 token 超时 → None（fail-closed：无回执 ≠ 已执行）
        assert wait_settlement("P-hub-none", 0.3) is None
        # peek 语义：读到不删（第二个等待者仍能取到）
        assert wait_settlement("P-hub-a", 0.5) is not None, "回执不得被取走即焚"
        # TTL 过期清扫（照抄挂起表 TTL 用例的 utime 推进手法）
        with FileLock(_STORE.with_suffix(".lock")):
            d = _json.loads(_STORE.read_text(encoding="utf-8"))
            d["P-hub-b"] = {"status": "rejected", "result": "x",
                            "ts": time.time() - _TTL_SECONDS - 10}
            _STORE.write_text(_json.dumps(d), encoding="utf-8")
        t = time.time() + 2
        _os.utime(_STORE, (t, t))  # 确定性失效 mtime 缓存
        assert wait_settlement("P-hub-b", 0.3) is None, "过期回执应被惰性清扫"
        print("OK 12 - settlement_hub：record/wait 往返、peek 不删、TTL 清扫")
    finally:
        if _STORE.exists():
            _STORE.unlink()


def test_approval_token_data_channel():
    """13. token 走 ToolResult.data 机器通道（同步人审桥的凭据管道）：
    reason=pending_approval + approval_token 在 data、str(result) 无 token
    （"token 不回传 AI 文本通道"不变量在 data 通道扩展后依旧成立）"""
    from core.data_ops import get_driver
    from core.pending_ops import pop_pending, _STORE as _PSTORE
    if _PSTORE.exists():
        _PSTORE.unlink()  # 清场（escalate dedup 残留会串 token）
    ctx = get_context()
    _scratch_table("t_mcp_t32c")
    ctx.set_channel("mcp")
    try:
        # drop_table：nuke 闸 → pending_approval + approval_token（data 通道）
        r = execute_tool("drop_table", table="t_mcp_t32c")
        tok = r.data.get("approval_token")
        assert r.data.get("reason") == "pending_approval", r.data
        assert tok and tok.startswith("P-"), f"token 应在 data 通道: {r.data}"
        assert tok not in str(r), "token 不得出现在 AI 可见文本"
        assert "t_mcp_t32c" in get_driver().list_tables(), "挂起期间表必须仍在"
        assert pop_pending(tok) is not None, "data 通道 token 应可结算（登记正常）"
        # escalate_permission：同口径（20260903 起返回 ToolResult 而非裸 str）
        r2 = execute_tool("escalate_permission", role="admin", ttl=60)
        tok2 = r2.data.get("approval_token")
        assert r2.data.get("reason") == "pending_approval", r2.data
        assert tok2 and tok2.startswith("P-"), f"提权 token 应在 data 通道: {r2.data}"
        assert tok2 not in str(r2), "提权 token 不得出现在 AI 可见文本"
        assert pop_pending(tok2) is not None
        print("OK 13 - token 走 data 机器通道：str(result) 无 token，可结算")
    finally:
        _drop_direct("t_mcp_t32c")
        ctx.set_channel("graph")


def test_e2e_stdio_sync_bridge():
    """14. 同步人审桥 e2e（真实 stdio）：passive 模式批准回执 / 超时
    fail-closed / off 精确回滚旧行为。安全边界：桥只回传结算回执，
    不执行任何操作（执行在管理端 settle 端点，此处直写 hub 模拟）"""
    import asyncio
    import threading
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from core.pending_ops import list_pending, pop_pending
    from core.settlement_hub import record_settlement, _STORE as _HSTORE

    def _clean():
        # 清 __escalate__ 挂起（escalate kwargs 固定 → dedup 会串分支）+ hub 回执
        for p in list_pending():
            if p["name"] == "__escalate__":
                pop_pending(p["token"])
        if _HSTORE.exists():
            _HSTORE.unlink()

    async def _call(env_extra, settle_after: bool):
        env = dict(os.environ)
        env.setdefault("INDUSTRY", "construction_engineering")
        env["MCP_USER"] = "admin"
        env.update(env_extra)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.join(root, "mcp_server.py")], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if settle_after:
                    def _bg():
                        # 等子进程登记 __escalate__ 后直写回执（模拟管理端结算完成）
                        time.sleep(1.5)
                        for p in list_pending():
                            if p["name"] == "__escalate__":
                                record_settlement(p["token"], "approved",
                                                  "测试回执：提权已批准")
                    threading.Thread(target=_bg, daemon=True).start()
                r = await session.call_tool("escalate_permission", {})
                return r.content[0].text

    try:
        # approve 分支：等待中收到回执 → 组合"已批准并执行"文本
        t1 = asyncio.run(_call({"MCP_APPROVAL_SYNC": "passive",
                                "MCP_APPROVAL_SYNC_MAX_WAIT": "8"},
                               settle_after=True))
        assert "已批准并执行" in t1 and "测试回执" in t1, t1[:200]
        # timeout 分支：无人结算 → 超时 fail-closed"未批准"
        _clean()
        t2 = asyncio.run(_call({"MCP_APPROVAL_SYNC": "passive",
                                "MCP_APPROVAL_SYNC_MAX_WAIT": "2"},
                               settle_after=False))
        assert "未在有效期内批准" in t2, t2[:200]
        # off 分支：精确旧行为（待批准文案立即返回）
        _clean()
        t3 = asyncio.run(_call({"MCP_APPROVAL_SYNC": "0"},
                               settle_after=False))
        assert "已发起提权请求" in t3, t3[:200]
        print("OK 14 - 同步人审桥 e2e：批准回执 / 超时 fail-closed / off 回滚")
    finally:
        _clean()


if __name__ == "__main__":
    test_registry_and_metadata()
    test_mcp_channel_approval_flow()
    test_graph_channel_unchanged()
    test_server_tool_face()
    test_structured_contract()
    test_pending_pop_concurrency()
    test_pending_expired_cannot_settle()
    test_pending_tamper_rejected()
    test_register_dedup_atomic()
    test_e2e_stdio_handshake()
    test_e2e_first_run_poison_regression()
    test_stdio_survives_logging()
    test_frozen_ids_cross_owner_settlement()
    test_frozen_ids_injection_stripped()
    test_settlement_hub_roundtrip()
    test_approval_token_data_channel()
    test_e2e_stdio_sync_bridge()
    print("\n✅ 层 28 全部通过：MCP 能力面 + 高危人审闸双通道桥接 + 同步人审桥")
