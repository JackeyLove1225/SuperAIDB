"""层 37：Management HTTP API 面（TestClient 离线，无需真实服务端口）

判据（评审三轮测试复核：管理端 13 个 router 在 CI 全黑，此处收口记录写路径）：
- 写端点事务纪律：update-by-pk/insert 提交后新连接可读（此前无 commit，重启即丢）
- 参数守卫：缺 pk_value / 主键在 values / 非法表名列名 → 400
- 认证闸：API_KEY_ENABLED=true 时无/错 key → 401
- 大小写变体攻击经 HTTP 面被屏蔽（契约单栈在 HTTP 入口同样生效）
- 字面值 round-trip：O'Brien 类撇号值完整落库（doubling 转义链）
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402  （仅用于 raises 语义可读；run_all 直接跑本文件亦可）


def _mk_client(tmp):
    """TestClient + 临时库驱动接管（core.data_ops._get_driver 调用期动态 import，
    打它即接管 deps._get_driver 的货源——不碰真实开发库）"""
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
    _orig = _ops._get_driver
    _ops._get_driver = lambda: drv

    from agent.management.server import mgmt_app
    client = TestClient(mgmt_app)
    return client, raw, _ops, _orig


def _restore(_ops, _orig):
    _ops._get_driver = _orig


def test_record_write_tx_and_guards():
    from config.settings import settings
    orig_enabled, orig_key = settings.API_KEY_ENABLED, settings.API_KEY
    settings.API_KEY_ENABLED = "true"
    settings.API_KEY = "test-key-37"
    with tempfile.TemporaryDirectory() as tmp:
        client, raw, _ops, _orig = _mk_client(tmp)
        try:
            H = {"X-API-Key": "test-key-37"}

            # 认证闸：无 key / 错 key → 401
            r = client.post("/api/database/table/t37/data", json={"rows": [{"name": "x"}]})
            assert r.status_code == 401, r.status_code
            r = client.post("/api/database/table/t37/data",
                            json={"rows": [{"name": "x"}]}, headers={"X-API-Key": "wrong"})
            assert r.status_code == 401, r.status_code

            # 认证闸：system 通道对内置凭证列无效（列级内置 deny 不看角色）。
            # 收紧（评审四轮测试复核）：拦截的通过条件不含 500——崩溃式拒绝不算拦截成功；
            # PermissionDenied 经 AppError 处理器统一映射 400
            r = client.post("/api/database/table/users/data",
                            json={"rows": [{"username": "b", "password_hash": "x",
                                            "salt": "y"}]}, headers=H)
            assert r.status_code == 400, (r.status_code, r.text[:200])
            assert "认证表" in str(r.json()) or "只读" in str(r.json()), r.text[:200]

            # S-1 回归锁：数据面改写 users.role（自助提权链）必拒——
            # 开放注册 user 也能试，整表只读兜底
            r = client.post("/api/database/table/users/data/update-by-pk",
                            json={"pk_column": "id", "pk_value": 1,
                                  "values": {"role": "admin"}}, headers=H)
            assert r.status_code == 400, (r.status_code, r.text[:200])
            assert "认证表" in str(r.json()) or "只读" in str(r.json()), r.text[:200]

            # update-by-pk 正常路径 + 撇号值 round-trip
            r = client.post("/api/database/table/t37/data/update-by-pk",
                            json={"pk_column": "id", "pk_value": 1,
                                  "values": {"name": "O'Brien", "price": 2.5}}, headers=H)
            assert r.status_code == 200, r.text[:300]

            # 事务纪律：新连接直读文件必须看到（commit 真实落盘）
            from core.drivers.sqlite_driver import SqliteDriver
            fresh = SqliteDriver(os.path.join(tmp, "mg.db"))
            rows = fresh.query("SELECT name, price FROM t37 WHERE id=1")
            assert rows and rows[0]["name"] == "O'Brien" and rows[0]["price"] == 2.5, rows
            fresh.close()

            # 参数守卫：缺 pk_value / 主键混入 values / 非法标识符
            r = client.post("/api/database/table/t37/data/update-by-pk",
                            json={"pk_column": "id", "values": {"name": "x"}}, headers=H)
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
                            json={"rows": [{"name": "第二批", "price": 3.0}]}, headers=H)
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
            settings.API_KEY_ENABLED, settings.API_KEY = orig_enabled, orig_key
            try:
                raw.close()  # Windows 文件锁：不关会锁死临时目录
            except Exception:
                pass
    print("OK - mgmt HTTP 面：认证闸/事务纪律/参数守卫/凭证屏蔽/撇号 round-trip 全过")


if __name__ == "__main__":
    test_record_write_tx_and_guards()
    print("\n=== ALL MGMT API TESTS PASSED ===")
