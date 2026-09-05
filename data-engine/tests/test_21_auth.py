"""层 21：身份认证全链路——token 生命周期 / 角色注入 / 角色×权限叠加（迭代 1.6）

覆盖：
- 注册/登录/验签/过期/错误密码（core.auth，隔离临时 users 库）
- contextvars 角色上下文（async 并发 task 隔离，不串角色）
- 角色规则叠加数据源规则（roles.<role>.deny/allow）
- auth_me 系统模式分支（API_KEY_ENABLED=false 返回 system）
"""
import sys; sys.path.insert(0, ".")
import asyncio
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def _isolated_auth(tmp: str):
    """构造隔离的 core.auth（users 表指向临时库，不碰生产库）"""
    import contextlib
    import core.auth as auth
    db_path = str(Path(tmp) / "users_test.db")

    @contextlib.contextmanager
    def _cm():
        with patch.object(auth, "_get_db_path", lambda: db_path):
            try:
                yield auth
            finally:
                # 连接缓存持有临时库文件（Windows 文件锁）——退出前显式释放，
                # 否则 TemporaryDirectory 清理 WinError 32
                auth._close_all_conns()
    return auth, _cm()


def test_register_login_verify():
    """注册→登录→验签 全链路；重复注册/错误密码如实拒绝"""
    with tempfile.TemporaryDirectory() as tmp:
        auth, p = _isolated_auth(tmp)
        with p:
            auth.init_users_table()
            # 注册
            r = auth.register_user("alice", "secret123", "user")
            assert r["ok"], r
            # 重复注册拒绝
            r2 = auth.register_user("alice", "other_pass", "user")
            assert not r2["ok"] and "已存在" in r2["message"]
            # 弱密码拒绝
            assert not auth.register_user("bob", "123", "user")["ok"]
            assert not auth.register_user("x", "secret123", "user")["ok"]
            # 非法角色拒绝（非内置三角色，且不符合自定义角色命名规则）
            assert not auth.register_user("carol", "secret123", "超级角色")["ok"]
            assert not auth.register_user("carol2", "secret123", "a b")["ok"]
            # 自定义用户级角色合法（如 user_zhangsan 专属角色）
            ok_custom = auth.register_user("zhangsan_t", "secret123", "user_zhangsan")
            assert ok_custom["ok"], f"自定义角色应可注册: {ok_custom}"
            # 登录成功 → token 可验签
            lr = auth.login_user("alice", "secret123")
            assert lr["ok"] and lr["token"] and lr["user"]["role"] == "user"
            payload = auth.verify_token(lr["token"])
            assert payload and payload["username"] == "alice" and payload["role"] == "user"
            # 错误密码 / 不存在用户
            assert not auth.login_user("alice", "wrong_pass")["ok"]
            assert not auth.login_user("nobody", "secret123")["ok"]
            # 篡改 token 验签失败
            assert auth.verify_token(lr["token"] + "x") is None
            assert auth.verify_token("garbage") is None
            assert auth.verify_token("") is None
    print("OK - 注册/登录/验签/拒绝分支全过")


def test_token_expiry():
    """过期 token 验签失败（monkeypatch 时间快进）"""
    with tempfile.TemporaryDirectory() as tmp:
        auth, p = _isolated_auth(tmp)
        with p:
            auth.init_users_table()
            auth.register_user("dave", "secret123", "readonly")
            token = auth.login_user("dave", "secret123")["token"]
            assert auth.verify_token(token) is not None
            # 时间快进到 TTL 之后
            future = time.time() + auth.TOKEN_TTL + 10
            with patch("core.auth.time.time", lambda: future):
                assert auth.verify_token(token) is None
    print("OK - 过期 token 正确失效")


def test_contextvars_role_isolation():
    """contextvars 角色上下文：并发 async task 互不串角色（1.1 改造核心验证）"""
    from core.permission import set_current_role, get_current_role

    async def worker(role: str, barrier: asyncio.Event, out: dict):
        set_current_role(role)
        await barrier.wait()  # 两个 task 都已设置完角色
        await asyncio.sleep(0)  # 让出事件循环
        out[role] = get_current_role()

    async def main():
        out = {}
        barrier = asyncio.Event()
        t1 = asyncio.create_task(worker("admin", barrier, out))
        t2 = asyncio.create_task(worker("readonly", barrier, out))
        await asyncio.sleep(0)
        barrier.set()
        await asyncio.gather(t1, t2)
        return out

    out = asyncio.run(main())
    assert out == {"admin": "admin", "readonly": "readonly"}, out
    # 主上下文不受影响（默认 system）
    assert get_current_role() == "system"
    print("OK - contextvars 并发 task 角色隔离（不串角色）")


