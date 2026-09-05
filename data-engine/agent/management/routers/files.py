"""文件上传落盘——把浏览器中的文件批量写入服务器 uploads/ 目录

全自动建库链路的第一环：AI 只能处理服务器磁盘上的文件（process_file 需要本地路径），
前端上传的文件原本只存在于浏览器内存，本端点接收 base64/文本内容并落盘，
返回服务器路径供后续 process_file 入库使用。

安全约束：
- 路径必须相对且不含 .. / 盘符 / 绝对路径（防路径穿越）
- 单文件最大 500MB，单批总量最大 2GB，单批最多 1000 个文件
"""
import json as _json

import base64
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from agent.management.deps import logger, settings, _project_root, _require_user, _upload_root

router = APIRouter()

_MAX_FILE_BYTES = 500 * 1024 * 1024       # 单文件 500MB（流式写盘，本地预览也走 Range 流式）
_MAX_BATCH_BYTES = 2 * 1024 * 1024 * 1024  # 单批总量 2GB
_MAX_BATCH_FILES = 1000                   # 单批最多 1000 个文件（前端按 100/批分片，此为兜底）
# 文件名黑名单：NTFS 保留字符 + 控制字符。
# 其余 Unicode 字符一律放行（间隔号·、书名号、日韩文等均合法）——
# 原白名单会误杀"·"（U+00B7）等中文公文名常见字符，导致整个文件夹落盘失败
_FILENAME_FORBIDDEN_RE = re.compile(r'[:*?"<>|\x00-\x1f]')


def _sanitize_relpath(raw: str) -> Path:
    """校验并规整相对路径：拒绝路径穿越、盘符、NTFS 保留字符（黑名单制）"""
    raw = (raw or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        raise HTTPException(status_code=400, detail="文件路径为空")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail=f"非法路径（含 ..）: {raw}")
    if re.match(r"^[a-zA-Z]:", raw):
        raise HTTPException(status_code=400, detail=f"非法路径（含盘符）: {raw}")
    for p in parts:
        if _FILENAME_FORBIDDEN_RE.search(p):
            raise HTTPException(
                status_code=400,
                detail=f'文件名含非法字符（:*?"<>| 或控制字符）: {p}')
        # Windows 保留设备名与尾部点/空格（写盘静默丢数据/落设备句柄）
        stem = p.rstrip(" .").split(".")[0].upper()
        if stem in ("CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))):
            raise HTTPException(status_code=400,
                                detail=f"文件名是 Windows 保留设备名: {p}")
        if p != p.rstrip(" ."):
            raise HTTPException(status_code=400,
                                detail=f"文件名尾部含点/空格: {p!r}")
    return Path(*parts)



@router.post("/api/files/upload")
async def upload_files(request: Request):
    """批量上传文件落盘——登录用户（readonly 拒；2GB/批写盘无闸是
    DoS 面 + 内容进向量库的扩散面）

    请求体：
        {
            "files": [
                {"path": "定额/xx.xlsx", "content_base64": "<base64>"},
                {"path": "说明/readme.md", "text": "<纯文本内容>"}
            ]
        }

    响应：
        {"ok": true, "upload_dir": "<服务器目录>",
         "files": [{"path": "定额/xx.xlsx", "server_path": "<绝对路径>", "size": 123}]}
    """
    from agent.management.deps import _require_user
    _require_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")

    files = body.get("files")
    if not isinstance(files, list) or not files:
        raise HTTPException(status_code=400, detail="缺少 files 数组")
    if len(files) > _MAX_BATCH_FILES:
        raise HTTPException(status_code=400, detail=f"单批最多 {_MAX_BATCH_FILES} 个文件")

    # 每次上传一个批次目录（时间戳），保留文件夹相对结构
    batch_dir = _upload_root() / time.strftime("batch_%Y%m%d_%H%M%S")
    results = []
    total_bytes = 0

    for i, item in enumerate(files):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"第 {i + 1} 个文件格式错误")
        rel = _sanitize_relpath(item.get("path", ""))

        # 两种内容形式：base64（二进制）或 text（纯文本）
        if item.get("content_base64"):
            try:
                data = base64.b64decode(item["content_base64"])
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"{rel}: base64 解码失败: {e}")
        elif item.get("text") is not None:
            data = str(item["text"]).encode("utf-8")
        else:
            raise HTTPException(status_code=400, detail=f"{rel}: 缺少 content_base64 或 text")

        if len(data) == 0:
            raise HTTPException(status_code=400, detail=f"{rel}: 文件内容为空")
        if len(data) > _MAX_FILE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"{rel}: 文件过大（{len(data) // (1024 * 1024)}MB > 500MB）")
        total_bytes += len(data)
        if total_bytes > _MAX_BATCH_BYTES:
            raise HTTPException(status_code=400, detail="单批总量超过 2GB")

        dest = batch_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        results.append({"path": str(rel).replace("\\", "/"),
                        "server_path": str(dest), "size": len(data)})

    logger.info(f"文件批量上传落盘: {len(results)} 个文件 → {batch_dir}")
    return {"ok": True, "upload_dir": str(batch_dir), "files": results}


