"""层 17：数据源权限控制——按 数据源×操作 的访问控制（独立层）"""
import sys; sys.path.insert(0, ".")
import tempfile
from pathlib import Path
from unittest.mock import patch


def _policy(rules):
    from core.permission import PermissionPolicy
    return PermissionPolicy.new_instance(rules)


def test_policy_modes():
    """四种模式语义：full / read_only / custom-allow / custom-deny"""
    from core.permission import Operation, PermissionDenied
    p = _policy({
        "default": "full",
        "datasources": {
            "legacy": {"mode": "read_only"},
            "ana": {"mode": "custom", "deny": ["delete", "drop", "ddl"]},
            "stg": {"mode": "custom", "allow": ["query", "insert"]},
        },
    })
    # default full：未配置的数据源全放
    p.check("any_db", Operation.DELETE)
    # read_only：query 放行，写全禁
    p.check("legacy", Operation.QUERY)
    for op in (Operation.INSERT, Operation.UPDATE, Operation.DELETE, Operation.DDL, Operation.DROP):
        try:
            p.check("legacy", op)
            raise AssertionError(f"read_only 应禁止 {op}")
        except PermissionDenied as e:
            assert "legacy" in str(e) and op.value in str(e)
    # custom-deny：黑名单内禁止、其余放行
    p.check("ana", Operation.QUERY)
    p.check("ana", Operation.INSERT)
    try:
        p.check("ana", Operation.DELETE)
        raise AssertionError("deny 应拦截 delete")
    except PermissionDenied:
        pass
    # custom-allow：白名单内放行、其余全禁
    p.check("stg", Operation.QUERY)
    p.check("stg", Operation.INSERT)
    try:
        p.check("stg", Operation.UPDATE)
        raise AssertionError("allow 模式非白名单应全禁")
    except PermissionDenied:
        pass
    print("OK - 四种模式语义正确（含错误消息含库名+操作）")


def test_default_full_backward_compat():
    """无规则文件时默认 full（向后兼容）"""
    from core.permission import Operation, PermissionPolicy
    p = PermissionPolicy.new_instance()  # 无规则 = default full
    p.check("whatever", Operation.DROP)
    print("OK - 无规则默认 full（向后兼容）")


def test_driver_enforcement():
    """驱动层拦截：read_only 库 delete 被拦、query 放行；full 库全放"""
    from core.permission import PermissionPolicy
    rules = {"default": "full",
             "datasources": {"legacy": {"mode": "read_only"}}}
    with tempfile.TemporaryDirectory() as tmp:
        from core.drivers.federated_driver import FederatedDriver
        from core.datasource_manager import DataSourceManager
        import yaml

        # 构造双数据源：primary(full) + legacy(read_only)，各建一表
        cfg = {
            "datasources": {
                "primary": {"type": "sqlite", "database": f"{tmp}/p.db", "default": True},
                "legacy": {"type": "sqlite", "database": f"{tmp}/l.db"},
            }
        }
        cfg_path = Path(tmp) / "ds.yml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        dsm = DataSourceManager.new_instance()
        dsm.load_config(str(cfg_path))

        from core.drivers.sqlite_driver import SqliteDriver
        from core.contract import ContractDriver
        p_raw = SqliteDriver(f"{tmp}/p.db")
        l_raw = SqliteDriver(f"{tmp}/l.db")
        for d in (p_raw, l_raw):
            d.execute('CREATE TABLE t1 (id INTEGER PRIMARY KEY, v TEXT)')
            d.execute("INSERT INTO t1 (v) VALUES ('x')")
            d.conn.commit()  # SqliteDriver.execute 不自动提交，测试数据需显式提交

        # 权限单栈（20260822 收口）在 ContractDriver——按生产形态包装
        #（DataSourceManager.get_driver 出厂即包 ContractDriver，此处手动复刻）
        p_drv = ContractDriver(p_raw, "sqlite")
        l_drv = ContractDriver(l_raw, "sqlite")
        dsm.get_driver_for_table = lambda t: l_drv if dsm._table_map.get(t) == "legacy" else p_drv
        with patch.object(DataSourceManager, "_instance", dsm), \
             patch("core.permission.policy.PermissionPolicy.get_instance",
                   classmethod(lambda cls: PermissionPolicy.new_instance(rules))):
            fed = FederatedDriver.__new__(FederatedDriver)
            fed._dsm = dsm
            fed._dirty_drivers = set()
            # 路由到 legacy（注册表映射）
            dsm._table_map = {"t1": "legacy"}
            # query 放行
            rows = fed.query("SELECT * FROM t1")
            assert len(rows) == 1
            # delete 被拦
            from core.permission import PermissionDenied
            try:
                fed.delete("t1", "id=1")
                raise AssertionError("read_only 库的 delete 应被拦截")
            except PermissionDenied as e:
                assert "legacy" in str(e) and "delete" in str(e)
            # primary（default full）放行
            dsm._table_map = {"t1": "primary"}
            r = fed.delete("t1", "id=1")
            # 收紧（评审三轮测试复核）：count 缺省 0 会让右支恒真——断言删除真实发生
            assert r.get("ok") and r.get("count") == 1, f"full 库删除应真实执行: {r}"
            assert not p_drv.query("SELECT * FROM t1"), "行应真实消失"
        for d in (p_raw, l_raw):
            for conn in d._conns.values():
                conn.close()
    print("OK - 驱动层：read_only 拦截 delete、放行 query，full 库全放")


