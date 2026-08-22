"""层 29：多表改/删合并确认闸（方案E 20260805）

测试网覆盖：
1. describe_table_mutation：单表影响面——条数/总行数/记录预览/反向引用计数齐全
2. _topo_sort_deletes：外键拓扑排序——子表（引用方）先删、主表后删；UPDATE 不参排
3. _multi_ops_confirmed：合并一张卡——interrupt 负载含全部表明细/执行顺序/折叠结构，
   批准/拒绝分流
4. mutate_natural 多表端到端：两表删除 → 合并闸批准 → 按拓扑序真实删库（先子后主）
"""
import sys; sys.path.insert(0, ".")
import agent  # noqa: F401——编排层初始化即注册决策树路由（DI，data_ops._route_tool 依赖）
from core.crypto.connection import open_db
import os
from unittest.mock import patch

DS_FIXTURE = os.path.join("tests", "fixtures", "datasources_multi_mutate.yml")
DB_PATH = os.path.join("db", "test_multi_mutate.db")

# 测试用 FK schema（替代 industries YAML，_load_table_schema 统一打桩）
_FK_SCHEMA = {
    "t_child": {"foreign_keys": [
        {"columns": ["pid"], "references": "t_parent", "ref_columns": ["id"]}]},
    "t_parent": {"foreign_keys": []},
}


def _fake_schema(table):
    return _FK_SCHEMA.get(table, {})


def _setup_scratch_db():
    """父表 t_parent + 子表 t_child（pid 逻辑引用 t_parent.id），各插一行"""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    import sqlite3
    conn = open_db(DB_PATH)
    conn.execute("CREATE TABLE t_parent(id INTEGER PRIMARY KEY, code TEXT)")
    conn.execute("CREATE TABLE t_child(id INTEGER PRIMARY KEY, pid INTEGER, note TEXT)")
    conn.execute("INSERT INTO t_parent VALUES(1, 'P1')")
    conn.execute("INSERT INTO t_child VALUES(1, 1, 'c1')")
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


def test_describe_table_mutation_impact():
    """单表影响面：条数/总行数/记录预览/正向FK/反向引用计数齐全"""
    import core.data_ops as ops
    _setup_scratch_db()
    try:
        with patch.object(ops, "_load_table_schema", _fake_schema):
            drv = ops._get_driver()
            info = ops.describe_table_mutation(
                drv, "t_parent", "DELETE", [1], [{"id": 1, "code": "P1"}])
        s = info["summary"]
        assert "【t_parent】删除 1 条（全表 1 行）" in s, s
        assert "不可恢复" in s
        assert "P1" in s, s  # 记录预览
        # 反向引用计数：t_child.pid 有 1 行引用将被删的 t_parent.id=1
        assert "t_child.pid → 1 行引用了将被影响的记录" in s, s
        assert "id" in info["structure"]
        # 子表视角：正向 FK 展示
        with patch.object(ops, "_load_table_schema", _fake_schema):
            info2 = ops.describe_table_mutation(
                drv, "t_child", "DELETE", [1], [{"id": 1, "pid": 1, "note": "c1"}])
        assert "pid → t_parent.id" in info2["summary"], info2["summary"]
    finally:
        _teardown_scratch_db()
    print("OK - 单表影响面：条数/预览/正反向外键信息齐全")


def test_topo_sort_child_first():
    """删除拓扑排序：子表先删、主表后删；UPDATE 不参排保留尾部；单 DELETE 原样"""
    import core.data_ops as ops
    with patch.object(ops, "_load_table_schema", _fake_schema):
        ordered = ops._topo_sort_deletes([
            {"table": "t_parent", "action": "DELETE"},
            {"table": "t_child", "action": "DELETE"},
        ])
        assert [p["table"] for p in ordered] == ["t_child", "t_parent"], ordered
        # 混合：UPDATE 不参排，附在 DELETE 之后
        ordered2 = ops._topo_sort_deletes([
            {"table": "t_parent", "action": "DELETE"},
            {"table": "t_child", "action": "DELETE"},
            {"table": "t_parent", "action": "UPDATE"},
        ])
        assert [p["table"] for p in ordered2[:2]] == ["t_child", "t_parent"]
        assert ordered2[2]["action"] == "UPDATE"
        # 单 DELETE 不排序
        single = [{"table": "t_parent", "action": "DELETE"}]
        assert ops._topo_sort_deletes(single) is single or \
               ops._topo_sort_deletes(single) == single
    print("OK - 拓扑排序：子表先删、主表后删、UPDATE 尾部保留")