def test_user_overlays_datasource_rules():
    """用户规则×数据源规则叠加：deny 优先、最严格者胜（矩阵用户用例扩展）"""
    from core.permission import Operation, PermissionDenied, PermissionPolicy, set_current_user
    p = PermissionPolicy.new_instance({
        "default": "full",
        "datasources": {"legacy": {"mode": "read_only"}},
        "users": {
            "bob": {"allow": ["query"]},
            "clerk": {"deny": ["delete", "drop"]},
        },
    })
    try:
        # bob：数据源 full 的库也只能 query（用户白名单收口）
        set_current_user("bob")
        p.check("any_db", Operation.QUERY)
        for op in (Operation.INSERT, Operation.UPDATE, Operation.DELETE):
            try:
                p.check("any_db", op)
                raise AssertionError(f"readonly 应禁止 {op}")
            except PermissionDenied:
                pass
        # clerk：full 库可 insert/update，deny 命中 delete/drop
        set_current_user("clerk")
        p.check("any_db", Operation.INSERT)
        p.check("any_db", Operation.UPDATE)
        for op in (Operation.DELETE, Operation.DROP):
            try:
                p.check("any_db", op)
                raise AssertionError(f"clerk 应禁止 {op}")
            except PermissionDenied:
                pass
        # 无用户名上下文：不查用户规则
        set_current_user("")
        p.check("any_db", Operation.DELETE)
        # 数据源规则依然独立生效（无用户上下文也过不了数据源层）
        try:
            p.check("legacy", Operation.DELETE)
            raise AssertionError("read_only 库应禁止 delete")
        except PermissionDenied:
            pass
    finally:
        set_current_user("")
    print("OK - 用户×数据源叠加：deny 优先、最严格者胜")


def test_auth_me_system_mode():
    """auth_me 系统模式：API_KEY_ENABLED=false 且无 Bearer → 返回 system 身份"""
    from agent.management.routers.auth import auth_me

    class _Req:
        def __init__(self, authz=""):
            self.headers = {"Authorization": authz} if authz else {}

    with patch("config.settings.settings") as s:
        s.API_KEY_ENABLED = "false"
        r = auth_me(_Req())
        assert r["role"] == "system" and r["username"] == "system"
    # 认证开启 + 无 Bearer → 401
    from fastapi import HTTPException
    with patch("config.settings.settings") as s:
        s.API_KEY_ENABLED = "true"
        try:
            auth_me(_Req())
            raise AssertionError("认证开启且无 Bearer 应 401")
        except HTTPException as e:
            assert e.status_code == 401
    print("OK - auth_me 系统模式/认证模式分支正确")


def test_token_revocation_and_lockout():
    """token 版本戳（logout/改密/角色变更吊销旧 token）+ 登录账号级锁定"""
    with tempfile.TemporaryDirectory() as tmp:
        auth, p = _isolated_auth(tmp)
        with p:
            auth.init_users_table()
            auth.register_user("mia", "secret123", "user")
            tk1 = auth.login_user("mia", "secret123")["token"]
            assert auth.verify_token(tk1), "登录后 token 应有效"
            # logout → 旧 token 立即失效（tv+1）
            assert auth.logout_user(tk1)["ok"]
            assert auth.verify_token(tk1) is None, "logout 后旧 token 必须失效"
            # 重新登录新 token 有效；改密后它也失效
            tk2 = auth.login_user("mia", "secret123")["token"]
            uid = auth.verify_token(tk2)["uid"]
            assert auth.change_password(uid, "secret123", "newsecret456")["ok"]
            assert auth.verify_token(tk2) is None, "改密后旧 token 必须失效"
            assert auth.login_user("mia", "newsecret456")["ok"]
            # 角色变更吊销旧 token
            tk3 = auth.login_user("mia", "newsecret456")["token"]
            auth.update_user_role(uid, "readonly")
            assert auth.verify_token(tk3) is None, "角色变更后旧 token 必须失效"
            # 登录锁定：连续 5 次失败 → 锁定，正确密码也拒
            for _ in range(5):
                auth.login_user("mia", "wrong")
            locked = auth.login_user("mia", "newsecret456")
            assert not locked["ok"] and "锁定" in locked["message"], locked
            auth._LOGIN_FAILS.clear()
            # 初始密码文件副作用清理（init_users_table 在隔离库无 admin 时会写
            # 真实 config/runtime/initial_admin.txt——测试不产生真实文件残留）
            from pathlib import Path as _P
            (_P("config/runtime") / "initial_admin.txt").unlink(missing_ok=True)
    print("OK - token 吊销（logout/改密/角色变更）+ 登录账号级锁定")


