"""HTTP Range 流式文件响应（PDF 预览等场景）

浏览器内置 PDF 查看器通过 Range 请求按需拉取文件片段（线性化加载），
大文件（几百页 PDF）不必整文件下载/解析即可秒开第一页。
FastAPI/Starlette 的 FileResponse 不支持 Range，这里统一实现单区间流式响应。
"""

from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

_CHUNK = 256 * 1024  # 256KB 块


def _iter_file(path: Path, start: int, length: int):
    remaining = length
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def ranged_file_response(
    request: Request,
    path: Path,
    content_type: str = "application/octet-stream",
    filename: str | None = None,
    download: bool = False,
) -> StreamingResponse:
    """返回支持 Range 的流式文件响应

    - 无 Range 头：200 全量流式返回（带 Accept-Ranges 广告）
    - 有 Range 头（单区间 bytes=start-end / bytes=start- / bytes=-N）：
      206 返回对应片段（带 Content-Range）
    - 区间非法：416
    - download=True 时 Content-Disposition: attachment（触发浏览器下载）
    """
    size = path.stat().st_size
    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": content_type,
    }
    if filename:
        # 文件名做 RFC 2616 安全处理
        safe = filename.replace('"', "").encode("ascii", "replace").decode() or "file"
        disposition = "attachment" if download else "inline"
        base_headers["Content-Disposition"] = f'{disposition}; filename="{safe}"'

    range_header = request.headers.get("range", "").strip()
    if not range_header.startswith("bytes="):
        return StreamingResponse(
            _iter_file(path, 0, size),
            status_code=200,
            headers={**base_headers, "Content-Length": str(size)},
            media_type=content_type,
        )

    # 解析单区间（多区间不支持——浏览器 PDF 查看器只发单区间）
    spec = range_header[len("bytes="):].split(",", 1)[0].strip()
    try:
        start_s, end_s = spec.split("-", 1)
        if start_s == "":
            # 后缀区间：最后 N 字节
            n = int(end_s)
            if n <= 0:
                raise ValueError
            start, end = max(size - n, 0), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        if start > end or start >= size:
            raise ValueError
        end = min(end, size - 1)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=416,
            detail=f"Range 不可满足: {range_header}（文件大小 {size}）",
            headers={"Content-Range": f"bytes */{size}"},
        )

    length = end - start + 1
    return StreamingResponse(
        _iter_file(path, start, length),
        status_code=206,
        headers={
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
        media_type=content_type,
    )


def resolve_under_root(root: Path, candidate: str) -> Path:
    """把客户端给的路径解析到 root 之下（防目录穿越），不存在/越界抛 4xx"""
    try:
        p = Path(candidate).resolve()
        root_resolved = root.resolve()
        p.relative_to(root_resolved)
    except (ValueError, OSError):
        raise HTTPException(status_code=403, detail="非法文件路径")
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {candidate}")
    return p
