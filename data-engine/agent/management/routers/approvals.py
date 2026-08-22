"""审批中心端点：高危挂起（核武闸）的查看与结算——admin 专属（20260822）

安全边界：MCP 通道的高危操作挂起后，token 不回传 AI 通道——批准/拒绝
只能发生在本管理端；AI 通道只负责转述影响面。挂起表跨进程落盘
（core/pending_ops.py），MCP 进程登记、本进程结算。
"""
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _require_admin(request: Request) -> None:
    """与 permissions.py 同款：Bearer 必须 admin；API Key（system）等同 admin；
    本地开发模式（API_KEY_ENABLED=false）不强制。"""
    from core.auth import verify_token, verify_api_key
    from config.settings import settings
    if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
        return
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = verify_token(auth_header[7:])
        if not payload:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可访问审批中心")
        return
    api_key = request.headers.get("X-API-Key")
    if api_key and verify_api_key(api_key):
        return
    raise HTTPException(status_code=401, detail="未授权：需要 admin 凭据")


@router.get("/api/approvals")
def list_approvals(request: Request):
    """列出当前待批准的高危操作（审批中心列表）"""
    _require_admin(request)
    from core.pending_ops import list_pending
    return {"pending": list_pending()}


@router.post("/api/approvals/{token}/settle")
def settle_approval(token: str, request: Request, body: dict):
    """批准/拒绝一项高危挂起（admin）。

    批准 → 在本进程以批量预批准通道放行执行原操作（与 mutate_natural
    多表卡同一机制）；拒绝 → 销毁 token，操作不执行。
    提权请求（__escalate__）走 /api/auth/escalations/{token}/approve，不在此结算。
    """
    _require_admin(request)
    from core.pending_ops import pop_pending
    op = pop_pending(token)
    if op is None:
        raise HTTPException(status_code=400,
                            detail=f"待批准操作 {token} 不存在或已过期（10 分钟有效期）")
    if op["name"] == "__escalate__":
        raise HTTPException(status_code=400,
                            detail="提权请求请走 /api/auth/escalations/{token}/approve")
    if not body.get("approve", True):
        return {"ok": True, "message": f"已拒绝：{op['name']}——操作未执行，数据未发生任何变化"}

    from core.context import get_context
    from core.tool_registry import execute_tool
    name, kwargs = op["name"], op["kwargs"]
    ctx = get_context()
    # 批量预批准通道放行：表已在登记时解析进 kwargs（MCP 进程侧），
    # "*" 兜底库级无表操作（clear_db 等）
    ctx.set_nuke_batch(tables={kwargs.get("table") or "*"}, ops={name})
    try:
        result = execute_tool(name, **kwargs)
    finally:
        ctx.clear_nuke_batch()
    return {"ok": True, "message": "已批准并执行", "result": str(result)[:2000]}
