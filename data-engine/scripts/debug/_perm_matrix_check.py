"""权限矩阵全量验收——UI 勾选 → permissions.yml → 双栈拦截 的穷举验证

覆盖（权限矩阵全量夯实，20260804）：
- 数据源级：read_only / frozen / no_access / custom-deny 四语义 × 六操作
  （产品模式只有 full/read_only/custom——mgmt API 的 _MODES 校验冻结：
   frozen 语义 = custom+deny[ddl,drop]；no_access 语义 = custom+allow[] 空白名单）
- 表级：read_only（只读表）、custom-deny 单操作（insert/update/delete/ddl/drop/query）
- 列级：query 屏蔽+显式拒绝 / update 写禁 / ddl 禁 / drop 禁
- 壳继承：库级壳禁止向下继承；表级显式 allow 重新放开
- 裸 SQL 护栏：ContractDriver.execute / FederatedDriver.execute 的 ALTER/UPDATE 裸语句
- 双栈：聊天 DML 栈（FederatedDriver）+ schema/Steward DDL 栈（ContractDriver）

注意：
- edit_data/delete_data 工具在进程内无人审上下文会被人审闸先拦（生产环境批准后才会
  撞到权限层），故 update/delete 用 FederatedDriver 直测——权限收口正在该层。
- 测试表 perm_probe / perm_probe2 为本脚本专用夹具，结束自动清理；
  permissions.yml 结束恢复 default full 空规则。

用法：python scripts/_perm_matrix_check.py
退出码：0=全绿，1=有失败项
"""
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MGMT = "http://127.0.0.1:2025"
# X-API-Key 系统通道已废除（20260903）——测试专用用户通道：
# 用 MGMT_TEST_USER / MGMT_TEST_PASS 指定的测试 admin 账号登录换 Bearer
# （账号由管理员预先创建，见 tests/_mgmt_auth.py 头部说明）
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from _mgmt_auth import auth_headers  # noqa: E402

_headers_cache: dict = {"h": None}


def _auth() -> dict:
    if _headers_cache["h"] is None:
        _headers_cache["h"] = auth_headers(MGMT)
    return _headers_cache["h"]


PROBE, PROBE2 = "perm_probe", "perm_probe2"

CLEAN = {"default": "full", "datasources": {}, "roles": {}}

_results = []  # (group, case, expect, actual, ok)


def put_rules(rules):
    req = urllib.request.Request(
        f"{MGMT}/api/permissions",
        data=json.dumps({"rules": rules}).encode(),
        headers={"Content-Type": "application/json", **_auth()},
        method="PUT")
    resp = json.loads(urllib.request.urlopen(req).read())
    assert resp.get("ok"), f"规则写入失败: {resp}"
    time.sleep(0.05)  # 给 ConfigHub mtime 热读留余量


def fixture_sql(sql, params=()):
    from config.settings import settings
    conn = sqlite3.connect(settings.SQLITE_DB_PATH)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.fetchall()
    finally:
        conn.close()


def setup():
    fixture_sql(f"DROP TABLE IF EXISTS {PROBE}")  # 防上次中断残留旧结构（列不齐）
    fixture_sql(f"CREATE TABLE {PROBE} (id INTEGER PRIMARY KEY, a TEXT, b TEXT, c TEXT)")
    fixture_sql(f"INSERT INTO {PROBE} (a, b, c) VALUES ('pa', 'pb', 'pc')")
    fixture_sql(f"CREATE TABLE IF NOT EXISTS {PROBE2} (id INTEGER PRIMARY KEY, x TEXT)")
    put_rules(CLEAN)


def teardown():
    put_rules(CLEAN)
    fixture_sql(f"DROP TABLE IF EXISTS {PROBE}")
    fixture_sql(f"DROP TABLE IF EXISTS {PROBE2}")
    n = fixture_sql("SELECT COUNT(*) FROM quota_items")[0][0]
    print(f"\n环境恢复：规则=default full，夹具表已删，quota_items={n} 行（应 40）")


# ── 操作执行器（每种操作取生产真实路径的最高层）──

def _tool(name, **kw):
    from core.tool_registry import execute_tool
    return execute_tool(name, **kw)


