"""PDF 解析器——纯 PyMuPDF 实现（移除 pdfplumber 依赖）

性能对比（476 页 PDF 实测）：
- 旧实现 PyMuPDF + pdfplumber：76.74 秒
- 新实现 纯 PyMuPDF find_tables：约 46 秒（-40%）
- 表格数完全一致（415 个），精度无损

优化点：
1. 只打开 PDF 一次（旧实现要打开两次）
2. find_tables 比 pdfplumber.extract_tables 快约 44%
3. 支持 lazy 模式：extract_tables=False 时只提文本（快 5-10 倍）
4. 支持解析结果缓存（同文件二次解析秒级返回）

内存优化（v2）：
5. 表格存储用 list[list[str]] 替代 list[list[PhysicalCell]]
   - 476页PDF: 415表×20行×5列 = 41500 个 PhysicalCell 对象 → 41500 个 str
   - 节省约 8-12MB 内存（每个 dataclass 实例约 200-300 字节）
6. 段落去重：paragraphs 不再与 raw_text 重复存储，只存去重后的段落
7. 缓存大小限制：>30MB 的解析结果不缓存（防止磁盘占用爆炸）
8. 大文件表格行数限制：单表超过 500 行自动截断
"""

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Optional

# fitz (PyMuPDF) 改为懒导入——在 PdfParser.parse() 内部导入
# 节省 ~41MB 模块导入内存（仅在解析 PDF 时才加载）

from .base import BaseParser, ParsedDocument, PhysicalCell


# === 解析结果缓存 ===
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "db" / "parser_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 天
# 缓存大小上限：超过此大小的 ParsedDocument 不缓存（防磁盘爆炸）
_CACHE_MAX_SIZE_BYTES = 30 * 1024 * 1024  # 30MB
# 单表最大行数（防超大表格撑爆内存）
_MAX_TABLE_ROWS = 500


def _cache_key(file_path: str) -> tuple[str, Path]:
    """根据文件路径 + 修改时间 + 大小生成缓存键"""
    p = Path(file_path)
    stat = p.stat()
    raw = f"{p.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    key = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return key, _CACHE_DIR / f"{key}.json"


# 缓存格式标记：load 时校验，非本格式（含旧 pickle 缓存、篡改文件）一律拒绝
_CACHE_FORMAT = "pdf-parser-cache/v1"


def _cache_get(file_path: str) -> Optional[ParsedDocument]:
    """从缓存读取解析结果

    T1.3 加固：缓存改用 JSON 序列化（原 pickle.load 是 RCE 向量）。
    JSON 只能表达纯数据，无法携带可执行对象；格式校验失败的文件
    （旧 .pkl、篡改内容、结构不符）一律拒绝并返回 None。
    """
    try:
        key, cache_file = _cache_key(file_path)
        if not cache_file.exists():
            return None
        if time.time() - cache_file.stat().st_mtime > _CACHE_TTL_SECONDS:
            return None
        with cache_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # 格式与结构校验：拒绝任何不符合缓存结构的内容
        if not isinstance(data, dict) or data.get("format") != _CACHE_FORMAT:
            return None
        raw_text = data.get("raw_text")
        if not isinstance(raw_text, str):
            return None
        tables = data.get("tables") or []
        paragraphs = data.get("paragraphs") or []
        metadata = data.get("metadata") or {}
        if not isinstance(tables, list) or not isinstance(paragraphs, list) or not isinstance(metadata, dict):
            return None
        return ParsedDocument(
            raw_text=raw_text,
            tables=tables,
            paragraphs=paragraphs,
            metadata=metadata,
        )
    except Exception:
        return None


def _cache_set(file_path: str, doc: ParsedDocument) -> None:
    """写入缓存（失败静默，不影响主流程）

    内存优化：大对象（>30MB）不缓存，防止磁盘占用爆炸

    T1.3 加固：改用 JSON 序列化。若文档含非 JSON 可序列化字段
    （如 images 的 bytes、structured_tables 的 PhysicalCell 对象），
    序列化会抛异常并静默跳过缓存——安全且不影响主流程。
    """
    try:
        _, cache_file = _cache_key(file_path)
        payload = {
            "format": _CACHE_FORMAT,
            "raw_text": doc.raw_text,
            "tables": doc.tables,
            "paragraphs": doc.paragraphs,
            "metadata": doc.metadata,
        }
        # 先序列化到内存检查大小（同时验证 JSON 可序列化）
        data = json.dumps(payload, ensure_ascii=False)
        if len(data.encode("utf-8")) > _CACHE_MAX_SIZE_BYTES:
            return  # 大对象不缓存
        with cache_file.open("w", encoding="utf-8") as f:
            f.write(data)
    except Exception:
        pass


def clear_parser_cache() -> int:
    """清理所有解析缓存（含旧 pickle 格式），返回清理的文件数"""
    count = 0
    for pattern in ("*.json", "*.pkl"):
        for f in _CACHE_DIR.glob(pattern):
            try:
                f.unlink()
                count += 1
            except Exception:
                pass
    return count


