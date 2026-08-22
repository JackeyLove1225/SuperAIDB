"""层 2：工具注册 —— 38 个工具全部注册，叶子节点全部对应"""
import sys; sys.path.insert(0, ".")

def test_all_tools_registered():
    # Force tools registration (bypass import chain issues)
    # 必须传独立 globals dict：裸 exec 在函数内执行时，def 进测试函数局部命名空间，
    # handler.__globals__ 指向本测试模块 globals（缺 _validate_table_name 等模块内互调
    # 名字）→ 后续测试经注册表拿到残缺 handler 必报 name not defined（test_22 事故）
    # 20260822 拆包：tools.py → agent/tools/ 包，exec 入口改为 facade __init__.py
    # （facade 用绝对 import，exec 独立 globals 下可正常触发注册；包缓存保证只注册一次）
    exec(open("agent/tools/__init__.py", "r", encoding="utf-8").read(), {})
    from core.tool_registry import _tools
    expected = [
        "list_databases", "describe_schema", "query",
        "insert_data", "batch_insert_data", "mutate_data", "edit_data", "delete_data",
        "clear_session", "batch_create_tables", "create_standard_tables",
        "drop_table", "add_column", "drop_column", "modify_column",
        "alter_precision", "set_not_null",
        "add_foreign_key", "drop_foreign_key",
        "create_index", "drop_index",
        "save_template", "list_templates", "import_template", "drop_template",
        "clear_db", "process_file", "upload_file",
        "list_selections", "search_documents", "list_vector_collections",
        # 多表联合查询 + 聚合统计 + 数据导出（FC 层新增）
        "join_query", "aggregate_query", "export_data",
        # 决策树无对应工具的意图统一路由（P1-3）
        "unsupported_op",
        # MCP 通道人审结算（形态①，20260807）：高危闸挂起表回执的结算工具
        "confirm_action",
        # sudo 提权（20260809）：AI 默认受限身份，需管理员时经人审提权/撤销
        "escalate_permission", "deescalate_permission",
    ]
    missing = [t for t in expected if t not in _tools]
    assert not missing, f"Missing tools: {missing}"
    extra = [t for t in _tools if t not in expected]
    assert not extra, f"Extra tools: {extra}"
    print(f"OK - all {len(expected)} tools registered")

def test_tree_leaves_match():
    from agent.router import _NODES
    exec(open("agent/tools/__init__.py", "r", encoding="utf-8").read(), {})  # 独立 globals（同上）
    from core.tool_registry import _tools
    leaves = {v["tool"] for k, v in _NODES.items() if "tool" in v}
    missing = [l for l in leaves if l not in _tools]
    assert not missing, f"Tree leaves not in tools: {missing}"
    # 辅助工具（不在决策树中）：
    # - list_vector_collections: 向量库维护辅助
    # - mutate_data: 统一智能体循环专用入口（agent_run 调用，方案C人审闸内置；
    #   决策树不路由——记级 DML 由 route_by_task_type 在 basic 分流到 agent_run）
    # - confirm_action: MCP 通道人审结算（高危闸挂起表回执，20260807）——
    #   由 AI 凭待批准 token 直接调用，不经意图路由
    # - escalate/deescalate_permission: sudo 提权（20260809）——通道级权限
    #   管理，AI 直接调用触发人审/撤销，不经业务意图路由
    auxiliary = {"list_vector_collections", "mutate_data", "confirm_action",
                 "escalate_permission", "deescalate_permission"}
    unused = [t for t in _tools if t not in leaves and t not in auxiliary]
    assert not unused, f"Tools not in tree: {unused}"
    print("OK - all tree leaves match registered tools")

