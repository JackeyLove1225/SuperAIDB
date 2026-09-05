"""层 37：Management HTTP API 面（TestClient 离线，无需真实服务端口）

判据（管理端 13 个 router 在 CI 全黑，此处收口记录写路径）：
- 写端点事务纪律：update-by-pk/insert 提交后新连接可读（无 commit 则重启即丢）
- 参数守卫：缺 pk_value / 主键在 values / 非法表名列名 → 400
- 认证闸：API_KEY_ENABLED=true 时无/错 key → 401
- 大小写变体攻击经 HTTP 面被屏蔽（契约单栈在 HTTP 入口同样生效）
- 字面值 round-trip：O'Brien 类撇号值完整落库（doubling 转义链）
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))



def _mk_client(tmp):
    """TestClient + 临时库驱动接管（core.data_ops.get_driver 调用期动态 import，
    打它即接管 deps.get_driver 的货源——不碰真实开发库）"""
    from fastapi.testclient import TestClient
    from core.drivers.sqlite_driver import SqliteDriver
    from core.contract import ContractDriver

    raw = SqliteDriver(os.path.join(tmp, "mg.db"))
    raw.execute("CREATE TABLE t37 (id INTEGER PRIMARY KEY, name TEXT, price REAL)")
    raw.execute("INSERT INTO t37 (name, price) VALUES ('初始', 1.5)")
    raw.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, "
                "password_hash TEXT, salt TEXT, role TEXT)")
    raw.execute("INSERT INTO users (username, password_hash, salt) VALUES ('a', 'hh', 'ss')")
    raw.conn.commit()
    drv = ContractDriver(raw, "sqlite")

    import core.data_ops as _ops
    _orig = _ops.get_driver
    _ops.get_driver = lambda: drv

    from agent.management.server import mgmt_app
    client = TestClient(mgmt_app)
    return client, raw, _ops, _orig


def _restore(_ops, _orig):
    _ops.get_driver = _orig


def _isolated_auth(tmp):
    """临时用户库接管 core.auth（X-API-Key 废除后的测试通道，20260903）

    中间件 verify_token 走 core.auth._get_db_path——patch 必须覆盖
    整个请求期，故以 contextmanager 供测试体包裹；返回 admin token。
    连接缓存在退出前显式释放（Windows 锁）。
    """
    from contextlib import contextmanager
    from pathlib import Path
    from unittest.mock import patch
    import core.auth as auth

    @contextmanager
    def _cm():
        db_path = str(Path(tmp) / "users_test.db")
        with patch.object(auth, "_get_db_path", lambda: db_path):
            try:
                auth.init_users_table()
                r = auth.register_user("t37admin", "TestBot#2026", "admin")
                assert r["ok"], r
                lr = auth.login_user("t37admin", "TestBot#2026")
                assert lr["ok"], lr
                yield lr["token"]
            finally:
                auth._close_all_conns()
    return _cm()


def test_record_write_tx_and_guards():
    from config.settings import settings
    orig_enabled = settings.API_KEY_ENABLED
    settings.API_KEY_ENABLED = "true"
    with tempfile.TemporaryDirectory() as tmp:
        client, raw, _ops, _orig = _mk_client(tmp)
        try:
            with _isolated_auth(tmp) as _tk:
                H = {"Authorization": f"Bearer {_tk}"}

                # 认证闸：无凭据 → 401
                r = client.post("/api/database/table/t37/data",
                                json={"rows": [{"name": "x"}]})
                assert r.status_code == 401, r.status_code
                # 认证闸：X-API-Key 系统通道已废除（20260903）——任何 key
                # 一律 401，废除回归锁：旁门不得复活
                r = client.post("/api/database/table/t37/data",
                                json={"rows": [{"name": "x"}]},
                                headers={"X-API-Key": "anything"})
                assert r.status_code == 401, (r.status_code, r.text[:200])

                # 认证闸：system 通道对内置凭证列无效（列级内置 deny 不看角色）。
                # 收紧：拦截的通过条件不含 500——崩溃式拒绝不算拦截成功；
                # PermissionDenied 经 AppError 处理器统一映射 400。
                # 20260903 系统表收口后，users 表 DML 在契约层整表拒（新文案）
                r = client.post("/api/database/table/users/data",
                                json={"rows": [{"username": "b", "password_hash": "x",
                                                "salt": "y"}]}, headers=H)
                assert r.status_code == 400, (r.status_code, r.text[:200])
                assert "系统表" in str(r.json()) or "认证表" in str(r.json()) or "只读" in str(r.json()), r.text[:200]

                # S-1 回归锁：数据面改写 users.role（自助提权链）必拒——
                # 开放注册 user 也能试，整表只读兜底
                r = client.post("/api/database/table/users/data/update-by-pk",
                                json={"pk_column": "id", "pk_value": 1,
                                      "values": {"role": "admin"}}, headers=H)
                assert r.status_code == 400, (r.status_code, r.text[:200])
                assert "系统表" in str(r.json()) or "认证表" in str(r.json()) or "只读" in str(r.json()), r.text[:200]

                # update-by-pk 正常路径 + 撇号值 round-trip
                r = client.post("/api/database/table/t37/data/update-by-pk",
                                json={"pk_column": "id", "pk_value": 1,
                                      "values": {"name": "O'Brien", "price": 2.5}},
                                headers=H)
                assert r.status_code == 200, r.text[:300]

                # 事务纪律：新连接直读文件必须看到（commit 真实落盘）
                from core.drivers.sqlite_driver import SqliteDriver
                fresh = SqliteDriver(os.path.join(tmp, "mg.db"))
                rows = fresh.query("SELECT name, price FROM t37 WHERE id=1")
                assert rows and rows[0]["name"] == "O'Brien" \
                    and rows[0]["price"] == 2.5, rows
                fresh.close()

                # 参数守卫：缺 pk_value / 主键混入 values / 非法标识符
                r = client.post("/api/database/table/t37/data/update-by-pk",
                                json={"pk_column": "id", "values": {"name": "x"}},
                                headers=H)
                assert r.status_code == 400, r.status_code
                r = client.post("/api/database/table/t37/data/update-by-pk",
                                json={"pk_column": "id", "pk_value": 1,
                                      "values": {"id": 9, "name": "x"}}, headers=H)
                assert r.status_code == 400 and "主键" in r.json()["detail"], r.text[:200]
                r = client.post("/api/database/table/t3;DROP/data/update-by-pk",
                                json={"pk_column": "id", "pk_value": 1,
                                      "values": {"name": "x"}}, headers=H)
                assert r.status_code == 400, r.status_code

                # insert 端点持久化：新连接可读
                r = client.post("/api/database/table/t37/data",
                                json={"rows": [{"name": "第二批", "price": 3.0}]},
                                headers=H)
                assert r.status_code == 200, r.text[:300]
                fresh2 = SqliteDriver(os.path.join(tmp, "mg.db"))
                assert len(fresh2.query("SELECT * FROM t37 WHERE name='第二批'")) == 1
                fresh2.close()

                # delete-by-pk 持久化 + 无键拒绝
                r = client.post("/api/database/table/t37/data/delete-by-pk",
                                json={"pk_column": "id", "pk_value": 2}, headers=H)
                assert r.status_code == 200, r.text[:300]
                fresh3 = SqliteDriver(os.path.join(tmp, "mg.db"))
                assert not fresh3.query("SELECT * FROM t37 WHERE id=2")
                fresh3.close()
                r = client.post("/api/database/table/t37/data/delete-by-pk",
                                json={"pk_column": "id"}, headers=H)
                assert r.status_code == 400, r.status_code

                # 大小写变体经 HTTP 面：读 users 屏蔽凭证列（契约单栈在 HTTP 入口生效）
                r = client.get("/api/database/table/Users/data", headers=H)
                assert r.status_code == 200, r.text[:200]
                body = r.json()
                rows = body.get("rows") or body.get("data") or []
                assert rows, f"应有行: {str(body)[:200]}"
                assert "password_hash" not in rows[0] and "salt" not in rows[0], rows[0]
        finally:
            _restore(_ops, _orig)
            settings.API_KEY_ENABLED = orig_enabled
            try:
                raw.close()  # Windows 文件锁：不关会锁死临时目录
            except Exception:
                pass
    print("OK - mgmt HTTP 面：认证闸/事务纪律/参数守卫/凭证屏蔽/撇号 round-trip 全过")


def test_anti_csrf_middleware():
    """浏览器防伪中间件 + 无认证模式敏感面防伪（20260824/25）：
    默认桌面模式无认证时，任意恶意网页不再能 no-cors POST 关停/写后端；
    无认证模式敏感面要本机回环令牌，无令牌 403、带令牌放行。
    环境钉死 API_KEY_ENABLED=false（真实威胁模型，不随 .env 漂移）。"""
    from config.settings import settings
    orig = settings.API_KEY_ENABLED
    settings.API_KEY_ENABLED = "false"
    try:
        from agent.management.server import mgmt_app
        from agent.management.deps import _loopback_token
        from fastapi.testclient import TestClient
        client = TestClient(mgmt_app)
        tok = _loopback_token()
        # 跨站 fetch：Origin 非 localhost → 403（handler 永不执行，stop 不会真触发）
        r = client.post("/api/stop", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, r.status_code
        # Sec-Fetch-Site: cross-site（无 Origin 也拦）
        r = client.post("/api/stop", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403, r.status_code
        # 本机 Origin + 无令牌 → 敏感面 403（无认证模式的回环防伪闸，防真触发 stop 改用 metrics）
        r = client.post("/api/metrics/reset", headers={"Origin": "http://localhost:3000"})
        assert r.status_code == 403, r.status_code
        # 畸形 Origin（方括号不配对）→ 按跨站 403（fail-closed，不裸抛 500）
        r = client.post("/api/metrics/reset", headers={"Origin": "http://[::1"})
        assert r.status_code == 403, r.status_code
        # preview/convert timeout 钳制：非数字如实 400（无界 timeout 是功能级
        # DoS 面——转换持全局锁串行；钳制发生在转换前，无需 soffice 环境）
        H = {"X-Loopback-Token": tok, "Origin": "http://localhost:3000"}
        r = client.post("/api/preview/convert", json={"timeout": "abc"}, headers=H)
        assert r.status_code == 400, r.status_code
        # 超界值静默钳到 600（走到缺文件 400 而非 timeout 报错即为钳制生效证据）
        r = client.post("/api/preview/convert", json={"timeout": 99999}, headers=H)
        assert r.status_code == 400 and "file_base64" in r.text, r.text[:120]
        # 本机 Origin + 本机回环令牌 → 放行（前端代理服务端注入同款）
        r = client.post("/api/metrics/reset",
                        headers={"Origin": "http://localhost:3000",
                                 "X-Loopback-Token": tok})
        assert r.status_code != 403, r.status_code
        # 无 Origin（curl/本地脚本）+ 令牌 → 放行
        r = client.post("/api/metrics/reset", headers={"X-Loopback-Token": tok})
        assert r.status_code != 403, r.status_code
    finally:
        settings.API_KEY_ENABLED = orig
    print("OK - 防伪中间件：跨站 403、敏感面无令牌 403、带令牌放行")


def test_escalation_settle_chain():
    """sudo 提权结算链（20260826 断头接通锁）：
    - 审批中心 settle 对 __escalate__ 条目一律 400（指去专属端点）
    - 无认证模式下 /api/auth/escalations/{token}/approve 经回环令牌可结算
      （该端点若无 Bearer 即 401——无认证模式是死路，提权链断头）
    - 批准后提权契约生效；无回环令牌 → 403（中间件防伪闸）"""
    from config.settings import settings
    orig = settings.API_KEY_ENABLED
    settings.API_KEY_ENABLED = "false"
    try:
        from agent.management.server import mgmt_app
        from agent.management.deps import _loopback_token
        from fastapi.testclient import TestClient
        from core.pending_ops import register_pending
        from core.permission import get_escalated_role, clear_escalation
        client = TestClient(mgmt_app)
        tok = _loopback_token()
        H = {"X-Loopback-Token": tok, "Origin": "http://localhost:3000"}

        t1 = register_pending("__escalate__", {"role": "admin", "ttl": 60},
                              "AI 请求临时提权为 admin（测试登记）")
        # 1) 审批中心 settle 拒收提权条目
        r = client.post(f"/api/approvals/{t1}/settle", json={"approve": True}, headers=H)
        assert r.status_code == 400 and "escalations" in str(r.json()), r.text
        # 2) 专属端点无回环令牌 → 403（写方法中间件闸）
        r = client.post(f"/api/auth/escalations/{t1}/approve?approve=true",
                        headers={"Origin": "http://localhost:3000"})
        assert r.status_code == 403, r.status_code
        # 3) 专属端点带令牌 → 结算成功，提权契约生效
        r = client.post(f"/api/auth/escalations/{t1}/approve?approve=true", headers=H)
        assert r.status_code == 200 and r.json().get("granted") is True, r.text
        assert get_escalated_role() == "admin"
        clear_escalation()
        # 4) 批准拒绝路径：新登记一条，approve=false → granted False
        t2 = register_pending("__escalate__", {"role": "admin", "ttl": 60},
                              "AI 请求临时提权为 admin（测试登记）")
        r = client.post(f"/api/auth/escalations/{t2}/approve?approve=false", headers=H)
        assert r.status_code == 200 and r.json().get("granted") is False, r.text
    finally:
        settings.API_KEY_ENABLED = orig
        from core.permission import clear_escalation
        clear_escalation()
    print("OK - 提权结算链：settle 拒收/无令牌 403/带令牌批准生效/拒绝销毁")


def test_settle_exception_honest_receipt():
    """结算执行异常如实回执（锁）：execute_tool 抛错 → ok=False + 异常摘要，
    绝不裸 500；PendingApproval 控制流原样上抛（不被吞成执行失败）"""
    from config.settings import settings
    orig = settings.API_KEY_ENABLED
    settings.API_KEY_ENABLED = "false"
    try:
        from agent.management.server import mgmt_app
        from agent.management.deps import _loopback_token
        from fastapi.testclient import TestClient
        from core.pending_ops import register_pending
        from unittest.mock import patch
        client = TestClient(mgmt_app)
        tok = _loopback_token()
        H = {"X-Loopback-Token": tok, "Origin": "http://localhost:3000"}
        t = register_pending("probe_op", {"table": "t_x"}, "结算异常探针")
        with patch("core.tool_registry.execute_tool",
                   side_effect=RuntimeError("模拟执行异常")):
            r = client.post(f"/api/approvals/{t}/settle", json={"approve": True},
                            headers=H)
        assert r.status_code == 200, r.status_code  # 不裸 500
        body = r.json()
        assert body.get("ok") is False and "执行异常" in body.get("message", ""), body
    finally:
        settings.API_KEY_ENABLED = orig
    print("OK - 结算执行异常如实回执（不裸 500）")


if __name__ == "__main__":
    test_record_write_tx_and_guards()
    test_anti_csrf_middleware()
    test_escalation_settle_chain()
    test_settle_exception_honest_receipt()
    print("\n=== ALL MGMT API TESTS PASSED ===")