def test_secret_files_chmod_600():
    """秘密文件 0600 收紧（跨平台锁——mock os.chmod 断言被以 0o600 调用，
    不依赖 Windows/POSIX 权限语义）：initial_admin.txt 与 .env 签名钥
    两个写入点都必须收紧（明文密码/签名钥是全仓最高价值落盘秘密）"""
    from unittest.mock import patch
    import core.auth as auth_mod

    # 1) initial_admin.txt：init_users_table 在隔离库无 admin 时写密码文件，
    #    必须以 os.open(..., 0o600) 创建即终态权限（不再是写后 chmod）
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        auth, p = _isolated_auth(tmp)
        import os as _real_os
        real_open = _real_os.open
        calls = []

        def _spy(*a, **kw):
            calls.append(a)
            return real_open(*a, **kw)

        # spy 而非纯 mock：真实 fd 必须照常返回（fdopen 要消费）
        with p, patch("core.auth.os.open", side_effect=_spy):
            auth.init_users_table()
            assert any(len(c) >= 3 and c[2] == 0o600
                       for c in calls), f"initial_admin.txt 未以 0600 创建: {calls[:3]}"
        from pathlib import Path as _P
        (_P("config/runtime") / "initial_admin.txt").unlink(missing_ok=True)
    # 2) _get_token_secret：.env 写入要么 0600 创建要么写后 chmod（两种契约都收）
    # 2) _get_token_secret：.env 写后必须以 0600 chmod（全平台可断言）
    auth_mod._TOKEN_SECRET = ""
    env_path = Path("config/.env")
    bak = env_path.read_bytes() if env_path.exists() else None
    try:
        with patch("core.auth.os.chmod") as m_chmod2:
            auth_mod._get_token_secret()
            assert any(c.args[1] == 0o600 for c in m_chmod2.call_args_list), \
                f".env 写后未以 0600 chmod: {m_chmod2.call_args_list}"
    finally:
        if bak is not None:
            env_path.write_bytes(bak)
        else:
            env_path.unlink(missing_ok=True)  # 无 .env 环境不留密钥残余
        auth_mod._TOKEN_SECRET = ""
    print("OK - 秘密文件 0600：.env 签名钥写后收紧（mock 跨平台断言）")