def test_decision_tree_yaml_loads():
    # YAML 外置加载：节点数/叶子数锁定（P1-3 清理全部 l==r 假决策节点及其
    # 坍缩链 q_struct/q_field/q_fk/q_idx/q_type/add_type/im_tmpl，新增 unsup 叶子、
    # 删除孤立的 ed 叶子后：47 决策 + 34 叶子 = 81 个节点条目（含 del_sel 删选择集路由））
    # 3.3 模块化：单 yaml 拆为 decision_tree/ 目录多文件合并加载，节点全集不变
    from agent.router import _NODES, _TREE_DIR
    assert _TREE_DIR.is_dir(), f"decision tree dir missing: {_TREE_DIR}"
    leaves = [v for v in _NODES.values() if "tool" in v]
    decisions = [v for v in _NODES.values() if "tool" not in v]
    assert len(_NODES) == 81, f"node count changed: {len(_NODES)}"
    assert len(leaves) == 34 and len(decisions) == 47
    # 加载即校验：模块导入成功说明三项校验（可达/悬空/工具注册）已通过
    print(f"OK - decision_tree/ dir loaded: {len(decisions)} decision + {len(leaves)} leaf nodes")

def test_tree_validation_catches_bad_trees():
    # 三项校验各造一例错误树，必须被 validate_tree 抓到并报出具体节点
    import copy
    from agent.router import _NODES, validate_tree, DecisionTreeError
    tool_names = {v["tool"] for v in _NODES.values() if "tool" in v}

    # 1) 悬空引用：q_obj 的 r 分支指向不存在的节点
    bad = copy.deepcopy(_NODES)
    bad["q_obj"]["r"] = "ghost_node"
    try:
        validate_tree(bad, tool_names=tool_names)
        raise AssertionError("dangling ref not caught")
    except DecisionTreeError as e:
        assert "q_obj" in str(e) and "ghost_node" in str(e), f"unclear error: {e}"

    # 2) 不可达：塞入一个没有任何引用的孤岛节点
    bad = copy.deepcopy(_NODES)
    bad["island"] = {"tool": "query"}
    try:
        validate_tree(bad, tool_names=tool_names)
        raise AssertionError("unreachable node not caught")
    except DecisionTreeError as e:
        assert "island" in str(e), f"unclear error: {e}"

    # 3) 未知工具：叶子指向注册表外的工具名
    bad = copy.deepcopy(_NODES)
    bad["ds"]["tool"] = "nonexistent_tool"
    try:
        validate_tree(bad, tool_names=tool_names)
        raise AssertionError("unknown tool not caught")
    except DecisionTreeError as e:
        assert "ds" in str(e) and "nonexistent_tool" in str(e), f"unclear error: {e}"

    # 4) 假决策节点：l == r 假装覆盖（P1-3 新增校验）
    bad = copy.deepcopy(_NODES)
    bad["q_agg"]["r"] = bad["q_agg"]["l"]
    try:
        validate_tree(bad, tool_names=tool_names)
        raise AssertionError("l==r fake node not caught")
    except DecisionTreeError as e:
        assert "q_agg" in str(e) and "假决策节点" in str(e), f"unclear error: {e}"

    print("OK - validation catches dangling/unreachable/unknown-tool/fake-node trees")

def test_query_no_keyword_hijack():
    """P1-4：含"暂存/安装"等词的正常数据查询不被关键词短路劫持

    意图路由是决策树的工作（白盒）；tools 层不得按 query 文本里的关键词
    劫持到 list_selections/list_databases——工程行业"安装"、业务数据"暂存"
    都是合法查询内容。
    """
    from unittest.mock import patch

    class _FakeDrv:
        def query(self, sql):
            if "COUNT(*)" in sql:
                return [{"c": 1}]
            return [{"id": 1, "name": "暂存材料", "status": "暂存"}]
        def get_columns(self, table):
            return [{"name": "id"}, {"name": "name"}, {"name": "status"}]
        def list_tables(self):
            return ["materials", "quota_items"]

    import agent.tools as tools
    conds = '[{"field":"status","op":"=","value":"暂存"}]'
    with patch('core.data_ops._get_driver', return_value=_FakeDrv()),          patch('core.context.get_context') as mock_ctx,          patch('core.db_chat.DBChat') as mock_chat,          patch('agent.tools.list_databases', return_value="数据库HIJACK"),          patch('agent.tools.list_selections_tool', return_value="选择集HIJACK"):
        mock_ctx.return_value.consume.return_value = None
        mock_ctx.return_value.save_selection.return_value = 1
        mock_chat.return_value._format_multi_table.return_value = "表格结果:暂存材料"

        # 含"暂存"的正常查询：必须走 SQL 路径
        out = tools._query_with_fallback(query="查询暂存状态的材料", table="materials", conditions=conds)
        assert "HIJACK" not in out, f"含暂存的查询被劫持: {out[:120]}"
        assert "表格结果:暂存材料" in out, f"正常查询未走 SQL 路径: {out[:120]}"

        # 含"安装"（工程行业高频词）的正常查询：必须走 SQL 路径
        out2 = tools._query_with_fallback(query="查询安装工程定额", table="quota_items", conditions=conds)
        assert "HIJACK" not in out2, f"含安装的查询被劫持: {out2[:120]}"
        assert "表格结果:暂存材料" in out2, f"正常查询未走 SQL 路径: {out2[:120]}"
    print("OK - 含暂存/安装的查询不被关键词短路劫持（P1-4）")

