"""统一循环真实 LLM 端到端实证（迭代 2.1/2.2 走查留证）

三条真实路径（handover P0）+ 跨进程提示（2.2）：
  A. 单条 UPDATE：全图 → agent_run → mutate_data → 唯一候选直执行 → 库内生效
  B. 多条 UPDATE：挂起（mutation_pending）→ 用户「确认」→ 结算执行 → 库内生效
  C. 含「暂存/安装」的查询不被写操作关键词劫持（库零改动、无挂起产生）
  D. 无挂起态的纯「确认」→ 显式提示会话已重置（2.2 跨进程场景，不落入 LLM 编造）

运行：cd data-engine && python scripts/verify_unified_loop_e2e.py
依赖：config/.env 的 AI_API_KEY（真实 LLM 全链路）；独立刮削库 db/test_unified_loop.db
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DS_FIXTURE = os.path.join("tests", "fixtures", "datasources_unified_loop.yml")
DB_PATH = os.path.join("db", "test_unified_loop.db")

EVIDENCE = []


def ev(tag, msg):
    line = f"[{tag}] {msg}"
    print(line)
    EVIDENCE.append(line)


def _setup():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE t_demo(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE, name TEXT, price FLOAT, amount INTEGER)""")
    conn.execute("INSERT INTO t_demo(code,name,price,amount) VALUES('U1','唯一件',5.0,50)")
    for i in range(3):
        conn.execute(
            f"INSERT INTO t_demo(code,name,price,amount) VALUES('B{i}','批量件{i}',10.0,{i})")
    conn.commit()
    conn.close()
    from core.datasource_manager import DataSourceManager
    DataSourceManager.reset_instance()
    DataSourceManager().load_config(DS_FIXTURE)
    import core.data_ops as _ops
    _ops._federated_driver = None


def _teardown():
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


def _rows(where=""):
    from core.data_ops import _get_driver
    sql = "SELECT * FROM t_demo" + (f" WHERE {where}" if where else "") + " ORDER BY id"
    return _get_driver().query(sql)


def _pending():
    from core.context import get_context
    return get_context().get("mutation_pending")


def main():
    _setup()
    try:
        from agent.open_layer.graph import run_open_agent

        # ── 路径 A：单条 UPDATE 直执行 ──
        ev("A.in", "把 t_demo 表中 code 为 U1 的记录的 price 改成 66")
        ans_a = run_open_agent("把 t_demo 表中 code 为 U1 的记录的 price 改成 66")
        ev("A.out", ans_a.replace(chr(10), " ")[:200])
        row = _rows("code='U1'")[0]
        assert row["price"] == 66.0, f"A 库内断言失败: {row}"
        assert _pending() is None, "A 单候选不应产生挂起"
        ev("A.assert", "OK price=66.0 库内生效，无挂起残留")

        # ── 路径 B：多条挂起 → 确认执行 ──
        ev("B.in", "把 t_demo 表中 amount 小于 10 的记录的 price 改成 1")
        ans_b1 = run_open_agent("把 t_demo 表中 amount 小于 10 的记录的 price 改成 1")
        ev("B.out1", ans_b1.replace(chr(10), " ")[:200])
        p = _pending()
        assert p is not None, "B 多候选应产生 mutation_pending 挂起"
        ev("B.pending", f"挂起内容: action={p.get('action')} table={p.get('table')} count={p.get('count')}")
        before = [(r["code"], r["price"]) for r in _rows("code LIKE 'B%'")]
        assert all(pr == 10.0 for _, pr in before), f"B 挂起期间不得有写入: {before}"
        ev("B.assert1", "OK 挂起期间库零改动")
        ev("B.in2", "确认")
        ans_b2 = run_open_agent("确认")
        ev("B.out2", ans_b2.replace(chr(10), " ")[:200])
        after = _rows("code LIKE 'B%'")
        assert all(r["price"] == 1.0 for r in after) and len(after) == 3, \
            f"B 确认后 3 条应全改: {after}"
        assert _pending() is None, "B 结算后挂起应清空"
        ev("B.assert2", "OK 「确认」后 3 条全改 price=1.0，挂起已消费")

        # ── 路径 C：含「暂存/安装」查询不被劫持 ──
        snapshot = [(r["code"], r["price"], r["amount"]) for r in _rows()]
        ev("C.in", "暂存量 amount 大于等于 0 的记录有多少条，顺便看下有没有已安装未入库的")
        ans_c = run_open_agent("暂存量 amount 大于等于 0 的记录有多少条，顺便看下有没有已安装未入库的")
        ev("C.out", ans_c.replace(chr(10), " ")[:200])
        now = [(r["code"], r["price"], r["amount"]) for r in _rows()]
        assert now == snapshot, f"C 查询路径库零改动: {now} != {snapshot}"
        assert _pending() is None, "C 查询不得产生写挂起"
        ev("C.assert", "OK 查询不被「暂存/安装」劫持：库零改动、无挂起")

        # ── D. 无挂起纯确认 → 会话重置显式提示（2.2）──
        ev("D.in", "确认")
        ans_d = run_open_agent("确认")
        ev("D.out", ans_d.replace(chr(10), " ")[:200])
        assert "没有待确认" in ans_d and "重新发起" in ans_d, f"D 应显式提示会话重置: {ans_d}"
        ev("D.assert", "OK 无挂起纯确认 → 显式提示（不落入 LLM 编造）")

        ev("DONE", "迭代 2.1/2.2 四路径实证全部通过")
    finally:
        _teardown()


if __name__ == "__main__":
    main()