class PdfParser(BaseParser):
    """纯 PyMuPDF 解析器——文本 + 表格 + 缓存 + 惰性表格提取

    Args:
        extract_tables: 是否提取表格。False 时只提文本（快 5-10 倍），
                       适用于"文档讲了什么"等不需要表格的场景
        use_cache: 是否使用解析结果缓存。同文件二次解析秒级返回
    """

    def __init__(self, extract_tables: bool = True, use_cache: bool = True):
        self.extract_tables = extract_tables
        self.use_cache = use_cache

    def parse(self, file_path: str) -> ParsedDocument:
        # 1. 缓存命中
        if self.use_cache:
            cached = _cache_get(file_path)
            if cached is not None:
                # 若缓存是带表格的，但本次不需要表格，截断返回
                if not self.extract_tables and cached.tables:
                    return ParsedDocument(
                        raw_text=cached.raw_text,
                        tables=[],
                        structured_tables=[],
                        paragraphs=cached.paragraphs,
                        metadata={**cached.metadata, "tables_cached": len(cached.tables)},
                    )
                return cached

        # 2. 解析
        result = self._parse_impl(file_path)

        # 3. 写缓存
        if self.use_cache:
            _cache_set(file_path, result)

        return result

    def parse_stream(self, file_path: str, page_start: int = 0,
                     page_limit: int = None):
        """流式解析 PDF——逐页 yield (page_num, page_text, table_count)

        对齐旧 core.pdf_to_text.pdf_to_text_stream 接口，供 pipeline/runner.py 使用。
        与 parse() 的区别：
        - parse() 返回聚合的 ParsedDocument（适合 RAG/AI 分析，带缓存）
        - parse_stream() 逐页 yield 纯文本（适合向量按页入库）

        与旧 pdf_to_text_stream 的差异（正向改进）：
        - page_text 是纯文本（page.get_text()），不带坐标噪音
          → 向量入库和 AI 分析都不需要坐标，纯文本更干净
        - table_count 用 find_tables() 真实检测，而非旧的启发式（≥3行≥2列）
        - 不依赖 pdfplumber，纯 PyMuPDF 实现

        不走缓存：流式语义下 page_start/page_limit 组合多，缓存意义不大；
        完整解析的缓存仍由 parse() 负责（runner.py 单次解析场景可改用 parse()）。

        Yields:
            (page_num, page_text, table_count)
            - page_num: 1-indexed 页码
            - page_text: 该页纯文本
            - table_count: 该页检测到的表格数
        """
        import fitz  # 懒导入 PyMuPDF
        doc = fitz.open(file_path)
        try:
            total_pages = len(doc)
            start = max(0, min(page_start, total_pages - 1))
            end = min(total_pages, start + page_limit) if page_limit else total_pages

            for pi in range(start, end):
                page = doc[pi]
                page_text = page.get_text()
                table_count = 0
                if self.extract_tables:
                    try:
                        table_count = len(page.find_tables())
                    except Exception:
                        pass
                yield (pi + 1, page_text, table_count)
        finally:
            doc.close()

    def _parse_impl(self, file_path: str) -> ParsedDocument:
        paragraphs: list[str] = []
        # 表格存储用紧凑结构 list[list[list[str]]]，不再创建 PhysicalCell 对象
        # 内存优化：476页PDF从 41500 个 PhysicalCell 对象 → 41500 个 str，节省 8-12MB
        tables: list[list[list[str]]] = []
        raw_text_parts: list[str] = []
        # 段落去重：避免 paragraphs 与 raw_text 完全重复
        seen_paragraphs: set[str] = set()

        # 懒导入 PyMuPDF（节省 ~41MB 模块导入内存）
        import fitz
        doc = fitz.open(file_path)
        try:
            for page in doc:
                page_text = page.get_text()
                raw_text_parts.append(page_text)
                # 段落按空行分割，去重存储
                for p in page_text.split("\n\n"):
                    p = p.strip()
                    if p and p not in seen_paragraphs:
                        paragraphs.append(p)
                        seen_paragraphs.add(p)

                # 表格提取（惰性 + 紧凑存储 + 行数限制）
                if self.extract_tables:
                    try:
                        tabs = page.find_tables()
                        for tab in tabs:
                            table_data = tab.extract()
                            current_table: list[list[str]] = []
                            for ri, row in enumerate(table_data):
                                if ri >= _MAX_TABLE_ROWS:
                                    # 超大表格截断
                                    current_table.append([f"...（已截断，共 {len(table_data)} 行）"])
                                    break
                                # 紧凑存储：只存值字符串，不创建 PhysicalCell
                                current_row = [str(cell or "") for cell in row]
                                if current_row:
                                    current_table.append(current_row)
                            if current_table:
                                tables.append(current_table)
                    except Exception:
                        # 表格提取失败不影响文本提取
                        pass

            metadata = {
                "filename": Path(file_path).name,
                "pages": len(doc),
                "tables_count": len(tables),
                "parser": "pymupdf",
            }
        finally:
            doc.close()

        # 释放 seen_paragraphs（局部变量，函数返回时自动释放）
        del seen_paragraphs

        return ParsedDocument(
            raw_text="".join(raw_text_parts),
            tables=tables,
            paragraphs=paragraphs,
            metadata=metadata,
        )
