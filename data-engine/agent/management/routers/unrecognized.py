"""未识别问法池端点：硬路由的映射自学习管理面（20260824）

池的来源：execute_instruction 路由不出来（cannot_route）的问法。
管理面闭环：列出 → admin 确认映射（意图标签或表别名）→ 写入行业
prompts.yml 热生效 → 同类问法下次直接命中（映射关系随使用自生长）。
"""
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _require_admin(request: Request) -> None:
    """写端点仅限 admin；X-API-Key 系统通道已废除（20260903），
    脚本/测试走真实用户 Bearer（见 tests/_mgmt_auth.py）。"""
    from core.auth import verify_token
    from config.settings import settings
    if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
        return
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = verify_token(auth_header[7:])
        if not payload:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可访问")
        return
    raise HTTPException(status_code=401, detail="未授权：需要 admin 凭据")


@router.get("/api/unrecognized")
def list_pool(request: Request):
    """列出未识别问法池（按最近出现倒序）"""
    _require_admin(request)
    from core.unrecognized import list_unrecognized
    return {"pool": list_unrecognized()}


@router.post("/api/unrecognized/learn")
def learn(request: Request, body: dict):
    """确认一条映射并写入行业 prompts.yml（热生效）

    body: {question, behavior?, db_category?}（意图映射）
       或 {question, table_alias_of}（表别名映射）
    学习成功即从池中移除该问法。
    """
    _require_admin(request)
    q = (body.get("question") or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="缺少 question")
    from core.unrecognized import learn_mapping, remove_from_pool
    r = learn_mapping(q, behavior=(body.get("behavior") or "").strip(),
                      db_category=(body.get("db_category") or "").strip(),
                      table_alias_of=(body.get("table_alias_of") or "").strip())
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("message", "学习失败"))
    remove_from_pool(q)
    return r


@router.post("/api/unrecognized/dismiss")
def dismiss(request: Request, body: dict):
    """忽略一条问法（从池中移除，不学习）"""
    _require_admin(request)
    q = (body.get("question") or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="缺少 question")
    from core.unrecognized import remove_from_pool
    remove_from_pool(q)
    return {"ok": True, "message": "已忽略"}
