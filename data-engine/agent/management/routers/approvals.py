"""审批中心端点：高危挂起（人审闸）的查看与结算——admin 专属（20260822）

安全边界：MCP 通道的高危操作挂起后，token 不回传 AI 通道——批准/拒绝
只能发生在本管理端；AI 通道只负责转述影响面。挂起表跨进程落盘
（core/pending_ops.py），MCP 进程登记、本进程结算。
"""
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _require_admin(request: Request) -> None:
    """与 permissions.py 同款：Bearer 必须 admin；
    本地开发模式（API_KEY_ENABLED=false）不强制。
    X-API-Key 系统通道已废除（20260903）——脚本/测试走真实用户 Bearer。"""
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
            raise HTTPException(status_code=403, detail="仅管理员可访问审批中心")
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
    from core.pending_ops import pop_pending, peek_pending
    # 先窥后取：__escalate__ 须分流到专属端点——若先 pop 再拒，token 已被
    # 销毁，前端改调正确端点时提权请求已消失（错通道尝试不得烧毁请求）
    peeked = peek_pending(token)
    if peeked is None:
        raise HTTPException(status_code=400,
                            detail=f"待批准操作 {token} 不存在或已过期（10 分钟有效期）")
    if peeked["name"] == "__escalate__":
        raise HTTPException(status_code=400,
                            detail="提权请求请走 /api/auth/escalations/{token}/approve")
    if not body.get("approve", False):
        # 拒绝不收密码（拒绝是安全方向，不给"拒绝攻击"加门槛）
        pop_pending(token)
        from core.settlement_hub import record_settlement
        record_settlement(token, "rejected",
                          f"已拒绝：{peeked['name']}——操作未执行，数据未发生任何变化")
        return {"ok": True, "message": f"已拒绝：{peeked['name']}——操作未执行，数据未发生任何变化"}
    # 操作密码闸（人因确认第二因子）：approve 必须出示操作密码——
    # admin token 可能被伪造/窃取，密码只在人脑里（慢哈希比对、失败锁定）；
    # 请求级能力凭证：重放的 drop_table 等结构高危操作同过契约层直调闸
    from core.operator_gate import request_scoped_unlock
    from core.exceptions import SecurityError, PendingApproval
    _op_user = ""
    _h = request.headers.get("Authorization", "")
    if _h.startswith("Bearer "):
        from core.auth import verify_token as _vt
        _p = _vt(_h[7:])
        if _p:
            _op_user = _p.get("username", "")
    try:
        with request_scoped_unlock(str(body.get("operator_password", "")),
                                   username=_op_user):
            r = _settle_approved(token)
    except SecurityError as e:
        # 密码错误不记回执：token 未焚，用户在 UI 重输即可，MCP 侧继续等待
        raise HTTPException(status_code=403, detail=f"操作密码错误或未提供（批准高危操作必须输入操作密码）: {e}")
    except PendingApproval:
        # 重放又触发人审挂起（批准后载荷漂移等）：如实回执，不伪装成功
        from core.settlement_hub import record_settlement
        record_settlement(token, "error",
                          "已批准但重放又触发人审挂起，新待批项已在审批中心")
        raise
    # 结算回执（MCP 同步等待桥取走；record 全兜底不影响结算本身）
    from core.settlement_hub import record_settlement
    record_settlement(
        token,
        "approved" if r.get("ok") else "approved_failed",
        f"{r.get('message', '')}\n{r.get('result', '')}".strip())
    return r


def _settle_approved(token: str):
    """批准分支的执行体（在请求级能力凭证内运行）"""
    from core.pending_ops import pop_pending
    op = pop_pending(token)
    if op is None:
        raise HTTPException(status_code=400,
                            detail=f"待批准操作 {token} 不存在或已过期（10 分钟有效期）")

    from core.context import get_context
    from core.tool_registry import execute_tool
    from core.tool_result import ToolResult
    name, kwargs = op["name"], op["kwargs"]
    ctx = get_context()
    if name == "__multi_mutate__":
        # 多表改/删合并卡结算（20260824）：登记时存好的表集/操作集做批量预批准，
        # 重放原指令——mutate_natural 多表路径命中预批准免检执行
        #（冻结 ops 重放不重跑 LLM 提取；候选 id 集漂移整组拒，批准语义绑定）
        ctx.set_nuke_batch(tables=set(kwargs.get("_tables", [])),
                           ops=set(kwargs.get("_ops", [])))
        try:
            from core.data_ops import mutate_natural
            result = mutate_natural(
                kwargs.get("instruction", ""),
                ops_override=kwargs.get("_ops_frozen") or None,
                expect_ids=kwargs.get("_expect_ids") or None)
        except Exception as e:
            from core.exceptions import PendingApproval as _PA
            if isinstance(e, _PA):
                raise  # 审批类异常是控制流（不是故障），原样向上
            # 与单工具分支同口径：执行面异常如实回执，不裸 500
            return {"ok": False,
                    "message": f"已批准但执行异常（未生效）: {type(e).__name__}: {str(e)[:300]}"}
        finally:
            ctx.clear_nuke_batch()
        if isinstance(result, ToolResult) and not result.data.get("ok"):
            return {"ok": False,
                    "message": f"已批准但执行未生效：{str(result)[:400]}",
                    "result": str(result)[:2000]}
        return {"ok": True, "message": "已批准并执行", "result": str(result)[:2000]}
    # 批量预批准通道放行：表已在登记时解析进 kwargs（MCP 进程侧），
    # "*" 兜底库级无表操作（clear_db 等）；force 挂起项 kwargs 自带
    # force=True（契约风险已出示），预批准免检通过 force 前置闸；
    # 单表改/删挂起项 kwargs 自带 frozen_ids（登记时刻冻结的 id 集——
    # 结算直执，不受选择集主体隔离/生命周期影响，批准对象=执行对象精确同一）
    ctx.set_nuke_batch(tables={kwargs.get("table") or "*"}, ops={name})
    try:
        result = execute_tool(name, **kwargs)
    except Exception as e:
        from core.exceptions import PendingApproval as _PA
        if isinstance(e, _PA):
            raise  # 审批类异常是控制流（不是故障），原样向上
        # 执行面异常如实回执（token 已焚、不得重试——但管理员有权知道
        # 死在哪儿，而不是无信息 500）
        return {"ok": False,
                "message": f"已批准但执行异常（未生效）: {type(e).__name__}: {str(e)[:300]}"}
    finally:
        ctx.clear_nuke_batch()
    # 结算响应如实（无条件"已批准并执行"会把执行失败埋进 result
    # 字段——UI 显示成功而实际未生效）
    if isinstance(result, ToolResult) and not result.data.get("ok"):
        return {"ok": False,
                "message": f"已批准但执行未生效：{str(result)[:400]}",
                "result": str(result)[:2000]}
    return {"ok": True, "message": "已批准并执行", "result": str(result)[:2000]}