def test_bare_filename_multi_candidate():
    """P1-12：裸文件名兜底遇同名多版本时报错列出候选，不静默选最新"""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    import agent.tools as tools

    with tempfile.TemporaryDirectory() as up:
        for sub in ("a", "b"):
            d = Path(up) / sub
            d.mkdir()
            (d / "同名文件.txt").write_text("内容" + sub, encoding="utf-8")
        from config.settings import settings as _rs
        with patch.object(_rs, 'UPLOAD_DIR', up):
            out = tools.process_file(filepath="同名文件.txt")
        assert "无法确定使用哪个" in out, f"多候选应显式报错: {out[:150]}"
        assert "同名文件.txt" in out and "完整路径" in out, f"应列出候选并提示完整路径: {out[:150]}"

    # 单候选：正常解析（txt 走文本入库路径，mock 向量库）
    with tempfile.TemporaryDirectory() as up:
        (Path(up) / "唯一文件.txt").write_text("一些文本内容", encoding="utf-8")
        class _FakeVS:
            def add(self, col, chunks, metas): pass
        from config.settings import settings as _rs
        with patch.object(_rs, 'UPLOAD_DIR', up),              patch('core.vector_store.get_vector_store', return_value=_FakeVS()):
            out = tools.process_file(filepath="唯一文件.txt")
        assert "文本文件已入向量库" in out, f"单候选应正常入库: {out[:150]}"
    print("OK - 裸文件名多候选报错、单候选正常解析（P1-12）")