def test_column_level_permissions():
    """列级权限：query 屏蔽禁列、显式引用禁列拒绝、insert 禁列拒绝"""
    import tempfile, yaml
    from pathlib import Path
    from unittest.mock import patch
    from core.permission import PermissionPolicy, PermissionDenied
    from core.datasource_manager import DataSourceManager
    from core.drivers.federated_driver import FederatedDriver
    from core.drivers.sqlite_driver import SqliteDriver

    rules = {"default": "full", "datasources": {"legacy": {
        "mode": "full",
        # op 如实判定（评审四轮）：insert 不再按 update 判定，禁写要显式列 insert
        "tables": {"t1": {"columns": {"secret": {"deny": ["query", "update", "insert"]}}}},
    }}}
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {"datasources": {
            "primary": {"type": "sqlite", "path": f"{tmp}/p.db", "default": True},
            "legacy": {"type": "sqlite", "path": f"{tmp}/l.db"}}}
        cfg_path = Path(tmp) / "ds.yml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        l_raw = SqliteDriver(f"{tmp}/l.db")
        l_raw.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, v TEXT, secret TEXT)")
        l_raw.execute("INSERT INTO t1 (v, secret) VALUES ('公开', '机密')")
        l_raw.conn.commit()
        from core.contract import ContractDriver
        l_drv = ContractDriver(l_raw, "sqlite")  # 权限单栈在契约层，按生产形态包装
        dsm = DataSourceManager.new_instance()
        dsm.load_config(str(cfg_path))
        dsm._table_map = {"t1": "legacy"}
        dsm.get_driver_for_table = lambda t: l_drv
        with patch.object(DataSourceManager, "_instance", dsm), \
             patch("core.permission.policy.PermissionPolicy.get_instance",
                   classmethod(lambda cls: PermissionPolicy.new_instance(rules))):
            fed = FederatedDriver.__new__(FederatedDriver)
            fed._dsm = dsm
            fed._dirty_drivers = set()
            rows = fed.query("SELECT * FROM t1")
            assert rows and "secret" not in rows[0] and "v" in rows[0],                 f"SELECT * 应屏蔽禁列: {rows}"
            try:
                fed.query("SELECT secret FROM t1")
                raise AssertionError("显式引用禁列应被拒")
            except PermissionDenied:
                pass
            try:
                fed.insert("t1", [{"v": "a", "secret": "b"}])
                raise AssertionError("insert 禁列应被拒")
            except PermissionDenied:
                pass
        for conn in l_raw._conns.values():
            conn.close()  # Windows 下不关闭会锁死临时目录
    print("OK - 列级：query 屏蔽/显式拒绝/insert 拦截")


