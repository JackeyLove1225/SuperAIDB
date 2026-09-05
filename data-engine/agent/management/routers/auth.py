"""用户认证端点——注册 / 登录 / 当前用户 / 用户管理"""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.post("/api/auth/register")
def auth_register(body: dict):
    """用户注册

    Body: {"username": str, "password": str}
    注：角色固定为 user，admin 由已有管理员创建
    """
    from core.auth import register_user
    from config.settings import settings
    # 开放注册是桌面端默认姿势；部署到服务器/暴露网络时应设 AUTH_REGISTER_ENABLED=false，
    # 由管理员创建账号——否则任何人自助注册即得 user 角色
    if str(getattr(settings, "AUTH_REGISTER_ENABLED", "true")).lower() not in ("true", "1", "yes"):
        raise HTTPException(status_code=403,
                            detail="注册已关闭（AUTH_REGISTER_ENABLED=false），请由管理员创建账号")
    username = body.get("username", "")
    password = body.get("password", "")
    # 注册端点强制普通用户角色，忽略 body 中的 role 字段；
    # admin 只能由已有管理员通过用户管理接口创建
    role = "user"
    result = register_user(username, password, role)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/api/auth/login")
def auth_login(body: dict):
    """用户登录

    Body: {"username": str, "password": str}
    Returns: {"token": str, "user": {"id", "username", "role"}}
    """
    from core.auth import login_user
    username = body.get("username", "")
    password = body.get("password", "")
    result = login_user(username, password)
    if not result["ok"]:
        raise HTTPException(status_code=401, detail=result["message"])
    return result


@router.get("/api/auth/me")
def auth_me(request: Request):
    """获取当前登录用户信息（需 Bearer Token）

    系统模式（API_KEY_ENABLED=false）：无 Bearer 时返回 system 身份——
    前端据此判断"认证未启用，无需登录"，避免开发模式被误踢到登录页。
    """
    from core.auth import verify_token
    from config.settings import settings
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
            return {"user_id": 0, "username": "system", "role": "system", "expires_at": 0}
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    token = auth_header[7:]
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    return {
        "user_id": payload["uid"],
        "username": payload["username"],
        "role": payload["role"],
        "expires_at": payload["exp"],
    }