def test_p1_5_explicit_error_channels():
    """P1-5：四条静默错误通道全部改为显式报错"""
    from unittest.mock import patch, MagicMock

    # 1) NullVectorStore：未实现类型返回占位实例，携带原因，使用时抛明确错误
    from core.vector_store import reset_vector_store, get_vector_store, NullVectorStore
    reset_vector_store()
    from config.settings import settings as _real_settings
    with patch.object(_real_settings, 'VECTOR_STORE_TYPE', "pgvector"):  # 未实现类型
        vs = get_vector_store()
    try:
        assert isinstance(vs, NullVectorStore), f"应返回 NullVectorStore, got {type(vs)}"
        assert not vs, "NullVectorStore 的 bool 应为 False（兼容 if not vs 判空）"
        assert "尚未实现" in vs.reason
        try:
            vs.add("col", ["t"], [{}])
            raise AssertionError("NullVectorStore.add 应抛错")
        except RuntimeError as e:
            assert "向量数据库不可用" in str(e)
    finally:
        reset_vector_store()

    # 2) COUNT 哨兵：COUNT 失败不再 -1 伪装，显式报错
    import agent.tools as tools
    class _CountFailDrv:
        def query(self, sql):
            if "COUNT(*)" in sql:
                raise RuntimeError("磁盘IO错误")
            return [{"id": 1}]
        def get_columns(self, table):
            return [{"name": "id"}]
        def list_tables(self):
            return ["t1"]
    with patch('core.data_ops._get_driver', return_value=_CountFailDrv()):
        out = tools._query_with_fallback(query="查", table="t1", conditions="[]")
    assert "查询总数统计失败" in out, f"COUNT 失败应显式报错: {out[:120]}"

    # 3) 跨库 JOIN 编排失败：多数据源时显式中止，不回退到注定失败的单连接 JOIN
    from core.data_ops import join_query
    def _fed_fail(*a, **kw):
        raise RuntimeError("编排器内部错误")
    class _DsDSM:
        def get_datasource_for_table(self, t):
            return "ds1" if t == "a" else "ds2"
    with patch('core.federation.join_executor.federated_join', _fed_fail),          patch('core.datasource_manager.DataSourceManager', return_value=_DsDSM()):
        out = join_query("a", "b", on_condition="a.id = b.a_id")
    assert "跨库 JOIN 编排失败" in out and "已中止" in out, f"跨库失败应显式中止: {out[:150]}"

    # 4) table.* 不再静默改 *：参与查询的表透传，未参与的显式报错
    class _JoinDrv:
        def table_exists(self, t): return True
        def query(self, sql):
            self.last_sql = sql
            if "COUNT(*)" in sql:
                return [{"c": 1}]
            return [{"id": 1, "name": "x"}]
    drv = _JoinDrv()
    with patch('core.federation.join_executor.federated_join', lambda *a, **kw: None),          patch('core.data_ops._get_driver', return_value=drv),          patch('core.db_chat.DBChat') as mock_chat:
        mock_chat.return_value._format_multi_table.return_value = "结果"
        out = join_query("a", "b", select_fields="a.*, b.id", on_condition="a.id = b.a_id")
        assert out == "结果", f"合法 table.* 应正常查询: {out[:150]}"
        assert "a.*, b.id" in drv.last_sql, f"table.* 应原生透传而非改 *: {drv.last_sql}"
        out2 = join_query("a", "b", select_fields="c.*", on_condition="a.id = b.a_id")
        assert "未参与本查询的表通配" in out2, f"未参与查询的 table.* 应显式报错: {out2[:150]}"
    print("OK - P1-5 四条静默错误通道全部显式化")

def test_executor_subtask_result_code():
    """P1-1/4.3：execute_sub_task 返回 SubTaskResult，code 来自 ToolResult 结构化字段（非文本分类）"""
    from unittest.mock import patch
    from agent.open_layer.executor import execute_sub_task, SubTaskResult, MAX_RETRIES
    from core.result_codes import ResultCode as RC
    from core.tool_result import ToolResult

    class _OKAgent:
        def execute_single(self, *a, **kw):
            return ToolResult.ok("查询结果: 5 条记录", row_count=5)
    with patch('agent.open_layer.executor.get_agent', return_value=_OKAgent()):
        r = execute_sub_task("查一下")
        assert isinstance(r, SubTaskResult) and r.code == RC.OK
        assert str(r) == "查询结果: 5 条记录"
        assert r.data.get("row_count") == 5, "data 通道应透传工具负载"

    class _DeterministicFailAgent:
        def __init__(self): self.calls = 0
        def execute_single(self, *a, **kw):
            self.calls += 1
            return ToolResult.fail("表 ghost_table 不存在", code="NOT_FOUND",
                                   reason="table_not_found")
    agent = _DeterministicFailAgent()
    with patch('agent.open_layer.executor.get_agent', return_value=agent):
        r = execute_sub_task("查 ghost")
        assert r.code == RC.NOT_FOUND, f"失败结果 code 应为 NOT_FOUND, got {r.code}"
        assert str(r).startswith("操作失败"), "失败文本应为友好错误"
        assert agent.calls == 1, f"确定性错误不应重试, 实际调用 {agent.calls} 次"

    class _TransientFailAgent:
        def __init__(self): self.calls = 0
        def execute_single(self, *a, **kw):
            self.calls += 1
            return ToolResult.fail("连接超时 timeout，请稍后重试", code="TRANSIENT",
                                   reason="timeout")
    agent2 = _TransientFailAgent()
    with patch('agent.open_layer.executor.get_agent', return_value=agent2),          patch('agent.open_layer.executor.time.sleep'):
        r = execute_sub_task("查一下")
        assert agent2.calls == 1 + MAX_RETRIES,             f"临时错误应重试 {MAX_RETRIES} 次, 实际 {agent2.calls} 次"
        assert r.code == RC.TRANSIENT
    print("OK - 4.3 execute_sub_task 结构化 code 判定+按 code 重试")