def test_role_level_permissions():
    """角色级权限：roles.deny 叠加命中即禁，白名单外全禁，system 不查"""
    from core.permission import Operation, PermissionDenied, PermissionPolicy
    p = PermissionPolicy.new_instance({
        "default": "full",
        "roles": {
            "readonly": {"deny": ["insert", "update", "delete", "ddl", "drop"]},
            "analyst": {"allow": ["query"]},
        }})
    # readonly：写全禁、读放行
    p.check("anydb", Operation.QUERY, role="readonly")
    for op in (Operation.INSERT, Operation.UPDATE, Operation.DELETE, Operation.DDL, Operation.DROP):
        try:
            p.check("anydb", op, role="readonly")
            raise AssertionError(f"readonly 应禁止 {op}")
        except PermissionDenied:
            pass
    # analyst 白名单：只允许 query
    p.check("anydb", Operation.QUERY, role="analyst")
    try:
        p.check("anydb", Operation.INSERT, role="analyst")
        raise AssertionError("analyst 白名单外应全禁")
    except PermissionDenied:
        pass
    # system（无角色上下文）不查角色规则
    p.check("anydb", Operation.DELETE, role="")
    print("OK - 角色级：deny 叠加/allow 白名单/system 放行")


def test_scope_shell_inherits_upward():
    """壳继承：仅挂 tables/columns 子节点、未声明 mode/allow/deny 的 scope 不产生语义，
    向上继承——否则给某列加黑名单会把整表/整库误锁成 fail-closed（C3 回归）"""
    from core.permission import Operation, PermissionDenied, PermissionPolicy
    # 库级壳（只有 tables）+ 表级壳（只有 columns）→ 都继承 default full
    p = PermissionPolicy.new_instance({
        "default": "full",
        "datasources": {"db1": {"tables": {"t1": {"columns": {"c1": {"deny": ["delete"]}}}}}},
    })
    p.check("db1", Operation.INSERT)
    p.check("db1", Operation.DROP)
    p.check("db1", Operation.QUERY, table="t1")
    p.check("db1", Operation.DELETE, table="t1")
    # 列级黑名单本身仍生效
    try:
        p.check_column("db1", "t1", "c1", Operation.DELETE)
        raise AssertionError("列级黑名单应拦截 c1 delete")
    except PermissionDenied:
        pass
    # 表壳继承库级 read_only（列黑名单场景的真实配置形态）
    p3 = PermissionPolicy.new_instance({
        "default": "full",
        "datasources": {"db1": {"mode": "read_only",
                                "tables": {"t1": {"columns": {"c1": {"deny": ["query"]}}}}}},
    })
    p3.check("db1", Operation.QUERY, table="t1")
    try:
        p3.check("db1", Operation.INSERT, table="t1")
        raise AssertionError("表壳应继承库级 read_only 禁 insert")
    except PermissionDenied:
        pass
    # 声明了 mode 的表节点仍是覆盖，不被壳逻辑吞掉
    p2 = PermissionPolicy.new_instance({
        "default": "full",
        "datasources": {"db1": {"tables": {"t1": {"mode": "read_only",
                                                  "columns": {"c1": {"deny": ["query"]}}}}}},
    })
    p2.check("db1", Operation.QUERY, table="t1")
    try:
        p2.check("db1", Operation.INSERT, table="t1")
        raise AssertionError("表级 read_only 覆盖应禁 insert")
    except PermissionDenied:
        pass
    p2.check("db1", Operation.INSERT)  # 库级壳继承 default full
    # 显式 fail-closed（mode:custom 无列表）仍全禁——语义保留
    p4 = PermissionPolicy.new_instance({
        "default": "full", "datasources": {"locked": {"mode": "custom"}}})
    try:
        p4.check("locked", Operation.QUERY)
        raise AssertionError("显式 custom 空壳应 fail-closed")
    except PermissionDenied:
        pass
    print("OK - 壳继承：tables/columns 壳向上继承，显式 mode/custom 语义保留")