@router.post("/api/auth/users")
def auth_create_user(body: dict, request: Request):
    """创建用户（仅管理员，可指定角色）

    Body: {"username": str, "password": str, "role": "admin|user|readonly|自定义角色"}
    与 /api/auth/register（公开注册、强制 user 角色）区分：本端点走管理面。
    自定义角色（如 user_zhangsan）需在 config/permissions.yml 的
    roles.<角色名> 下配置专属表/列 deny，实现"仅该用户受限"。
    """
    from core.auth import verify_token, register_user
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    payload = verify_token(auth_header[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    if payload["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from agent.management.deps import require_operator_password
    require_operator_password(request, body)
    result = register_user(
        body.get("username", ""), body.get("password", ""), body.get("role", "user"))
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/api/auth/users")
def auth_list_users(request: Request):
    """列出所有用户（仅管理员）"""
    from core.auth import verify_token, list_users
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    payload = verify_token(auth_header[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    if payload["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return {"users": list_users()}


@router.delete("/api/auth/users/{user_id}")
def auth_delete_user(user_id: int, request: Request, body: dict = None):
    """删除用户（仅管理员 + 操作密码）

    Body: {"operator_password": str}"""
    from core.auth import verify_token, delete_user
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    payload = verify_token(auth_header[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    if payload["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from agent.management.deps import require_operator_password
    require_operator_password(request, body)
    result = delete_user(user_id)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/api/auth/escalations/{token}/approve")
def auth_approve_escalation(token: str, request: Request, body: dict = None, approve: bool = True):
    """批准/拒绝 AI 提权请求（仅管理员）

    这是提权的唯一结算通道：AI 调 escalate_permission 后登记提权请求
    （token 只在本管理端可见，AI 拿不到），管理员登录后在此批准——
    批准则临时提权 admin（TTL 到期自动降回），拒绝则销毁请求。
    """
    from core.auth import verify_token
    from core.pending_ops import pop_pending
    from core.permission import set_escalated_role
    from config.settings import settings
    if settings.API_KEY_ENABLED.lower() in ("true", "1", "yes"):
        # 认证模式：Bearer 必须 admin（与审批中心同口径）
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="未提供认证 Token")
        payload = verify_token(auth_header[7:])
        if not payload:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
        if payload["role"] != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可访问")
    # 本地无密码模式：服务级 loopback 闸（写方法强制 X-Loopback-Token）
    # 已在中间件验过本机回环令牌，此处不再要求 Bearer（否则无路可走）
    # 先窥后取：非提权 token 提交到本端点只报错不焚毁（与审批中心
    # settle 的"错通道不烧毁请求"同原则）
    from core.pending_ops import peek_pending
    peeked = peek_pending(token)
    if peeked is None:
        raise HTTPException(status_code=400, detail=f"提权请求 {token} 不存在或已过期")
    if peeked["name"] != "__escalate__":
        raise HTTPException(status_code=400, detail="该 token 不是提权请求")
    op = pop_pending(token)
    if op is None:
        raise HTTPException(status_code=400, detail=f"提权请求 {token} 不存在或已过期")
    if not approve:
        from core.settlement_hub import record_settlement
        record_settlement(token, "rejected", "已拒绝提权请求")
        return {"ok": True, "message": "已拒绝提权请求", "granted": False}
    # 批准提权 = 把 admin 能力交给 AI 通道：与审批中心同级的操作密码人因确认
    from agent.management.deps import require_operator_password
    try:
        require_operator_password(request, body)
    except Exception as e:
        # 密码闸在 pop 之后：token 已焚但提权未生效——如实回执让 MCP 侧
        # 尽快返回（否则傻等满超时），用户须重新发起提权
        from core.settlement_hub import record_settlement
        record_settlement(token, "error",
                          f"提权批准失败（请求已销毁，请重新发起）: {e}")
        raise
    # 结算端复检（纵深）：挂起表载荷即便验签通过，也只按登记语义采信——
    # role 白名单与 ttl clamp 与工具侧（agent/tools/admin.py）同口径，
    # 不依赖单一防线
    role = op["kwargs"].get("role", "admin")
    if role not in ("admin",):
        raise HTTPException(status_code=400,
                            detail=f"提权目标角色非法: {role}（仅支持 admin）")
    ttl = max(60, min(int(op["kwargs"].get("ttl", 600)), 3600))
    set_escalated_role(role, ttl_seconds=ttl)
    # 结算回执（MCP 同步等待桥取走；提权经 escalation.json 跨进程生效，
    # MCP 进程下一次调用即提权生效——回执提示重试原操作）
    from core.settlement_hub import record_settlement
    record_settlement(token, "approved",
                      f"已批准提权为 {role}（{ttl // 60} 分钟）——现在可以重试原操作")
    return {"ok": True, "message": f"已批准提权为 {role}（{ttl // 60} 分钟）", "granted": True}


@router.put("/api/auth/users/{user_id}/role")
def auth_update_user_role(user_id: int, body: dict, request: Request):
    """修改用户角色（仅管理员）

    Body: {"role": "admin|user|readonly|自定义角色"}
    即"管理员给其他用户分配权限"的官方入口：可把普通用户提为受限
    （readonly）或升级（admin），也支持降级；默认管理员与最后一名
    admin 受 core.auth.update_user_role 内保护。
    自定义角色（如 user_zhangsan）配在 permissions.yml 的 roles.<角色名>
    下，实现"仅该用户查不了某表/某字段"的用户级权限。
    """
    from core.auth import verify_token, update_user_role
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    payload = verify_token(auth_header[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    if payload["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from agent.management.deps import require_operator_password
    require_operator_password(request, body)
    result = update_user_role(user_id, body.get("role", ""))
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/api/auth/my-rules")
def auth_get_my_rules(request: Request):
    """当前用户的权限视图：管理员授予的用户级规则（只读）+ 我的自助收紧规则"""
    from core.auth import verify_token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    payload = verify_token(auth_header[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    username = payload.get("username", "")
    if not username:
        raise HTTPException(status_code=400, detail="系统模式无用户身份，无自助规则")
    from core.permission.policy import PermissionPolicy, _SELF_RULES_PATH
    from core.config_hub import load_yaml
    rules = PermissionPolicy.get_instance()._current_rules()
    admin_rules = (rules.get("users") or {}).get(username, {})
    self_rules = (load_yaml(_SELF_RULES_PATH, default={}) or {}).get(username, {})
    return {"username": username, "admin_rules": admin_rules, "self_rules": self_rules}


def _validate_self_rules_deny_only(node, loc: str = "") -> None:
    """自助规则结构校验：只允许 deny 键（allow/mode 等一切可能放松的键拒收）——
    写入端收口 + 引擎读取端再过滤，双保险保证"只能比管理员更严"。
    deny 列表元素必须是合法操作名。"""
    from core.permission import Operation
    valid_ops = {o.value for o in Operation}
    if not isinstance(node, dict):
        raise HTTPException(status_code=400, detail=f"{loc or '规则'}必须是对象")
    for k, v in node.items():
        if k == "deny":
            if not isinstance(v, list) or any(str(o) not in valid_ops for o in v):
                raise HTTPException(status_code=400, detail=f"{loc} 的 deny 必须是合法操作名列表")
        elif k in ("tables", "columns"):
            if not isinstance(v, dict):
                raise HTTPException(status_code=400, detail=f"{loc}.{k} 必须是对象")
            for name, sub in v.items():
                _validate_self_rules_deny_only(sub, f"{loc}.{k}.{name}")
        else:
            raise HTTPException(status_code=400,
                                detail=f"自助收紧只允许 deny（只能更严）: {loc}.{k} 不支持")


@router.put("/api/auth/my-rules")
def auth_put_my_rules(body: dict, request: Request):
    """自助收紧：当前用户整体替换自己的收紧规则（deny-only，只能更严）。

    Body: {"rules": {"deny": [...], "tables": {...}}}——只收 deny 键；
    管理员的授权（permissions.yml users 段）不受影响、不可经此解除。"""
    from core.auth import verify_token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    payload = verify_token(auth_header[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    username = payload.get("username", "")
    if not username:
        raise HTTPException(status_code=400, detail="系统模式无用户身份")
    rules = body.get("rules")
    _validate_self_rules_deny_only(rules or {}, "rules")
    from core.permission.policy import _SELF_RULES_PATH
    from core.config_hub import load_yaml, write_yaml_atomic
    all_rules = load_yaml(_SELF_RULES_PATH, default={}) or {}
    if rules:
        all_rules[username] = rules
    else:
        all_rules.pop(username, None)  # 空规则=清除自助收紧（自助项本来就可自助解除）
    write_yaml_atomic(_SELF_RULES_PATH, all_rules, backup=True)
    return {"ok": True, "message": "我的收紧规则已保存（立即生效；管理员的授权不受影响）"}


@router.post("/api/auth/logout")
def auth_logout(request: Request):
    """登出：吊销当前用户全部在发 token（tv+1，token 版本戳语义）"""
    from core.auth import logout_user
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    result = logout_user(auth_header[7:])
    if not result["ok"]:
        raise HTTPException(status_code=401, detail=result["message"])
    return result


@router.post("/api/auth/change-password")
def auth_change_password(body: dict, request: Request):
    """修改密码（本人+旧密码验证；成功后全部旧 token 失效）

    Body: {"old_password": str, "new_password": str}
    """
    from core.auth import change_password, verify_token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    payload = verify_token(auth_header[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    result = change_password(int(payload["uid"]),
                             body.get("old_password", ""), body.get("new_password", ""))
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result