def _fed():
    from core.data_ops import _get_driver
    return _get_driver()


def _contract():
    from core.schema_manager import get_driver  # Steward()._get_driver → ContractDriver
    return get_driver()


def op_query():        return _tool("query", table=PROBE, page_size=1)
def op_query_on_b():   return _tool("query", table=PROBE, page_size=1,
                                    conditions=json.dumps([{"field": "b", "op": "=", "value": "pb"}]))
def op_query_on_B():   return _fed().query(f"SELECT B FROM {PROBE}")  # 大写显式引用（修复 a 回归）
def op_insert():       return _tool("insert_data", table=PROBE, data=json.dumps({"a": "ia", "b": "ib"}))
def op_insert_ab():    return op_insert()
def op_insert_a_only(): return _tool("insert_data", table=PROBE, data=json.dumps({"a": "ia"}))
def op_update_b():     return _fed().update(PROBE, "b='ux'", "id=1")
def op_update_a():     return _fed().update(PROBE, "a='ux'", "id=1")
def op_delete():       return _fed().delete(PROBE, "id=999")  # 不存在 id：权限先判，放行则删 0 行
def op_add_col():      return _contract().add_column(PROBE, "tmpc", "TEXT")
def op_modify_b():     return _contract().modify_column(PROBE, "b", "TEXT")
def op_modify_tmpc():  return _contract().modify_column(PROBE, "tmpc", "TEXT")
def op_drop_col_b():   return _contract().drop_column(PROBE, "b")
def op_drop_col_tmpc(): return _contract().drop_column(PROBE, "tmpc")
def op_drop_table2():  return _contract().drop_table(PROBE2)
def op_exec_alter():   return _contract().execute(f"ALTER TABLE {PROBE} ADD COLUMN sneaky TEXT")
def op_exec_update():  return _fed().execute(f"UPDATE {PROBE} SET a='raw' WHERE id=1")


def allowed(fn):
    """执行并判定是否被权限拦截。返回 True=放行，False=权限拒绝。"""
    try:
        r = fn()
    except Exception as e:
        if "权限不足" in str(e) or "权限" in type(e).__name__:
            return False
        raise
    s = str(r)
    if "权限不足" in s:
        return False
    return True


def case(group, name, rules, fn, expect_allow, note=""):
    """一个矩阵用例：设规则 → 执行 → 断言 放行/拦截 是否符合预期"""
    put_rules(rules)
    ok_perm = allowed(fn)
    expect_txt = "放行" if expect_allow else "拦截"
    passed = (ok_perm == expect_allow)
    _results.append((group, name, expect_txt, "放行" if ok_perm else "拦截", passed))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}：预期{expect_txt}，实际{'放行' if ok_perm else '拦截'}"
          + (f"（{note}）" if note else ""))
    return passed


def ds_rules(mode=None, deny=None, allow=None, tables=None):
    node = {}
    if mode: node["mode"] = mode
    if deny is not None: node["deny"] = deny
    if allow is not None: node["allow"] = allow
    if tables is not None: node["tables"] = tables
    return {"default": "full", "roles": {}, "datasources": {"primary": node}}


def tbl_rules(tnode):
    return ds_rules(tables={PROBE: tnode})


def tbl2_rules(tnode):
    """表级规则挂 PROBE2（drop_table 用例的操作对象是 PROBE2）"""
    return ds_rules(tables={PROBE2: tnode})


def col_rules(deny):
    return tbl_rules({"columns": {"b": {"deny": deny}}})


