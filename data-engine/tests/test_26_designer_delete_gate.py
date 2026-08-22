"""层 31：表设计界面删除确认弹窗的后端预检（20260805）

画布删表/删外键边从无确认/一行 confirm 升级为影响面弹窗，
弹窗数据来自 SchemaGraphService 的两个只读预检方法：

1. delete_table_precheck：行数 + 正向外键 + 反向引用（含实际引用行数）
2. delete_relationship_precheck：受影响行数（from_table 中该列非空的行数）
3. 不存在的表/外键 → ok=False（前端 fail-closed，仅允许取消）
"""
import sys; sys.path.insert(0, ".")
from unittest.mock import MagicMock, patch


def _make_service(fk_rows, table_meta):
    """构造打桩后的 SchemaGraphService：MetaDB/Ladybug/Steward 全部隔离"""
    from core.graph.meta_db import MetaDB
    from core.graph.ladybug_store import LadybugStore

    meta = MagicMock()
    meta.get_table.side_effect = lambda name: table_meta.get(name)
    meta.get_all_foreign_keys.return_value = fk_rows
    meta.get_foreign_keys.side_effect = lambda t: [
        {"column": r["column"], "references": r["ref_table"],
         "ref_column": r["ref_column"], "constraint_name": "fk"}
        for r in fk_rows if r["table_name"] == t
    ]

    # 驱动：table_exists 全真；query 按目标表回行数（t1=3, t2=7, 其他=0）
    drv = MagicMock()
    drv.table_exists.return_value = True

    def _query(sql, *_a):
        for name, n in (("t1", 3), ("t2", 7)):
            if f'"{name}"' in sql:
                return [{"c": n}]
        return [{"c": 0}]
    drv.query.side_effect = _query

    # patch 须在服务方法调用期间持续生效（with 块退出即失效）——
    # 本测试独立进程运行，start 后不 stop 不影响其他层
    patch.object(MetaDB, "get_instance", return_value=meta).start()
    patch.object(LadybugStore, "init_schema", return_value=None).start()
    patch("core.steward.Steward._get_driver", return_value=drv).start()
    from core.graph.schema_graph_service import SchemaGraphService
    svc = SchemaGraphService.__new__(SchemaGraphService)
    svc._meta = meta
    return svc, drv


FK_ROWS = [
    {"table_name": "t1", "column": "quota_id", "ref_table": "t2",
     "ref_column": "id"},
]
TABLE_META = {
    "t1": {"name": "t1", "datasource": "primary"},
    "t2": {"name": "t2", "datasource": "primary"},
}


def test_delete_table_precheck():
    """删表预检：行数/正向外键/反向引用行数三段齐全"""
    svc, _ = _make_service(FK_ROWS, TABLE_META)
    # t2 被 t1.quota_id 引用
    r = svc.delete_table_precheck("t2")
    assert r["ok"] is True and r["table"] == "t2"
    assert r["row_count"] == 7, r          # t2 自身行数
    assert r["outgoing_fks"] == [], r      # t2 无外键
    assert len(r["referenced_by"]) == 1, r
    ref = r["referenced_by"][0]
    assert ref["table"] == "t1" and ref["column"] == "quota_id"
    assert ref["rows"] == 3, ref           # t1 中 quota_id 非空的行数

    # t1：有正向外键、无人引用
    r2 = svc.delete_table_precheck("t1")
    assert r2["row_count"] == 3
    assert r2["outgoing_fks"] == [
        {"column": "quota_id", "references": "t2", "ref_column": "id"}]
    assert r2["referenced_by"] == []
    print("OK - 删表预检：行数/正向FK/反向引用行数三段齐全")


def test_delete_table_precheck_missing_table():
    """表不存在 → ok=False（前端 fail-closed 仅允许取消）"""
    svc, _ = _make_service(FK_ROWS, TABLE_META)
    r = svc.delete_table_precheck("ghost")
    assert r["ok"] is False and "不存在" in r["message"]
    print("OK - 删表预检：不存在的表 ok=False")


def test_delete_relationship_precheck():
    """删外键预检：返回引用目标与受影响行数"""
    svc, _ = _make_service(FK_ROWS, TABLE_META)
    r = svc.delete_relationship_precheck("t1", "quota_id")
    assert r["ok"] is True
    assert r["references"] == "t2" and r["ref_column"] == "id"
    assert r["affected_rows"] == 3, r      # t1 中 quota_id 非空行数
    print("OK - 删外键预检：引用目标+受影响行数正确")


def test_delete_relationship_precheck_missing():
    """外键关系不存在 → ok=False"""
    svc, _ = _make_service(FK_ROWS, TABLE_META)
    r = svc.delete_relationship_precheck("t1", "ghost_col")
    assert r["ok"] is False and "不存在" in r["message"]
    print("OK - 删外键预检：不存在的关系 ok=False")


if __name__ == "__main__":
    test_delete_table_precheck()
    test_delete_table_precheck_missing_table()
    test_delete_relationship_precheck()
    test_delete_relationship_precheck_missing()
    print("\n层 31 全绿")