def test_case_variant_attacks():
    """大小写变体攻击矩阵（评审三轮安全 A/B 的回归锁）：
    SQL 标识符大小写不敏感，权限判定必须同样不敏感——Users/Password_Hash 变体
    不得穿透内置凭证 deny 与 yml 表/列规则；禁列出现在 WHERE 谓词同样拒绝。"""
    import tempfile, yaml
    from pathlib import Path
    from unittest.mock import patch
    from core.permission import PermissionPolicy, PermissionDenied
    from core.datasource_manager import DataSourceManager
    from core.drivers.federated_driver import FederatedDriver
    from core.drivers.sqlite_driver import SqliteDriver
    from core.contract import ContractDriver

    rules = {"default": "full", "datasources": {"legacy": {
        "mode": "full",
        # 配置侧大小写混合：Secret_Col 大写键也必须对全小写查询生效（双向归一）
        "tables": {"T1": {"columns": {"Secret_Col": {"deny": ["query", "update"]}}}},
    }}}
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {"datasources": {
            "primary": {"type": "sqlite", "path": f"{tmp}/p.db", "default": True},
            "legacy": {"type": "sqlite", "path": f"{tmp}/l.db"}}}
        cfg_path = Path(tmp) / "ds.yml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        raw = SqliteDriver(f"{tmp}/l.db")
        raw.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, v TEXT, secret_col TEXT)")
        raw.execute("INSERT INTO t1 (v, secret_col) VALUES ('公开', '机密')")
        # 认证表同名即可被内置凭证保护命中（按表名，与数据源无关）
        raw.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, salt TEXT, role TEXT)")
        raw.execute("INSERT INTO users (username, password_hash, salt) VALUES ('a', 'deadbeef', 'ff')")
        raw.conn.commit()
        drv = ContractDriver(raw, "sqlite")
        dsm = DataSourceManager.new_instance()
        dsm.load_config(str(cfg_path))
        dsm._table_map = {"t1": "legacy", "users": "legacy"}
        dsm.get_driver_for_table = lambda t: drv
        with patch.object(DataSourceManager, "_instance", dsm), \
             patch("core.permission.policy.PermissionPolicy.get_instance",
                   classmethod(lambda cls: PermissionPolicy.new_instance(rules))):
            fed = FederatedDriver.__new__(FederatedDriver)
            fed._dsm = dsm
            fed._dirty_drivers = set()

            # 1. 表名大写变体：SELECT * FROM "Users" 必须同样屏蔽凭证列
            rows = fed.query('SELECT * FROM "Users"')
            assert rows and "password_hash" not in rows[0] and "salt" not in rows[0], \
                f"大写表名变体不得绕过凭证屏蔽: {rows}"

            # 2. WHERE 谓词预言机：禁列出现在 WHERE 同样拒绝（不再只罩投影）
            for evil in ("SELECT * FROM users WHERE password_hash LIKE 'a%'",
                         "SELECT id FROM users WHERE salt = 'ff'",
                         "SELECT * FROM users ORDER BY password_hash"):
                try:
                    fed.query(evil)
                    raise AssertionError(f"WHERE/ORDER BY 引用禁列应被拒: {evil}")
                except PermissionDenied:
                    pass

            # 3. 列名大小写变体写入：Password_Hash/Salt 穿透 update 的列级检查
            try:
                fed.update("users", "Password_Hash='x', Salt='y'", "id=1")
                raise AssertionError("列名大小写变体不得绕过凭证写保护")
            except PermissionDenied:
                pass

            # 4. 列名变体插入（塞一个 admin）
            try:
                fed.insert("users", [{"username": "x", "Password_Hash": "h",
                                      "Salt": "s", "role": "admin"}])
                raise AssertionError("列名大小写变体不得绕过凭证写保护")
            except PermissionDenied:
                pass

            # 5. 配置侧大小写归一：规则键 T1/Secret_Col 对全小写查询生效
            try:
                fed.query("SELECT secret_col FROM t1")
                raise AssertionError("配置大写键的列规则应对小写查询生效")
            except PermissionDenied:
                pass
            rows = fed.query("SELECT * FROM t1")
            assert rows and "secret_col" not in rows[0] and "v" in rows[0], \
                f"SELECT * 应屏蔽配置禁列: {rows}"

            # 6. 合法访问不受影响：非禁列正常读写
            rows = fed.query("SELECT id, username FROM users")
            assert rows and rows[0]["username"] == "a"
            fed.update("t1", "v='公开2'", "id=1")
        for conn in raw._conns.values():
            conn.close()
        for _d in getattr(dsm, "_drivers", {}).values():
            try:
                _d.close()  # dsm.get_driver 通道创建的驱动也要关（Windows 文件锁）
            except Exception:
                pass
    print("OK - 大小写变体攻击矩阵：表/列变体全拦、WHERE 谓词封锁、合法访问不误伤")


