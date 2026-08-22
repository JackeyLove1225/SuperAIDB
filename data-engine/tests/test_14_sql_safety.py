"""层 14：SQL 注入安全回归测试

验证 T1.1 引入的 safe_* helper 函数和 SecurityContract.validate_identifier
能正确拦截各类 SQL 注入攻击。

覆盖 10 个注入场景：
  1. 合法标识符（基线）
  2. 分号注入（堆叠查询）
  3. UNION 注入
  4. 中文字符表名
  5. 空格注入
  6. 引号注入（提前闭合字符串）
  7. 注释注入（-- 注释掉后续 SQL）
  8. SQL 关键字作为表名
  9. 空字符串
  10. 超长标识符（>64 字符）

设计原则：
  - 纯单元测试，不依赖数据库或外部服务
  - 只测试 SecurityContract 和 helper 函数的校验逻辑
  - 每个 case 验证「合法的放行 + 非法的拦截」
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.crypto.connection import open_db

from core.contract.security_contract import (
    SecurityContract,
    is_valid_identifier,
    safe_table_sql,
    safe_column_sql,
    safe_index_sql,
    safe_pragma_arg,
    safe_savepoint_name,
)
from core.exceptions import SecurityError

pass_count = 0
fail_count = 0
errors = []


def check(name, condition, detail=""):
    global pass_count, fail_count
    if condition:
        pass_count += 1
        print(f"  PASS [{name}]")
    else:
        fail_count += 1
        errors.append(f"{name}: {detail}")
        print(f"  FAIL [{name}] {detail}")


def expect_security_error(name, func, *args, **kwargs):
    """断言 func(*args) 抛出 SecurityError"""
    global pass_count, fail_count
    try:
        func(*args, **kwargs)
        fail_count += 1
        errors.append(f"{name}: 未抛出 SecurityError")
        print(f"  FAIL [{name}] 未抛出 SecurityError（应被拦截）")
        return False
    except SecurityError:
        pass_count += 1
        print(f"  PASS [{name}]")
        return True
    except Exception as e:
        fail_count += 1
        errors.append(f"{name}: 抛出了 {type(e).__name__} 而非 SecurityError")
        print(f"  FAIL [{name}] 抛出了 {type(e).__name__} 而非 SecurityError")
        return False


# ──────────────────────────────────────────────
# 测试开始
# ──────────────────────────────────────────────

print()
print("=" * 60)
print("层 14：SQL 注入安全回归测试")
print("=" * 60)

# ── 场景 1：合法标识符（基线）──
print("\n=== 14.1 合法标识符（基线）===")
check("合法表名 users", is_valid_identifier("users"))
check("合法表名 _private", is_valid_identifier("_private"))
check("合法表名 user_logs_2024", is_valid_identifier("user_logs_2024"))
check("合法字段名 id", is_valid_identifier("id"))

result = safe_table_sql("users")
check("safe_table_sql 返回带引号", result == '"users"', f"got {result!r}")

result = safe_column_sql("name")
check("safe_column_sql 返回带引号", result == '"name"', f"got {result!r}")

result = safe_pragma_arg("users")
check("safe_pragma_arg 返回裸标识符", result == "users", f"got {result!r}")

result = safe_index_sql("idx_users_id")
check("safe_index_sql 返回带引号", result == '"idx_users_id"', f"got {result!r}")

result = safe_savepoint_name("sp1")
check("safe_savepoint_name 返回裸标识符", result == "sp1", f"got {result!r}")

# ── 场景 2：分号注入（堆叠查询）──
print("\n=== 14.2 分号注入（堆叠查询）===")
expect_security_error(
    "分号注入 table", safe_table_sql, "users; DROP TABLE users"
)
expect_security_error(
    "分号注入 column", safe_column_sql, "id; DELETE FROM users"
)
expect_security_error(
    "分号注入 validate", SecurityContract.validate_identifier, "x; SELECT 1", "表名"
)
check("分号注入 is_valid=False", not is_valid_identifier("users; DROP TABLE users"))

# ── 场景 3：UNION 注入 ──
print("\n=== 14.3 UNION 注入 ===")
expect_security_error(
    "UNION 注入 table", safe_table_sql, "users UNION SELECT * FROM passwords"
)
expect_security_error(
    "UNION 注入空格分隔", safe_table_sql, "users UNION SELECT 1"
)
check("UNION is_valid=False", not is_valid_identifier("users UNION SELECT 1"))

# ── 场景 4：中文字符表名 ──
print("\n=== 14.4 中文字符表名 ===")
expect_security_error("中文表名", safe_table_sql, "用户表")
expect_security_error("中文混合", safe_column_sql, "name用户")
check("中文 is_valid=False", not is_valid_identifier("用户表"))

# ── 场景 5：空格注入 ──
print("\n=== 14.5 空格注入 ===")
expect_security_error("含空格表名", safe_table_sql, "user table")
expect_security_error("前导空格", safe_table_sql, " users")
expect_security_error("后缀空格", safe_column_sql, "name ")
check("空格 is_valid=False", not is_valid_identifier("user table"))

# ── 场景 6：引号注入（提前闭合字符串）──
print("\n=== 14.6 引号注入 ===")
expect_security_error("双引号注入", safe_table_sql, 'users"-- ')
expect_security_error("单引号注入", safe_column_sql, "name'--")
expect_security_error(
    "引号截断", safe_table_sql, 'users" OR "1"="1'
)
check("引号 is_valid=False", not is_valid_identifier('users"-- '))

# ── 场景 7：注释注入 ──
print("\n=== 14.7 注释注入 ===")
expect_security_error("行注释--", safe_table_sql, "users--")
expect_security_error("块注释/*", safe_column_sql, "name/*")
expect_security_error(
    "注释后堆叠", safe_table_sql, "users--\nDROP TABLE users"
)
check("注释 is_valid=False", not is_valid_identifier("users--"))

# ── 场景 8：SQL 关键字作为表名 ──
print("\n=== 14.8 SQL 关键字作为表名 ===")
# SQL 关键字（SELECT/INSERT/UPDATE/DELETE 等）在格式上是合法标识符
# validate_identifier 只检查格式，不检查是否是保留字
# 这是设计决策：保留字检查由上层（如 _validate_table_name 的白名单校验）负责
check("SELECT 格式合法", is_valid_identifier("SELECT"))
check("INSERT 格式合法", is_valid_identifier("INSERT"))
check("TABLE 格式合法", is_valid_identifier("TABLE"))

# 但 safe_table_sql 仍然会加引号，SQLite 中引号包裹的保留字可用
result = safe_table_sql("SELECT")
check("safe_table_sql(SELECT) 返回引号", result == '"SELECT"', f"got {result!r}")

# ── 场景 9：空字符串 ──
print("\n=== 14.9 空字符串 ===")
expect_security_error("空字符串", safe_table_sql, "")
expect_security_error("None", safe_table_sql, None)
expect_security_error("空column", safe_column_sql, "")
check("空 is_valid=False", not is_valid_identifier(""))
check("None is_valid=False", not is_valid_identifier(None))

# ── 场景 10：超长标识符（>64 字符）──
print("\n=== 14.10 超长标识符（>64 字符）===")
long_name = "a" * 65  # 65 字符，超过 64 限制
expect_security_error("65字符表名", safe_table_sql, long_name)
expect_security_error("100字符字段", safe_column_sql, "b" * 100)
check("65字符 is_valid=False", not is_valid_identifier(long_name))

# 正好 64 字符应该通过
name_64 = "c" * 64
check("64字符 is_valid=True", is_valid_identifier(name_64))
result = safe_table_sql(name_64)
check("64字符 safe_table_sql通过", result == f'"{name_64}"', f"got {result!r}")

# ── 额外：数字开头（非法）──
print("\n=== 14.11 数字开头（非法）===")
expect_security_error("数字开头", safe_table_sql, "1users")
expect_security_error("纯数字", safe_column_sql, "123")
check("数字开头 is_valid=False", not is_valid_identifier("1users"))

# ── 额外：特殊字符 ──
print("\n=== 14.12 特殊字符 ===")
for char in ["$", "#", "@", "!", "%", "&", "*", "(", ")", "=", "+", "<", ">", "?"]:
    expect_security_error(
        f"特殊字符 {char}", safe_table_sql, f"users{char}"
    )


# ── 场景 13：build_where 注入防护（query 工具 conditions 路径）──
print("\n=== 14.13 build_where 注入防护（conditions 路径）===")
from core.condition_parser import build_where

def expect_value_error(name, func, *args, **kwargs):
    """断言 func(*args) 抛出 ValueError"""
    global pass_count, fail_count
    try:
        r = func(*args, **kwargs)
        fail_count += 1
        errors.append(f"{name}: 未抛出 ValueError（返回 {r!r}）")
        print(f"  FAIL [{name}] 未抛出 ValueError（返回 {r!r}）")
    except ValueError:
        pass_count += 1
        print(f"  PASS [{name}]")
    except Exception as e:
        fail_count += 1
        errors.append(f"{name}: 抛出了 {type(e).__name__} 而非 ValueError")
        print(f"  FAIL [{name}] 抛出了 {type(e).__name__} 而非 ValueError")

def expect_valid_where(name, cond_list):
    """断言 build_where 输出能通过 SecurityContract.validate_where"""
    global pass_count, fail_count
    try:
        w = build_where(cond_list)
        assert w.startswith(" WHERE "), f"输出缺 WHERE 前缀: {w!r}"
        SecurityContract.validate_where(w.strip()[6:])
        pass_count += 1
        print(f"  PASS [{name}]")
    except Exception as e:
        fail_count += 1
        errors.append(f"{name}: 合法条件被误拒: {e}")
        print(f"  FAIL [{name}] 合法条件被误拒: {e}")

# 已实证可利用的注入 payload：字段合法但值含闭合引号 + 注释
payload = [{"field": "name", "op": "=", "value": "x' OR 1=1 --"}]
w = build_where(payload)
check("注入值被转义（'' 双写）", "x'' OR 1=1 --" in w, f"got {w!r}")
expect_security_error(
    "注入 payload 被 validate_where 拦截",
    SecurityContract.validate_where, w.strip()[6:]
)

# 恶意字段名 / 恶意连接符 → build_where 直接拒绝
expect_value_error(
    "恶意字段名注入", build_where,
    [{"field": "name' OR 1=1 --", "op": "=", "value": "x"}]
)
expect_value_error(
    "字段名含空格", build_where,
    [{"field": "name OR 1=1", "op": "=", "value": "x"}]
)
expect_value_error(
    "恶意 link 连接符", build_where,
    [{"field": "a", "op": "=", "value": "1"},
     {"field": "b", "op": "=", "value": "2", "link": "OR 1=1 --"}]
)

# 合法条件必须仍然通过（中文值 / LIKE / 数值比较 / IN 列表 / 多条件）
expect_valid_where("中文值等值查询", [{"field": "name", "op": "=", "value": "张三"}])
expect_valid_where("LIKE 中文模糊", [{"field": "name", "op": "LIKE", "value": "%钢筋%"}])
expect_valid_where("数值比较 >=", [{"field": "price", "op": ">=", "value": "100"}])
expect_valid_where("IN 列表", [{"field": "unit", "op": "IN", "value": "m2,m3,项"}])
expect_valid_where("BETWEEN 区间", [{"field": "price", "op": "BETWEEN", "value": "10,20"}])
expect_valid_where("多条件 AND 组合", [
    {"field": "name", "op": "LIKE", "value": "%混凝土%"},
    {"field": "price", "op": "<=", "value": "500", "link": "AND"},
])
check("空条件返回空串", build_where([]) == "")
check("IS NULL 无值也合法", build_where([{"field": "name", "op": "IS NULL"}]) == " WHERE name IS NULL")

# ── 场景 14：db_chat 四子句校验（fields/where/order_by/group_by）──
print("\n=== 14.14 db_chat 子句安全校验 ===")
from core.data_ops import validate_select_fields, validate_order_by, validate_group_by

# 合法 SELECT 字段
check("select * 合法", validate_select_fields("*"))
check("select 单字段合法", validate_select_fields("name"))
check("select 多字段合法", validate_select_fields("quota_id, name, price"))
check("select AS 别名合法", validate_select_fields("name AS item_name"))
check("select 聚合合法", validate_select_fields("COUNT(*)"))
check("select 聚合+别名合法", validate_select_fields("SUM(price) AS total"))
check("select table.column 合法", validate_select_fields("t1.name, t2.price"))
# 注入 SELECT 字段
check("select 注入堆叠被拒", not validate_select_fields("name; DROP TABLE users"))
check("select 注释注入被拒", not validate_select_fields("name FROM users--"))
check("select 引号注入被拒", not validate_select_fields("name' --"))
check("select 子查询注入被拒", not validate_select_fields("(SELECT password FROM users)"))
check("select 空串被拒", not validate_select_fields(""))

# 合法 ORDER BY
check("order_by 单字段合法", validate_order_by("price"))
check("order_by DESC 合法", validate_order_by("price DESC"))
check("order_by 多字段合法", validate_order_by("unit ASC, price DESC"))
# 注入 ORDER BY
check("order_by 堆叠注入被拒", not validate_order_by("price; DROP TABLE users"))
check("order_by 注释注入被拒", not validate_order_by("price DESC--"))
check("order_by 函数注入被拒", not validate_order_by("(CASE WHEN 1=1 THEN price END)"))

# 合法 GROUP BY
check("group_by 单字段合法", validate_group_by("unit"))
check("group_by 多字段合法", validate_group_by("unit, quota_id"))
# 注入 GROUP BY
check("group_by 注释注入被拒", not validate_group_by("unit--"))
check("group_by HAVING 拼接被拒", not validate_group_by("unit HAVING 1=1"))

# WHERE 子句（AI FC 裸拼路径）经 validate_where 拦截
expect_security_error("where 注释注入", SecurityContract.validate_where, "name='x' OR 1=1 --")
expect_security_error("where 堆叠注入", SecurityContract.validate_where, "1=1; DROP TABLE users")


# ── 场景 15：标识符正则统一标准（T1.3 收敛）──
print("\n=== 14.15 标识符正则统一（security_contract 唯一标准）===")
from core.contract.security_contract import IDENTIFIER_RE
from core.drivers.mysql_driver import _validate_identifier as mysql_validate_identifier

# 边界：63 / 64 字符放行，65 字符拒绝
check("63字符 is_valid=True", is_valid_identifier("d" * 63))
check("64字符 is_valid=True", is_valid_identifier("d" * 64))
check("65字符 is_valid=False", not is_valid_identifier("d" * 65))
check("IDENTIFIER_RE 与 is_valid_identifier 一致(64)",
      bool(IDENTIFIER_RE.match("d" * 64)))
check("IDENTIFIER_RE 与 is_valid_identifier 一致(65)",
      not IDENTIFIER_RE.match("d" * 65))

# 下划线开头放行 / 数字开头拒绝 / 中文拒绝 / 注入字符拒绝
check("下划线开头 _t1 合法", is_valid_identifier("_t1"))
check("数字开头 1t 非法", not is_valid_identifier("1t"))
check("中文 表 非法", not is_valid_identifier("表"))
check("注入字符反引号 非法", not is_valid_identifier("a`b"))
check("注入字符括号 非法", not is_valid_identifier("a(b)"))

# mysql_driver 复用同一标准（此前本地正则无长度上限）
check("mysql 64字符通过", mysql_validate_identifier("d" * 64) == "d" * 64)

def expect_value_error_fn(name, func, *args, **kwargs):
    """断言抛出 ValueError"""
    global pass_count, fail_count
    try:
        r = func(*args, **kwargs)
        fail_count += 1
        errors.append(f"{name}: 未抛出 ValueError（返回 {r!r}）")
        print(f"  FAIL [{name}] 未抛出 ValueError（返回 {r!r}）")
    except ValueError:
        pass_count += 1
        print(f"  PASS [{name}]")
    except Exception as e:
        fail_count += 1
        errors.append(f"{name}: 抛出了 {type(e).__name__} 而非 ValueError")
        print(f"  FAIL [{name}] 抛出了 {type(e).__name__} 而非 ValueError")

expect_value_error_fn("mysql 65字符拒绝", mysql_validate_identifier, "d" * 65)
expect_value_error_fn("mysql 数字开头拒绝", mysql_validate_identifier, "1abc")
expect_value_error_fn("mysql 注入字符拒绝", mysql_validate_identifier, "a; DROP TABLE x")
expect_value_error_fn("mysql 中文拒绝", mysql_validate_identifier, "用户表")

# ── 场景 16：MySQL SET 参数化 + _map_type 白名单（T1.2）──
print("\n=== 14.16 MySQL SET 参数化 + _map_type 白名单 ===")
from core.drivers.mysql_driver import _map_type, MysqlDriver

# _map_type 合法形态
check("VARCHAR(255) 透传", _map_type("VARCHAR(255)") == "VARCHAR(255)")
check("varchar(50) 大写规范化", _map_type("varchar(50)") == "VARCHAR(50)")
check("裸 VARCHAR 补默认长度", _map_type("VARCHAR") == "VARCHAR(255)")
check("INTEGER -> INT", _map_type("INTEGER") == "INT")
check("FLOAT -> DOUBLE", _map_type("FLOAT") == "DOUBLE")
check("未知类型回退 TEXT", _map_type("FOOBAR") == "TEXT")

# _map_type 拒绝注入/非法形态（此前 VARCHAR 前缀原样透传）
expect_value_error_fn("VARCHAR 堆叠注入被拒", _map_type, "VARCHAR(255)); DROP TABLE users--")
expect_value_error_fn("类型含分号被拒", _map_type, "TEXT; DROP TABLE x")
expect_value_error_fn("类型含空格被拒", _map_type, "INT UNSIGNED")
expect_value_error_fn("类型含注释被拒", _map_type, "INT--")
expect_value_error_fn("非数字精度被拒", _map_type, "VARCHAR(abc)")
expect_value_error_fn("空类型被拒", _map_type, "")

# update() SET 子句参数化实证（假连接，无需真实 MySQL）
class _FakeCursor:
    def __init__(self):
        self.rowcount = 1
        self.executed = []
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
    def close(self):
        pass

class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()
    def cursor(self):
        return self.cur
    def ping(self, reconnect=False):
        pass
    def commit(self):
        pass
    def rollback(self):
        pass

def _make_fake_mysql():
    drv = MysqlDriver.__new__(MysqlDriver)  # 跳过 __init__/_connect
    drv._conn = _FakeConn()
    return drv

# 注入 payload：未加引号的表达式（旧代码会作为 SQL 表达式执行）
drv = _make_fake_mysql()
r = drv.update("users", "bio=(SELECT password FROM users)", "id=1")
sql, params = drv._conn.cur.executed[0]
check("SET 子查询注入被参数化", "SELECT" not in sql.upper(),
      f"SQL 仍含 SELECT: {sql!r}")
check("注入 payload 作为字面值参数", params == ["(SELECT password FROM users)"],
      f"got {params!r}")
check("占位符为 %s", "`bio` = %s" in sql, f"got {sql!r}")

# 加引号的注入值（值整体在引号内，作为字面值参数化）
drv = _make_fake_mysql()
drv.update("users", "bio='OR 1=1 --'", "id=1")
sql, params = drv._conn.cur.executed[0]
check("引号注入 payload 参数化", "OR 1=1" not in sql, f"got {sql!r}")
check("引号注入值为字面值", params == ["OR 1=1 --"], f"got {params!r}")

# 引号外拼接的注入尾巴（与 sqlite 驱动行为一致：解析出非法字段名，直接拒绝）
expect_security_error(
    "SET 引号外注入尾巴被拒", _make_fake_mysql().update,
    "users", "bio='x' OR 1=1 --'", "id=1"
)

# 多字段 SET 参数化（数值字面量解码为 int——类型整形更干净，驱动行为不变量）
drv = _make_fake_mysql()
drv.update("users", "name='张三', age=30", "id=1")
sql, params = drv._conn.cur.executed[0]
check("多字段 SET 参数化", sql.count("%s") == 2 and params == ["张三", 30],
      f"sql={sql!r} params={params!r}")

# 恶意字段名被标识符白名单拦截
expect_security_error(
    "SET 恶意字段名拦截", _make_fake_mysql().update,
    "users", "1col='x'", "id=1"
)
expect_value_error_fn("SET 无等号格式错误", _make_fake_mysql().update, "users", "noequalssign")

# ── 场景 17：sqlite_driver col_type 驱动层校验（T1.2 纵深防御）──
print("\n=== 14.17 sqlite add_column col_type 校验 ===")
from core.drivers.sqlite_driver import SqliteDriver, _validate_col_type

check("TEXT 合法", _validate_col_type("TEXT") == "TEXT")
check("VARCHAR(255) 合法", _validate_col_type("VARCHAR(255)") == "VARCHAR(255)")
expect_value_error_fn("sqlite 类型堆叠注入被拒", _validate_col_type, "TEXT; DROP TABLE users--")
expect_value_error_fn("sqlite 类型注释注入被拒", _validate_col_type, "TEXT--")
expect_value_error_fn("sqlite 类型含空格被拒", _validate_col_type, "DOUBLE PRECISION")
expect_value_error_fn("sqlite 空类型被拒", _validate_col_type, "")

# 内存库实证：驱动层直接拒绝非法类型串
_memdrv = SqliteDriver(":memory:")
_memdrv.execute('CREATE TABLE t17 (id INTEGER PRIMARY KEY, name TEXT)')
expect_value_error_fn(
    "add_column 注入类型实证拦截", _memdrv.add_column,
    "t17", "evil", "TEXT; DROP TABLE t17--"
)
r = _memdrv.add_column("t17", "ok_col", "VARCHAR(255)")
check("add_column 合法类型放行", r.get("ok") is True, f"got {r!r}")
_memdrv.close()

# ── 场景 18：PDF 解析缓存 JSON 加固（T1.3 pickle RCE 消除）──
print("\n=== 14.18 解析缓存篡改拒绝 ===")
import tempfile
from core.parser import pdf_parser as pp
from core.parser.base import ParsedDocument

_fd, _tmp_pdf = tempfile.mkstemp(suffix=".pdf")
os.write(_fd, b"fake-pdf-content")
os.close(_fd)
try:
    # 正常往返：写入后可读回，内容一致（含中文）
    _doc = ParsedDocument(
        raw_text="你好 PDF",
        tables=[[["a", "b"], ["c", "d"]]],
        paragraphs=["段落一"],
        metadata={"pages": 1, "parser": "pymupdf"},
    )
    pp._cache_set(_tmp_pdf, _doc)
    _got = pp._cache_get(_tmp_pdf)
    check("缓存往返 raw_text 一致", _got is not None and _got.raw_text == "你好 PDF")
    check("缓存往返 tables 一致", _got is not None and _got.tables == [[["a", "b"], ["c", "d"]]])
    check("缓存往返 metadata 一致", _got is not None and _got.metadata.get("pages") == 1)

    _key, _cf = pp._cache_key(_tmp_pdf)

    # 篡改 1：垃圾字节
    _cf.write_bytes(b"\x00\x01\x02 not json at all")
    check("垃圾字节缓存被拒", pp._cache_get(_tmp_pdf) is None)

    # 篡改 2：旧 pickle 格式（RCE 载体）——必须拒绝而不是 load
    import pickle as _pickle
    _cf.write_bytes(_pickle.dumps({"raw_text": "pwned"}))
    check("pickle 载荷缓存被拒", pp._cache_get(_tmp_pdf) is None)

    # 篡改 3：合法 JSON 但缺格式标记
    _cf.write_text('{"raw_text": "forged"}', encoding="utf-8")
    check("无格式标记 JSON 被拒", pp._cache_get(_tmp_pdf) is None)

    # 篡改 4：有格式标记但结构非法
    _cf.write_text('{"format": "pdf-parser-cache/v1", "raw_text": 123}', encoding="utf-8")
    check("结构非法 JSON 被拒", pp._cache_get(_tmp_pdf) is None)

    # 篡改 5：截断的 JSON
    _cf.write_text('{"format": "pdf-parser-cache/v1", "raw_te', encoding="utf-8")
    check("截断 JSON 被拒", pp._cache_get(_tmp_pdf) is None)
finally:
    try:
        os.unlink(_tmp_pdf)
    except OSError:
        pass

# ── 场景 19：备份恢复 SQLite backup API（T1.4）──
print("\n=== 14.19 备份-修改-恢复往返 ===")
import sqlite3 as _sqlite3
from config.settings import settings as _settings
from core import backup as _backup_mod

_orig_db_path = _settings.SQLITE_DB_PATH
_tmpdir = tempfile.mkdtemp(prefix="backup_test_")
try:
    _db_file = os.path.join(_tmpdir, "data_engine.db")
    _settings.SQLITE_DB_PATH = _db_file  # 绝对路径，_get_db_path 直接使用

    # 建库 + 初始数据
    _c = open_db(_db_file)
    _c.execute("CREATE TABLE t19 (id INTEGER PRIMARY KEY, v TEXT)")
    _c.execute("INSERT INTO t19 (v) VALUES ('原始值')")
    _c.commit()
    _c.close()

    # 备份
    r = _backup_mod.backup_database()
    check("备份成功", r.get("ok") is True, f"got {r!r}")
    _backup_name = os.path.basename(r.get("path", ""))

    # 修改数据（模拟误操作）
    _c = open_db(_db_file)
    _c.execute("UPDATE t19 SET v='被破坏' WHERE id=1")
    _c.execute("INSERT INTO t19 (v) VALUES ('多余行')")
    _c.commit()
    # 故意保持一个活动连接不关闭，实证恢复不再受文件锁影响（Windows）
    _held_conn = open_db(_db_file)
    _held_conn.execute("SELECT COUNT(*) FROM t19").fetchone()

    # 恢复
    r = _backup_mod.restore_database(_backup_name)
    check("恢复成功", r.get("ok") is True, f"got {r!r}")

    # 校验数据回到备份时点
    _c2 = open_db(_db_file)
    _rows = _c2.execute("SELECT v FROM t19 ORDER BY id").fetchall()
    _c2.close()
    check("恢复后数据一致", _rows == [("原始值",)], f"got {_rows!r}")
    _held_conn.close()

    # 紧急备份已生成
    _backups = [b["filename"] for b in _backup_mod.list_backups()]
    check("恢复前紧急备份存在", any("pre_restore" in n for n in _backups),
          f"got {_backups!r}")

    # 不存在的备份文件
    r = _backup_mod.restore_database("no_such_file.db")
    check("缺失备份报错不崩溃", r.get("ok") is False)
finally:
    _settings.SQLITE_DB_PATH = _orig_db_path
    import shutil as _shutil
    try:
        _shutil.rmtree(_tmpdir, ignore_errors=True)
    except OSError:
        pass


# ── 场景 20：_build_select_sql 纯函数（db_chat SQL 构建拆分回归）──
print("\n=== 14.20 _build_select_sql 纯函数 ===")
from core.db_chat import _build_select_sql


def expect_value_error(name, func, *args, **kwargs):
    """断言 func(*args) 抛出 ValueError（SQL 构建安全校验拒绝），返回错误消息"""
    global pass_count, fail_count
    try:
        func(*args, **kwargs)
        fail_count += 1
        errors.append(f"{name}: 未抛出 ValueError")
        print(f"  FAIL [{name}] 未抛出 ValueError（应被拦截）")
        return None
    except ValueError as e:
        pass_count += 1
        print(f"  PASS [{name}]")
        return str(e)
    except Exception as e:
        fail_count += 1
        errors.append(f"{name}: 抛出了 {type(e).__name__} 而非 ValueError")
        print(f"  FAIL [{name}] 抛出了 {type(e).__name__} 而非 ValueError")
        return None


# ── 合法：仅 fields ──
_r = _build_select_sql("users", "*")
check("纯函数 星号字段", _r == 'SELECT * FROM "users"', f"got {_r!r}")

_r = _build_select_sql("users", "quota_id, unit")
check("纯函数 标识符列表", _r == 'SELECT quota_id, unit FROM "users"', f"got {_r!r}")

_r = _build_select_sql("users", "COUNT(*) AS cnt")
check("纯函数 聚合+别名", _r == 'SELECT COUNT(*) AS cnt FROM "users"', f"got {_r!r}")

# ── 合法：四子句组合（fields + where + order_by + group_by + limit）──
_r = _build_select_sql("quota_header", "unit, AVG(labor_cost) AS avg_cost",
                       "quota_id='A1-28'", "avg_cost DESC", "unit", 10)
check("纯函数 四子句组合",
      _r == ('SELECT unit, AVG(labor_cost) AS avg_cost FROM "quota_header"'
             " WHERE quota_id='A1-28' ORDER BY avg_cost DESC GROUP BY unit LIMIT 10"),
      f"got {_r!r}")

# ── 边界：空 where / 空 order_by / 空 group_by / limit=0 → 无对应子句 ──
_r = _build_select_sql("users", "id", "", "", "", 0)
check("纯函数 空子句全跳过", _r == 'SELECT id FROM "users"', f"got {_r!r}")

_r = _build_select_sql("users", "id", limit=-5)
check("纯函数 负 limit 忽略", _r == 'SELECT id FROM "users"', f"got {_r!r}")

_r = _build_select_sql("users", "id", limit="10")
check("纯函数 非 int limit 忽略", _r == 'SELECT id FROM "users"', f"got {_r!r}")

# ── 拒绝：空 fields ──
_msg = expect_value_error("纯函数 空 fields 拒绝", _build_select_sql, "users", "")
check("纯函数 空 fields 消息文案",
      _msg == "SELECT 字段不安全，已拒绝执行: ''", f"got {_msg!r}")

# ── 拒绝：fields 注入 payload ──
expect_value_error("纯函数 fields 分号注入", _build_select_sql,
                   "users", "id; DROP TABLE users")
expect_value_error("纯函数 fields 堆叠+注释", _build_select_sql,
                   "users", "1; DROP TABLE users--")
expect_value_error("纯函数 fields UNION 注入", _build_select_sql,
                   "users", "id UNION SELECT password FROM admin")

# ── 拒绝：where 注入 payload ──
_msg = expect_value_error("纯函数 where 分号注入", _build_select_sql,
                          "users", "id", "id=1; DROP TABLE users")
check("纯函数 where 消息前缀",
      _msg is not None and _msg.startswith("WHERE 条件不安全，已拒绝执行"),
      f"got {_msg!r}")
expect_value_error("纯函数 where 注释注入", _build_select_sql,
                   "users", "id", "id=1 --")
expect_value_error("纯函数 where 非法格式", _build_select_sql,
                   "users", "id", "OR 1=1")

# ── 拒绝：order_by 注入 payload ──
_msg = expect_value_error("纯函数 order_by 分号注入", _build_select_sql,
                          "users", "id", "", "id; DROP TABLE users")
check("纯函数 order_by 消息前缀",
      _msg is not None and _msg.startswith("ORDER BY 子句不安全，已拒绝执行"),
      f"got {_msg!r}")
expect_value_error("纯函数 order_by 子查询注入", _build_select_sql,
                   "users", "id", "", "(SELECT 1) DESC")

# ── 拒绝：group_by 注入 payload ──
_msg = expect_value_error("纯函数 group_by 分号注入", _build_select_sql,
                          "users", "id", "", "", "unit; DROP TABLE users")
check("纯函数 group_by 消息前缀",
      _msg is not None and _msg.startswith("GROUP BY 子句不安全，已拒绝执行"),
      f"got {_msg!r}")
expect_value_error("纯函数 group_by 函数包裹", _build_select_sql,
                   "users", "id", "", "", "COUNT(unit)")

# ── 表名注入：safe_table_sql 抛 SecurityError（与 ask 原行为一致，不包装）──
expect_security_error("纯函数 表名注入", _build_select_sql,
                      "users; DROP TABLE users", "id")


# ── 场景 21：ContractDriver.execute() 后门加固（单语句/禁注释/首关键字白名单）──
print("\n=== 14.21 ContractDriver.execute 加固 ===")
from core.contract.base import ContractDriver, _validate_execute_sql

# ── 拒绝：语句堆叠（分号在字面量之外）──
expect_security_error("execute 堆叠注入", _validate_execute_sql,
                      "CREATE TABLE a (id INTEGER); DROP TABLE a")
expect_security_error("execute 堆叠+注释", _validate_execute_sql,
                      "SELECT 1; DROP TABLE users--")
# ── 拒绝：SQL 注释 ──
expect_security_error("execute -- 注释", _validate_execute_sql,
                      "DROP TABLE a -- force")
expect_security_error("execute /* 注释", _validate_execute_sql,
                      "SELECT 1 /* comment */")
# ── 拒绝：非白名单开头 ──
expect_security_error("execute VACUUM 拒绝", _validate_execute_sql, "VACUUM")
expect_security_error("execute ATTACH 拒绝", _validate_execute_sql,
                      "ATTACH DATABASE '/etc/passwd' AS x")
expect_security_error("execute 空 SQL 拒绝", _validate_execute_sql, "")

# ── 放行：合法 DDL/DML/PRAGMA（纯校验不抛）──
def _expect_pass(name, sql):
    global pass_count, fail_count
    try:
        _validate_execute_sql(sql)
        pass_count += 1
        print(f"  PASS [{name}]")
    except Exception as e:
        fail_count += 1
        errors.append(f"{name}: 合法 SQL 被误拦: {e}")
        print(f"  FAIL [{name}] 合法 SQL 被误拦: {e}")

_expect_pass("execute CREATE INDEX 放行",
             'CREATE UNIQUE INDEX IF NOT EXISTS idx_a ON "t" ("c")')
_expect_pass("execute ALTER DROP COLUMN 放行",
             'ALTER TABLE "t" DROP COLUMN "c"')
_expect_pass("execute PRAGMA 放行", "PRAGMA foreign_keys=OFF")
_expect_pass("execute 字面量内分号放行", "INSERT INTO t (v) VALUES ('a;b')")
_expect_pass("execute 字面量内注释符放行", "SELECT * FROM t WHERE v = 'x--y'")

# ── 实证：ContractDriver 包真实 SqliteDriver ──
_cdrv_raw = SqliteDriver(":memory:")
_cdrv = ContractDriver(_cdrv_raw)
_cdrv.execute("CREATE TABLE t21 (id INTEGER PRIMARY KEY, name TEXT)")
_cdrv.execute("CREATE INDEX idx_t21_name ON t21 (name)")
check("execute 实证 CREATE INDEX 成功", True)
_cdrv.execute("PRAGMA foreign_keys=OFF")
_fk = _cdrv_raw.conn.execute("PRAGMA foreign_keys").fetchone()
check("execute 实证 PRAGMA 已生效", _fk is not None and _fk[0] == 0, f"got {_fk!r}")
_cdrv.execute("PRAGMA foreign_keys=ON")
expect_security_error("execute 实证堆叠被拦", _cdrv.execute,
                      "INSERT INTO t21 (name) VALUES ('x'); DROP TABLE t21")
expect_security_error("execute 实证 VACUUM 被拦", _cdrv.execute, "VACUUM")
# 字面量内分号真实落库
_cdrv.execute("INSERT INTO t21 (name) VALUES ('a;b')")
_row = _cdrv_raw.conn.execute("SELECT name FROM t21 WHERE id=1").fetchone()
check("execute 实证字面量分号落库", _row is not None and _row[0] == "a;b",
      f"got {_row!r}")
_cdrv_raw.close()


# ── 场景 22：file_tools 敏感文件黑名单（.env/.pem/id_rsa 不可读）──
print("\n=== 14.22 file_tools 敏感文件黑名单 ===")
from agent.open_layer import file_tools as _ft

check("file_tools .env 移出可读扩展名", ".env" not in _ft._TEXT_EXTS)

_orig_root = _settings.FILE_ACCESS_ROOT
_tmpdir22 = tempfile.mkdtemp(prefix="ft_sensitive_")
try:
    with open(os.path.join(_tmpdir22, ".env"), "w", encoding="utf-8") as _f:
        _f.write("API_KEY=sk-supersecret")
    with open(os.path.join(_tmpdir22, "app.pem"), "w", encoding="utf-8") as _f:
        _f.write("-----BEGIN PRIVATE KEY-----")
    with open(os.path.join(_tmpdir22, "id_rsa"), "w", encoding="utf-8") as _f:
        _f.write("PRIVATE KEY DATA")
    with open(os.path.join(_tmpdir22, "note.txt"), "w", encoding="utf-8") as _f:
        _f.write("普通文本内容")
    _settings.FILE_ACCESS_ROOT = _tmpdir22

    _r = _ft.read_file(".env")
    check("read_file .env 被拒",
          "敏感文件不可访问" in _r and "sk-supersecret" not in _r, f"got {_r!r}")
    _r = _ft.read_file("app.pem")
    check("read_file .pem 被拒",
          "敏感文件不可访问" in _r and "PRIVATE KEY" not in _r, f"got {_r!r}")
    _r = _ft.read_file("id_rsa")
    check("read_file id_rsa 被拒",
          "敏感文件不可访问" in _r and "PRIVATE KEY DATA" not in _r, f"got {_r!r}")
    _r = _ft.read_file("note.txt")
    check("read_file 正常文本可读", "普通文本内容" in _r, f"got {_r!r}")
finally:
    _settings.FILE_ACCESS_ROOT = _orig_root
    import shutil as _shutil22
    _shutil22.rmtree(_tmpdir22, ignore_errors=True)


# ──────────────────────────────────────────────
# 场景 23：db_chat 选择集补齐 + 别名解析收敛（db_chat 支线收敛）
# ──────────────────────────────────────────────
print("\n=== 14.23 db_chat 选择集 + 别名收敛 ===")
from core.db_chat import DBChat
from core.data_ops import _resolve_field
from core.context import get_context
import yaml as _yaml23
import shutil as _shutil23

# 23.1 别名解析两条路径结果一致（db_chat._resolve_fields 已薄委托 data_ops._resolve_field）
_chat23 = DBChat()
for _expr in ["地区编码 = '001'", "地区 = '武汉'", "name = '张三'", "name LIKE '%张%'"]:
    _a = _chat23._resolve_fields(_expr, "students")
    _b = _resolve_field(_expr, "students")
    check(f"别名两路径一致: {_expr}", _a == _b, f"db_chat={_a!r} data_ops={_b!r}")

# 23.2 无空格别名变体（db_chat 独有行为已合并进 data_ops._resolve_field）
_ns_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "industries", "_test_dbchat_ns")
os.makedirs(os.path.join(_ns_dir, "fields"), exist_ok=True)
os.makedirs(os.path.join(_ns_dir, "schemas"), exist_ok=True)
with open(os.path.join(_ns_dir, "fields", "fields.yml"), "w", encoding="utf-8") as _f:
    _yaml23.safe_dump({"score": {"alias": ["total score"], "type": "INTEGER"}},
                      _f, allow_unicode=True)
_orig_ind23 = _settings.INDUSTRY
try:
    _settings.INDUSTRY = "_test_dbchat_ns"
    check("带空格别名命中", _resolve_field("total score > 90") == "score > 90")
    check("无空格别名变体命中", _resolve_field("totalscore > 90") == "score > 90")
finally:
    _settings.INDUSTRY = _orig_ind23
    _shutil23.rmtree(_ns_dir, ignore_errors=True)

# 23.3 单表查询后选择集已创建（内存库隔离：monkeypatch data_ops._get_driver）
import core.data_ops as _do23
from core.drivers.sqlite_driver import SqliteDriver as _SqliteDriver23

_mem23 = _SqliteDriver23(":memory:")
_mem23.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
_mem23.execute("CREATE TABLE teachers (id INTEGER PRIMARY KEY, name TEXT)")
_mem23.execute("INSERT INTO students (name, email) VALUES ('张三', 'z@x.com')")
_mem23.execute("INSERT INTO students (name, email) VALUES ('李四', 'l@x.com')")
_mem23.execute("INSERT INTO teachers (name) VALUES ('王老师')")
_mem23.commit()


class _FakeAI23:
    """固定返回预设的 function calling 参数，不依赖真实 AI"""
    def __init__(self, fn_args):
        self._fn_args = fn_args

    def call_function(self, functions, question, system_prompt=""):
        return "query_tables", self._fn_args


_orig_get_driver23 = _do23._get_driver
_do23._get_driver = lambda: _mem23
# table_map 来自 industries/{INDUSTRY}/schemas：students/teachers 的 YAML 已迁到
# industries/_test_school/（学校示例表不再混入 engineering 生产配置），
# 本场景临时指向该目录（下划线目录不被 discover 注册，仅作 schema 夹具使用）
_orig_ind23_3 = _settings.INDUSTRY
_settings.INDUSTRY = "_test_school"
try:
    _ctx23 = get_context()
    _ctx23.clear_selections()  # 清空历史选择集（文件化共享态），隔离其他场景

    # 单表 SELECT * → 存选择集
    out = DBChat(ai=_FakeAI23({"tables": [
        {"table": "students", "where": "name='张三'", "select": "*"},
    ]})).ask("查询张三")
    _sels = _ctx23.list_selections()
    check("单表查询后选择集已创建", len(_sels) == 1, f"selections={_sels!r}")
    check("选择集表名正确", _sels and _sels[0]["table"] == "students")
    check("选择集条数正确", _sels and _sels[0]["count"] == 1)
    check("选择集含 id 列表", bool(_ctx23.get_selection(_sels[0]["id"]).get("ids")))
    check("输出含 selection_id 提示", "selection_id=" in out, f"out={out[:120]!r}")

    # 聚合结果 → 不存（行不对应单表记录）
    _ctx23.clear_selections()
    DBChat(ai=_FakeAI23({"tables": [
        {"table": "students", "select": "COUNT(*)"},
    ]})).ask("统计学生人数")
    check("聚合结果不存选择集", len(_ctx23.list_selections()) == 0,
          f"selections={_ctx23.list_selections()!r}")

    # 多表结果 → 不存
    _ctx23.clear_selections()
    DBChat(ai=_FakeAI23({"tables": [
        {"table": "students", "select": "*"},
        {"table": "teachers", "select": "*"},
    ]})).ask("查询所有学生和老师")
    check("多表结果不存选择集", len(_ctx23.list_selections()) == 0,
          f"selections={_ctx23.list_selections()!r}")
finally:
    _do23._get_driver = _orig_get_driver23
    _settings.INDUSTRY = _orig_ind23_3
    _mem23.close()


# ──────────────────────────────────────────────
# 24. 建表主键键名归一化 + id 无 is_pk 拒收（问题2）
# ──────────────────────────────────────────────
from core.contract.schema_change_contract import SchemaChangeContract as _SCC24
from core.exceptions import PrimaryKeyError as _PKE24


def _expect_pk_error24(name, cols, needle="is_pk"):
    """断言 assert_id_pk_declared 抛出 PrimaryKeyError 且提示 is_pk"""
    try:
        _SCC24.assert_id_pk_declared(cols)
        check(name, False, "未抛出 PrimaryKeyError（应被拒绝）")
    except _PKE24 as e:
        check(name, needle in str(e), f"err={e}")
    except Exception as e:
        check(name, False, f"抛出了 {type(e).__name__} 而非 PrimaryKeyError: {e}")


# 24.1 主键别名归一化为 is_pk
_cols24a = [
    {"name": "id", "type": "INTEGER", "primary_key": True},
    {"name": "code", "type": "TEXT", "primaryKey": True},
    {"name": "seq", "type": "INTEGER", "is_primary": True},
    {"name": "note", "type": "TEXT"},
]
_SCC24.normalize_pk_aliases(_cols24a)
check("primary_key 别名归一化为 is_pk",
      _cols24a[0].get("is_pk") is True and "primary_key" not in _cols24a[0],
      f"cols={_cols24a!r}")
check("primaryKey 别名归一化为 is_pk",
      _cols24a[1].get("is_pk") is True and "primaryKey" not in _cols24a[1],
      f"cols={_cols24a!r}")
check("is_primary 别名归一化为 is_pk",
      _cols24a[2].get("is_pk") is True and "is_primary" not in _cols24a[2],
      f"cols={_cols24a!r}")
check("无主键标记的列不受影响", "is_pk" not in _cols24a[3], f"cols={_cols24a!r}")

# 24.2 别名为 False 不误标主键
_cols24b = [{"name": "id", "type": "INTEGER", "primary_key": False}]
_SCC24.normalize_pk_aliases(_cols24b)
check("别名为 False 不误标主键",
      not _cols24b[0].get("is_pk") and "primary_key" not in _cols24b[0],
      f"cols={_cols24b!r}")

# 24.3 id 列未标 is_pk → 拒绝（含 primary_key=False 归一化后的场景）
_expect_pk_error24("id 无任何主键标记被拒",
                   [{"name": "id", "type": "INTEGER"}, {"name": "name", "type": "TEXT"}])
_expect_pk_error24("primary_key=False 的 id 仍被拒", _cols24b)

# 24.4 id 带 is_pk → 放行
try:
    _SCC24.assert_id_pk_declared([{"name": "id", "type": "INTEGER", "is_pk": True},
                                  {"name": "name", "type": "TEXT"}])
    check("id 带 is_pk 放行", True)
except Exception as e:
    check("id 带 is_pk 放行", False, f"err={e}")

# 24.5 别名归一化后 id 主键校验放行（端点调用顺序：先归一化再校验）
_cols24c = [{"name": "id", "type": "INTEGER", "primaryKey": True}]
_SCC24.normalize_pk_aliases(_cols24c)
try:
    _SCC24.assert_id_pk_declared(_cols24c)
    check("别名归一化后 id 主键校验放行", True)
except Exception as e:
    check("别名归一化后 id 主键校验放行", False, f"err={e}")

# 24.6 无 id 列 → 校验不拦截（id 是否必含由建表流程自身决定）
try:
    _SCC24.assert_id_pk_declared([{"name": "name", "type": "TEXT"}])
    check("无 id 列时校验不拦截", True)
except Exception as e:
    check("无 id 列时校验不拦截", False, f"err={e}")


# ──────────────────────────────────────────────
# ── 场景 24：裸 SQL 护栏补全（TEMP 变体/writable_schema/execute 列级/幻影列）──
print("\n=== 14.24 护栏补全（评审三轮）===")
from core.permission.sql_guard import guard_write_sql
from core.permission import PermissionDenied, PermissionPolicy
from core.contract.security_contract import split_top_commas


def _expect_denied(name, sql):
    try:
        guard_write_sql(sql)
        check(name, False, "未拦截")
    except PermissionDenied:
        check(name, True)
    except Exception as e:
        # 只接受权限拒绝；别的异常类型说明拦错层
        check(name, False, f"异常类型不符: {type(e).__name__}: {e}")


# TEMP/TEMPORARY 变体不再落进"未识别头默认放行"——readonly 语义下 DDL 必拦
from unittest.mock import patch as _patch
_deny_ddl = {"default": "full", "roles": {"readonly": {"deny": ["ddl", "drop"]}}}
from core.permission import set_current_role as _set_role
with _patch("core.permission.policy.PermissionPolicy.get_instance",
            classmethod(lambda cls: PermissionPolicy.new_instance(_deny_ddl))):
    _set_role("readonly")
    try:
        _expect_denied("TEMP TABLE 变体拦截", "CREATE TEMP TABLE loot AS SELECT * FROM users")
        _expect_denied("TEMP TRIGGER 变体拦截",
                       "CREATE TEMP TRIGGER evil AFTER INSERT ON t BEGIN SELECT 1; END")
        _expect_denied("普通 CREATE TABLE 仍拦截", "CREATE TABLE x (id INTEGER)")
    finally:
        _set_role("system")
# TEMP 变体本身已被护栏识别（full 权限下不抛即识别成功；未识别会走 warning 放行，
# 此处用"识别后不崩"验证不误伤合法 TEMP 用法）
guard_write_sql("CREATE TEMP TABLE ok_tmp (id INTEGER)")
check("full 权限下 TEMP TABLE 不误伤", True)

# writable_schema 硬阻断（任何角色）
_expect_denied("writable_schema=ON 硬阻断", "PRAGMA writable_schema=ON")
_expect_denied("writable_schema=1 硬阻断", "PRAGMA writable_schema = 1")
guard_write_sql("PRAGMA foreign_keys=ON")  # 合法 PRAGMA 不误伤
check("合法 PRAGMA 不误伤", True)

# execute 透传口列级：内置凭证列经 INSERT/UPDATE 必拦
_expect_denied("execute INSERT 凭证列拦截",
               "INSERT INTO users (username,password_hash,salt,role) VALUES ('x','h','s','admin')")
_expect_denied("execute UPDATE 凭证列大写变体拦截",
               "UPDATE users SET Password_Hash='x' WHERE id=1")
guard_write_sql("INSERT INTO t_biz (username, role) VALUES ('x', 'user')")
check("execute INSERT 普通业务表不误伤（users 表整表只读后，换业务表验证）", True)

# 方言变体与 fail-closed（评审五轮）：
# UPDATE/DELETE 的 OR IGNORE 变体曾穿透 users 整表只读（护栏 UPDATE 头无 OR 容错）
_expect_denied("UPDATE OR IGNORE 变体拦截（users 只读复活链封堵）",
               "UPDATE OR IGNORE users SET role='admin' WHERE id=1")
_expect_denied("CTE+OR IGNORE 变体拦截",
               "WITH x AS (SELECT 1) UPDATE OR IGNORE users SET role='admin' WHERE id=1")
# 未识别写头 fail-closed（旧策略是放行留痕——透传口迟早被变体打穿）
_expect_denied("未识别写头 fail-closed（GRANT）", "GRANT ALL ON t TO x")
_expect_denied("未识别写头 fail-closed（VACUUM）", "VACUUM")
guard_write_sql("SELECT 1")
check("读语句不受 fail-closed 影响", True)
guard_write_sql("BEGIN")
check("事务语句不受 fail-closed 影响", True)

# 幻影列：SET 值里的 = 不产生幻影列名（引号感知分词）
_cols = [seg.split("=", 1)[0].strip() for seg in split_top_commas("name='k=v', age=30")]
check("SET 幻影列不成立", _cols == ["name", "age"], f"got {_cols!r}")
_cols2 = [seg.split("=", 1)[0].strip() for seg in split_top_commas("note='a,b''c', v=1")]
check("SET 值内逗号不误切", _cols2 == ["note", "v"], f"got {_cols2!r}")


# ──────────────────────────────────────────────
# 汇总
print()
print("=" * 60)
print(f"SQL SAFETY: PASS={pass_count}  FAIL={fail_count}  TOTAL={pass_count + fail_count}")
if fail_count == 0:
    print("=== ALL SQL SAFETY TESTS PASSED ===")
else:
    print("=== SOME SQL SAFETY TESTS FAILED ===")
    for e in errors:
        print(f"  - {e}")
print("=" * 60)

# pytest 收集时会 import 本模块，不能在模块级 sys.exit（会导致 INTERNALERROR）
if __name__ == "__main__":
    sys.exit(1 if fail_count else 0)

