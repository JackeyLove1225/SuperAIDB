"""层 23：目标达成检测独立模块（core/goal_verify，迭代 4.5）

验证器从写操作 effects 提取目标，独立发起复查查询（走驱动标准 query，
自身过权限），比对数据库真实状态与 AI 声明。覆盖：

1. 删后复查：DELETE 后目标 id 不存在 → 通过；仍存在 → 复查不符
2. 改后对账：UPDATE 后记录存在 + 字段值与声明值一致 → 通过；值不符 → 复查不符
3. 批量行数：声明 affected 与目标集规模不一致 → 复查不符
4. 规则关闭态：总开关关闭 → 钩子不产生报告；单操作规则关闭 → 显式 skipped
5. 权限受限：验证器自身查询被权限拒绝 → verified=None + 显式原因（不装死）
6. 插入复查：INSERT values 等值对账
7. 挂起态：pending effects（人审未确认）不复查
8. 钩子集成：attach 把报告挂到 ToolResult 的 text/data 双通道
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.goal_verify.hooks import after_write, attach
from core.goal_verify.report import VerifyReport
from core.goal_verify.verifier import verify
from core.tool_result import ToolResult


def _mk_driver():
    """临时 SQLite 库 + 演示表，返回（driver, 已插入的 id 列表）"""
    from core.drivers.sqlite_driver import SqliteDriver
    tmp = tempfile.mkdtemp()
    drv = SqliteDriver(os.path.join(tmp, "t.db"))
    drv.conn.execute(
        "CREATE TABLE t_demo (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " code TEXT, name TEXT, price FLOAT)")
    for code, name, price in [("A1", "水泥", 12.5), ("A2", "钢筋", 30.0), ("A3", "砂石", 8.0)]:
        drv.conn.execute("INSERT INTO t_demo (code, name, price) VALUES (?, ?, ?)",
                         (code, name, price))
    drv.conn.commit()
    ids = [r["id"] for r in drv.query("SELECT id FROM t_demo ORDER BY id")]
    return drv, ids


def test_delete_verify_pass_and_mismatch():
    """删后复查：目标不存在→通过；仍存在→不符（'声明删 N 条但复查仍在'）"""
    drv, ids = _mk_driver()
    # 真删前 2 行
    drv.conn.execute(f"DELETE FROM t_demo WHERE id IN ({ids[0]},{ids[1]})")
    drv.conn.commit()
    r = verify({"table": "t_demo", "action": "DELETE",
                "affected": 2, "affected_ids": ids[:2]}, driver=drv)
    assert r.verified is True, f"删后复查应通过: {r.mismatch_detail}"

    # 假 effects：声明删了 id3，但它仍在（模拟 AI 谎报/执行半截）
    r2 = verify({"table": "t_demo", "action": "DELETE",
                 "affected": 1, "affected_ids": [ids[2]]}, driver=drv)
    assert r2.verified is False, "目标仍存在应判复查不符"
    assert "仍有 1 条存在" in r2.mismatch_detail
    print("OK - 删后复查：通过/不符两态正确")


def test_update_verify_fields_match():
    """改后对账：记录存在 + 字段值对账（通过/不符两态）"""
    drv, ids = _mk_driver()
    drv.conn.execute(f"UPDATE t_demo SET price = 99.0 WHERE id = {ids[0]}")
    drv.conn.commit()
    r = verify({"table": "t_demo", "action": "UPDATE",
                "affected": 1, "affected_ids": [ids[0]],
                "changed_fields": ["price"],
                "expected_values": {"price": 99.0}}, driver=drv)
    assert r.verified is True, f"改后字段对账应通过: {r.mismatch_detail}"

    r2 = verify({"table": "t_demo", "action": "UPDATE",
                 "affected": 1, "affected_ids": [ids[0]],
                 "changed_fields": ["price"],
                 "expected_values": {"price": 55.0}}, driver=drv)
    assert r2.verified is False and "price" in r2.mismatch_detail, \
        f"声明值与库内不符应报差异: {r2.mismatch_detail}"

    # 记录不存在（声明更新的 id 已没了）
    r3 = verify({"table": "t_demo", "action": "UPDATE",
                 "affected": 1, "affected_ids": [9999]}, driver=drv)
    assert r3.verified is False and "仅 0 条存在" in r3.mismatch_detail
    print("OK - 改后对账：字段值/记录存在性三态正确")


def test_row_count_reconciliation():
    """批量行数：声明 affected 与目标集规模不符 → 复查不符"""
    drv, ids = _mk_driver()
    drv.conn.execute(f"DELETE FROM t_demo WHERE id IN ({ids[0]},{ids[1]},{ids[2]})")
    drv.conn.commit()
    r = verify({"table": "t_demo", "action": "DELETE",
                "affected": 2, "affected_ids": ids}, driver=drv)  # 声明 2 实际目标 3
    assert r.verified is False and "声明影响 2 行" in r.mismatch_detail
    print("OK - 批量行数对账：声明与目标集规模差异可检出")


def test_rules_disabled():
    """规则关闭态：总开关关闭钩子静默；单操作关闭显式 skipped"""
    drv, ids = _mk_driver()
    # 单操作规则关闭（显式 rules 注入）
    off_rules = {"DELETE": {"enabled": False, "checks": []}}
    r = verify({"table": "t_demo", "action": "DELETE",
                "affected": 1, "affected_ids": [ids[0]]}, driver=drv, rules=off_rules)
    assert r.verified is None and "已关闭" in r.skipped_reason

    # 总开关关闭：after_write 不产生任何报告（rules=None 走文件总开关，
    # 这里用 monkeypatch 模拟 enabled=false）
    import core.goal_verify.hooks as hooks
    orig = hooks.rules_enabled
    hooks.rules_enabled = lambda: False
    try:
        reports = after_write({"ok": True, "effects": {
            "table": "t_demo", "action": "DELETE", "affected": 1,
            "affected_ids": [ids[0]]}}, driver=drv)
        assert reports == [], "总开关关闭时不应产生复查报告"
    finally:
        hooks.rules_enabled = orig
    print("OK - 规则关闭态：总开关/单操作两级关闭行为正确")


def test_verifier_permission_limited():
    """权限受限：验证器自身查询被拒 → verified=None + 显式原因（不装死、不越权）"""
    from core.permission import PermissionDenied

    class _DeniedDriver:
        def query(self, sql):
            raise PermissionDenied("数据源对该角色禁止查询")

    r = verify({"table": "t_demo", "action": "DELETE",
                "affected": 1, "affected_ids": [1]}, driver=_DeniedDriver())
    assert r.verified is None, "权限受限应判无法复查而非通过/不符"
    assert "权限受限" in r.skipped_reason
    print("OK - 验证器自身权限受限：显式报告无法复查")


def test_insert_verify():
    """插入复查：values 等值对账（通过/不符两态）"""
    drv, ids = _mk_driver()
    r = verify({"table": "t_demo", "action": "INSERT", "affected": 1,
                "values": [{"code": "A1", "name": "水泥", "price": 12.5}]}, driver=drv)
    assert r.verified is True, f"插入复查应通过: {r.mismatch_detail}"

    r2 = verify({"table": "t_demo", "action": "INSERT", "affected": 1,
                 "values": [{"code": "ZZZ", "name": "不存在", "price": 1.0}]}, driver=drv)
    assert r2.verified is False and "复查不存在" in r2.mismatch_detail
    print("OK - 插入复查：values 等值对账两态正确")


def test_pending_effects_skipped():
    """挂起态：pending effects（人审未确认，操作未执行）不产生复查报告"""
    reports = after_write({"ok": True, "reason": "pending_confirm", "effects": {
        "table": "t", "action": "DELETE", "affected": 0,
        "candidate_ids": [1, 2], "pending": True, "selection_id": 7}},
        driver=_mk_driver()[0], rules={})
    assert reports == [], "挂起未执行的操作不应复查"
    # 失败结果同样不复查
    reports2 = after_write({"ok": False, "code": "VALIDATION"}, rules={})
    assert reports2 == []
    print("OK - 挂起态/失败态：不产生复查报告")


def test_attach_integration():
    """钩子集成：attach 把报告挂到 ToolResult 的 text 追加 + data['verify'] 双通道"""
    drv, ids = _mk_driver()
    drv.conn.execute(f"DELETE FROM t_demo WHERE id = {ids[0]}")
    drv.conn.commit()
    tr = ToolResult.ok("已从 t_demo 删除 1 条记录", table="t_demo", action="DELETE",
                       affected=1,
                       effects={"table": "t_demo", "action": "DELETE",
                                "affected": 1, "affected_ids": [ids[0]]})
    reports = attach(tr, driver=drv)
    assert len(reports) == 1 and reports[0].verified is True
    assert "✔ 已复查" in tr.text, "text 通道应追加复查呈现"
    assert tr.data["verify"][0]["verified"] is True, "data 通道应挂结构化报告"

    # 不符场景：声明删了实际仍在 → text 显式报差异
    tr2 = ToolResult.ok("已从 t_demo 删除 1 条记录", table="t_demo", action="DELETE",
                        affected=1,
                        effects={"table": "t_demo", "action": "DELETE",
                                 "affected": 1, "affected_ids": [ids[1]]})
    attach(tr2, driver=drv)
    assert "✘ 复查不符" in tr2.text
    print("OK - 钩子集成：text/data 双通道挂载正确")


if __name__ == "__main__":
    test_delete_verify_pass_and_mismatch()
    test_update_verify_fields_match()
    test_row_count_reconciliation()
    test_rules_disabled()
    test_verifier_permission_limited()
    test_insert_verify()
    test_pending_effects_skipped()
    test_attach_integration()
    print("\n✅ 层 23 全部通过：目标达成检测独立模块（删后复查/改后对账/行数对账/"
          "规则关闭/权限受限/插入复查/挂起跳过/钩子集成）")