def test_p1_2_generic_unique_key_and_display():
    """P1-2：唯一业务键机制通用化（驱动不识 quota_id）+ 排版策略配置化"""
    import tempfile, os
    from pathlib import Path
    from unittest.mock import patch

    # 1) 驱动冲突检测走 YAML 声明的唯一键（非 quota_id 的键名也生效）
    from core.schema_matcher import get_unique_key_column
    # 适配空行业：当前 engineering 无业务表，唯一键机制用 fake_schema mock 验证
    fake_uk = [{
        "name": "quota_items",
        "columns": [{"name": "id", "type": "INTEGER", "pk": True},
                    {"name": "quota_code", "type": "TEXT", "unique": True},
                    {"name": "name", "type": "TEXT"}],
    }]
    with patch('core.schema_matcher._load_schemas', return_value=fake_uk):
        assert get_unique_key_column("quota_items") == "quota_code", \
            "YAML 声明 quota_code 唯一应被识别"
        assert get_unique_key_column("quota_materials") == "", "未声明唯一键应返回空"

    from core.drivers.sqlite_driver import SqliteDriver
    with tempfile.TemporaryDirectory() as tmp:
        drv = SqliteDriver(f"{tmp}/t.db")
        try:
            drv.conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                             " order_no TEXT, item TEXT)")
            fake_schema = [{
                "name": "orders",
                "columns": [{"name": "id", "type": "INTEGER", "pk": True},
                            {"name": "order_no", "type": "TEXT", "unique": True},
                            {"name": "item", "type": "TEXT"}],
            }]
            with patch('core.schema_matcher._load_schemas', return_value=fake_schema):
                r1 = drv.insert("orders", [{"order_no": "PO-001", "item": "水泥"}])
                assert r1.get("ok"), f"首次插入应成功: {r1}"
                r2 = drv.insert("orders", [{"order_no": "PO-001", "item": "钢筋"}])
                assert r2.get("conflict"), f"同 order_no 应被通用唯一键拦截: {r2}"
                r3 = drv.insert("orders", [{"order_no": "PO-001", "item": "钢筋"}], overwrite=True)
                assert r3.get("ok"), f"overwrite 应放行: {r3}"
                cnt = drv.query("SELECT COUNT(*) as c FROM orders")[0]["c"]
                assert cnt == 1, f"overwrite 后应只剩 1 条, got {cnt}"
        finally:
            for conn in drv._conns.values():
                conn.close()

    # 2) 驱动层无 quota 字样（架构红线：行业知识不进代码）
    src = Path("core/drivers/sqlite_driver.py").read_text(encoding="utf-8")
    assert "quota_id" not in src, "驱动层不应再出现 quota_id 硬编码"

    # 3) 排版策略读 display 配置（kv 布局 + hide_columns）
    from core.db_chat import DBChat
    chat = DBChat.__new__(DBChat)
    chat._fields_desc = ""
    fake_map = {
        "quota_items": {"display": {"layout": "kv", "hide_columns": ["id"]}},
        "quota_materials": {"display": {"hide_columns": ["id", "quota_item_id"]}},
    }
    with patch.object(DBChat, "_get_table_map", return_value=fake_map):
        out_kv = chat._format_multi_table({"quota_items": [{"id": 1, "quota_code": "A1-1", "name": "混凝土"}]})
        assert "| 字段 | 值 |" in out_kv, f"display.layout=kv 应走 KV 布局: {out_kv[:100]}"
        out_tbl = chat._format_multi_table({"quota_materials": [
            {"id": 1, "quota_item_id": 1, "material_name": "水泥", "unit": "t"}]})
        assert "| 序号 |" in out_tbl, f"无 kv 配置应走表格布局: {out_tbl[:100]}"
        assert "quota_item_id" not in out_tbl and "| id |" not in out_tbl,             f"hide_columns 应隐藏 id/quota_item_id: {out_tbl[:150]}"
        assert "水泥" in out_tbl
    print("OK - P1-2 通用唯一键（非quota键名生效）+ 驱动无quota字样 + 排版配置化")