def test_operator_gate():
    """操作密码闸：verify_operator_password 正误分支 + 进程能力凭证生命周期 +
    契约层结构高危直调无凭证 fail-closed（TEST_MODE 摘除后）+ 真密码解锁放行"""
    import os
    import core.operator_gate as og
    from core.exceptions import SecurityError
    with tempfile.TemporaryDirectory() as tmp:
        auth, p = _isolated_auth(tmp)
        with p:
            auth.init_users_table()
            assert auth.register_user("boss", "secret123", "admin")["ok"]
            # 密码验证：正确/错误/空（admin 回退）
            assert auth.verify_operator_password("secret123") is True
            assert auth.verify_operator_password("wrong_pw") is False
            assert auth.verify_operator_password("") is False
            # 身份语义：谁的会话谁确认——普通用户的密码只验他本人
            assert auth.register_user("worker", "worker_pw99", "user")["ok"]
            assert auth.verify_operator_password("worker_pw99", username="worker") is True
            assert auth.verify_operator_password("secret123", username="worker") is False  # admin 密码不能替 worker 确认
            assert auth.verify_operator_password("worker_pw99", username="boss") is False  # worker 密码不能替 admin 确认
            assert auth.verify_operator_password("worker_pw99", username="ghost") is False  # 不存在的用户
            # 能力凭证生命周期须在非测试模式下验证（TEST_MODE 会直放 unlock）
            saved = os.environ.pop("SUPERAI_TEST_MODE", None)
            try:
                # 能力凭证：错密码不解锁
                og.lock()
                assert og.unlock("wrong_pw") is False
                assert not og.has_capability()
                # 正确密码解锁 → TTL 有效
                assert og.unlock("secret123") is True
                assert og.has_capability() and og.capability_remaining() > 0
                # 过期即失效
                og._cap["until"] = time.time() - 1
                assert not og.has_capability()
                og.lock()
            finally:
                if saved is not None:
                    os.environ["SUPERAI_TEST_MODE"] = saved

            # 契约层直调闸：摘除 TEST_MODE 后无凭证 drop_table fail-closed
            from core.drivers.sqlite_driver import SqliteDriver
            from core.contract.base import ContractDriver
            raw = SqliteDriver(":memory:")
            drv = ContractDriver(raw)
            raw.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, v TEXT)")
            saved = os.environ.pop("SUPERAI_TEST_MODE", None)
            try:
                og.lock()
                try:
                    drv.drop_table("t1")
                    raise AssertionError("无凭证 drop_table 应被拒")
                except SecurityError as e:
                    assert "操作密码" in str(e)
                try:
                    drv.execute("DROP TABLE t1")
                    raise AssertionError("无凭证 execute(DROP) 应被拒")
                except SecurityError:
                    pass
                # 真密码解锁后放行（证明凭证链路真实有效，不是一拒了之）
                assert og.unlock("secret123") is True
                drv.drop_table("t1")
                assert not raw.table_exists("t1")

                # 记录级写同闸（写皆密码）：无凭证 insert/update/delete 全拒
                og.lock()
                raw.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY, v TEXT)")
                for op, fn in (
                    ("insert", lambda: drv.insert("t2", [{"v": "x"}])),
                    ("update", lambda: drv.update("t2", "v='y'", "id=1")),
                    ("delete", lambda: drv.delete("t2", "id=1")),
                    ("delete_by_pk", lambda: drv.delete_by_pk("t2", "id", 1)),
                ):
                    try:
                        fn()
                        raise AssertionError(f"无凭证 {op} 应被拒")
                    except SecurityError:
                        pass
                # 系统自愈旁路：saga 补偿等系统写不受人因闸约束
                from core.operator_gate import system_bypass
                with system_bypass():
                    drv.insert("t2", [{"v": "x"}])  # 旁路内放行
                assert raw.query("SELECT COUNT(*) AS c FROM t2")[0]["c"] == 1
                # 旁路结束立即恢复闸态
                try:
                    drv.delete("t2", "id=1")
                    raise AssertionError("旁路退出后 delete 应再次被拒")
                except SecurityError:
                    pass
            finally:
                if saved is not None:
                    os.environ["SUPERAI_TEST_MODE"] = saved
                og.lock()

            # 声明表覆盖锁：GATED_CONTRACT_OPS 每个操作在契约层都有闸点
            import re as _re2
            from pathlib import Path as _P2
            _src = _P2("core/contract/base.py").read_text(encoding="utf-8")
            for op in og.GATED_CONTRACT_OPS:
                assert f'_require_operator_cap("{op}")' in _src, \
                    f"声明表中的 {op} 在契约层无闸点（声明/调用漂移）"
            _calls = set(_re2.findall(r'_require_operator_cap\("([^"]+)"\)', _src))
            assert _calls == set(og.GATED_CONTRACT_OPS), \
                f"契约闸点与声明表不一致: {sorted(_calls ^ set(og.GATED_CONTRACT_OPS))}"
    print("OK - 操作密码闸：验证/凭证生命周期/契约直调 fail-closed/解锁放行/记录级写闸/系统旁路/声明表覆盖")


if __name__ == "__main__":
    test_register_login_verify()
    test_token_expiry()
    test_contextvars_role_isolation()
    test_user_overlays_datasource_rules()
    test_auth_me_system_mode()
    test_token_revocation_and_lockout()
    test_secret_files_chmod_600()
    test_operator_gate()
    print("\n层 21 全绿")
