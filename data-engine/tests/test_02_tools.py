"""层 2：工具注册 —— 39 个工具全部注册，叶子节点全部对应"""
import sys; sys.path.insert(0, ".")

def test_all_tools_registered():
    # Force tools registration (bypass import chain issues)
    # 必须传独立 globals dict：裸 exec 在函数内执行时，def 进测试函数局部命名空间，
    # handler.__globals__ 指向本测试模块 globals（缺 _validate_table_name 等模块内互调
    # 名字）→ 后续测试经注册表拿到残缺 handler 必报 name not defined
    # 20260822 拆包：tools.py → agent/tools/ 包，exec 入口改为 facade __init__.py
    # （facade 用绝对 import，exec 独立 globals 下可正常触发注册；包缓存保证只注册一次）
    exec(open("agent/tools/__init__.py", "r", encoding="utf-8").read(), {})
    from core.tool_registry import get_tools
    _tools = get_tools()  # 注册表快照（exec 已触发注册，快照即可枚举）
    expected = [
        "execute_instruction",
        # 结构化指令契约（20260905，MCP 面唯一数据通道）
        "execute_structured",
        "list_databases", "describe_schema", "query",
        "insert_data", "batch_insert_data", "edit_data", "delete_data",
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
        # 决策树无对应工具的意图统一路由
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
    from core.tool_registry import get_tools
    _tools = get_tools()  # 注册表快照（exec 已触发注册，快照即可枚举）
    leaves = {v["tool"] for k, v in _NODES.items() if "tool" in v}
    missing = [l for l in leaves if l not in _tools]
    assert not missing, f"Tree leaves not in tools: {missing}"
    # 辅助工具（不在决策树中）：
    # - list_vector_collections: 向量库维护辅助
    # - confirm_action: 人审机制面（高危闸挂起表回执的配套只读件）——
    #   20260822 起不再结算（防 AI 自助人审），不经意图路由
    # - escalate/deescalate_permission: sudo 提权（20260809）——通道级权限
    #   管理，AI 直接调用触发人审/撤销，不经业务意图路由
    # - execute_instruction: 硬路由元工具（20260824）——它是路由的入口
    #   （P1→树→P2 的发起方），树自然不路由到它自身；20260905 起保留仓内
    #   服务自然语言链（Web/测试），不上 MCP 面
    # - execute_structured: 结构化指令契约入口（20260905，MCP 面唯一数据
    #   通道）——契约发起方，树不路由到它自身
    auxiliary = {"list_vector_collections", "confirm_action",
                 "escalate_permission", "deescalate_permission",
                 "execute_instruction", "execute_structured"}
    unused = [t for t in _tools if t not in leaves and t not in auxiliary]
    assert not unused, f"Tools not in tree: {unused}"
    print("OK - all tree leaves match registered tools")

def test_decision_tree_yaml_loads():
    # YAML 外置加载：节点数/叶子数锁定（清理全部 l==r 假决策节点及其
    # 坍缩链 q_struct/q_field/q_fk/q_idx/q_type/add_type/im_tmpl，新增 unsup 叶子、
    # 删除孤立的 ed 叶子后：48 决策 + 33 叶子 = 81 个节点条目（含 del_sel 删选择集路由））
    # 3.3 模块化：单 yaml 拆为 decision_tree/ 目录多文件合并加载，节点全集不变
    from agent.router import _NODES, _TREE_DIR
    assert _TREE_DIR.is_dir(), f"decision tree dir missing: {_TREE_DIR}"
    leaves = [v for v in _NODES.values() if "tool" in v]
    decisions = [v for v in _NODES.values() if "tool" not in v]
    assert len(_NODES) == 81, f"node count changed: {len(_NODES)}"
    assert len(leaves) == 33 and len(decisions) == 48
    # 加载即校验：模块导入成功说明五项校验（可达/悬空/工具注册/假节点/无环）已通过
    print(f"OK - decision_tree/ dir loaded: {len(decisions)} decision + {len(leaves)} leaf nodes")

def test_tree_validation_catches_bad_trees():
    # 五项校验各造一例错误树，必须被 validate_tree 抓到并报出具体节点
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

    # 4) 假决策节点：l == r 假装覆盖
    bad = copy.deepcopy(_NODES)
    bad["q_agg"]["r"] = bad["q_agg"]["l"]
    try:
        validate_tree(bad, tool_names=tool_names)
        raise AssertionError("l==r fake node not caught")
    except DecisionTreeError as e:
        assert "q_agg" in str(e) and "假决策节点" in str(e), f"unclear error: {e}"

    # 5) 环：合成小树——a.r 回指根 l1 构成环且全部节点仍可达（绕过前序校验），
    #    必须被 DAG 校验抓到
    cyc = {
        "l1": {"c": "behavior", "m": {"查"}, "l": "a", "r": "leaf_q"},
        "a": {"c": "db", "m": "表", "l": "leaf_q", "r": "l1"},
        "leaf_q": {"tool": "query"},
    }
    try:
        validate_tree(cyc, tool_names=tool_names)
        raise AssertionError("cycle not caught")
    except DecisionTreeError as e:
        assert "环" in str(e), f"unclear error: {e}"

    print("OK - validation catches dangling/unreachable/unknown-tool/fake-node/cyclic trees")


def test_trace_path_degenerate_fails_closed():
    # 结构异常（环/悬空）时 trace_path 落 unsupported_op 如实报，绝不静默路由到读工具
    from agent.router import _DecisionTree
    tree = _DecisionTree()
    saved = tree.nodes
    try:
        # 环：l1 的右链回指 l1（绕过加载期校验直塞运行时单例）
        tree.nodes = dict(saved)
        tree.nodes["l1"] = {"c": "behavior", "m": {"查"}, "l": "query", "r": "l1"}
        tool, path, _ = tree.trace_path("删", "记录")
        assert tool == "unsupported_op", f"cycle should fail closed, got {tool}"
        # 悬空：右链指向不存在的节点
        tree.nodes = dict(saved)
        tree.nodes["l1"] = {"c": "behavior", "m": {"查"}, "l": "query", "r": "ghost"}
        tool, _, _ = tree.trace_path("删", "记录")
        assert tool == "unsupported_op", f"dangling should fail closed, got {tool}"
    finally:
        tree.nodes = saved
    print("OK - trace_path degenerate (cycle/dangling) fails closed to unsupported_op")

def test_query_no_keyword_hijack():
    """含"暂存/安装"等词的正常数据查询不被关键词短路劫持

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
    with patch('core.data_ops.get_driver', return_value=_FakeDrv()),          patch('core.context.get_context') as mock_ctx,          patch('core.formatters.format_multi_table', return_value="表格结果:暂存材料"),          patch('agent.tools.list_databases', return_value="数据库HIJACK"),          patch('agent.tools.list_selections_tool', return_value="选择集HIJACK"):
        mock_ctx.return_value.consume.return_value = None
        mock_ctx.return_value.save_selection.return_value = 1

        # 含"暂存"的正常查询：必须走 SQL 路径
        out = tools._query_with_fallback(query="查询暂存状态的材料", table="materials", conditions=conds)
        assert "HIJACK" not in out, f"含暂存的查询被劫持: {out[:120]}"
        assert "表格结果:暂存材料" in out, f"正常查询未走 SQL 路径: {out[:120]}"

        # 含"安装"（工程行业高频词）的正常查询：必须走 SQL 路径
        out2 = tools._query_with_fallback(query="查询安装工程定额", table="quota_items", conditions=conds)
        assert "HIJACK" not in out2, f"含安装的查询被劫持: {out2[:120]}"
        assert "表格结果:暂存材料" in out2, f"正常查询未走 SQL 路径: {out2[:120]}"
    print("OK - 含暂存/安装的查询不被关键词短路劫持")

def test_bare_filename_multi_candidate():
    """裸文件名兜底遇同名多版本时报错列出候选，不静默选最新"""
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
    print("OK - 裸文件名多候选报错、单候选正常解析")

def test_p1_5_explicit_error_channels():
    """四条静默错误通道全部改为显式报错"""
    from unittest.mock import patch

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
    with patch('core.data_ops.get_driver', return_value=_CountFailDrv()):
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
        out = join_query("a", "b", on_condition='[{"left": "a.id", "op": "=", "right": "b.a_id"}]')
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
    with patch('core.federation.join_executor.federated_join', lambda *a, **kw: None),          patch('core.data_ops.get_driver', return_value=drv),          patch('core.formatters.format_multi_table', return_value="结果"):
        out = join_query("a", "b", select_fields="a.*, b.id", on_condition='[{"left": "a.id", "op": "=", "right": "b.a_id"}]')
        assert out == "结果", f"合法 table.* 应正常查询: {out[:150]}"
        assert "a.*, b.id" in drv.last_sql, f"table.* 应原生透传而非改 *: {drv.last_sql}"
        out2 = join_query("a", "b", select_fields="c.*", on_condition='[{"left": "a.id", "op": "=", "right": "b.a_id"}]')
        assert "未参与本查询的表通配" in out2, f"未参与查询的 table.* 应显式报错: {out2[:150]}"
    print("OK - 四条静默错误通道全部显式化")

def test_p1_2_generic_unique_key_and_display():
    """唯一业务键机制通用化（驱动不识 quota_id）+ 排版策略配置化"""
    import tempfile
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
    print("OK - 通用唯一键（非quota键名生效）+ 驱动无quota字样 + 排版配置化")

def test_canonicalize_intent():
    """意图标签归一——别名→canonical，树消费同一组标签"""
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
    print("OK - 意图标签 canonical 归一（别名→canonical→树同路由）")
def test_p2_4_state_and_singleton_hygiene():
    """DSM 显式实例化入口与单例互不影响（单例卫生回归锁）"""
    from core.datasource_manager import DataSourceManager
    singleton = DataSourceManager()
    fresh = DataSourceManager.new_instance()
    assert fresh is not singleton, "new_instance 应绕过单例"
    assert DataSourceManager() is singleton, "单例语义不变"
    DataSourceManager.reset_instance()
    assert DataSourceManager() is not singleton, "reset_instance 公开入口生效"
    print("OK - DSM 显式实例化/公开重置")

def test_execute_tool_signature_no_collision():
    """execute_tool 首参为 positional-only 的 tool_name（阻断回归锁）：
    三个模板工具的首参叫 name（save_template/import_template/drop_template），
    若 execute_tool 首参仍叫 name，execute_tool("save_template", name="x")
    在参数绑定期必 TypeError——注册在册的工具从未被真调通过。"""
    import inspect
    from core.tool_registry import execute_tool, get_tools
    p0 = list(inspect.signature(execute_tool).parameters.values())[0]
    assert p0.name == "tool_name" and p0.kind == inspect.Parameter.POSITIONAL_ONLY, \
        f"首参应为 positional-only tool_name: {p0}"
    # 全量绑定冒烟：每个工具的首个 str 参数带值调用，绑定不碰撞
    sig = inspect.signature(execute_tool)
    for tname, t in get_tools().items():
        probe = {p.name: "probe" for p in t.params[:1] if p.type == "str"}
        sig.bind(tname, **probe)  # 签名碰撞会在这里 TypeError
    print("OK - execute_tool 首参 tool_name（positional-only），39 工具绑定零碰撞")


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
    # commit/_get_unique_key_column 下沉为基类共享默认实现，抽象数 29→27
    assert len(abstracts) >= 27, f"接口数应 ≥27, got {len(abstracts)}"
    for cls in (SqliteDriver, MysqlDriver, FederatedDriver, ContractDriver, DaemonDriver):
        missing = [n for n in abstracts
                   if not callable(getattr(cls, n, None))
                   or getattr(getattr(cls, n), "__isabstractmethod__", False)]
        assert not missing, f"{cls.__name__} 未实现接口: {missing}"
    # 签名一致性（防 ABC 与实现的参数前缀漂移）：
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
    """问表铁证：'数据库中有哪些表'→表（防 list_databases 答非所问的回归锁），防误伤矩阵"""
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


def test_formatters_real_output():
    """core/formatters 真实输出钉死（收敛后的排版真身——此前仅被 mock 覆盖，
    排版回归零防线）。三种布局 + 空结果逐字断言。"""
    from core.formatters import format_multi_table
    out = format_multi_table({"t": [{"id": 1, "name": "甲"}]}, table_map={"t": {}})
    assert "| 序号 | name |" in out and "| 1 | 甲 |" in out, out  # 默认隐藏 id 列
    assert "暂无数据" in format_multi_table({"t": []}, table_map={"t": {}})
    kv = format_multi_table({"t": [{"id": 1, "a": "x", "b": "y"}]},
                            table_map={"t": {"display": {"layout": "kv"}}})
    assert "| 字段 | 值 |" in kv and "| a | x |" in kv, kv
    print("OK - formatters 真实排版：表格/空态/KV 三布局逐字钉死")


def test_tool_arg_guard_boundary():
    """生成后校验边界闸（20260824）：MCP 直连与图路径同闸——假想/带噪/业务名
    表名在执行前被确定性拦截或归一，报错附可用表清单。纯离线：枚举与驱动全 mock。"""
    from unittest.mock import patch
    from core import tool_arg_guard as g

    schemas = [{"name": "quota_items", "business_name": "定额项目主表",
                "columns": [{"name": "id"}, {"name": "quota_code"}, {"name": "base_price"}]}]
    all_tables = {"quota_items"}

    # 1. 噪声尾巴归一：LLM 把子任务文本糊进表名 → 唯一候选确定性纠正
    args = {"table": "quota_items批量插入2条"}
    assert g.validate_tool_args("batch_insert_data", args, schemas, all_tables) is None
    assert args["table"] == "quota_items", f"带噪表名应归一: {args}"

    # 2. 声明业务名精确命中（YAML 元数据，不是猜）
    args2 = {"table": "定额项目主表"}
    assert g.validate_tool_args("batch_insert_data", args2, schemas, all_tables) is None
    assert args2["table"] == "quota_items", f"业务名应解析: {args2}"

    # 3. 假想表名拦截：报错带肇事名 + 可用表清单（AI 可自我修正）
    err = g.validate_tool_args("batch_insert_data", {"table": "ghost_table"}, schemas, all_tables)
    assert err and "ghost_table" in err and "可用表" in err and "quota_items" in err, err

    # 4. 多候选不猜：一个短表名命中多张表时如实报不存在（不替用户选）
    schemas4 = [{"name": "quota_items", "columns": []}, {"name": "quota_labor", "columns": []}]
    err4 = g.validate_tool_args("batch_insert_data", {"table": "quota"}, schemas4,
                                {"quota_items", "quota_labor"})
    assert err4 and "'quota'" in err4, f"多候选应如实报错不猜: {err4!r}"

    # 4b. 反向子串不猜：假想名包含真实表名作子串时
    #（如 raw_quota_items_stock 含 quota_items）不得静默改写写操作目标表
    err4b = g.validate_tool_args("batch_insert_data", {"table": "raw_quota_items_stock"},
                                 schemas, all_tables)
    assert err4b and "raw_quota_items_stock" in err4b, \
        f"反向子串命中不得归一（应如实报不存在）: {err4b!r}"

    # 4c. 纯 ASCII 尾巴不归一：users2/users_backup 是"像真名的后缀"
    # 而非噪声——startswith 前缀命中也不许改写写操作目标表；
    # CJK 噪声尾巴仍归一（'quota_items批量插入2条' 场景，硬路由实测路径）
    _s5 = schemas + [{"name": "users", "columns": [{"name": "id"}]}]
    _t5 = set(all_tables) | {"users"}
    err4c = g.validate_tool_args("batch_insert_data", {"table": "users2"}, _s5, _t5)
    assert err4c and "users2" in err4c, f"users2 不得归一进 users: {err4c!r}"
    _args4d = {"table": "users记录插入"}
    assert g.validate_tool_args("batch_insert_data", _args4d, _s5, _t5) is None \
        and _args4d["table"] == "users", f"CJK 尾巴应归一: {_args4d}"

    # 5. execute_tool 边界闸：假想表名在 handler 执行前被拦（VALIDATION/arg_validation）
    fake_enum = (["db"], schemas, sorted(all_tables), ["id", "quota_code", "base_price"], [])
    with patch("core.tool_arg_guard.enumerate_objects", return_value=fake_enum):
        import agent.tools  # noqa: F401 —— 触发注册
        from core.tool_registry import execute_tool
        r = execute_tool("batch_insert_data", table="ghost_table",
                         data='[{"quota_code": "X1"}]')
        assert r.data.get("code") == "VALIDATION" and r.data.get("reason") == "arg_validation", \
            f"假想表名应被边界闸拦截: {r.data}"
        assert "ghost_table" in r.text and "可用表" in r.text, r.text

        # 6. 合法表名过闸（闸不误伤）：handler 收到归一后的表名
        # （batch_insert_data 已升格 nuke 人审闸——写皆密码；此处测的是边界闸
        #  归一逻辑，用批量预批准上下文模拟审批后的放行路径，与人审结算同型）
        seen = {}
        import core.data_ops as _ops
        class _TR:
            def __init__(s, text): s.text, s.data = text, {"ok": True, "code": "OK"}
            def __str__(s): return s.text
        def _fake_insert_row(t, j):
            seen["table"] = t
            return _TR(f"已插入{t}数据")
        from core.context import get_context as _gc
        _ctx = _gc()
        _ctx.set_nuke_batch(tables={"quota_items"}, ops={"batch_insert_data"})
        try:
            with patch.object(_ops, "insert_row", _fake_insert_row):
                r2 = execute_tool("batch_insert_data", table="quota_items批量插入2条",
                                  data='[{"quota_code": "X2"}]')
        finally:
            _ctx.clear_nuke_batch()
        assert seen.get("table") == "quota_items", f"归一表名应直达 handler: {seen}"
        assert r2.data.get("ok"), f"合法调用不应被闸拦截: {r2.data}"

        # 7. drop_table 豁免："所有表格"批量关键词由 handler 语义转换，闸不拦
        r3 = execute_tool("drop_table", table="所有表格")
        assert r3.data.get("reason") != "arg_validation", \
            f"drop_table 批量关键词不应被边界闸拦截: {r3.data}"
    print("OK - 生成后校验边界闸：噪声归一/业务名解析/假想拦截/多候选不猜/drop_table 豁免")


if __name__ == "__main__":
    test_all_tools_registered()
    test_tree_leaves_match()
    test_decision_tree_yaml_loads()
    test_tree_validation_catches_bad_trees()
    test_query_no_keyword_hijack()
    test_bare_filename_multi_candidate()
    test_p1_5_explicit_error_channels()
    test_p1_2_generic_unique_key_and_display()
    test_canonicalize_intent()
    test_formatters_real_output()
    test_text_db_override_tables()
    test_p2_4_state_and_singleton_hygiene()
    test_driver_interface_completeness()
    test_execute_tool_signature_no_collision()
    test_tool_arg_guard_boundary()