def test_sanitize_dangling_tool_calls():
    """P1-11：后端真实补全悬空 tool_calls——如实说明，不再伪造成功"""
    from agent.open_layer.graph import _sanitize_dangling_tool_calls

    complete = [
        {"type": "human", "content": "查一下"},
        {"type": "ai", "content": "", "tool_calls": [{"id": "c1", "name": "query"}]},
        {"type": "tool", "tool_call_id": "c1", "name": "query", "content": "5 条记录"},
    ]
    out = _sanitize_dangling_tool_calls(complete)
    assert out is complete, "无悬空应原样返回（identity）"

    dangling = [
        {"type": "ai", "content": "", "tool_calls": [{"id": "c9", "name": "query"}]},
        {"type": "human", "content": "新消息"},
    ]
    out2 = _sanitize_dangling_tool_calls(dangling)
    assert len(out2) == 3, f"悬空应补 1 条 tool 响应, got {len(out2)}"
    injected = out2[1]
    assert injected["type"] == "tool" and injected["tool_call_id"] == "c9"
    assert "未执行" in injected["content"], f"应如实说明未执行: {injected['content']}"
    assert "Successfully" not in injected["content"], "不得伪造成功"
    print("OK - P1-11 悬空 tool_calls 后端真实补全")

def test_canonicalize_intent():
    """P2-3：意图标签归一——别名→canonical，树消费同一组标签"""
    from agent.router import canonicalize_intent, get_tree
    bk, dk, ct = canonicalize_intent("看看", "记录")
    assert bk == "查", f"别名'看看'应归一为'查', got {bk}"
    assert dk == "记录", f"未命中对象别名应原样保留, got {dk}"
    bk2, dk2, _ = canonicalize_intent("查", "表")
    assert bk2 == "查" and dk2 == "表", "canonical 值应原样通过"
    # 树消费：别名输入与 canonical 输入路由一致（两端词表漂移消失）
    t1 = get_tree().route("看看", "记录")
    t2 = get_tree().route("查", "记录")
    assert t1 == t2, f"别名与 canonical 应同路由: {t1} vs {t2}"
    print("OK - P2-3 意图标签 canonical 归一（别名→canonical→树同路由）")

def test_p2_4_state_and_singleton_hygiene():
    """P2-4：AgentState 无黑钥匙 + 单例显式实例化入口"""
    # 1) research_context_cache 已在 AgentState 声明（_cached_context 黑钥匙正名）
    from agent.open_layer.state import AgentState
    assert "research_context_cache" in AgentState.__annotations__,         "research_context_cache 应在 AgentState 声明"
    import agent.open_layer.research as research_mod, inspect
    src = inspect.getsource(research_mod)
    assert 'state["_cached_context"]' not in src and 'state.get("_cached_context"' not in src,         "research.py 不应再使用黑钥匙 _cached_context"

    # 2) DSM 显式实例化：new_instance 与单例互不影响
    from core.datasource_manager import DataSourceManager
    singleton = DataSourceManager()
    fresh = DataSourceManager.new_instance()
    assert fresh is not singleton, "new_instance 应绕过单例"
    assert DataSourceManager() is singleton, "单例语义不变"
    DataSourceManager.reset_instance()
    assert DataSourceManager() is not singleton, "reset_instance 公开入口生效"
    print("OK - P2-4 state 无黑钥匙 + DSM 显式实例化/公开重置")
