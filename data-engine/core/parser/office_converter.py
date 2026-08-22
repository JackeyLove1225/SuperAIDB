"""Office 文件转 PDF 模块

使用 LibreOffice (soffice) 命令行模式把 Office 文件转为 PDF，前端通过 iframe 渲染。

为什么不用前端 JS 库（docx-preview/mammoth.js）：
  - docx-preview 在浏览器解析大文件慢且不稳定（experimental 选项触发死循环）
  - mammoth.js 仅生成语义 HTML，丢失样式（字体/颜色/页眉页脚/复杂表格边框）
  - LibreOffice 是成熟 Office 引擎，渲染保真度等同桌面版打开效果

架构：
  前端 base64 → 后端写入临时文件 → soffice --convert-to pdf → 读取 PDF → 返回 base64
  缓存：data-engine/cache/preview/{sha1}.pdf，同一文件只转换一次

性能：
  - 首次转换：2-5 秒（soffice 启动 + 渲染）
  - 缓存命中：<50ms（仅读文件 + base64 编码）
  - PDF 渲染：浏览器原生支持，比 docx-preview 快 10-50 倍

支持的格式：docx/doc/rtf/odt/xlsx/xls/ods/csv/pptx/ppt/odp
"""

import os
import sys
import hashlib
import shutil
import subprocess
import tempfile
import threading
import time
import platform
from pathlib import Path
from typing import Optional, Tuple
import base64
from core.logger import get_logger

logger = get_logger(__name__)

# 项目根目录（data-engine/）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 缓存目录：data-engine/cache/preview/
CACHE_DIR = _PROJECT_ROOT / "cache" / "preview"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 支持转换的扩展名（小写，无点）
SUPPORTED_EXTS = {
    # Word
    "docx", "doc", "rtf", "odt",
    # Excel
    "xlsx", "xls", "ods",
    # PowerPoint
    "pptx", "ppt", "odp",
}

# soffice 全局锁：LibreOffice 不支持并发实例（同一 user profile 会冲突）
_soffice_lock = threading.Lock()

# soffice 路径缓存
_soffice_path: Optional[str] = None


def find_soffice() -> Optional[str]:
    """查找 soffice 可执行文件路径

    查找顺序：
    1. 环境变量 SOFFICE_PATH
    2. PATH 中的 soffice
    3. Windows 常见安装路径
    4. 项目同级 LibreOffice 便携目录
    """
    global _soffice_path
    if _soffice_path:
        return _soffice_path

    # 1. 环境变量
    env_path = os.environ.get("SOFFICE_PATH")
    if env_path and Path(env_path).is_file():
        _soffice_path = env_path
        logger.info(f"soffice found via SOFFICE_PATH: {env_path}")
        return _soffice_path

    # 2. PATH
    which = shutil.which("soffice") or shutil.which("soffice.exe")
    if which:
        _soffice_path = which
        logger.info(f"soffice found in PATH: {which}")
        return _soffice_path

    # 3. Windows 常见安装路径
    if platform.system() == "Windows":
        candidates = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\LibreOffice\program\soffice.exe"),
            # 便携版可能放在项目同级目录
            str(_PROJECT_ROOT.parent / "LibreOffice" / "program" / "soffice.exe"),
            str(_PROJECT_ROOT / "LibreOffice" / "program" / "soffice.exe"),
        ]
        for c in candidates:
            if Path(c).is_file():
                _soffice_path = c
                logger.info(f"soffice found at: {c}")
                return _soffice_path
    else:
        # Linux/macOS
        candidates = [
            "/usr/bin/soffice",
            "/usr/bin/libreoffice",
            "/snap/bin/libreoffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
        for c in candidates:
            if Path(c).is_file():
                _soffice_path = c
                logger.info(f"soffice found at: {c}")
                return _soffice_path

    return None


def is_supported(filename: str) -> bool:
    """检查文件扩展名是否支持转换"""
    ext = Path(filename).suffix.lower().lstrip(".")
    return ext in SUPPORTED_EXTS


def _file_sha1(data: bytes) -> str:
    """计算文件内容的 SHA1 哈希（用于缓存键）"""
    return hashlib.sha1(data).hexdigest()


def _get_user_profile_dir() -> str:
    """获取 LibreOffice user profile 目录（隔离并发实例）

    LibreOffice 不支持多实例共享同一 user profile，会报错。
    使用固定目录避免与桌面版 LibreOffice 冲突。
    """
    profile = _PROJECT_ROOT / "cache" / "lo_profile"
    profile.mkdir(parents=True, exist_ok=True)
    return f"file:///{str(profile).replace(chr(92), '/')}"


def convert_to_pdf_path(
    file_bytes: bytes,
    filename: str,
    timeout: int = 60,
) -> Tuple[Path, bool, int]:
    """把 Office 文件转换为 PDF，返回缓存 PDF 的文件路径（不读回字节）

    供流式预览端点使用：转换/命中后直接以文件路径做 Range 流式响应，
    避免大 PDF 整份读进内存再 base64。
    """
    start = time.time()

    # 检查 soffice 是否可用
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError(
            "LibreOffice (soffice) 未安装。请安装 LibreOffice 或设置 SOFFICE_PATH 环境变量。"
            "Windows: winget install TheDocumentFoundation.LibreOffice"
        )

    # 检查扩展名
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的文件格式: .{ext}")

    # 计算缓存键
    file_hash = _file_sha1(file_bytes)
    cache_pdf = CACHE_DIR / f"{file_hash}.pdf"

    # 缓存命中
    if cache_pdf.is_file() and cache_pdf.stat().st_size > 0:
        elapsed = int((time.time() - start) * 1000)
        logger.info(f"Office 转 PDF 缓存命中: {filename} (hash={file_hash[:8]}, {elapsed}ms)")
        return cache_pdf, True, elapsed

    # 加锁：soffice 不支持并发实例
    with _soffice_lock:
        # 双重检查缓存（可能其他线程已转换完成）
        if cache_pdf.is_file() and cache_pdf.stat().st_size > 0:
            elapsed = int((time.time() - start) * 1000)
            return cache_pdf, True, elapsed

        # 写入临时文件（soffice 需要文件路径，不能从 stdin 读）
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_dir_path = Path(tmp_dir)
            input_file = tmp_dir_path / f"{file_hash}.{ext}"
            input_file.write_bytes(file_bytes)

            # 输出目录（与输入同目录，soffice 会自动生成同名 .pdf）
            output_dir = tmp_dir_path

            # 构建 soffice 命令
            # -env:UserInstallation 隔离 user profile，避免与桌面版冲突
            cmd = [
                soffice,
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--nodefault",
                "--nofirststartwizard",
                f"-env:UserInstallation={_get_user_profile_dir()}",
                "--convert-to", "pdf",
                "--outdir", str(output_dir),
                str(input_file),
            ]

            logger.info(f"soffice 转换开始: {filename} (hash={file_hash[:8]})")

            try:
                # subprocess.run 阻塞直到完成或超时
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout,
                    # Windows 下避免弹出控制台窗口
                    creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
                )
            except subprocess.TimeoutExpired:
                logger.error(f"soffice 转换超时 ({timeout}s): {filename}")
                raise TimeoutError(f"LibreOffice 转换超时（{timeout}s），文件可能过大或结构异常")

            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
                logger.error(f"soffice 转换失败 (code={result.returncode}): {stderr}")
                raise RuntimeError(f"LibreOffice 转换失败: {stderr[:500]}")

            # 查找输出 PDF（soffice 会生成 {file_hash}.pdf）
            output_pdf = output_dir / f"{file_hash}.pdf"
            if not output_pdf.is_file():
                # 备选：查找目录下所有 .pdf
                pdfs = list(output_dir.glob("*.pdf"))
                if pdfs:
                    output_pdf = pdfs[0]
                else:
                    raise RuntimeError("LibreOffice 转换完成但未找到输出 PDF")

            # 移入缓存（不写回内存字节，调用方按路径流式读取）
            cache_pdf.write_bytes(output_pdf.read_bytes())

            elapsed = int((time.time() - start) * 1000)
            logger.info(
                f"soffice 转换成功: {filename} → PDF "
                f"(input={len(file_bytes)} bytes, output={cache_pdf.stat().st_size} bytes, {elapsed}ms)"
            )

            return cache_pdf, False, elapsed


