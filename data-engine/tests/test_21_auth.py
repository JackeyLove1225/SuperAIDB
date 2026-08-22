"""层 21：身份认证全链路——token 生命周期 / 角色注入 / 角色×权限叠加（迭代 1.6）

覆盖：
- 注册/登录/验签/过期/错误密码（core.auth，隔离临时 users 库）
- contextvars 角色上下文（async 并发 task 隔离，不串角色）
- 角色规则叠加数据源规则（roles.<role>.deny/allow）
- graph 节点 _with_role 包装器（token→角色；无效 token 按认证开关降级）
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
    import core.auth as auth
    db_path = str(Path(tmp) / "users_test.db")
    return auth, patch.object(auth, "_get_db_path", lambda: db_path)


def test_register_login_verify():
    """注册→登录→验签 全链路；重复注册/错误密码如实拒绝"""
    with tempfile.TemporaryDirectory() as tmp:
        import core.auth as auth
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
        import core.auth as auth
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


def test_role_overlays_datasource_rules():
    """角色规则×数据源规则叠加：deny 优先、最严格者胜（矩阵角色用例扩展）"""
    from core.permission import Operation, PermissionDenied, PermissionPolicy, set_current_role
    p = PermissionPolicy.new_instance({
        "default": "full",
        "datasources": {"legacy": {"mode": "read_only"}},
        "roles": {
            "readonly": {"allow": ["query"]},
            "clerk": {"deny": ["delete", "drop"]},
        },
    })
    try:
        # readonly：数据源 full 的库也只能 query（角色白名单收口）
        set_current_role("readonly")
        p.check("any_db", Operation.QUERY)
        for op in (Operation.INSERT, Operation.UPDATE, Operation.DELETE):
            try:
                p.check("any_db", op)
                raise AssertionError(f"readonly 应禁止 {op}")
            except PermissionDenied:
                pass
        # clerk：full 库可 insert/update，deny 命中 delete/drop
        set_current_role("clerk")
        p.check("any_db", Operation.INSERT)
        p.check("any_db", Operation.UPDATE)
        for op in (Operation.DELETE, Operation.DROP):
            try:
                p.check("any_db", op)
                raise AssertionError(f"clerk 应禁止 {op}")
            except PermissionDenied:
                pass
        # system：不查角色规则（内部路径全权限）
        set_current_role("system")
        p.check("any_db", Operation.DELETE)
        # 数据源规则依然独立生效（system 也过不了数据源层）
        try:
            p.check("legacy", Operation.DELETE)
            raise AssertionError("read_only 库应禁止 delete")
        except PermissionDenied:
            pass
    finally:
        set_current_role("system")
    print("OK - 角色×数据源叠加：deny 优先、最严格者胜")


def test_with_role_wrapper():
    """graph 节点 _with_role 包装器：token→角色；无效 token 按认证开关降级"""
    import core.auth as auth
    from agent.open_layer.graph import _with_role
    from core.permission import get_current_role, set_current_role

    class _FakeRuntime:
        def __init__(self, ctx): self.context = ctx

    seen = {}

    @_with_role
    def _probe(state, runtime=None):
        seen["role"] = get_current_role()
        return state

    with tempfile.TemporaryDirectory() as tmp:
        auth, p = _isolated_auth(tmp)
        with p:
            auth.init_users_table()
            auth.register_user("erin", "secret123", "readonly")
            token = auth.login_user("erin", "secret123")["token"]

            # 有效 token → token 角色
            _probe({}, _FakeRuntime({"user_token": token}))
            assert seen["role"] == "readonly", seen

            # 无 token + 认证关闭 → system
            with patch("agent.open_layer.graph.settings") as s:
                s.API_KEY_ENABLED = "false"
                _probe({}, _FakeRuntime({}))
                assert seen["role"] == "system", seen

            # 无 token + 认证开启 → readonly（安全降级）
            with patch("agent.open_layer.graph.settings") as s:
                s.API_KEY_ENABLED = "true"
                _probe({}, _FakeRuntime({}))
                assert seen["role"] == "readonly", seen
                # 无效 token + 认证开启 → readonly
                _probe({}, _FakeRuntime({"user_token": "forged.token"}))
                assert seen["role"] == "readonly", seen
    set_current_role("system")
    print("OK - _with_role：有效 token 注入角色，无效/缺失按认证开关降级")


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
        import core.auth as auth
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


if __name__ == "__main__":
    test_register_login_verify()
    test_token_expiry()
    test_contextvars_role_isolation()
    test_role_overlays_datasource_rules()
    test_with_role_wrapper()
    test_auth_me_system_mode()
    test_token_revocation_and_lockout()
    print("\n层 21 全绿")