def test_multi_ops_confirmed_merged_card():
    """合并确认卡：interrupt 负载含全部表明细+拓扑顺序说明+折叠结构；批准/拒绝分流"""
    import core.data_ops as ops
    _setup_scratch_db()
    captured: dict = {}

    def _approve(payload):
        captured.update(payload)
        return {"decisions": [{"type": "approve"}]}

    pending = [
        {"table": "t_child", "action": "DELETE", "ids": [1],
         "sample": [{"id": 1, "pid": 1, "note": "c1"}], "set_data": ""},
        {"table": "t_parent", "action": "DELETE", "ids": [1],
         "sample": [{"id": 1, "code": "P1"}], "set_data": ""},
    ]
    try:
        with patch.object(ops, "_load_table_schema", _fake_schema), \
             patch("langgraph.types.interrupt", _approve):
            assert ops._multi_ops_confirmed(pending, "删除父子表") is True
        ar = captured["action_requests"][0]
        assert "2" in ar["name"] and "表" in ar["name"], ar["name"]
        detail = ar["args"]["操作明细"]
        assert "t_child" in detail and "t_parent" in detail, detail
        assert "拓扑" in ar["args"]["执行顺序"]
        fold = ar["args"]["各表结构"]
        assert "__fold__" in fold, fold  # 折叠协议：字段全展示但默认收起
        assert "t_child" in fold["content"] and "t_parent" in fold["content"]
        # 描述含反向引用计数（t_parent 被 t_child 引用）
        assert "t_child.pid → 1 行引用" in ar["description"], ar["description"]
        # 拒绝路径
        with patch.object(ops, "_load_table_schema", _fake_schema), \
             patch("langgraph.types.interrupt",
                   lambda _p: {"decisions": [{"type": "reject"}]}):
            assert ops._multi_ops_confirmed(pending, "删除父子表") is False
    finally:
        _teardown_scratch_db()
    print("OK - 合并确认卡：一卡两表明细+折叠结构+反向引用+批准/拒绝分流")


def test_mutate_natural_multi_table_e2e():
    """多表删除端到端：合并闸批准 → 按拓扑序真实删库（先子后主）+ effects 逐表收集"""
    import core.data_ops as ops
    _setup_scratch_db()
    call_order: list[str] = []
    real_delete = ops.delete_rows

    def _rec_delete(table, where=""):
        call_order.append(table)
        return real_delete(table, where)

    ops_parsed = [
        {"table": "t_parent", "action": "DELETE", "set_fields": [],
         "where_conditions": [{"field": "id", "op": "=", "value": "1"}]},
        {"table": "t_child", "action": "DELETE", "set_fields": [],
         "where_conditions": [{"field": "pid", "op": "=", "value": "1"}]},
    ]
    try:
        with patch.object(ops, "_load_table_schema", _fake_schema), \
             patch.object(ops, "_extract_mutation_ops", lambda _i: ops_parsed), \
             patch.object(ops, "delete_rows", _rec_delete), \
             patch("langgraph.types.interrupt",
                   lambda _p: {"decisions": [{"type": "approve"}]}):
            r = ops.mutate_natural("删除父表 id=1 与子表 pid=1 的记录")
        # 拓扑序：先删子表（引用方）后删主表（被引用方）
        assert call_order == ["t_child", "t_parent"], call_order
        drv = ops._get_driver()
        assert drv.query("SELECT COUNT(*) c FROM t_parent")[0]["c"] == 0
        assert drv.query("SELECT COUNT(*) c FROM t_child")[0]["c"] == 0
        assert r.data.get("ok") is True, r.data
        # effects 逐表收集（供目标达成检测复查）
        effects = r.data.get("effects_list") or []
        tables = {e.get("table") for e in effects}
        assert tables == {"t_child", "t_parent"}, effects
    finally:
        _teardown_scratch_db()
    print("OK - 多表端到端：合并闸批准→先子后主真实删库→effects 逐表收集")