def main():
    import agent.tools  # noqa: F401 —— 触发内置工具注册，否则 execute_tool 全返回"未知工具"
    setup()
    print("=" * 70)
    print("权限矩阵全量验收（双栈 + 裸 SQL 护栏）")
    print("=" * 70)
    try:
        print("\n[G1] 数据源级模式")
        case("G1", "read_only: query 放行", ds_rules("read_only"), op_query, True)
        case("G1", "read_only: insert 拦截", ds_rules("read_only"), op_insert, False)
        case("G1", "read_only: update 拦截(联邦栈)", ds_rules("read_only"), op_update_a, False)
        case("G1", "read_only: delete 拦截(联邦栈)", ds_rules("read_only"), op_delete, False)
        case("G1", "read_only: add_column 拦截(契约栈)", ds_rules("read_only"), op_add_col, False)
        case("G1", "read_only: drop_table 拦截(契约栈)", ds_rules("read_only"), op_drop_table2, False)
        # frozen 语义（DML 放行 + DDL/DROP 拦截）= custom+deny[ddl,drop]
        case("G1", "frozen: insert 放行", ds_rules("custom", deny=["ddl", "drop"]), op_insert, True)
        case("G1", "frozen: delete 放行", ds_rules("custom", deny=["ddl", "drop"]), op_delete, True)
        case("G1", "frozen: add_column 拦截", ds_rules("custom", deny=["ddl", "drop"]), op_add_col, False)
        case("G1", "frozen: drop_table 拦截", ds_rules("custom", deny=["ddl", "drop"]), op_drop_table2, False)
        # no_access 语义（全禁含 query）= custom+allow[] 空白名单 fail-closed
        case("G1", "no_access: query 拦截", ds_rules("custom", allow=[]), op_query, False)
        case("G1", "no_access: insert 拦截", ds_rules("custom", allow=[]), op_insert, False)
        case("G1", "no_access: add_column 拦截", ds_rules("custom", allow=[]), op_add_col, False)
        case("G1", "custom deny[ddl]: add_column 拦截", ds_rules("custom", deny=["ddl"]), op_add_col, False)
        case("G1", "custom deny[ddl]: insert 放行", ds_rules("custom", deny=["ddl"]), op_insert, True)

        print("\n[G2] 表级规则")
        case("G2", "read_only表: query 放行", tbl_rules({"mode": "read_only"}), op_query, True)
        case("G2", "read_only表: insert 拦截", tbl_rules({"mode": "read_only"}), op_insert, False)
        case("G2", "read_only表: update 拦截", tbl_rules({"mode": "read_only"}), op_update_a, False)
        case("G2", "read_only表: delete 拦截", tbl_rules({"mode": "read_only"}), op_delete, False)
        case("G2", "read_only表: add_column 拦截", tbl_rules({"mode": "read_only"}), op_add_col, False)
        case("G2", "read_only表: drop_table 拦截", tbl2_rules({"mode": "read_only"}), op_drop_table2, False)
        case("G2", "deny[insert]: insert 拦截", tbl_rules({"mode": "custom", "deny": ["insert"]}), op_insert, False)
        case("G2", "deny[insert]: update 放行", tbl_rules({"mode": "custom", "deny": ["insert"]}), op_update_a, True)
        case("G2", "deny[update]: update 拦截", tbl_rules({"mode": "custom", "deny": ["update"]}), op_update_a, False)
        case("G2", "deny[update]: insert 放行", tbl_rules({"mode": "custom", "deny": ["update"]}), op_insert, True)
        case("G2", "deny[delete]: delete 拦截", tbl_rules({"mode": "custom", "deny": ["delete"]}), op_delete, False)
        case("G2", "deny[delete]: update 放行", tbl_rules({"mode": "custom", "deny": ["delete"]}), op_update_a, True)
        case("G2", "deny[ddl]: add_column 拦截", tbl_rules({"mode": "custom", "deny": ["ddl"]}), op_add_col, False)
        case("G2", "deny[ddl]: modify_column 拦截", tbl_rules({"mode": "custom", "deny": ["ddl"]}), op_modify_b, False)
        case("G2", "deny[ddl]: insert 放行", tbl_rules({"mode": "custom", "deny": ["ddl"]}), op_insert, True)
        case("G2", "deny[drop]: drop_table 拦截", tbl2_rules({"mode": "custom", "deny": ["drop"]}), op_drop_table2, False)
        case("G2", "deny[drop]: add_column 放行", tbl_rules({"mode": "custom", "deny": ["drop"]}), op_add_col, True)
        case("G2", "deny[query]: query 拦截", tbl_rules({"mode": "custom", "deny": ["query"]}), op_query, False)
        case("G2", "deny[query]: insert 放行", tbl_rules({"mode": "custom", "deny": ["query"]}), op_insert, True)

        print("\n[G3] 列级规则（列 b）")
        put_rules(col_rules(["query"]))
        r = _tool("query", table=PROBE, page_size=1)
        masked_ok = "b =" not in str(r)
        _results.append(("G3", "deny[query]: SELECT 结果屏蔽 b 列", "屏蔽", "屏蔽" if masked_ok else "未屏蔽", masked_ok))
        print(f"  [{'PASS' if masked_ok else 'FAIL'}] deny[query]: SELECT 结果屏蔽 b 列")
        case("G3", "deny[query]: 显式按 b 查 拦截", col_rules(["query"]), op_query_on_b, False)
        case("G3", "deny[query]: 大写 SELECT B 同样拦截", col_rules(["query"]), op_query_on_B, False)
        put_rules(col_rules(["query"]))
        r = _fed().query(
            f"SELECT * FROM {PROBE} p1 WHERE EXISTS "
            f"(SELECT * FROM {PROBE} p2 WHERE p2.id = p1.id)")
        subq_ok = bool(r) and "b" not in r[0] and "a" in r[0]
        _results.append(("G3", "deny[query]: 子查询星号同样屏蔽 b", "屏蔽", "屏蔽" if subq_ok else "未屏蔽", subq_ok))
        print(f"  [{'PASS' if subq_ok else 'FAIL'}] deny[query]: 子查询星号同样屏蔽 b")
        case("G3", "deny[update]: update b 拦截", col_rules(["update"]), op_update_b, False)
        case("G3", "deny[update]: update a 放行", col_rules(["update"]), op_update_a, True)
        case("G3", "deny[update]: insert 含 b 拦截(写列约定)", col_rules(["update"]), op_insert_ab, False)
        case("G3", "deny[update]: insert 仅 a 放行", col_rules(["update"]), op_insert_a_only, True)
        case("G3", "deny[ddl]: modify_column b 拦截", col_rules(["ddl"]), op_modify_b, False)
        case("G3", "deny[ddl]: modify_column 他列 放行", col_rules(["ddl"]), op_modify_tmpc, True)
        case("G3", "deny[drop]: drop_column b 拦截", col_rules(["drop"]), op_drop_col_b, False)
        case("G3", "deny[drop]: drop_column 他列 放行", col_rules(["drop"]), op_drop_col_tmpc, True)

        print("\n[G4] 壳继承")
        case("G4", "库壳 deny[delete]: delete 拦截(继承)", ds_rules("custom", deny=["delete"]), op_delete, False)
        case("G4", "库壳deny+表显式allow: delete 放行",
             ds_rules("custom", deny=["delete"], tables={PROBE: {"mode": "custom", "allow": ["delete"]}}),
             op_delete, True)

        print("\n[G5] 裸 SQL 护栏（execute 透传口）")
        case("G5", "契约栈 execute ALTER 在 deny[ddl] 下拦截",
             tbl_rules({"mode": "custom", "deny": ["ddl"]}), op_exec_alter, False)
        case("G5", "契约栈 execute ALTER 在默认 full 下放行", CLEAN, op_exec_alter, True)
        case("G5", "联邦栈 execute UPDATE 在 deny[update] 下拦截",
             tbl_rules({"mode": "custom", "deny": ["update"]}), op_exec_update, False)
        case("G5", "联邦栈 execute UPDATE 在默认 full 下放行", CLEAN, op_exec_update, True)

        print("\n[G6] 默认 full 回归（无规则全部放行）")
        case("G6", "full: query/insert/update/delete 放行", CLEAN, op_query, True)
        case("G6", "full: delete 放行", CLEAN, op_delete, True)
        case("G6", "full: add_column 放行", CLEAN, op_add_col, True)
    finally:
        teardown()

    fails = [r for r in _results if not r[4]]
    print("\n" + "=" * 70)
    print(f"矩阵结果：{len(_results) - len(fails)}/{len(_results)} 通过")
    if fails:
        print("失败项：")
        for g, n, e, a, _ in fails:
            print(f"  [{g}] {n}：预期{e} 实际{a}")
        sys.exit(1)
    print("=== ALL PERMISSION MATRIX CASES PASSED ===")


if __name__ == "__main__":
    main()
