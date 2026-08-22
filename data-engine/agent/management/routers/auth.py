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
    # 由管理员创建账号——否则任何人自助注册即得 user 角色（评审三轮 D 链收口）
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
def auth_delete_user(user_id: int, request: Request):
    """删除用户（仅管理员）"""
    from core.auth import verify_token, delete_user
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    payload = verify_token(auth_header[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    if payload["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    result = delete_user(user_id)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/api/auth/escalations/{token}/approve")
def auth_approve_escalation(token: str, request: Request, approve: bool = True):
    """批准/拒绝 AI 提权请求（仅管理员）

    这是提权的唯一结算通道：AI 调 escalate_permission 后登记提权请求
    （token 只在本管理端可见，AI 拿不到），管理员登录后在此批准——
    批准则临时提权 admin（TTL 到期自动降回），拒绝则销毁请求。
    """
    from core.auth import verify_token
    from core.pending_ops import pop_pending
    from core.permission import set_escalated_role
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    payload = verify_token(auth_header[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    if payload["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    op = pop_pending(token)
    if op is None:
        raise HTTPException(status_code=400, detail=f"提权请求 {token} 不存在或已过期")
    if op["name"] != "__escalate__":
        raise HTTPException(status_code=400, detail="该 token 不是提权请求")
    if not approve:
        return {"ok": True, "message": "已拒绝提权请求", "granted": False}
    role = op["kwargs"].get("role", "admin")
    ttl = int(op["kwargs"].get("ttl", 600))
    set_escalated_role(role, ttl_seconds=ttl)
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
    result = update_user_role(user_id, body.get("role", ""))
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


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
