"""LibreOffice 解析器——补充 PPTX/老格式支持

设计动机：
- core/parser/ 原有 PdfParser/WordParser/ExcelParser 覆盖 PDF/DOCX/XLSX
- 但 PPTX/PPT 和老格式 .doc/.xls/.ppt 不支持：
  - python-pptx 未引入项目依赖
  - python-docx 只支持 .docx，不支持 .doc
  - openpyxl 只支持 .xlsx，不支持 .xls（当前 .xls 映射到 ExcelParser 是潜在 bug）
- LibreOffice 已为前端预览引入，可复用做后端解析的补充路径

工作流程：
  Office 文件 → LibreOffice 转 PDF → PdfParser 解析 → ParsedDocument

双层缓存（关键性能优化）：
  1. office_converter SHA1 缓存：同一文件只转一次 PDF（cache/preview/{sha1}.pdf）
  2. PdfParser 7 天缓存：同一 PDF 只解析一次（db/parser_cache/{key}.json）
  关键设计：用 get_cache_path() 拿固定路径，让 PdfParser 缓存能命中

性能：
  - 首次：LibreOffice 转换 3-5s + PyMuPDF 解析 0.1-1s ≈ 3-6s
  - PDF 缓存命中但解析缓存未命中：0.1-1s（只跑 PyMuPDF）
  - 双层缓存命中：<50ms

支持的格式：
  - PowerPoint: .pptx .ppt .odp（无原生库）
  - Word 老格式: .doc .odt .rtf（python-docx 不支持）
  - Excel 老格式: .xls .ods（openpyxl 不支持 .xls）

注意：
  - .docx/.xlsx 优先用 WordParser/ExcelParser（更快更准，直接读 XML 结构）
  - 本解析器只在原生库不支持的格式上使用
  - 复用 office_converter.convert_to_pdf() 和 PdfParser，不重复造轮子
"""

import time
import tempfile
from pathlib import Path

from .base import BaseParser, ParsedDocument
from .pdf_parser import PdfParser


# 本解析器支持的扩展名（原生库不支持的格式）
SUPPORTED_EXTS = {
    ".pptx", ".ppt", ".odp",        # PowerPoint（项目无 python-pptx 依赖）
    ".doc", ".odt", ".rtf",          # Word 老格式（python-docx 只支持 .docx）
    ".xls", ".ods",                  # Excel 老格式（openpyxl 只支持 .xlsx）
}


class LibreOfficeParser(BaseParser):
    """LibreOffice 转 PDF 后用 PyMuPDF 解析——补充 PPTX/老格式支持

    适用场景：
    - PPTX/PPT/ODP（无 python-pptx 依赖）
    - .doc/.odt/.rtf 老格式（python-docx 只支持 .docx）
    - .xls/.ods 老格式（openpyxl 只支持 .xlsx）

    不适用场景（用原生库更快更准）：
    - .pdf  → PdfParser（直接读，无需转换）
    - .docx → WordParser（直接读 XML 结构）
    - .xlsx → ExcelParser（直接读单元格坐标）

    Args:
        extract_tables: 是否提取表格（透传给 PdfParser）
        use_cache: 是否使用 PdfParser 的解析结果缓存
        convert_timeout: LibreOffice 转换超时秒数
    """

    def __init__(
        self,
        extract_tables: bool = True,
        use_cache: bool = True,
        convert_timeout: int = 60,
    ):
        self.extract_tables = extract_tables
        self.use_cache = use_cache
        self.convert_timeout = convert_timeout

    def parse(self, file_path: str) -> ParsedDocument:
        """解析 Office 文件

        流程：
        1. 读取文件二进制
        2. 调用 office_converter 转 PDF（带 SHA1 缓存）
        3. 用缓存的 PDF 固定路径让 PdfParser 解析（解析结果可缓存）
        4. 在 metadata 里标记原始格式和解析路径

        Raises:
            RuntimeError: LibreOffice 未安装
            FileNotFoundError: 源文件不存在
            ValueError: 不支持的格式
        """
        # 懒导入：避免在模块加载时就依赖 office_converter
        # （office_converter 可能在 LibreOffice 未装时被 import，只要不调用 convert 就行）
        from core.parser.office_converter import (
            convert_to_pdf,
            find_soffice,
            get_cache_path,
        )

        # 0. 检查 LibreOffice 是否可用
        if not find_soffice():
            raise RuntimeError(
                "LibreOffice (soffice) 未安装，无法解析 PPTX/老格式 Office 文件。"
                "请安装 LibreOffice：winget install TheDocumentFoundation.LibreOffice"
            )

        src_path = Path(file_path)
        if not src_path.is_file():
            raise FileNotFoundError(f"文件不存在：{file_path}")

        ext = src_path.suffix.lower()
        if ext not in SUPPORTED_EXTS:
            raise ValueError(
                f"LibreOfficeParser 不支持 .{ext.lstrip('.')} 格式。"
                f"支持的格式：{', '.join(sorted(SUPPORTED_EXTS))}"
            )

        # 1. 读取源文件
        file_bytes = src_path.read_bytes()
        filename = src_path.name

        # 2. LibreOffice 转 PDF（带 SHA1 缓存，同一文件只转一次）
        convert_start = time.time()
        pdf_bytes, pdf_cached, convert_ms = convert_to_pdf(
            file_bytes, filename, timeout=self.convert_timeout
        )
        convert_elapsed = time.time() - convert_start

        # 3. 用缓存的 PDF 路径让 PdfParser 解析
        #    关键设计：用固定路径（cache/preview/{sha1}.pdf）而非临时文件
        #    这样 PdfParser 的缓存（基于路径+mtime+size）能命中，二次解析秒级返回
        pdf_path = get_cache_path(file_bytes)

        # 准备 PdfParser
        pdf_parser = PdfParser(
            extract_tables=self.extract_tables,
            use_cache=self.use_cache,
        )

        if pdf_path.is_file():
            # 正常路径：用缓存文件（固定路径，PdfParser 缓存可命中）
            doc = pdf_parser.parse(str(pdf_path))
        else:
            # 极端兜底：convert_to_pdf 返回了 bytes 但缓存文件不在
            # （概率极低：仅在文件系统异常时发生）
            # 写到临时文件，用完即删
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_pdf = Path(tmp_dir) / f"{src_path.stem}.pdf"
                tmp_pdf.write_bytes(pdf_bytes)
                doc = pdf_parser.parse(str(tmp_pdf))

        # 4. 补充 metadata：标记原始格式和解析路径
        doc.metadata.update({
            "original_filename": filename,
            "original_format": ext.lstrip("."),
            "parser": "libreoffice+pymupdf",
            "pdf_convert_time_ms": convert_ms,
            "pdf_cached": pdf_cached,
            "total_convert_time_s": round(convert_elapsed, 2),
            "original_size_bytes": len(file_bytes),
            "pdf_size_bytes": len(pdf_bytes),
        })

        return doc
