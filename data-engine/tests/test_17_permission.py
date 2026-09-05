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
            # 收紧：count 缺省 0 会让右支恒真——断言删除真实发生
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
        # op 如实判定：insert 不再按 update 判定，禁写要显式列 insert
        "tables": {"t1": {"columns": {"secret": {"deny": ["query", "update", "insert"]}}}},
    }}}
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {"datasources": {
            "primary": {"type": "sqlite", "path": f"{tmp}/p.db", "is_default": True},
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


def test_user_level_permissions():
    """用户级权限：users.deny 叠加命中即禁，白名单外全禁，无用户名上下文不查"""
    from core.permission import (Operation, PermissionDenied, PermissionPolicy,
                                 set_current_user)
    p = PermissionPolicy.new_instance({
        "default": "full",
        "users": {
            "bob": {"deny": ["insert", "update", "delete", "ddl", "drop"]},
            "alice": {"allow": ["query"]},
        }})
    try:
        # bob：写全禁、读放行
        set_current_user("bob")
        p.check("anydb", Operation.QUERY)
        for op in (Operation.INSERT, Operation.UPDATE, Operation.DELETE, Operation.DDL, Operation.DROP):
            try:
                p.check("anydb", op)
                raise AssertionError(f"bob 应禁止 {op}")
            except PermissionDenied:
                pass
        # alice 白名单：只允许 query
        set_current_user("alice")
        p.check("anydb", Operation.QUERY)
        try:
            p.check("anydb", Operation.INSERT)
            raise AssertionError("alice 白名单外应全禁")
        except PermissionDenied:
            pass
        # 无用户名上下文（system/未注入）：不查用户规则
        set_current_user("")
        p.check("anydb", Operation.DELETE)
    finally:
        set_current_user("")
    print("OK - 用户级：deny 叠加/allow 白名单/无用户名不查")


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
    # 声明了 mode 的表节点仍生效（级联下与覆盖同结果：该表唯一语义层是表级）
    p2 = PermissionPolicy.new_instance({
        "default": "full",
        "datasources": {"db1": {"tables": {"t1": {"mode": "read_only",
                                                  "columns": {"c1": {"deny": ["query"]}}}}}},
    })
    p2.check("db1", Operation.QUERY, table="t1")
    try:
        p2.check("db1", Operation.INSERT, table="t1")
        raise AssertionError("表级 read_only 应禁 insert")
    except PermissionDenied:
        pass
    p2.check("db1", Operation.INSERT)  # 库级壳不产生语义，default full 放行
    # 显式 fail-closed（mode:custom 无列表）仍全禁——语义保留
    p4 = PermissionPolicy.new_instance({
        "default": "full", "datasources": {"locked": {"mode": "custom"}}})
    try:
        p4.check("locked", Operation.QUERY)
        raise AssertionError("显式 custom 空壳应 fail-closed")
    except PermissionDenied:
        pass
    print("OK - 壳继承：tables/columns 壳向上继承，显式 mode/custom 语义保留")


def test_cascade_upper_ban_not_overridable():
    """级联（20260901）：上级禁止不可被下级解禁——库级 read_only + 表级
    custom deny=[query] 时，insert 仍被库级禁止（旧覆盖语义下表规则顶掉
    库级只读，会 fail-open 恢复可写——本测试锁死级联语义）"""
    from core.permission import Operation, PermissionDenied, PermissionPolicy
    p = PermissionPolicy.new_instance({
        "default": "full",
        "datasources": {"db1": {"mode": "read_only",
                                "tables": {"t1": {"mode": "custom", "deny": ["query"]}}}},
    })
    # 表级禁 query + 库级只读 → query/insert 双双被禁（两级各禁一个，级联叠加）
    try:
        p.check("db1", Operation.QUERY, table="t1")
        raise AssertionError("表级 deny=[query] 应禁 query")
    except PermissionDenied as e:
        assert "表级" in str(e), f"首个禁止级应为表级: {e}"
    try:
        p.check("db1", Operation.INSERT, table="t1")
        raise AssertionError("级联：库级 read_only 应仍禁 insert（不可被表级解禁）")
    except PermissionDenied as e:
        assert "库级" in str(e), f"首个禁止级应为库级: {e}"
    # 对照：表级规则不存在的表，库级语义不变
    p.check("db1", Operation.QUERY, table="t2")
    try:
        p.check("db1", Operation.DELETE, table="t2")
        raise AssertionError("库级 read_only 应禁 delete")
    except PermissionDenied:
        pass
    # first_deny 与 check 同源
    d = p.first_deny("db1", Operation.INSERT, table="t1")
    assert d and d["scope"] == "库级", d
    assert p.first_deny("db1", Operation.QUERY, table="t2") is None
    print("OK - 级联：上级禁止不可被下级解禁（库级 read_only 不被表级顶掉）")


def test_case_variant_attacks():
    """大小写变体攻击矩阵（回归锁）：
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
            "primary": {"type": "sqlite", "path": f"{tmp}/p.db", "is_default": True},
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
            #（20260904 系统表收口后 users 表 DML 在契约层整表先拒——
            #  SecurityError（整表）与 PermissionDenied（列级）都算拦截成功）
            from core.exceptions import SecurityError as _SecErr
            try:
                fed.update("users", "Password_Hash='x', Salt='y'", "id=1")
                raise AssertionError("列名大小写变体不得绕过凭证写保护")
            except (PermissionDenied, _SecErr):
                pass

            # 4. 列名变体插入（塞一个 admin）
            try:
                fed.insert("users", [{"username": "x", "Password_Hash": "h",
                                      "Salt": "s", "role": "admin"}])
                raise AssertionError("列名大小写变体不得绕过凭证写保护")
            except (PermissionDenied, _SecErr):
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
    """攻击面回归锁（每个修复配对立面用例）：
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
            "primary": {"type": "sqlite", "path": f"{tmp}/p.db", "is_default": True},
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
            #（20260904 系统表收口：契约层 SecurityError 整表先拒，与列级
            #  PermissionDenied 同为合法拦截形态——拦截更强不算回归）
            from core.exceptions import SecurityError as _SecErr
            for fn in (lambda: fed.update("users", "role='admin'", "id=1"),
                       lambda: fed.insert("users", [{"username": "x", "role": "admin"}]),
                       lambda: fed.delete("users", "id=1"),
                       lambda: fed.delete_by_pk("users", "id", 1)):
                try:
                    fn()
                    raise AssertionError("users 表数据面写必须拒绝（含 system 角色）")
                except (PermissionDenied, _SecErr) as e:
                    assert "系统表" in str(e) or "认证表" in str(e) or "只读" in str(e), str(e)[:120]

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
            # 引号包裹 schema 前缀/逗号连接/换行星号
            rows = fed.query('SELECT * FROM "main"."users"')
            assert rows and "password_hash" not in rows[0], f"引号 schema 前缀应屏蔽: {rows}"
            rows = fed.query("SELECT\n  *\nFROM users")
            assert rows and "password_hash" not in rows[0], f"换行星号应屏蔽: {rows}"
            # 空白点号/不对称引号变体同样不得漏出凭证列
            for _variant in ("SELECT * FROM main . users",
                             'SELECT * FROM "main".users',
                             'SELECT * FROM main."users"',
                             "SELECT * FROM `main`.`users`",
                             "SELECT * FROM main.users"):
                rows = fed.query(_variant)
                assert rows and "password_hash" not in rows[0] and "salt" not in rows[0], \
                    f"schema 前缀变体应屏蔽凭证列: {_variant} -> {rows}"
            # 注释截断变体——FROM/**/ 与 -- 行注释同样不得让表提取落空
            for _variant in ("SELECT * FROM/**/users",
                             "SELECT * FROM users--\n",
                             "SELECT * FROM /*x*/ users"):
                rows = fed.query(_variant)
                assert rows and "password_hash" not in rows[0] and "salt" not in rows[0], \
                    f"注释截断变体应屏蔽凭证列: {_variant!r} -> {rows}"
            # 大小写混排不得落到默认数据源解析（users 注册在 legacy）
            rows = fed.query("SELECT * FROM USERS")
            assert rows and "password_hash" not in rows[0], f"大小写混排表名应屏蔽: {rows}"
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
    print("OK - 攻击面回归锁：整表只读/双引号载荷/WHERE 谓词/提取器形态全部在位")


def test_escalation_ttl_reentrant():
    """提权 TTL 到期自动失效 + 嵌套锁不死锁（RLock 回归锁）

    死锁路径：get_escalated_role 持 _ESCALATION_LOCK 判定过期 → 内调
    clear_escalation 再取同一把锁——非重入 Lock 在这里必挂。
    本用例 ttl=-1 让 expires_at 直接落在过去，瞬间踩中
    到期分支；若退回 Lock()，本测试将在嵌套取锁处永久阻塞直至层超时。
    """
    from core.permission import policy as pol
    try:
        pol.clear_escalation()
        assert pol.get_escalated_role() == "", "未提权时应为空角色"
        pol.set_escalated_role("admin", ttl_seconds=3600)
        assert pol.get_escalated_role() == "admin", "提权窗口内应生效"
        pol.set_escalated_role("admin", ttl_seconds=-1)  # 已过期
        assert pol.get_escalated_role() == "", "TTL 到期应自动失效（且不死锁）"
        pol.clear_escalation()
        assert pol.get_escalated_role() == "", "撤销后应读空"
    finally:
        pol.clear_escalation()
    print("OK - 提权 TTL 到期自动失效 + 嵌套锁可重入（RLock）")


def test_escalation_cross_process():
    """提权契约跨进程可见（回归锁）：
    A 进程 set → 独立 B 进程 get 到；A clear → B 见不到（设计核心场景：
    管理端批准落盘 → MCP 进程新鲜读取）"""
    import subprocess, sys as _sys
    from core.permission import policy as pol
    code = ("import sys; sys.path.insert(0,'.');"
            "from core.permission import policy;"
            "print(policy.get_escalated_role())")
    try:
        pol.set_escalated_role("admin", ttl_seconds=60)
        out = subprocess.run([_sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=30)
        assert out.stdout.strip() == "admin", \
            f"跨进程读不到提权: {out.stdout!r} {out.stderr[-200:]}"
        pol.clear_escalation()
        out2 = subprocess.run([_sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=30)
        assert out2.stdout.strip() == "", f"撤销后跨进程仍见提权: {out2.stdout!r}"
    finally:
        pol.clear_escalation()
    print("OK - 提权契约跨进程：A 置 B 见 / A 撤 B 不见")


def test_drop_index_datasource_coherence():
    """drop_index/无表闸的目标描述符同源（回归锁）：
    - guard 判定域=执行目标库（datasource 参数）——read_only 库上的
      DROP INDEX 必拒，而不是按默认库放行（旧实现判定/执行错位）
    - federated.drop_index 按对象实际所在库路由（不再只打默认库）
    - 多数据源同名索引如实报歧义（不静默选一个）"""
    import tempfile, yaml
    from pathlib import Path
    from unittest.mock import patch
    from core.datasource_manager import DataSourceManager
    from core.drivers.federated_driver import FederatedDriver
    from core.drivers.sqlite_driver import SqliteDriver
    from core.permission import PermissionPolicy, PermissionDenied
    from core.permission.sql_guard import guard_write_sql

    rules = {"default": "full",
             "users": {"bob": {"deny": ["drop", "ddl"]}}}
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {"datasources": {
            "primary": {"type": "sqlite", "path": f"{tmp}/p.db", "is_default": True},
            "legacy": {"type": "sqlite", "path": f"{tmp}/l.db"}}}
        cfg_path = Path(tmp) / "ds.yml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        raw = SqliteDriver(f"{tmp}/l.db")
        raw.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, v TEXT)")
        raw.execute("CREATE INDEX idx_sec ON t1 (v)")
        raw.conn.commit()
        dsm = DataSourceManager.new_instance()
        dsm.load_config(str(cfg_path))
        with patch.object(DataSourceManager, "_instance", dsm), \
             patch("core.permission.policy.PermissionPolicy.get_instance",
                   classmethod(lambda cls: PermissionPolicy.new_instance(rules))):
            # 1) 描述符：索引唯一定位在 legacy
            ds, st = dsm.resolve_object_datasource("idx_sec", "index")
            assert (ds, st) == ("legacy", "ok"), (ds, st)
            # 2) 无表闸：执行目标 legacy + readonly 角色 → 必拒（旧实现按默认库放行）
            from core.permission import set_current_user
            set_current_user("bob")
            try:
                guard_write_sql("DROP INDEX idx_sec", datasource="legacy")
                raise AssertionError("受限用户（deny drop）的 DROP INDEX 必须拒绝")
            except PermissionDenied:
                pass
            set_current_user("")  # 先复位用户，再走默认库对照（无用户规则全放）
            # 对照：执行目标默认库（full）→ 放行不误伤
            guard_write_sql("DROP INDEX idx_sec", datasource="")
            # 无表闸的同类：drop_view 同域判定（受限用户下必拒）
            set_current_user("bob")
            try:
                guard_write_sql("DROP VIEW v1", datasource="legacy")
                raise AssertionError("受限用户的 DROP VIEW 必须拒绝")
            except PermissionDenied:
                pass
            set_current_user("")
            # 4) federated 路由：按对象所在库取驱动（不再打默认库）
            fed = FederatedDriver.__new__(FederatedDriver)
            fed._dsm = dsm
            fed._dirty_drivers = set()
            fed._savepoint_stack = []
            fed.drop_index("idx_sec")
            left = raw.query("SELECT COUNT(*) AS c FROM sqlite_master WHERE name='idx_sec'")
            assert left[0]["c"] == 0, "索引必须按对象所在库被实际删除"

            # 5) 数据源级规则的判定域判别（N2 回归锁：把数据源名当表名解析曾
            # 恒落默认库——库级规则在 drop_index 上整体失效，且角色级断言
            # 结构性地测不到这个域错，必须用库级规则）
            raw.execute("CREATE INDEX idx_sec2 ON t1 (v)")
            raw.conn.commit()
            ds_rules = {"default": "full",
                        "datasources": {"legacy": {"mode": "read_only"}}}
            with patch("core.permission.policy.PermissionPolicy.get_instance",
                       classmethod(lambda cls: PermissionPolicy.new_instance(ds_rules))):
                try:
                    guard_write_sql("DROP INDEX idx_sec2", datasource="legacy")
                    raise AssertionError("read_only 数据源上的 DROP INDEX 必须拒绝")
                except PermissionDenied:
                    pass
                # 对照：同一语句在 full 的默认库上放行（证明判定域真在起作用）
                guard_write_sql("DROP INDEX idx_sec2", datasource="")
                # 契约层命名路径同域：drop_index 按对象所在库判 read_only 必拒
                from core.contract import ContractDriver
                cd = ContractDriver(dsm.get_driver("legacy"), "sqlite")
                try:
                    cd.drop_index("idx_sec2")
                    raise AssertionError("契约层 drop_index 在 read_only 库必须拒绝")
                except PermissionDenied:
                    pass
                except Exception as e:
                    assert "只读" in str(e) or "read_only" in str(e).lower(), str(e)[:100]
            # 5) 歧义如实报错
            raw2 = SqliteDriver(f"{tmp}/p.db")
            raw2.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, v TEXT)")
            raw2.execute("CREATE INDEX idx_dup ON t1 (v)")
            raw2.execute("CREATE INDEX idx_dup ON t1 (id)") if False else None
            raw2.conn.commit()
            raw.execute("CREATE INDEX idx_dup ON t1 (id)")
            raw.conn.commit()
            try:
                dsm.resolve_object_datasource("idx_dup", "index")
                raise AssertionError("多源同名必须如实报歧义")
            except Exception as e:
                assert "歧义" in str(e) or "多个数据源同名" in str(e), str(e)[:120]
            raw2.conn.close()
        raw.conn.close()
        # dsm 缓存的驱动连接一并关（Windows 文件锁——tempdir 清理前提）
        for _d in getattr(dsm, "_drivers", {}).values():
            try:
                _d.close()
            except Exception:
                pass  # 清理尽力而为
    print("OK - drop_index 目标描述符同源：判定=执行库/路由=对象库/歧义 fail-loud")


def test_datasource_role_key_canonical():
    """数据源/角色键规范形：规则键与调用方大小写错位时仍命中——
    裸 .get() 精确匹配会让错位键静默落空 fail-open 到 default:full
    （与表/列键的 _canon/_get_ci 同标准收口）"""
    from core.permission import Operation, PermissionDenied, PermissionPolicy
    p = PermissionPolicy.new_instance({
        "default": "full",
        "datasources": {"Legacy_DB": {"mode": "read_only"}},
        "users": {"Clerk": {"deny": ["delete"]}},
    })
    # 数据源键大小写错位：read_only 规则仍命中（不再静默失效）
    p.check("legacy_db", Operation.QUERY)
    try:
        p.check("legacy_db", Operation.INSERT)
        raise AssertionError("错位数据源键应仍命中 read_only")
    except PermissionDenied:
        pass
    # 用户键大小写错位：deny delete 仍命中
    from core.permission import set_current_user as _scu
    p2 = PermissionPolicy.new_instance({
        "default": "full",
        "users": {"Clerk": {"deny": ["delete"]}},
    })
    _scu("clerk")
    try:
        p2._check_user("clerk", Operation.DELETE)
        raise AssertionError("错位用户键应仍命中 deny")
    except PermissionDenied:
        pass
    p2._check_user("clerk", Operation.QUERY)
    _scu("")
    print("OK - 数据源/用户键规范形：大小写错位命中，不再静默 fail-open")


def test_escalation_sign_and_forgery():
    """提权契约 HMAC 验签（20260826 加签后的锁）：
    - 正常 set/get 回环可用
    - 直接改写 escalation.json（无签/错签/改 role 不改 sig）→ 一律拒认按未提权
    - 未置 MCP 通道旗标的进程吃到有效提权契约不生效（通道域化）"""
    from core.permission import policy as pol
    try:
        pol.clear_escalation()
        # 1) 正常回环
        pol.set_escalated_role("admin", ttl_seconds=60)
        assert pol.get_escalated_role() == "admin"
        # 2) 伪造：无签名
        pol._ESCALATION_CONTRACT.write({"role": "admin",
                                        "expires_at": 9999999999.0})
        assert pol.get_escalated_role() == "", "无签伪造契约必须拒认"
        # 3) 伪造：错签名
        pol._ESCALATION_CONTRACT.write({"role": "admin",
                                        "expires_at": 9999999999.0,
                                        "sig": "0" * 64})
        assert pol.get_escalated_role() == "", "错签伪造契约必须拒认"
        # 4) 篡改：真签名但改 role（签名绑 role+expires_at）
        pol.set_escalated_role("user", ttl_seconds=60)
        esc = dict(pol._ESCALATION_CONTRACT.read())
        esc["role"] = "admin"
        pol._ESCALATION_CONTRACT.write(esc)
        assert pol.get_escalated_role() == "", "篡改 role 后签名即失效，必须拒认"
        # 5) 通道域化：有效契约但进程未置 MCP 旗标 → get_effective_role 不吃提权
        pol.set_escalated_role("admin", ttl_seconds=60)
        pol.set_mcp_channel(False)
        assert pol.get_effective_role() != "admin", "非 MCP 通道进程不得吃提权窗口"
        pol.set_mcp_channel(True)
        assert pol.get_effective_role() == "admin", "MCP 通道进程应吃提权窗口"
        pol.set_mcp_channel(False)  # 复位：本进程默认非 MCP 通道
    finally:
        pol.set_mcp_channel(False)
        pol.clear_escalation()
    print("OK - 提权契约：验签/伪造拒认/篡改失效/通道域化")


def test_user_level_and_self_rules():
    """用户级规则（users.<用户名>）+ 自助收紧（deny-only）：
    - 同角色两用户差异化（user_1 禁删、user_2 白名单只允许查/插）
    - 用户级无法解禁上级（库级禁 ddl，用户白名单也救不回来——级联）
    - 自助规则只认 deny：自助禁查生效；手写 allow/mode 被忽略
    - 自助禁列进 denied_columns（query 屏蔽）"""
    import tempfile as _tf
    from pathlib import Path as _P
    from core.permission import (Operation, PermissionDenied, PermissionPolicy,
                                 set_current_user)
    import core.permission.policy as _pol
    rules = {
        "default": "full",
        "datasources": {"db1": {"mode": "custom", "deny": ["ddl"]}},
        "users": {
            "user_1": {"deny": ["delete"]},
            "user_2": {"allow": ["query", "insert"]},
        },
    }
    p = PermissionPolicy.new_instance(rules)
    # 同角色两用户差异化
    set_current_user("user_1")
    try:
        p.check("db1", Operation.DELETE, table="t1")
        raise AssertionError("user_1 应被用户级禁删")
    except PermissionDenied as e:
        assert "user_1" in str(e)
    p.check("db1", Operation.QUERY, table="t1")  # user_1 查放行
    set_current_user("user_2")
    try:
        p.check("db1", Operation.DELETE, table="t1")
        raise AssertionError("user_2 白名单外（delete）应被禁")
    except PermissionDenied:
        pass
    p.check("db1", Operation.INSERT, table="t1")  # 白名单内放行
    # 用户级无法解禁上级：库级禁 ddl，user_2 白名单含 ddl 也无效
    set_current_user("user_1")  # user_1 无 allow 键
    try:
        p.check("db1", Operation.DDL, table="t1")
        raise AssertionError("级联：库级禁 ddl 不可被用户级解禁")
    except PermissionDenied as e:
        assert "库级" in str(e), f"首个禁止级应为库级: {e}"

    # 自助收紧（deny-only，隔离临时规则文件）
    with _tf.TemporaryDirectory() as _tmp:
        orig = _pol._SELF_RULES_PATH
        _pol._SELF_RULES_PATH = _P(_tmp) / "self.yml"
        try:
            import yaml as _y
            _P(_tmp, "self.yml").write_text(_y.safe_dump({
                "user_1": {"deny": ["query"], "allow": ["query"],  # allow 属非法放松，应被忽略
                           "tables": {"t1": {"columns": {"c2": {"deny": ["query"]}}}}},
            }, allow_unicode=True), encoding="utf-8")
            set_current_user("user_1")
            try:
                p.check("db1", Operation.QUERY, table="t2")  # 自助禁查生效（注意 user_1 还有用户级禁删）
                raise AssertionError("自助禁 query 应生效")
            except PermissionDenied as e:
                assert "自助" in str(e), str(e)
            # 自助禁列进 denied_columns
            set_current_user("user_1")
            denied = p.denied_columns("db1", "t1", Operation.QUERY)
            assert "c2" in denied, f"自助禁列应进屏蔽集: {denied}"
        finally:
            _pol._SELF_RULES_PATH = orig
    set_current_user("")
    print("OK - 用户级规则：差异化/级联不可解禁 + 自助收紧 deny-only/列屏蔽")


if __name__ == "__main__":
    test_policy_modes()
    test_default_full_backward_compat()
    test_driver_enforcement()
    test_column_level_permissions()
    test_user_level_permissions()
    test_scope_shell_inherits_upward()
    test_cascade_upper_ban_not_overridable()
    test_user_level_and_self_rules()
    test_datasource_role_key_canonical()
    test_case_variant_attacks()
    test_round4_regression_locks()
    test_escalation_ttl_reentrant()
    test_escalation_cross_process()
    test_escalation_sign_and_forgery()
    test_drop_index_datasource_coherence()
    print("\n=== ALL PERMISSION TESTS PASSED ===")
