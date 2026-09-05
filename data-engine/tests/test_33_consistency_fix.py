# -*- coding: utf-8 -*-
"""层 33：一致性加固回归（20260809 修复 4 个生产致命点）

覆盖：
1. allow_heal="drop"：DB 多余表删除放行（护栏死锁修复）
2. 跨行业归属：任一行业 YAML 定义的表不算多余（表归属修复）
3. 跨行业删表 YAML 同步（正向不一致修复）
4. 无主表仍被识别为多余（反向不一致保留）
"""
import sys, tempfile, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

def _check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    return cond

def run():
    import core.schema_manager as sm
    ok = True

    # ── 1. 跨行业归属收集（不依赖 mock，直接扫真实 industries）──
    all_t = sm._all_industry_yaml_tables()
    # 适配单行业：只剩 construction_engineering + _test_* fixture 不参与
    # suppliers 等旧多行业表已随行业删除，不再被收集
    ok &= _check("跨行业收集不再含 suppliers（单行业已删）", "suppliers" not in all_t, f"共 {len(all_t)} 表")
    # 定额库 Schema 包随行业发布：4 表应被跨行业收集到
    quota4 = {"quota_items", "quota_labor", "quota_machines", "quota_materials"}
    ok &= _check("跨行业收集含定额库 4 表（行业包已发布）", quota4 <= all_t, f"缺 {sorted(quota4 - all_t)}")

    # ── 2. 多余表判定：无主表 = 多余，有主表 = 不算（用临时行业目录）──
    tmp = Path(tempfile.mkdtemp(prefix="fix_"))
    indA = tmp / "indA" / "schemas"; indA.mkdir(parents=True)
    indB = tmp / "indB" / "schemas"; indB.mkdir(parents=True)
    for d in (indA, indB):
        (d / "suppliers.yaml").write_text("business_name: suppliers\ncolumns:\n- name: id\n  type: INTEGER\nname: suppliers\n", encoding="utf-8")

    orig_all = sm._all_industry_yaml_tables
    sm._all_industry_yaml_tables = lambda: {"suppliers"}
    # mock DataSourceManager 抛异常 → _default_drv_tables 回退到传入的 db_tables
    import core.schema_manager as _sm_mod
    from core.datasource_manager import DataSourceManager as _RealDSM
    class _NoDSM:
        def get_default_name(self): return "primary"
        def get_driver(self, name): raise RuntimeError("no dsm")
    _orig_dsm = _sm_mod.DataSourceManager if hasattr(_sm_mod, "DataSourceManager") else _RealDSM
    try:
        # 注意：_check_tables 内部 `from core.datasource_manager import DataSourceManager`
        # 直接 import 模块，改 _sm_mod 无效；需 patch 模块属性
        import core.datasource_manager as _dsm_mod
        _orig_dsm_mod = _dsm_mod.DataSourceManager
        _dsm_mod.DataSourceManager = _NoDSM
        try:
            yaml_t, errA = sm._check_tables(indA, {"suppliers", "orphan_table", "meta_columns"})
            # suppliers 有主不算多余；orphan 无主算多余；meta_columns 豁免
            ok &= _check("无主表 orphan 判多余", "orphan_table" in str(errA), str(errA)[:70])
            ok &= _check("有主表 suppliers 不判多余", "suppliers" not in str(errA))
            ok &= _check("meta_ 前缀豁免", "meta_columns" not in str(errA))
            ok &= _check("_last_extra_tables 记录 orphan", sm._last_extra_tables == {"orphan_table"}, str(sm._last_extra_tables))
        finally:
            _dsm_mod.DataSourceManager = _orig_dsm_mod
    finally:
        sm._all_industry_yaml_tables = orig_all
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 3. allow_heal="drop" 真实装饰器行为（非复刻逻辑）──
    # 用真实 require_consistency(allow_heal="drop") 装饰一个临时函数，
    # mock _preflight_check 返回"多余表"错误，验证放行/拦截
    _orig_preflight = sm._preflight_check
    _orig_last = sm._last_extra_tables

    @sm.require_consistency(allow_heal="drop")
    def _fake_drop(table=""):
        return {"ok": True, "message": "executed"}

    # 场景1：待删表在多余表集合 + 错误是多余表错误 → 放行
    sm._last_extra_tables = {"orphan_table"}
    sm._preflight_check = lambda: "数据库中有配置文件未定义的表: orphan_table，可能是其他行业表残留；单行业应用请清理这些表或为其补充配置"
    r = _fake_drop(table="orphan_table")
    ok &= _check("真实装饰器放行多余表删除", r.get("ok") is True, str(r)[:60])

    # 场景2：待删表不在多余表集合 → 拦截
    sm._preflight_check = lambda: "数据库中有配置文件未定义的表: other，可能是其他行业表残留；单行业应用请清理这些表或为其补充配置"
    r = _fake_drop(table="other")
    ok &= _check("真实装饰器拦截非多余表删除", r.get("ok") is False, str(r)[:60])

    # 场景3：错误不是"多余表"类型（如缺表错误）→ 即使 tbl 在集合中也拦截
    sm._preflight_check = lambda: "配置与数据库不一致"
    sm._last_extra_tables = {"orphan_table"}
    r = _fake_drop(table="orphan_table")
    ok &= _check("非多余表错误不误放行", r.get("ok") is False, str(r)[:60])

    # 场景4：路径遍历表名（../）→ 即使看似多余也拦截（安全）
    sm._preflight_check = lambda: "数据库中有配置文件未定义的表: ..%2f..%2fetc，可能是其他行业表残留"
    sm._last_extra_tables = {"../etc/passwd"}
    r = _fake_drop(table="../etc/passwd")
    ok &= _check("路径遍历表名被拦截", r.get("ok") is False, str(r)[:60])

    sm._preflight_check = _orig_preflight
    sm._last_extra_tables = _orig_last

    # ── 4. 装饰器挂载断言（防止未来重构丢 allow_heal）──
    import inspect
    src = inspect.getsource(sm.drop_table)
    ok &= _check("drop_table 装饰器 allow_heal=drop", 'allow_heal="drop"' in src)

    # ── 5. 跨行业删表同步函数存在 + 路径遍历防护 ──
    ok &= _check("跨行业清理函数存在", hasattr(sm, "_remove_table_config_across_industries"))
    # 非法表名（含路径遍历）应抛 ValueError，绝不删除文件
    _traversal_rejected = False
    try:
        sm._remove_table_config_across_industries("../../outside")
    except ValueError:
        _traversal_rejected = True
    except Exception:
        _traversal_rejected = False
    ok &= _check("路径遍历表名在清理函数被拒", _traversal_rejected)
    # 合法表名不抛错（验证返回值类型，注入临时 industries_root，绝不碰真实目录）
    _tmp5 = Path(tempfile.mkdtemp(prefix="fix5_"))
    try:
        (_tmp5 / "industries").mkdir(parents=True, exist_ok=True)
        _r = sm._remove_table_config_across_industries(
            "nonexistent_table_xyz", industries_root=_tmp5 / "industries")
        ok &= _check("合法表名清理返回 dict", isinstance(_r, dict))
    finally:
        shutil.rmtree(_tmp5, ignore_errors=True)

    # ── 6. 回滚对称：跨行业删除返回 {path: 原内容}，内容可恢复 ──
    _rtmp = Path(tempfile.mkdtemp(prefix="fix_rb_"))
    _rindA = _rtmp / "industries" / "indA" / "schemas"; _rindA.mkdir(parents=True)
    _rindB = _rtmp / "industries" / "indB" / "schemas"; _rindB.mkdir(parents=True)
    _content = "business_name: rollback_test\ncolumns:\n- name: id\n  type: INTEGER\nname: rollback_test\n"
    for _d in (_rindA, _rindB):
        (_d / "rollback_test.yaml").write_text(_content, encoding="utf-8")
    # 用真实 _remove_table_config_across_industries 测试（industries_root 注入临时根）
    _orig_load_config = sm._load_config
    sm._load_config = lambda: {"tables": [], "field_dict": {}}  # 当前行业为空，跳过 _save_config 写入
    try:
        _deleted_map = sm._remove_table_config_across_industries(
            "rollback_test", industries_root=_rtmp / "industries")
    finally:
        sm._load_config = _orig_load_config
    ok &= _check("真实函数跨行业删除返回映射", len(_deleted_map) == 2, f"{len(_deleted_map)} 个")
    ok &= _check("返回内容可恢复（原样保留）", all(v == _content for v in _deleted_map.values()))
    ok &= _check("文件确实已删除", not (_rindA / "rollback_test.yaml").exists())
    # 恢复（回滚模拟）
    for _p, _c in _deleted_map.items():
        Path(_p).write_text(_c, encoding="utf-8")
    ok &= _check("回滚重写恢复文件", (_rindA / "rollback_test.yaml").exists()
                 and (_rindA / "rollback_test.yaml").read_text(encoding="utf-8") == _content)
    shutil.rmtree(_rtmp, ignore_errors=True)

    # ── 7. holder 时序：unlink 前 holder 已含完整映射（MEDIUM 修复核心）──
    # mock unlink 抛异常，验证 holder 仍被置入（回滚有据）
    _rtmp2 = Path(tempfile.mkdtemp(prefix="fix_hold_"))
    _r2a = _rtmp2 / "industries" / "indA" / "schemas"; _r2a.mkdir(parents=True)
    _content2 = "name: hold_test\n"
    (_r2a / "hold_test.yaml").write_text(_content2, encoding="utf-8")
    _holder = {}
    _orig_load = sm._load_config
    sm._load_config = lambda: {"tables": [], "field_dict": {}}
    _orig_unlink = None
    try:
        import pathlib
        _orig_unlink = pathlib.Path.unlink
        def _boom(self, *a, **kw):
            raise OSError("simulated unlink failure")
        pathlib.Path.unlink = _boom
        try:
            sm._remove_table_config_across_industries("hold_test",
                industries_root=_rtmp2 / "industries", holder=_holder)
            _holder_populated = False  # 不应走到这里——unlink 必抛异常
        except OSError:
            _holder_populated = ("deleted" in _holder
                                 and len(_holder["deleted"]) == 1
                                 and "hold_test" in _holder["deleted"].get(next(iter(_holder["deleted"])), ""))
    finally:
        if _orig_unlink is not None:
            pathlib.Path.unlink = _orig_unlink
        sm._load_config = _orig_load
        shutil.rmtree(_rtmp2, ignore_errors=True)
    ok &= _check("unlink 异常时 holder 已含完整映射（可回滚）", _holder_populated)

    # ── 8. 真实 drop_table 删除 DB 多余表（review blocking 修复验证）──
    # 场景：DB 有表 orphan_real、当前行业 YAML 无此表（_last_extra_tables 场景）
    # 装饰器 allow_heal="drop" 放行后，drop_table 必须真正删掉 DB 表（而非"表不存在"）
    _orig_drop_load = sm._load_config
    _orig_drop_driver = sm.get_driver
    _orig_drop_fk = sm._check_fk_references
    _orig_drop_commit = sm._commit_table_delete
    _dropped = []
    class _MockDrv:
        def list_tables(self):
            return ["users", "orphan_real"]
        def drop_table(self, t):
            _dropped.append(t)
        def get_referencing_tables(self, t):
            return []
    try:
        sm._load_config = lambda: {"tables": [], "field_dict": {}}
        sm.get_driver = lambda: _MockDrv()
        sm._check_fk_references = lambda t: []
        sm._commit_table_delete = lambda t, holder=None: None
        # 装饰器在真实 drop_table 上：先 mock preflight 让"多余表"检查放行
        _orig_preflight = sm._preflight_check
        sm._preflight_check = lambda: "数据库中有配置文件未定义的表: orphan_real，可能是其他行业表残留"
        sm._last_extra_tables = {"orphan_real"}
        try:
            r = sm.drop_table("orphan_real")
        finally:
            sm._preflight_check = _orig_preflight
        ok &= _check("真实 drop_table 删除 DB 多余表", r.get("ok") is True, str(r)[:70])
        ok &= _check("DB 表确实被删（driver 收到 drop）", "orphan_real" in _dropped, str(_dropped))
    finally:
        sm._load_config = _orig_drop_load
        sm.get_driver = _orig_drop_driver
        sm._check_fk_references = _orig_drop_fk
        sm._commit_table_delete = _orig_drop_commit

    # ── 9. 系统表豁免：YAML 无配置时也不能删系统表（should-fix 安全边际）──
    _orig9_load = sm._load_config
    _orig9_driver = sm.get_driver
    _orig9_fk = sm._check_fk_references
    _orig9_commit = sm._commit_table_delete
    _dropped9 = []
    class _MockDrv9:
        def list_tables(self):
            return ["users", "orphan_real"]
        def drop_table(self, t):
            _dropped9.append(t)
        def get_referencing_tables(self, t):
            return []
    try:
        sm._load_config = lambda: {"tables": [], "field_dict": {}}
        sm.get_driver = lambda: _MockDrv9()
        sm._check_fk_references = lambda t: []
        sm._commit_table_delete = lambda t, holder=None: None
        _orig9_preflight = sm._preflight_check
        sm._preflight_check = lambda: ""  # 无不一致
        try:
            r9 = sm.drop_table("users")
        finally:
            sm._preflight_check = _orig9_preflight
        ok &= _check("系统表 users 被拒（YAML 无配置也不删）",
                     r9.get("ok") is False and "系统表" in str(r9.get("message", "")), str(r9)[:70])
        ok &= _check("users 未被删（driver 未收到 drop）", "users" not in _dropped9, str(_dropped9))
    finally:
        sm._load_config = _orig9_load
        sm.get_driver = _orig9_driver
        sm._check_fk_references = _orig9_fk
        sm._commit_table_delete = _orig9_commit

    # ── 10. 系统表豁免覆盖正常路径（YAML 有 users 配置也不能删）──
    # security_review MEDIUM：豁免前置到 drop_table 入口，覆盖全部分支
    _orig10_load = sm._load_config
    _orig10_driver = sm.get_driver
    _orig10_fk = sm._check_fk_references
    _orig10_commit = sm._commit_table_delete
    _dropped10 = []
    class _MockDrv10:
        def list_tables(self):
            return ["users"]
        def drop_table(self, t):
            _dropped10.append(t)
        def get_referencing_tables(self, t):
            return []
    try:
        # YAML 里有 users 配置（正常路径）→ 仍应被豁免拒绝
        sm._load_config = lambda: {"tables": [{"name": "users", "columns": [{"name": "id", "type": "INTEGER"}]}], "field_dict": {}}
        sm.get_driver = lambda: _MockDrv10()
        sm._check_fk_references = lambda t: []
        sm._commit_table_delete = lambda t, holder=None: None
        _orig10_preflight = sm._preflight_check
        sm._preflight_check = lambda: ""  # 无不一致
        try:
            r10 = sm.drop_table("users")
        finally:
            sm._preflight_check = _orig10_preflight
        ok &= _check("正常路径系统表 users 也被拒", r10.get("ok") is False and "系统表" in str(r10.get("message", "")), str(r10)[:70])
        ok &= _check("users 未被删（正常路径）", "users" not in _dropped10, str(_dropped10))
    finally:
        sm._load_config = _orig10_load
        sm.get_driver = _orig10_driver
        sm._check_fk_references = _orig10_fk
        sm._commit_table_delete = _orig10_commit

    # ── 11. 大小写变体绕过防护：drop_table("USERS") 也必须被拒（HIGH）──
    # security_review 复查：豁免必须大小写归一化（SQLite 表名大小写不敏感）
    _orig11_load = sm._load_config
    _orig11_driver = sm.get_driver
    _orig11_fk = sm._check_fk_references
    _orig11_commit = sm._commit_table_delete
    _dropped11 = []
    class _MockDrv11:
        def list_tables(self):
            return ["users"]
        def drop_table(self, t):
            _dropped11.append(t)
        def get_referencing_tables(self, t):
            return []
    try:
        sm._load_config = lambda: {"tables": [], "field_dict": {}}
        sm.get_driver = lambda: _MockDrv11()
        sm._check_fk_references = lambda t: []
        sm._commit_table_delete = lambda t, holder=None: None
        _orig11_preflight = sm._preflight_check
        sm._preflight_check = lambda: ""  # 无不一致
        try:
            r11 = sm.drop_table("USERS")
        finally:
            sm._preflight_check = _orig11_preflight
        ok &= _check("大小写变体 USERS 也被拒", r11.get("ok") is False and "系统表" in str(r11.get("message", "")), str(r11)[:70])
        ok &= _check("USERS 未被删（大小写变体）", "USERS" not in _dropped11 and "users" not in _dropped11, str(_dropped11))
    finally:
        sm._load_config = _orig11_load
        sm.get_driver = _orig11_driver
        sm._check_fk_references = _orig11_fk
        sm._commit_table_delete = _orig11_commit

    # ── 12. driver 层豁免：绕过 schema_manager 直接删系统表也被拒（HIGH 旁路）──
    # security_review 复查：DELETE /api/schema-graph/table/{name} → SchemaGraphService
    # → drv.drop_table 绕过 schema_manager 豁免 → 在 ContractDriver.drop_table 加兜底防线
    from core.contract.base import ContractDriver
    from core.exceptions import SecurityError as _SecErr
    _called12 = []
    class _RawDrv12:
        def drop_table(self, t):
            _called12.append(t)
            return f"deleted {t}"
    try:
        _cd = ContractDriver(_RawDrv12(), driver_type="sqlite")
        try:
            _cd.drop_table("USERS")
            _rejected12 = False
        except _SecErr:
            _rejected12 = True
        except Exception:
            _rejected12 = False
        ok &= _check("driver 层 USERS 被拒（SecurityError）", _rejected12)
        ok &= _check("底层 driver 未被调用", "USERS" not in _called12 and "users" not in _called12, str(_called12))
        # 非系统表正常放行
        try:
            _cd.drop_table("orders")
            _orders_ok = True
        except Exception:
            _orders_ok = False
        ok &= _check("driver 层非系统表正常放行", _orders_ok and "orders" in _called12, str(_called12))
    finally:
        _called12 = []

    # ── 13. clear_database FK finally：非 SecurityError 异常也恢复 FK ──
    # review should-fix：try 上移到 FK OFF 之后，异常路径也必须恢复
    _fk_states = []
    class _FkConn:
        def execute(self, sql):
            _fk_states.append(sql)
    class _Drv13:
        def list_tables(self):
            return ["orders", "users"]
        def get_referencing_tables(self, t):
            return []
        def drop_table(self, t):
            if t == "orders":
                raise RuntimeError("simulated drop failure")  # 非 SecurityError
            return "ok"
        def commit(self):
            pass
    _orig13_load = sm._load_config
    _orig13_driver = sm.get_driver
    _orig13_unwrap = sm._unwrap_sqlite_conn
    try:
        sm._load_config = lambda: {"tables": [], "field_dict": {}}
        sm.get_driver = lambda: _Drv13()
        sm._unwrap_sqlite_conn = lambda drv: _FkConn()
        try:
            sm.clear_database(drop_tables=True)
            _ex13 = None
        except Exception as e:
            _ex13 = e
        # 非 SecurityError 异常应传播
        ok &= _check("clear_database 异常传播（非 SecurityError）", _ex13 is not None and isinstance(_ex13, RuntimeError), str(_ex13)[:50] if _ex13 else "")
        # FK 恢复执行（finally）：OFF 后必有 ON
        ok &= _check("FK finally 恢复（OFF→ON 成对）",
                     _fk_states.count("PRAGMA foreign_keys=OFF") == 1
                     and _fk_states.count("PRAGMA foreign_keys=ON") == 1
                     and _fk_states[-1] == "PRAGMA foreign_keys=ON", str(_fk_states))
    finally:
        sm._load_config = _orig13_load
        sm.get_driver = _orig13_driver
        sm._unwrap_sqlite_conn = _orig13_unwrap

    # ── 14. execute() 系统表豁免（security_review MEDIUM 纵深缺口）──
    # DROP TABLE users 经 execute 放行会绕过 drop_table 豁免 → 加 _validate_execute_sql 拦截
    from core.contract.base import _validate_execute_sql
    from core.exceptions import SecurityError as _SecErr14
    _rej14 = None
    try:
        _validate_execute_sql("DROP TABLE users")
        _rej14 = "NOT_REJECTED"
    except _SecErr14:
        _rej14 = "REJECTED"
    except Exception as e:
        _rej14 = f"OTHER:{type(e).__name__}"
    ok &= _check("execute DROP TABLE users 被拒", _rej14 == "REJECTED", str(_rej14))
    # 大小写变体 + IF EXISTS
    _rej14b = None
    try:
        _validate_execute_sql("drop table if exists USERS")
        _rej14b = "NOT_REJECTED"
    except _SecErr14:
        _rej14b = "REJECTED"
    except Exception as e:
        _rej14b = f"OTHER:{type(e).__name__}"
    ok &= _check("execute DROP IF EXISTS USERS 大小写变体被拒", _rej14b == "REJECTED", str(_rej14b))
    # 非系统表放行
    _ok14 = None
    try:
        _validate_execute_sql("DROP TABLE orders")
        _ok14 = "ALLOWED"
    except Exception:
        _ok14 = "REJECTED"
    ok &= _check("execute DROP TABLE orders 放行", _ok14 == "ALLOWED", str(_ok14))
    # 内部固定 SQL（建索引）不受影响
    _ok14b = None
    try:
        _validate_execute_sql('CREATE UNIQUE INDEX IF NOT EXISTS "idx_fed_a_code" ON "fed_a" ("code")')
        _ok14b = "ALLOWED"
    except Exception:
        _ok14b = "REJECTED"
    ok &= _check("execute CREATE INDEX 正常放行", _ok14b == "ALLOWED", str(_ok14b))
    # 方括号标识符（SQLite 方言）绕过防护
    _rej14c = None
    try:
        _validate_execute_sql("DROP TABLE [users]")
        _rej14c = "NOT_REJECTED"
    except _SecErr14:
        _rej14c = "REJECTED"
    except Exception as e:
        _rej14c = f"OTHER:{type(e).__name__}"
    ok &= _check("execute DROP TABLE [users] 方括号被拒", _rej14c == "REJECTED", str(_rej14c))
    # schema 前缀绕过防护
    _rej14d = None
    try:
        _validate_execute_sql("DROP TABLE main.users")
        _rej14d = "NOT_REJECTED"
    except _SecErr14:
        _rej14d = "REJECTED"
    except Exception as e:
        _rej14d = f"OTHER:{type(e).__name__}"
    ok &= _check("execute DROP TABLE main.users 前缀被拒", _rej14d == "REJECTED", str(_rej14d))
    # schema 前缀 + 非系统表放行
    _ok14c = None
    try:
        _validate_execute_sql("DROP TABLE main.orders")
        _ok14c = "ALLOWED"
    except Exception:
        _ok14c = "REJECTED"
    ok &= _check("execute DROP TABLE main.orders 前缀非系统表放行", _ok14c == "ALLOWED", str(_ok14c))
    # 包裹的 schema 前缀 + 系统表（SQLite 方言 [main].[users]）
    _rej14e = None
    try:
        _validate_execute_sql("DROP TABLE [main].[users]")
        _rej14e = "NOT_REJECTED"
    except _SecErr14:
        _rej14e = "REJECTED"
    except Exception as e:
        _rej14e = f"OTHER:{type(e).__name__}"
    ok &= _check("execute DROP TABLE [main].[users] 包裹前缀被拒", _rej14e == "REJECTED", str(_rej14e))
    # 包裹的 schema 前缀 + 非系统表放行
    _ok14d = None
    try:
        _validate_execute_sql('DROP TABLE "main"."orders"')
        _ok14d = "ALLOWED"
    except Exception:
        _ok14d = "REJECTED"
    ok &= _check("execute DROP TABLE \"main\".\"orders\" 包裹前缀非系统表放行", _ok14d == "ALLOWED", str(_ok14d))

    # ── FileLock 陈尸回收：持有线程死亡而注册项未清时，
    # 取锁线程不得被误判"同线程重入"——陈尸（fd+计数）回收后按新锁取。
    #（存活核查若拿调用方自己的 ident 则恒真，即成不可达死代码——本用例在
    #  旧实现下计数 3→4 且永不释放，断言必红）
    import os as _os_fl, weakref as _wr_fl, threading as _th_fl, tempfile as _tf_fl
    from core.file_contract import FileLock as _FL
    with _tf_fl.TemporaryDirectory() as _tmp_fl:
        _lp = _os_fl.path.join(_tmp_fl, "x.lock")
        _key = (_lp, _th_fl.get_ident())
        _fd = _os_fl.open(_lp, _os_fl.O_CREAT | _os_fl.O_RDWR)
        class _DeadFl:
            pass
        _d = _DeadFl()
        _ref = _wr_fl.ref(_d)
        del _d
        _FL._REGISTRY[_key] = (_fd, 3, _ref)  # 陈尸：计数非零、持有者已死
        _fl_ok = True
        try:
            with _FL(_lp, timeout=2):
                pass  # 命中陈尸 → 回收 → 新取锁成功（超时=回收失败）
            _fl_ok = _FL._REGISTRY.get(_key) is None
        except Exception as _e_fl:
            _fl_ok = False
        finally:
            _FL._REGISTRY.pop(_key, None)
    ok &= _check("FileLock 陈尸回收：死持有者不误判重入且锁可释放", _fl_ok)

    print()
    if not ok:
        print("❌ 层 33 失败")
        return False
    print("✅ 层 33 全部通过：一致性加固")
    return True


def test_run():
    """pytest 兼容入口：run() 失败时抛 AssertionError（而非 sys.exit）"""
    ok = run()
    assert ok, "层 33 一致性加固测试失败"


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