def convert_to_pdf(
    file_bytes: bytes,
    filename: str,
    timeout: int = 60,
) -> Tuple[bytes, bool, int]:
    """把 Office 文件转换为 PDF

    Returns:
        (pdf_bytes, cached, convert_time_ms)

    Raises:
        RuntimeError: soffice 未安装或转换失败
        TimeoutError: 转换超时
    """
    pdf_path, cached, elapsed = convert_to_pdf_path(file_bytes, filename, timeout)
    return pdf_path.read_bytes(), cached, elapsed


def convert_base64_to_pdf_b64(
    file_b64: str,
    filename: str,
    timeout: int = 60,
) -> Tuple[str, bool, int]:
    """便捷方法：base64 输入 → base64 输出

    Args:
        file_b64: 文件 base64 字符串
        filename: 原始文件名
        timeout: 转换超时秒数

    Returns:
        (pdf_b64, cached, convert_time_ms)
    """
    file_bytes = base64.b64decode(file_b64)
    pdf_bytes, cached, elapsed = convert_to_pdf(file_bytes, filename, timeout)
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    return pdf_b64, cached, elapsed


def get_cache_path(file_bytes: bytes) -> Path:
    """获取文件对应的缓存 PDF 路径（无论是否已转换）

    用于让其他模块复用转换后的 PDF 文件，避免重复存储。
    LibreOfficeParser 用此路径让 PdfParser 基于固定路径做解析结果缓存。

    Args:
        file_bytes: 原始文件二进制内容

    Returns:
        缓存 PDF 的路径（文件可能尚未生成，需先调用 convert_to_pdf）
    """
    file_hash = _file_sha1(file_bytes)
    return CACHE_DIR / f"{file_hash}.pdf"


def get_cache_stats() -> dict:
    """获取缓存统计信息（供调试/管理界面使用）"""
    files = list(CACHE_DIR.glob("*.pdf"))
    total_size = sum(f.stat().st_size for f in files)
    return {
        "cache_dir": str(CACHE_DIR),
        "file_count": len(files),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "soffice_available": find_soffice() is not None,
        "soffice_path": find_soffice(),
    }


def clear_cache(max_age_days: int = 30) -> int:
    """清理超过 max_age_days 天的缓存文件

    Returns: 清理的文件数量
    """
    now = time.time()
    cleared = 0
    for f in CACHE_DIR.glob("*.pdf"):
        try:
            age = now - f.stat().st_mtime
            if age > max_age_days * 86400:
                f.unlink()
                cleared += 1
        except Exception as e:
            logger.warning(f"清理缓存失败 {f}: {e}")
    if cleared:
        logger.info(f"清理 Office 转 PDF 缓存: {cleared} 个文件 (>{max_age_days}天)")
    return cleared


# 启动时清理超过 30 天的缓存
try:
    clear_cache(max_age_days=30)
except Exception as e:
    logger.warning(f"启动清理缓存失败: {e}")