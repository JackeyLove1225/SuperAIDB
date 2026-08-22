"""Office 文件预览转换（LibreOffice 后端转 PDF）

前端 DocxPreview/PptxPreview/XlsxPreview 调用此接口，
后端用 soffice --convert-to pdf 转换，缓存到 data-engine/cache/preview/
"""

from fastapi import APIRouter, HTTPException, Request, Query

from agent.management.deps import logger

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


@router.get("/api/preview/status")
def preview_status():
    """检查 LibreOffice 是否可用 + 缓存统计

    前端启动时可调用此接口，若 soffice 不可用则降级到前端 docx-preview
    """
    from core.parser.office_converter import get_cache_stats, is_supported
    stats = get_cache_stats()
    return {
        "available": stats["soffice_available"],
        "soffice_path": stats["soffice_path"],
        "cache_dir": stats["cache_dir"],
        "cache_file_count": stats["file_count"],
        "cache_size_mb": stats["total_size_mb"],
        "supported_exts": sorted(
            {"docx", "doc", "rtf", "odt", "xlsx", "xls", "ods", "pptx", "ppt", "odp"}
        ),
    }


@router.post("/api/preview/convert")
async def preview_convert(request: Request):
    """把 Office 文件转换为 PDF

    请求体：
        {
            "file_base64": "<base64 编码的文件内容>",
            "filename": "report.docx",
            "timeout": 60  // 可选，默认 60 秒
        }

    响应：
        {
            "pdf_base64": "<base64 编码的 PDF>",
            "cached": false,
            "convert_time_ms": 2345,
            "filename": "report.docx"
        }

    错误：
        400: 文件格式不支持 / base64 解码失败
        500: LibreOffice 未安装 / 转换失败
        504: 转换超时
    """
    import base64 as _b64
    from core.parser.office_converter import (
        convert_to_pdf, is_supported, find_soffice, SUPPORTED_EXTS
    )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")

    file_b64 = body.get("file_base64")
    filename = body.get("filename", "unknown.docx")
    timeout = int(body.get("timeout", 60))

    if not file_b64 or not isinstance(file_b64, str):
        raise HTTPException(status_code=400, detail="缺少 file_base64 字段")

    # 检查文件格式
    if not is_supported(filename):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {filename}（支持: {', '.join(sorted(SUPPORTED_EXTS))}）"
        )

    # 检查 soffice 是否可用
    if not find_soffice():
        raise HTTPException(
            status_code=500,
            detail="LibreOffice (soffice) 未安装。Windows: winget install TheDocumentFoundation.LibreOffice"
        )

    # 解码 base64
    try:
        file_bytes = _b64.b64decode(file_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"base64 解码失败: {e}")

    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="文件内容为空")

    # 限制文件大小（100MB）
    max_size = 100 * 1024 * 1024
    if len(file_bytes) > max_size:
        raise HTTPException(
            status_code=400,
            detail=f"文件过大: {len(file_bytes) // (1024*1024)}MB，最大支持 100MB"
        )

    # 转换
    try:
        pdf_bytes, cached, elapsed_ms = convert_to_pdf(file_bytes, filename, timeout=timeout)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        logger.error(f"Office 转 PDF 失败: {filename} - {e}")
        raise HTTPException(status_code=500, detail=f"转换失败: {e}")

    pdf_b64 = _b64.b64encode(pdf_bytes).decode("ascii")

    return {
        "pdf_base64": pdf_b64,
        "cached": cached,
        "convert_time_ms": elapsed_ms,
        "filename": filename,
        "original_size": len(file_bytes),
        "pdf_size": len(pdf_bytes),
    }


@router.post("/api/preview/cache/clear")
def preview_cache_clear(max_age_days: int = Query(30, ge=0, le=365), request: Request = None):
    """清理超过 N 天的预览缓存（管理用）——仅 admin"""
    _require_admin(request)
    from core.parser.office_converter import clear_cache, get_cache_stats
    cleared = clear_cache(max_age_days=max_age_days)
    stats = get_cache_stats()
    return {
        "cleared": cleared,
        "remaining_files": stats["file_count"],
        "remaining_size_mb": stats["total_size_mb"],
    }


@router.get("/api/preview/pdf")
def preview_pdf_stream(request: Request, path: str, timeout: int = Query(120, ge=10, le=600)):
    """Office 文件转 PDF 的流式预览（支持 HTTP Range）

    与 POST /api/preview/convert 同一条 LibreOffice 转换链路与 SHA1 缓存，
    但输入是 uploads 目录下的服务器路径、输出是 Range 流式响应——
    浏览器 PDF 查看器按需拉取片段，大文件秒开，不再整份 base64 往返。

    安全：path 必须解析到 uploads 根目录之下（防路径穿越）。
    认证双通道：Bearer（XHR）或 sig 签名参数（浏览器原生资源；fail-closed）。
    """
    _sig = request.query_params.get("sig", "")
    if _sig:
        from core.auth import verify_media_token
        if not verify_media_token(path, _sig):
            raise HTTPException(status_code=401, detail="签名无效或已过期")

    from core.parser.office_converter import (
        convert_to_pdf_path, is_supported, find_soffice, SUPPORTED_EXTS
    )
    from agent.management.ranged_response import ranged_file_response, resolve_under_root
    from agent.management.routers.files import _upload_root

    src = resolve_under_root(_upload_root(), path)
    if not is_supported(src.name):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {src.name}（支持: {', '.join(sorted(SUPPORTED_EXTS))}）"
        )
    if not find_soffice():
        raise HTTPException(status_code=500, detail="LibreOffice (soffice) 未安装")

    try:
        pdf_path, _cached, _elapsed = convert_to_pdf_path(
            src.read_bytes(), src.name, timeout=timeout
        )
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        logger.error(f"Office 转 PDF 失败: {src.name} - {e}")
        raise HTTPException(status_code=500, detail=f"转换失败: {e}")

    return ranged_file_response(
        request, pdf_path, content_type="application/pdf",
        filename=f"{src.stem}.pdf",
    )