def test_industry_pack():
    """行业注册包：生成解析 + 落盘 + lint（建库流程吸收原向导三要素）"""
    import json as _json
    import tempfile, shutil
    from pathlib import Path
    from agent.open_layer import industry_pack as ip

    # 1) gen_industry_pack：解析 LLM 产出的 config+prompts
    good = {
        "config": {"name": "libs", "description": "图书",
                   "expert_role": "图书管理员", "hierarchy_desc": "馆藏",
                   "default_table_name": "books"},
        "prompts": {"terminology": {"table_aliases": {"books": ["图书"]}},
                    "decompose_examples": [], "router_examples": []},
    }
    good_json = _json.dumps(good, ensure_ascii=False)
    class _GoodLLM:
        def invoke(self, prompt):
            class R:
                content = good_json
            return R()
    pack = ip.gen_industry_pack(_GoodLLM(), "创建图书行业", {"tables": []})
    assert pack["config"]["name"] == "libs"
    assert "terminology" in pack["prompts"]

    # 2) gen_industry_pack：缺 config.name 抛错
    class _BadLLM:
        def invoke(self, prompt):
            class R:
                content = _json.dumps({"prompts": {}}, ensure_ascii=False)
            return R()
    try:
        ip.gen_industry_pack(_BadLLM(), "x", {"tables": []})
        raise AssertionError("缺 config.name 应抛错")
    except ValueError:
        pass

    # 3) write_industry_pack：落盘 schemas/config/prompts + 用户标记
    tmp_ind = Path(tempfile.mkdtemp())
    try:
        from unittest.mock import patch
        tables = [{"name": "books", "business_name": "图书",
                   "columns": [{"name": "book_code", "type": "TEXT",
                                "business_name": "编号", "unique": True},
                               {"name": "title", "type": "TEXT", "business_name": "书名"}],
                   "foreign_keys": [],
                   "indexes": [{"columns": ["book_code"], "unique": True}]}]
        with patch("core.industry_manager.INDUSTRIES_DIR", tmp_ind):
            # industry_pack 内部 from core.industry_manager import INDUSTRIES_DIR——
            # 在函数内导入，patch 原模块属性即可
            name, errors = ip.write_industry_pack(pack, tables)
        assert name == "libs"
        assert (tmp_ind / "libs" / "schemas" / "books.yaml").exists(), "schemas 未落盘"
        assert (tmp_ind / "libs" / ".user_created").exists(), "缺用户创建标记"
        assert (tmp_ind / "libs" / "prompts" / "prompts.yml").exists(), "prompts 未落盘"
    finally:
        shutil.rmtree(tmp_ind, ignore_errors=True)
    print("OK - 行业注册包：生成解析/校验/落盘")

def test_driver_interface_completeness():
    """驱动接口完整性：每个具体驱动实现基类全部抽象方法（接口契约）+ 签名逐字对齐"""
    import inspect
    from core.drivers.base import Driver
    from core.drivers.sqlite_driver import SqliteDriver
    from core.drivers.mysql_driver import MysqlDriver
    from core.drivers.federated_driver import FederatedDriver
    from core.contract import ContractDriver
    from core.daemon.client import DaemonDriver

    abstracts = {n for n, m in inspect.getmembers(Driver, predicate=inspect.isfunction)
                 if getattr(m, "__isabstractmethod__", False)}
    # R4：commit/_get_unique_key_column 下沉为基类共享默认实现，抽象数 29→27
    assert len(abstracts) >= 27, f"接口数应 ≥27, got {len(abstracts)}"
    for cls in (SqliteDriver, MysqlDriver, FederatedDriver, ContractDriver, DaemonDriver):
        missing = [n for n in abstracts
                   if not callable(getattr(cls, n, None))
                   or getattr(getattr(cls, n), "__isabstractmethod__", False)]
        assert not missing, f"{cls.__name__} 未实现接口: {missing}"
    # 签名一致性（评审三轮：ABC 四参 vs 实现五参漂移曾致 daemon 模式 add_column 必炸）：
    # LSP 可替换判定——实现必须能以 ABC 调用形态被调用：ABC 参数名前缀逐字一致，
    # 允许实现追加带默认值的控制参数（如契约层的 force 确认闸，物理层无感知）
    for name in abstracts:
        want = [p for p in inspect.signature(getattr(Driver, name)).parameters][1:]
        for cls in (SqliteDriver, MysqlDriver, FederatedDriver, ContractDriver, DaemonDriver):
            params = list(inspect.signature(getattr(cls, name)).parameters.values())[1:]
            got = [p.name for p in params]
            assert got[:len(want)] == want, \
                f"{cls.__name__}.{name} 签名漂移: {got} 前缀 != {want}"
            extras = params[len(want):]
            assert all(p.default is not inspect.Parameter.empty for p in extras), \
                f"{cls.__name__}.{name} 追加参数必须带默认值: {[p.name for p in extras]}"
    print(f"OK - 5 个驱动实现全部 {len(abstracts)} 个接口且签名逐字一致"
          "（含 delete_by_pk/_get_unique_key_column/daemon RPC 代理）")

