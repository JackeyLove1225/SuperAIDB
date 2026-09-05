"""层 35：数据守护进程（core/daemon）——拉起/令牌/CRUD 对等/事务/角色传递

判据：DaemonDriver 是 29 接口的 RPC 代理——经它的操作与直连驱动结果一致；
无令牌/错令牌必拒；daemon 可被客户端自动拉起。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _scratch_ds(tmp: str) -> str:
    """注册一个临时数据源（yaml 写在 config/datasources.yml 追加后删）"""
    import yaml
    from core.datasource_manager import DataSourceManager
    ds_path = os.path.join("config", "datasources.yml")
    with open(ds_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    db_file = os.path.join(tmp, "t35.db")
    cfg.setdefault("datasources", {})["t35_scratch"] = {
        "type": "sqlite", "path": db_file, "is_default": False}
    with open(ds_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    DataSourceManager.reset_instance()
    return db_file


def _drop_scratch(ds_path: str):
    import yaml
    from core.datasource_manager import DataSourceManager
    with open(ds_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.get("datasources", {}).pop("t35_scratch", None)
    with open(ds_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    DataSourceManager.reset_instance()


def test_daemon_end_to_end():
    """拉起 → 建表 → 增查改删 → 事务回滚 → 列结构，全经 RPC 与直连对等"""
    from core.daemon.client import DaemonDriver
    from core.daemon.runtime import read_runtime
    with tempfile.TemporaryDirectory() as tmp:
        _scratch_ds(tmp)
        try:
            # 生产组装形态：ContractDriver 包 DaemonDriver（DataSourceManager.get_driver
            # 出厂同款）——CRUD 对等判据必须覆盖契约+RPC 双层，而非裸 RPC 自嗨
            #（裸 RPC 曾掩盖 add_column 签名漂移：daemon 模式生产路径必炸而测试全绿）
            from core.contract import ContractDriver
            drv = ContractDriver(DaemonDriver("t35_scratch"), "sqlite")  # ensure_daemon 自动拉起
            rt = read_runtime()
            assert rt and rt.get("port") and rt.get("token"), "运行文件应有端口+令牌"

            # 建表 → 插入 → 查询
            r = drv.create_table({"name": "t_demo", "business_name": "演示",
                                  "columns": [{"name": "id", "type": "INTEGER", "pk": True, "business_name": "主键"},
                                              {"name": "code", "type": "TEXT",
                                               "business_name": "编码"}]})
            assert "t_demo" in drv.list_tables(), f"建表未生效: {r}"
            drv.insert("t_demo", [{"code": "A1"}, {"code": "A2"}])
            rows = drv.query("SELECT * FROM t_demo ORDER BY id")
            assert [r["code"] for r in rows] == ["A1", "A2"], rows

            # 改删
            drv.update("t_demo", "code='A1x'", "id=1")
            assert drv.query("SELECT code FROM t_demo WHERE id=1")[0]["code"] == "A1x"
            drv.delete_by_pk("t_demo", "id", 2)
            assert len(drv.query("SELECT * FROM t_demo")) == 1

            # 事务：先收敛前序 DML 的隐式事务，再 begin → 插 → rollback → 不可见
            drv.commit()
            drv.begin()
            drv.insert("t_demo", [{"code": "TX"}])
            drv.rollback()
            assert len(drv.query("SELECT * FROM t_demo WHERE code='TX'")) == 0, "回滚后应不可见"

            # 结构读取
            cols = {c["name"] for c in drv.get_columns("t_demo")}
            assert {"id", "code"} <= cols, cols
            assert drv.table_exists("t_demo") and drv.column_exists("t_demo", "code")

            # 接口面扩面（生产 daemon 模式覆盖底座）：DDL/索引/改名/execute 护栏
            drv.add_column("t_demo", "extra", "TEXT")
            assert drv.column_exists("t_demo", "extra")
            drv.modify_column("t_demo", "extra", "TEXT")
            drv.create_index("t_demo", "code", unique=False)
            drv.execute("INSERT INTO t_demo (code, extra) VALUES ('E1', 'x')")
            assert drv.query("SELECT extra FROM t_demo WHERE code='E1'")[0]["extra"] == "x"
            drv.rename_table("t_demo", "t_demo2")
            assert not drv.table_exists("t_demo") and drv.table_exists("t_demo2")
            idx_rows = drv.query(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='t_demo2'")
            if idx_rows:  # 自动命名索引存在则删（drop_index 走默认数据源语义）
                drv.drop_index(idx_rows[0]["name"])
            drv.drop_column("t_demo2", "extra")
            assert not drv.column_exists("t_demo2", "extra")
            assert drv.get_referencing_tables("t_demo2") == []
            assert drv.ping()

            # 令牌错误必拒
            from core.daemon.protocol import rpc_call
            try:
                rpc_call(rt["port"], "wrong-token", "list_tables", {})
                raise SystemExit("错令牌竟然通了——认证失效！")
            except RuntimeError as e:
                assert "令牌无效" in str(e), str(e)
        finally:
            try:
                drv.close()  # 释放 daemon 侧文件句柄（Windows 下 tmp 目录才能删）
            except Exception:
                pass
            _drop_scratch("config/datasources.yml")
    print("OK - daemon 端到端：自动拉起/CRUD/事务回滚/结构读取/令牌拒伪")


def test_daemon_role_passthrough():
    """身份随调用传递：daemon 侧权限判定用调用方真实身份（受限用户写入被拦，
    _user 随 RPC 传递——用户级规则在 daemon 侧同样生效）"""
    from core.daemon.client import DaemonDriver
    from core.permission import set_current_user
    import yaml as _y
    from core.config_hub import write_yaml_atomic
    _perm_path = "config/permissions.yml"
    _orig_rules = open(_perm_path, encoding="utf-8").read()
    _doc = _y.safe_load(_orig_rules) or {}
    _doc.setdefault("users", {})["bob"] = {"deny": ["insert", "update", "delete", "ddl", "drop"]}
    write_yaml_atomic(_perm_path, _doc, backup=False)
    with tempfile.TemporaryDirectory() as tmp:
        _scratch_ds(tmp)
        try:
            drv = DaemonDriver("t35_scratch")
            # 信任模型：daemon 不做身份钳制（认证在 mgmt/MCP 层），
            # 调用方进程无身份（system+无用户）——DDL 直接放行
            drv.create_table({"name": "t_role", "business_name": "角色测试",
                              "columns": [{"name": "id", "type": "INTEGER", "pk": True, "business_name": "主键"},
                                          {"name": "v", "type": "TEXT",
                                           "business_name": "值"}]})
            # 受限用户写入：daemon 侧应被权限层拦（users.bob deny 全写）
            # 与直连同语义：权限拒绝以异常传播（RPC 侧还原为 RuntimeError）
            set_current_user("bob")
            try:
                r = drv.insert("t_role", [{"v": "x"}])
                blocked, text = False, str(r)
            except RuntimeError as e:
                blocked, text = True, str(e)
            set_current_user("")
            assert blocked and ("权限不足" in text or "禁止" in text), \
                f"受限用户写入应被 daemon 侧权限层拦截: {text[:100]}"
        finally:
            set_current_user("")
            try:
                drv.close()
            except Exception:
                pass
            write_yaml_atomic(_perm_path, _y.safe_load(_orig_rules), backup=False)
            _drop_scratch("config/datasources.yml")
    print("OK - 身份随 RPC 传递：受限用户写入在 daemon 侧被拦（_user 传递）")


def test_daemon_client_self_heal():
    """客户端自愈：daemon 被杀后，下一次调用自动重拉并完成"""
    import time
    from core.daemon.client import DaemonDriver
    from core.daemon.runtime import read_runtime
    with tempfile.TemporaryDirectory() as tmp:
        _scratch_ds(tmp)
        try:
            drv = DaemonDriver("t35_scratch")
            rt = read_runtime()
            assert rt and rt.get("pid")
            # 杀掉 daemon（模拟崩溃/断电）——运行文件残留旧端口
            import os as _os
            _os.kill(int(rt["pid"]), 9)
            time.sleep(1.0)
            # 旧端口已死：list_tables 应经自愈链（重读运行文件失败→ensure_daemon 重拉）
            tables = drv.list_tables()
            assert isinstance(tables, list), f"自愈后应正常返回: {tables}"
            rt2 = read_runtime()
            assert rt2 and rt2["pid"] != rt["pid"], "应拉起了新 daemon"
        finally:
            try:
                drv.close()
            except Exception:
                pass
            _drop_scratch("config/datasources.yml")
    print("OK - daemon 崩溃自愈：客户端自动重拉，业务零中断")


def test_daemon_env_contract():
    """环境契约：缺 DAEMON_MODE=false 拉起的 daemon 启动即失败
    （fail-fast），而不是业务假活"""
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    env.pop("DAEMON_MODE", None)  # 剥离契约变量
    r = subprocess.run([sys.executable, "-m", "core.daemon.server"],
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 2, f"缺环境契约应退出码 2，实际 {r.returncode}: {r.stderr[-300:]}"
    assert "DAEMON_MODE" in (r.stderr + r.stdout), "报错应指明契约"
    print("OK - daemon 环境契约：缺 DAEMON_MODE=false 启动即失败（不假活）")


def test_maintenance_window():
    """维护窗：flag 置位期间 daemon 业务调用如实拒绝，清除后恢复"""
    from core.daemon.client import DaemonDriver
    from core.daemon.runtime import set_maintenance
    with tempfile.TemporaryDirectory() as tmp:
        _scratch_ds(tmp)
        try:
            drv = DaemonDriver("t35_scratch")
            drv.list_tables()
            set_maintenance(True)
            try:
                drv.list_tables()
                raise AssertionError("维护窗期间调用应拒绝")
            except RuntimeError as e:
                assert "维护" in str(e), str(e)
            set_maintenance(False)
            assert isinstance(drv.list_tables(), list), "清除后应恢复"
        finally:
            set_maintenance(False)
            try:
                drv.close()
            except Exception:
                pass
            _drop_scratch("config/datasources.yml")
    print("OK - 维护窗：置位拒绝/清除恢复（恢复期间不再被抢库）")


def test_maintenance_ttl():
    """维护窗 TTL（P4-D3）：超期旗标=断电残留，自动清除并按非维护放行

    不真等 30 分钟——直接写一个过去的 mtime/内容，in_maintenance 应立即
    自清并返回 False；新置位的旗标必须在窗内判 True。
    """
    from core.daemon import runtime as rt
    try:
        # 超期残留：内容时间戳设为 1 小时前
        rt.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        rt.MAINTENANCE_FILE.write_text(str(time.time() - 3600), encoding="utf-8")
        assert rt.in_maintenance() is False, "超期旗标应判非维护"
        assert not rt.MAINTENANCE_FILE.exists(), "超期旗标应被自动清除"
        # 窗内置位：判维护
        rt.set_maintenance(True)
        assert rt.in_maintenance() is True, "窗内旗标必须判维护"
        rt.set_maintenance(False)
        assert rt.in_maintenance() is False, "显式清除后判非维护"
    finally:
        rt.set_maintenance(False)
    print("OK - 维护窗 TTL：超期自清放行/窗内判定/显式清除")


def test_role_kwarg_cannot_override():
    """daemon _role 写序安全（安全复核 N1 回归锁）：调用方在 kwargs 里
    塞 _role="system" 不得顶掉真实角色——合并顺序必须 _role 在后"""
    from unittest.mock import patch
    from core.daemon import client as dc
    from core.permission import set_current_role
    drv = dc.DaemonDriver.__new__(dc.DaemonDriver)
    drv._ds, drv._port, drv._token = "primary", 0, ""
    drv._local = dc._th.local()
    drv._local.session = "s"
    try:
        set_current_role("readonly")
        with patch.object(dc, "rpc_call", return_value={"result": []}) as m:
            drv._call("query", sql="SELECT 1", _role="system")
            args = m.call_args[0][3]  # rpc_call(port, token, method, args, ...)
            assert args["_role"] == "readonly",                 f"调用方注入的 _role 顶掉了真实角色: {args['_role']}"
    finally:
        set_current_role("system")
    print("OK - daemon _role 写序：调用方注入不得顶掉真实角色")


if __name__ == "__main__":
    test_daemon_end_to_end()
    test_daemon_role_passthrough()
    test_daemon_client_self_heal()
    test_daemon_env_contract()
    test_maintenance_window()
    test_maintenance_ttl()
    test_role_kwarg_cannot_override()
    print("\n=== ALL DAEMON TESTS PASSED ===")