def test_round4_regression_locks():
    """四轮修复的攻击回归锁（每个修复配对立面用例）：
    - users 表整表只读：任何角色（含 system）写必拒——自助提权链封堵（S-1）
    - SET 双引号载荷：契约与驱动同源解析，禁列经双引号串藏逗号也必拒（S-2）
    - update/delete 的 WHERE 引用禁列必拒——布尔预言机封堵（H-1）
    - 提取器形态归一：方括号/schema 前缀/别名星号/大小写错位星号（H-3）
    """
    import tempfile, yaml
    from pathlib import Path
    from unittest.mock import patch
    from core.permission import PermissionPolicy, PermissionDenied
    from core.datasource_manager import DataSourceManager
    from core.drivers.federated_driver import FederatedDriver
    from core.drivers.sqlite_driver import SqliteDriver
    from core.contract import ContractDriver

    rules = {"default": "full", "datasources": {"legacy": {
        "mode": "full",
        "tables": {"t1": {"columns": {"secret_col": {"deny": ["query", "update", "insert",
                                                              "delete"]}}}},
    }}}
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {"datasources": {
            "primary": {"type": "sqlite", "path": f"{tmp}/p.db", "default": True},
            "legacy": {"type": "sqlite", "path": f"{tmp}/l.db"}}}
        cfg_path = Path(tmp) / "ds.yml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        raw = SqliteDriver(f"{tmp}/l.db")
        raw.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, v TEXT, name TEXT, secret_col TEXT)")
        raw.execute("INSERT INTO t1 (v, name, secret_col) VALUES ('公开', '甲', '机密')")
        raw.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, "
                    "password_hash TEXT, salt TEXT, role TEXT)")
        raw.execute("INSERT INTO users (username, password_hash, salt, role) "
                    "VALUES ('a', 'deadbeef', 'ff', 'user')")
        raw.conn.commit()
        drv = ContractDriver(raw, "sqlite")
        dsm = DataSourceManager.new_instance()
        dsm.load_config(str(cfg_path))
        dsm._table_map = {"t1": "legacy", "users": "legacy"}
        dsm.get_driver_for_table = lambda t: drv
        with patch.object(DataSourceManager, "_instance", dsm), \
             patch("core.permission.policy.PermissionPolicy.get_instance",
                   classmethod(lambda cls: PermissionPolicy.new_instance(rules))):
            fed = FederatedDriver.__new__(FederatedDriver)
            fed._dsm = dsm
            fed._dirty_drivers = set()

            # S-1：users 表整表只读——update/insert/delete/delete_by_pk 全拒（含 system）
            for fn in (lambda: fed.update("users", "role='admin'", "id=1"),
                       lambda: fed.insert("users", [{"username": "x", "role": "admin"}]),
                       lambda: fed.delete("users", "id=1"),
                       lambda: fed.delete_by_pk("users", "id", 1)):
                try:
                    fn()
                    raise AssertionError("users 表数据面写必须拒绝（含 system 角色）")
                except PermissionDenied as e:
                    assert "认证表" in str(e) or "只读" in str(e), str(e)[:120]

            # S-2：双引号串藏逗号的禁列载荷（旧契约分词会吞逗号只见 name 放行）
            try:
                fed.update("t1", "name=\"O'Neil\", secret_col='x'", "id=1")
                raise AssertionError("双引号载荷不得绕过列级权限")
            except PermissionDenied:
                pass
            # 合法双引号值不误伤（name 非禁列）
            fed.update("t1", "name=\"O'Neil\"", "id=1")
            row = raw.query("SELECT name FROM t1 WHERE id=1")[0]
            assert row["name"] == "O'Neil", f"合法值 round-trip 被破坏: {row}"

            # H-1：update/delete 的 WHERE 引用禁列必拒（带主键的预言机真实形态）
            for fn in (lambda: fed.update("t1", "v='x'", "id=1 AND secret_col='机密'"),
                       lambda: fed.delete("t1", "id=1 AND secret_col LIKE '机%'")):
                try:
                    fn()
                    raise AssertionError("WHERE 引用禁列必须拒绝")
                except PermissionDenied:
                    pass

            # H-3：提取器形态四连（方括号/schema 前缀/别名星号/大小写错位星号）
            try:
                fed.query("SELECT password_hash FROM [users]")
                raise AssertionError("方括号形态不得穿透")
            except PermissionDenied:
                pass
            rows = fed.query("SELECT * FROM main.users")
            assert rows and "password_hash" not in rows[0], f"schema 前缀形态应屏蔽: {rows}"
            rows = fed.query("SELECT u.* FROM users u")
            assert rows and "password_hash" not in rows[0], f"别名星号应展开白名单: {rows}"
            rows = fed.query("SELECT USERS.* FROM users")
            assert rows and "password_hash" not in rows[0], f"大小写错位星号应展开: {rows}"
            # 五轮续：引号包裹 schema 前缀/逗号连接/换行星号
            rows = fed.query('SELECT * FROM "main"."users"')
            assert rows and "password_hash" not in rows[0], f"引号 schema 前缀应屏蔽: {rows}"
            rows = fed.query("SELECT\n  *\nFROM users")
            assert rows and "password_hash" not in rows[0], f"换行星号应屏蔽: {rows}"
            # 逗号连接（多表星号展开后裸列名可能歧义错拦——文档化取舍：
            # 宁可错拦不可泄露；安全断言=凭证列永不出现在结果里）
            rows = fed.query("SELECT t1.id, users.username FROM t1, users")
            assert rows and "password_hash" not in rows[0], f"逗号连接限定列应放行: {rows}"
            from core.exceptions import AppError as _AppError
            try:
                rows = fed.query("SELECT * FROM t1, users")
                leaked = rows and ("password_hash" in rows[0] or "salt" in rows[0])
                assert not leaked, f"逗号连接泄露凭证列: {rows}"
            except _AppError:
                pass  # 歧义错拦（可接受方向）
        for conn in raw._conns.values():
            conn.close()
        for _d in getattr(dsm, "_drivers", {}).values():
            try:
                _d.close()  # dsm.get_driver 通道创建的驱动也要关（Windows 文件锁）
            except Exception:
                pass
    print("OK - 四轮回归锁：整表只读/双引号载荷/WHERE 谓词/提取器形态全部在位")


if __name__ == "__main__":
    test_policy_modes()
    test_default_full_backward_compat()
    test_driver_enforcement()
    test_column_level_permissions()
    test_role_level_permissions()
    test_scope_shell_inherits_upward()
    test_case_variant_attacks()
    test_round4_regression_locks()
    print("\n=== ALL PERMISSION TESTS PASSED ===")