def test_text_db_override_tables():
    """问表铁证：'数据库中有哪些表'→表（list_databases 答非所问事故回归），防误伤矩阵"""
    from agent.router import get_tree, text_db_override as o
    cases = {
        "数据库中有哪些表？": "表",
        "现在数据库里有哪些表？": "表",
        "库里有哪些表": "表",
        "数据库里有多少张表": "表",
        "有哪些表和订单表关联": "表",
        # 防误伤：这些不得被打成"表"
        "有哪些数据库": "",
        "数据库有哪些字段": "",
        "学生表有哪些字段": "",
        "学生表有哪些记录": "",
        # 原有规则不破
        "查询每条主表记录对应的明细数据": "关联",
    }
    for text, want in cases.items():
        got = o(text)
        assert got == want, f"{text!r}: 期望 {want!r}, got {got!r}"
    # 树路由闭环：查+表 → describe_schema；查+数据库 → list_databases（不变）
    assert get_tree().route("查", "表") == "describe_schema"
    assert get_tree().route("查", "数据库") == "list_databases"
    print("OK - 问表铁证（哪些表→表）+ 防误伤矩阵 + 树路由闭环")


def test_apply_text_evidence_decompose_layer():
    """拆解层文本铁证：单 db 子任务用原句纠偏（LLM 改写丢词事故的系统性修复）"""
    from agent.open_layer.graph import _apply_text_evidence
    # 事故场景：query 被 LLM 改写成"查询数据库"（丢掉"哪些表"），dk 打成数据库
    tasks = [{"type": "db", "query": "查询数据库", "behavior_key": "查", "db_category_key": "数据库"}]
    out = _apply_text_evidence([dict(t) for t in tasks], "数据库中有哪些表？")
    assert out[0]["db_category_key"] == "表", f"单子任务应用原句纠偏: {out[0]}"
    # 多子任务不动（防串台：原句铁证不能盲目归到某一个子任务）
    tasks2 = [
        {"type": "db", "query": "查询数据库", "behavior_key": "查", "db_category_key": "数据库"},
        {"type": "db", "query": "统计数量", "behavior_key": "查", "db_category_key": "统计"},
    ]
    out2 = _apply_text_evidence([dict(t) for t in tasks2], "数据库中有哪些表？")
    assert out2[0]["db_category_key"] == "数据库", "多子任务不得用原句纠偏"
    # 原句无铁证不动
    out3 = _apply_text_evidence(
        [{"type": "db", "query": "查询数据库", "behavior_key": "查", "db_category_key": "数据库"}],
        "有哪些数据库")
    assert out3[0]["db_category_key"] == "数据库", "原句无铁证不得改动标签"
    # 非 db 子任务（rag/file_query）不计入 db 子任务数
    out4 = _apply_text_evidence(
        [{"type": "rag", "query": "检索文档"},
         {"type": "db", "query": "查询数据库", "behavior_key": "查", "db_category_key": "数据库"}],
        "数据库中有哪些表？")
    assert out4[1]["db_category_key"] == "表", "rag 不计入，单 db 子任务仍应纠偏"
    print("OK - 拆解层文本铁证（单db子任务原句纠偏/多任务防串台/无铁证不动/rag不计入）")


if __name__ == "__main__":
    test_all_tools_registered()
    test_tree_leaves_match()
    test_decision_tree_yaml_loads()
    test_tree_validation_catches_bad_trees()
    test_query_no_keyword_hijack()
    test_bare_filename_multi_candidate()
    test_p1_5_explicit_error_channels()
    test_executor_subtask_result_code()
    test_p1_2_generic_unique_key_and_display()
    test_sanitize_dangling_tool_calls()
    test_canonicalize_intent()
    test_text_db_override_tables()
    test_apply_text_evidence_decompose_layer()
    test_p2_4_state_and_singleton_hygiene()
    test_industry_pack()
    test_driver_interface_completeness()
