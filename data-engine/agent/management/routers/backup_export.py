"""备份恢复与数据导出端点"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from core.contract.security_contract import is_valid_identifier
from agent.management.deps import _project_root

router = APIRouter()


def _require_admin(request: "Request | None") -> None:
    """写端点仅限 admin（security_review 修复，与 routers/permissions.py 同款）

    中间件只校验"有无合法凭据"（Bearer 任意角色 或 API Key=system），
    不校验角色——文件级写端点此前普通 user 登录即可调用。
    本依赖强制：Bearer 必须是 admin；API Key（system）等同 admin
    （system 是可信系统级身份，见 server 中间件注释）。
    request=None：进程内直接调用（测试/内部），不经 HTTP 闸，放行。
    """
    from fastapi import HTTPException
    from core.auth import verify_token, verify_api_key
    from config.settings import settings
    if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
        return  # 本地开发模式不强制
    if request is None:
        return  # 进程内直接调用（测试/内部），非 HTTP 入口
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = verify_token(auth_header[7:])
        if not payload:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
        return
    api_key = request.headers.get("X-API-Key")
    if api_key and verify_api_key(api_key):
        return  # API Key = system 身份，等同 admin
    raise HTTPException(status_code=401, detail="未授权：需要 admin 凭据")


def _require_user(request: "Request | None") -> None:
    """读/取回端点至少要求登录用户（readonly 拒）——评审五轮：自助注册的
    user 此前可下载 admin 导出的全表 CSV（列权限的二次扩散面）。
    request=None：进程内直接调用（测试/内部），不经 HTTP 闸，放行。
    签名媒体通道（sig 由端点 fail-closed 验过才到角色判定）视为 user 级已认证。"""
    from fastapi import HTTPException
    from core.auth import verify_token, verify_api_key
    from config.settings import settings
    if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
        return  # 本地开发模式不强制
    if request is None:
        return
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = verify_token(auth_header[7:])
        if not payload:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
        if payload.get("role") == "readonly":
            raise HTTPException(status_code=403, detail="只读角色不可执行此操作")
        return
    api_key = request.headers.get("X-API-Key")
    if api_key and verify_api_key(api_key):
        return  # API Key = system 身份
    if request.query_params.get("sig"):
        return  # 签名媒体通道（端点已验签）
    raise HTTPException(status_code=401, detail="未授权：需要登录")


@router.get("/api/backups")
def list_backups(request: Request):
    """列出所有数据库备份——仅 admin（备份是全库内容）"""
    _require_admin(request)
    from core.backup import list_backups as _list
    return {"backups": _list()}


@router.post("/api/backup")
def create_backup(request: Request = None):
    """手动触发数据库备份——仅 admin"""
    _require_admin(request)
    from core.backup import backup_database
    result = backup_database()
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@router.post("/api/restore")
def restore_backup(backup_filename: str, request: Request = None):
    """从备份恢复数据库（整库覆盖）——仅 admin"""
    _require_admin(request)
    from core.backup import restore_database
    result = restore_database(backup_filename)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["message"])
    return result


# ── 数据导出 ──

@router.post("/api/export")
def export_data(table: str = "", selection_id: int = 0, where: str = "", format: str = "csv",
                request: Request = None):
    """导出数据为 CSV 或 Excel 文件——仅 admin

    - table: 表名（导出全表数据）
    - selection_id: 选择集编号（导出选择集数据，暂只支持 CSV）
    - where: WHERE 条件（可选，筛选导出数据）
    - format: 导出格式，csv（默认）或 excel
    """
    _require_admin(request)
    from core.exporter import export_table_to_csv, export_selection_to_csv, export_table_to_excel
    fmt = (format or "csv").lower().strip()
    if fmt not in ("csv", "excel"):
        raise HTTPException(status_code=400, detail=f"不支持的导出格式: {format}，只支持 csv 或 excel")
    if selection_id:
        if fmt != "csv":
            raise HTTPException(status_code=400, detail="选择集导出暂只支持 CSV 格式")
        result = export_selection_to_csv(selection_id)
    elif table:
        if not is_valid_identifier(table):
            raise HTTPException(status_code=400, detail="非法表名")
        if fmt == "excel":
            result = export_table_to_excel(table, where=where)
        else:
            result = export_table_to_csv(table, where=where)
    else:
        raise HTTPException(status_code=400, detail="请指定表名或选择集编号")
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@router.get("/api/exports")
def list_exports(request: Request):
    """列出所有已导出的文件（CSV / Excel）——登录用户（readonly 拒）"""
    _require_user(request)
    from core.exporter import list_exports as _list
    return {"exports": _list()}


@router.get("/api/exports/{filename}/download")
def download_export(filename: str, request: Request):
    """下载导出的 CSV 文件——登录用户（readonly 拒）

    认证双通道：Bearer（XHR）或 sig 签名参数（浏览器下载锚点；fail-closed）。
    sig 的签名对象是文件名（与路径语义一致）。
    """
    _require_user(request)
    from fastapi.responses import FileResponse
    _sig = request.query_params.get("sig", "")
    if _sig:
        from core.auth import verify_media_token
        if not verify_media_token(Path(filename).name, _sig):
            raise HTTPException(status_code=401, detail="签名无效或已过期")
    # 安全校验：只允许文件名，不允许路径
    safe_name = Path(filename).name
    export_dir = Path(_project_root) / "exports"
    filepath = export_dir / safe_name
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(filepath), filename=safe_name, media_type="text/csv")