@router.post("/api/files/upload-multipart")
async def upload_files_multipart(request: Request):
    """multipart 流式批量上传（大文件夹载入的首选路径）

    与 /api/files/upload（JSON+base64）相比：
    - 浏览器直接发送 File 原始字节（FormData），无 base64 33% 膨胀
    - 前端无需 FileReader 预读整个文件进内存
    - 服务端 SpooledTemporaryFile 流式写盘，大文件不占内存

    表单字段：
        paths: JSON 字符串数组（与 files 一一对应的相对路径）
        files: 多个文件字段

    响应与 /api/files/upload 相同：
        {"ok": true, "files": [{"path": "...", "server_path": "...", "size": 123}]}
    """
    # 与 JSON 姊妹端点同一道闸（multipart 变体漏闸=
    # readonly 可经此写 2GB/批磁盘，同款修复在不同入口间必须同步）
    _require_user(request)
    from starlette.datastructures import UploadFile as StarletteUploadFile

    form = await request.form()
    paths_raw = form.get("paths")
    if not isinstance(paths_raw, str) or not paths_raw:
        raise HTTPException(status_code=400, detail="缺少 paths 字段（JSON 相对路径数组）")
    try:
        paths = _json.loads(paths_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="paths 不是合法 JSON")
    # 注意：表单解析得到的是 starlette UploadFile（fastapi.UploadFile 是其子类，反向不成立）
    files = [v for v in form.getlist("files") if isinstance(v, StarletteUploadFile)]
    if not files or len(files) != len(paths):
        raise HTTPException(
            status_code=400,
            detail=f"files 数量（{len(files)}）与 paths 数量（{len(paths)}）不一致",
        )
    if len(files) > _MAX_BATCH_FILES:
        raise HTTPException(status_code=400, detail=f"单批最多 {_MAX_BATCH_FILES} 个文件")

    batch_dir = _upload_root() / time.strftime("batch_%Y%m%d_%H%M%S")
    results = []
    errors = []  # 单文件级错误：不拖垮整批，前端按 path 标记 persist_failed
    total_bytes = 0

    for rel_raw, up in zip(paths, files):
        # 单文件级校验失败只记录、不中断批次（否则一个超限/非法文件名拖垮整个文件夹）
        try:
            rel = _sanitize_relpath(str(rel_raw))
        except HTTPException as e:
            errors.append({"path": str(rel_raw), "error": str(e.detail)})
            continue
        dest = batch_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 流式写盘（UploadFile 内部为 SpooledTemporaryFile，大文件不爆内存）
        size = 0
        oversize = False
        with open(dest, "wb") as out:
            while True:
                chunk = await up.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_FILE_BYTES:
                    oversize = True
                    break
                out.write(chunk)
        if oversize:
            dest.unlink(missing_ok=True)
            errors.append({
                "path": str(rel),
                "error": f"文件过大（>{_MAX_FILE_BYTES // (1024 * 1024)}MB）",
            })
            continue
        if size == 0:
            dest.unlink(missing_ok=True)
            errors.append({"path": str(rel), "error": "文件内容为空"})
            continue
        total_bytes += size
        if total_bytes > _MAX_BATCH_BYTES:
            # 总量超限：本文件及后续无法接收（已落盘的保留）
            errors.append({"path": str(rel), "error": "单批总量超过 2GB，本文件及后续未接收"})
            break
        results.append({
            "path": str(rel).replace("\\", "/"),
            "server_path": str(dest),
            "size": size,
        })

    logger.info(
        f"multipart 批量上传落盘: {len(results)} 成功 / {len(errors)} 失败 → {batch_dir}"
    )
    return {
        "ok": len(errors) == 0,
        "upload_dir": str(batch_dir),
        "files": results,
        "errors": errors,
    }


@router.get("/api/ingest/progress")
def get_ingest_progress():
    """入库进度心跳——pipeline 每批落盘的进度文件（无任务时返回空状态）"""
    prog = Path(settings.SQLITE_DB_PATH).parent / "ingest_progress.json"
    if not prog.exists():
        return {"status": "idle"}
    try:
        return _json.loads(prog.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "idle"}


@router.get("/api/media/sign")
def sign_media(request: Request, path: str, kind: str = "upload"):
    """签发媒体签名令牌（HMAC(path|exp)，默认 5 分钟）——浏览器原生资源
    （iframe/img/下载锚点）无法带 Bearer，签名 URL 是它们的认证通道。
    kind=upload：uploads 根内路径；kind=export：exports 目录内文件名。
    角色闸：签发过 _require_user——readonly 自助注册账号
    不得为 exports 铸签（admin 全表导出的二次扩散面）。"""
    from agent.management.deps import _require_user
    _require_user(request)
    from agent.management.ranged_response import resolve_under_root
    if kind == "export":
        safe = Path(path).name
        p = Path(_project_root) / "exports" / safe
        path = safe
    else:
        try:
            p = resolve_under_root(_upload_root(), path)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="路径越界")
    if not p.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    from core.auth import sign_media_token
    return {"sig": sign_media_token(path), "ttl": 300}


@router.get("/api/files/raw")
def get_file_raw(request: Request, path: str, download: bool = False):
    """流式读取 uploads 目录下的文件（支持 HTTP Range）

    大文件预览（几百页 PDF）的关键路径：浏览器 PDF 查看器通过 Range 请求
    按需拉取片段，秒开第一页，不再整文件 base64 进内存。

    安全：path 必须解析到 uploads 根目录之下（防路径穿越）。
    download=true 时以附件形式返回（触发浏览器下载）。
    认证双通道：Bearer（XHR）或 sig 签名参数（浏览器原生资源；fail-closed）。
    """
    _sig = request.query_params.get("sig", "")
    if _sig:
        from core.auth import verify_media_token
        if not verify_media_token(path, _sig):
            raise HTTPException(status_code=401, detail="签名无效或已过期")
    else:
        # 无 sig 的 Bearer 通道过 _require_user（无角色闸时
        # readonly Bearer 可直读 uploads 原始业务文件）
        from agent.management.deps import _require_user
        _require_user(request)

    from agent.management.ranged_response import ranged_file_response, resolve_under_root

    p = resolve_under_root(_upload_root(), path)
    ctype = "application/pdf" if p.suffix.lower() == ".pdf" else "application/octet-stream"
    return ranged_file_response(request, p, content_type=ctype, filename=p.name, download=download)