def test_mutate_natural_multi_table_rejected():
    """多表删除用户拒绝：一条不删，如实返回未执行"""
    import core.data_ops as ops
    _setup_scratch_db()
    ops_parsed = [
        {"table": "t_parent", "action": "DELETE", "set_fields": [],
         "where_conditions": [{"field": "id", "op": "=", "value": "1"}]},
        {"table": "t_child", "action": "DELETE", "set_fields": [],
         "where_conditions": [{"field": "pid", "op": "=", "value": "1"}]},
    ]
    try:
        with patch.object(ops, "_load_table_schema", _fake_schema), \
             patch.object(ops, "_extract_mutation_ops", lambda _i: ops_parsed), \
             patch("langgraph.types.interrupt",
                   lambda _p: {"decisions": [{"type": "reject"}]}):
            r = ops.mutate_natural("删除父表 id=1 与子表 pid=1 的记录")
        assert r.data.get("ok") is False
        assert r.data.get("reason") == "nuke_rejected", r.data
        drv = ops._get_driver()
        assert drv.query("SELECT COUNT(*) c FROM t_parent")[0]["c"] == 1
        assert drv.query("SELECT COUNT(*) c FROM t_child")[0]["c"] == 1
    finally:
        _teardown_scratch_db()
    print("OK - 多表用户拒绝：一条不删，库内数据原样保留")


def test_tree_routing_in_mutate_natural():
    """mutate_natural 内部走树路由（20260805 统一）：删/改+记录→delete_data/edit_data，
    不再硬编码工具选择——树是路由唯一事实源"""
    import core.data_ops as ops
    from agent.router import get_tree
    # 先验证树本身：删/改+记录 → delete_data/edit_data
    assert get_tree().route("删", "记录", "") == "delete_data"
    assert get_tree().route("改", "记录", "") == "edit_data"

    route_calls: list[tuple] = []
    real_route = get_tree().route
    def _track(bk, dk, ct=""):
        route_calls.append((bk, dk, ct))
        return real_route(bk, dk, ct)

    _setup_scratch_db()
    ops_parsed = [
        {"table": "t_parent", "action": "DELETE", "set_fields": [],
         "where_conditions": [{"field": "id", "op": "=", "value": "1"}]},
    ]
    try:
        with patch.object(ops, "_load_table_schema", _fake_schema), \
             patch.object(ops, "_extract_mutation_ops", lambda _i: ops_parsed), \
             patch("agent.router.get_tree") as mock_gt, \
             patch("core.tool_registry._nuke_confirmed", return_value=True):
            mock_gt.return_value.route = _track
            r = ops.mutate_natural("删除 t_parent id=1 的记录")
        assert route_calls == [("删", "记录", "")], \
            f"应经树路由（删+记录）: {route_calls}"
        assert r.data.get("ok") is True, r.data
        # 库内真实删除（经 execute_tool → delete_data handler → delete_rows）
        drv = ops._get_driver()
        assert drv.query("SELECT COUNT(*) c FROM t_parent")[0]["c"] == 0
    finally:
        _teardown_scratch_db()
    print("OK - 树路由：mutate_natural 内部走树（删+记录→delete_data），不再硬编码")


if __name__ == "__main__":
    test_describe_table_mutation_impact()
    test_topo_sort_child_first()
    test_multi_ops_confirmed_merged_card()
    test_mutate_natural_multi_table_e2e()
    test_mutate_natural_multi_table_rejected()
    test_tree_routing_in_mutate_natural()
    print("\n层 29 全绿")
