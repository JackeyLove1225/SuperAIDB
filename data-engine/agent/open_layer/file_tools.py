"""文件工具——供 deep research 的 Researcher 调用

复用能力：
- core/parser/*：PDF/Excel/Word 结构化解析（现有）
- 普通文本文件直接读取

新增能力：
- list_files：浏览目录结构
- search_in_files：跨文件 grep 检索
- write_file：代码保留，FILE_WRITE_ENABLED 开关永久关闭

设计原则：
- 只读为主，写权限永久关闭（预防日后需求）
- 文件访问沙箱限制（FILE_ACCESS_ROOT）
- 复用现有 parser，不重新造轮子
"""

import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config.settings import settings


# === 沙箱安全 ===

def _safe_path(path: str) -> Path:
    """将用户路径转为沙箱内安全路径

    - 相对路径基于 FILE_ACCESS_ROOT 解析
    - 防止路径穿越（../）
    """
    root = Path(settings.FILE_ACCESS_ROOT).resolve()
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    # 检查是否在沙箱内
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError(f"路径越界（仅允许访问 {root}）：{path}")
    return p


# === 只读工具 ===

def list_files(path: str = ".", max_depth: int = 3, max_items: int = 100) -> str:
    """列目录（树状结构，限制深度）

    Args:
        path: 目录路径（相对路径基于 FILE_ACCESS_ROOT）
        max_depth: 最大递归深度
        max_items: 最大返回条目数（防止输出过大）

    Returns:
        树状结构文本
    """
    try:
        target = _safe_path(path)
    except ValueError as e:
        return str(e)

    if not target.exists():
        return f"路径不存在：{path}"
    if not target.is_dir():
        return f"不是目录：{path}"

    lines = []
    item_count = 0

    def _walk(dir_path: Path, prefix: str, depth: int):
        nonlocal item_count
        if depth > max_depth or item_count >= max_items:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return

        # 跳过隐藏目录/文件、__pycache__、.git 等
        skip_dirs = {".git", "__pycache__", ".libs", "node_modules", ".venv", "venv"}
        entries = [e for e in entries if e.name not in skip_dirs and not e.name.startswith(".")]

        for i, entry in enumerate(entries):
            if item_count >= max_items:
                lines.append(f"{prefix}...（已达最大条目数，省略后续）")
                return
            is_last = (i == len(entries) - 1)
            connector = "└── " if is_last else "├── "
            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                item_count += 1
                extension = "    " if is_last else "│   "
                _walk(entry, prefix + extension, depth + 1)
            else:
                size = entry.stat().st_size
                size_str = f" ({_format_size(size)})" if size > 0 else ""
                lines.append(f"{prefix}{connector}{entry.name}{size_str}")
                item_count += 1

    lines.append(f"{target.name}/")
    _walk(target, "", 1)
    return "\n".join(lines)


def read_file(path: str, max_chars: int = 8000, extract_tables: bool = True) -> str:
    """读文件——复用现有 parser

    支持格式：
    - 结构化文件：PDF/Excel/Word（复用 core/parser/*）
    - 文本文件：txt/csv/json/md/py/yaml/yml/log/xml/html/js/ts/sql

    Args:
        path: 文件路径（相对路径基于 FILE_ACCESS_ROOT）
        max_chars: 最大返回字符数（防止 token 溢出）
        extract_tables: PDF 是否提取表格（False 时只提文本，快 5-10 倍）

    Returns:
        文件内容文本
    """
    try:
        target = _safe_path(path)
    except ValueError as e:
        return str(e)

    if _is_sensitive_file(target):
        return f"{_SENSITIVE_FILE_MESSAGE}：{target.name}"

    if not target.exists():
        return f"文件不存在：{path}"
    if not target.is_file():
        return f"不是文件：{path}"
    if target.stat().st_size > 10 * 1024 * 1024:  # 10MB 限制
        return f"文件过大（>{_format_size(target.stat().st_size)}），请用 search_in_files 检索关键内容"

    content = _parse_file(target, extract_tables=extract_tables)

    # 截断
    if len(content) > max_chars:
        head = content[:int(max_chars * 0.7)]
        tail = content[-int(max_chars * 0.2):]
        omitted = len(content) - len(head) - len(tail)
        content = f"{head}\n\n...（已省略约 {omitted} 字符）...\n\n{tail}"

    return content


# 结构化文件扩展名 → parser 工厂
# (parser类名, 是否支持extract_tables参数)
_EXT_PARSERS = {
    # 原生库直接读（更快更准）
    ".pdf":  ("PdfParser", True),       # PyMuPDF 直接读
    ".xlsx": ("ExcelParser", False),    # openpyxl 直接读
    ".docx": ("WordParser", False),     # python-docx 直接读
    # LibreOffice 补充路径（原生库不支持的格式）
    ".xls":  ("LibreOfficeParser", True),  # openpyxl 不支持 .xls 老格式
    ".pptx": ("LibreOfficeParser", True),  # 项目无 python-pptx 依赖
    ".ppt":  ("LibreOfficeParser", True),
    ".odp":  ("LibreOfficeParser", True),
    ".doc":  ("LibreOfficeParser", True),  # python-docx 不支持 .doc 老格式
    ".odt":  ("LibreOfficeParser", True),
    ".rtf":  ("LibreOfficeParser", True),
    ".ods":  ("LibreOfficeParser", True),
}


# parser 工厂字典：parser_name → (extract_tables: bool) -> BaseParser
# 懒导入：工厂函数内部 import，避免模块加载时引入 parser 依赖
# 新增 parser 只需加一行字典项，不再修改 _parse_file 业务逻辑
def _make_pdf_parser(extract_tables: bool):
    from core.parser import PdfParser
    return PdfParser(extract_tables=extract_tables)


def _make_excel_parser(extract_tables: bool):
    from core.parser import ExcelParser
    return ExcelParser()


def _make_word_parser(extract_tables: bool):
    from core.parser import WordParser
    return WordParser()


def _make_libreoffice_parser(extract_tables: bool):
    from core.parser import LibreOfficeParser
    # LibreOffice 转 PDF + PyMuPDF 解析（支持 extract_tables 透传）
    return LibreOfficeParser(extract_tables=extract_tables)


_PARSER_FACTORIES = {
    "PdfParser": _make_pdf_parser,
    "ExcelParser": _make_excel_parser,
    "WordParser": _make_word_parser,
    "LibreOfficeParser": _make_libreoffice_parser,
}


# 普通文本文件扩展名（.env 已移除——敏感文件黑名单统一拦截）
_TEXT_EXTS = {".txt", ".csv", ".json", ".md", ".py", ".yaml", ".yml",
              ".log", ".xml", ".html", ".js", ".ts", ".sql", ".ini",
              ".cfg", ".toml", ".sh", ".bat", ".ps1", ".rb",
              ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp"}

# 敏感文件名黑名单（无论扩展名一律拒读，防 API 密钥/私钥被 AI 工具读出）
# fnmatch 模式，统一小写比较
_SENSITIVE_FILE_PATTERNS = (
    ".env", ".env.*",          # 环境变量/密钥配置
    "*.pem", "*.key",          # 证书/私钥
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",  # SSH 私钥
    "*.p12", "*.pfx",          # PKCS#12 密钥库
    ".netrc", ".npmrc",        # 明文凭据
    "datasources.yml", "datasources.yaml",  # 数据源连接串（MySQL 等凭据所在）
    "master.key",              # 隔离模式密钥库文件
    "initial_admin.txt",       # 初始管理员密码（首轮登录凭证）
    "pending_approvals.json",  # 人审待批 token（"token 不出管理通道"的文件面）
    "escalation.json",         # 提权契约
    "daemon.json",             # daemon IPC 令牌+端口
    "daemon.spawn.lock", "keygen.lock", "isolated.flag",  # 运行时内部态
)

_SENSITIVE_FILE_MESSAGE = "敏感文件不可访问（命中敏感文件黑名单，可能含密钥/凭据）"


def _is_sensitive_file(target: Path) -> bool:
    """文件名级敏感文件判定（无论扩展名）"""
    name = target.name.lower()
    from fnmatch import fnmatch
    return any(fnmatch(name, pat) for pat in _SENSITIVE_FILE_PATTERNS)


def _parse_file(target: Path, extract_tables: bool = True) -> str:
    """解析单个文件并返回文本内容（内部复用函数）

    内存优化：解析后立即释放 ParsedDocument 对象，只保留 to_structured_text 文本
    - 476页PDF: ParsedDocument 对象约 15-20MB → 释放后只保留文本约 1-2MB
    """
    # 敏感文件黑名单（read_directory 等批量入口的兜底防线）
    if _is_sensitive_file(target):
        return f"{_SENSITIVE_FILE_MESSAGE}：{target.name}"

    ext = target.suffix.lower()

    # 结构化文件：复用现有 parser
    if ext in _EXT_PARSERS:
        parser_name, _ = _EXT_PARSERS[ext]
        try:
            factory = _PARSER_FACTORIES.get(parser_name)
            if factory is None:
                return f"未知 parser: {parser_name}"
            # 解析 + 立即转文本 + 释放 ParsedDocument 对象
            parser = factory(extract_tables)
            doc = parser.parse(str(target))
            text = doc.to_structured_text()
            # 显式释放 ParsedDocument（含 raw_text/tables/paragraphs 等大字段）
            del doc
            return text
        except Exception as e:
            return f"解析失败：{e}"

    # 普通文本文件直接读取（已有 10MB 上限保护）
    encodings = ["utf-8", "gbk", "latin-1"]
    for enc in encodings:
        try:
            return target.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return f"无法解码文件（非文本文件）：{target.name}"


def read_directory(path: str = ".", max_chars_per_file: int = 4000,
                   max_total_chars: int = 20000, max_files: int = 20,
                   file_pattern: str = "*", extract_tables: bool = False,
                   max_depth: int = 3) -> str:
    """批量读取整个目录下的不同文件——自动按扩展名选择 parser

    场景：
    - 用户问"分析 uploads 文件夹里所有文件的内容"
    - 用户问"这个项目里有哪些文档，分别讲了什么"
    - 深度研究时一次扫描整个目录

    策略：
    1. 按扩展名自动选择 parser（PDF/Excel/Word/文本）
    2. 每个文件内容带文件名前缀（便于 AI 区分来源）
    3. 三重限制防 token 溢出：
       - 单文件 max_chars_per_file
       - 总字符 max_total_chars
       - 文件数 max_files
    4. 默认 extract_tables=False（文件夹批量读取通常只需文本概览，速度快 5-10 倍）

    Args:
        path: 目录路径
        max_chars_per_file: 单文件最大字符数
        max_total_chars: 所有文件总字符数上限
        max_files: 最大文件数
        file_pattern: 文件名模式（如 *.pdf）
        extract_tables: PDF 是否提取表格（批量场景默认 False）
        max_depth: 最大递归深度

    Returns:
        合并后的多文件内容（每段带文件名前缀）
    """
    try:
        target = _safe_path(path)
    except ValueError as e:
        return str(e)

    if not target.exists():
        return f"路径不存在：{path}"
    if not target.is_dir():
        return f"不是目录：{path}"

    skip_dirs = {".git", "__pycache__", ".libs", "node_modules", ".venv",
                 "venv", ".pytest_cache", ".langgraph_api", "db"}
    skip_exts = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".dat",
                 ".pkl", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico",
                 ".zip", ".tar", ".gz", ".rar", ".7z"}

    # 收集文件
    files: list[Path] = []
    def _collect(dir_path: Path, depth: int):
        if depth > max_depth or len(files) >= max_files:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return
        for entry in entries:
            if len(files) >= max_files:
                return
            if entry.name in skip_dirs or entry.name.startswith("."):
                continue
            if entry.is_dir():
                _collect(entry, depth + 1)
            elif entry.is_file():
                if entry.name.startswith("."):
                    continue
                if entry.suffix.lower() in skip_exts:
                    continue
                if not entry.match(file_pattern):
                    continue
                # 大小限制：单文件 >5MB 跳过（提示用 read_file 单独读）
                if entry.stat().st_size > 5 * 1024 * 1024:
                    continue
                files.append(entry)

    _collect(target, 1)

    if not files:
        return f"目录 {path} 下未找到可读取的文件（已跳过二进制/大文件/隐藏文件）"

    # 逐个解析并合并
    root = Path(settings.FILE_ACCESS_ROOT).resolve()
    parts: list[str] = []
    total_chars = 0
    skipped: list[str] = []
    processed = 0

    for f in files:
        if total_chars >= max_total_chars or processed >= max_files:
            break
        try:
            rel = f.resolve().relative_to(root)
        except ValueError:
            rel = f
        # 解析
        try:
            content = _parse_file(f, extract_tables=extract_tables)
        except Exception as e:
            skipped.append(f"{f.name}（解析失败：{e}）")
            continue

        if content.startswith("解析失败") or content.startswith("无法解码"):
            skipped.append(f"{f.name}（{content}）")
            continue

        # 单文件截断
        if len(content) > max_chars_per_file:
            head = content[:int(max_chars_per_file * 0.7)]
            tail = content[-int(max_chars_per_file * 0.2):]
            omitted = len(content) - len(head) - len(tail)
            content = f"{head}\n...（已省略约 {omitted} 字符）...\n{tail}"

        # 检查总长度
        block = f"\n\n========== 文件：{rel}（{_format_size(f.stat().st_size)}） ==========\n{content}"
        if total_chars + len(block) > max_total_chars:
            # 加不下了，截断最后一块
            remaining = max_total_chars - total_chars
            if remaining < 200:
                break
            block = block[:remaining] + "\n...（已达总长度上限）..."
            parts.append(block)
            total_chars = max_total_chars
            processed += 1
            break

        parts.append(block)
        total_chars += len(block)
        processed += 1

    # 汇总
    summary = [f"目录 {path} 共扫描 {len(files)} 个文件，已解析 {processed} 个"]
    if skipped:
        summary.append(f"跳过 {len(skipped)} 个：{'; '.join(skipped[:5])}")
    if total_chars >= max_total_chars:
        summary.append(f"已达总长度上限 {max_total_chars} 字符，未读取的文件可用 read_file 单独读取")
    if len(files) >= max_files:
        summary.append(f"已达最大文件数 {max_files}，目录中剩余文件未读取")

    header = "\n".join(summary) + "\n" + "=" * 60
    return header + "".join(parts)


def search_in_files(keyword: str, path: str = ".", file_pattern: str = "*",
                    max_matches: int = 30, max_depth: int = 5) -> str:
    """跨文件全文检索（grep 风格）

    Args:
        keyword: 搜索关键词
        path: 搜索起始目录
        file_pattern: 文件名模式（如 *.py, *.yaml）
        max_matches: 最大匹配数
        max_depth: 最大递归深度

    Returns:
        匹配结果（文件名:行号:内容）

    内存优化：逐行读取代替一次性 read_text，峰值内存从 2MB → 单行 KB 级
    """
    if not keyword:
        return "请提供搜索关键词"

    try:
        target = _safe_path(path)
    except ValueError as e:
        return str(e)

    # 可搜索的文本文件扩展名
    text_exts = {".txt", ".csv", ".json", ".md", ".py", ".yaml", ".yml",
                 ".log", ".xml", ".html", ".js", ".ts", ".sql", ".ini", ".cfg", ".toml"}

    skip_dirs = {".git", "__pycache__", ".libs", "node_modules", ".venv", "venv"}
    root = Path(settings.FILE_ACCESS_ROOT).resolve()
    matches = []
    kw_lower = keyword.lower()

    def _search(dir_path: Path, depth: int):
        if depth > max_depth or len(matches) >= max_matches:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return

        for entry in entries:
            if len(matches) >= max_matches:
                return
            if entry.name in skip_dirs or entry.name.startswith("."):
                continue
            if entry.is_dir():
                _search(entry, depth + 1)
            elif entry.is_file():
                # 文件名模式过滤
                if not entry.match(file_pattern):
                    continue
                # 扩展名过滤
                if entry.suffix.lower() not in text_exts:
                    continue
                # 大小限制
                if entry.stat().st_size > 2 * 1024 * 1024:  # 2MB
                    continue
                # 逐行读取（内存优化：峰值内存从 2MB → 单行 KB 级）
                try:
                    with entry.open("r", encoding="utf-8", errors="ignore") as f:
                        for line_num, line in enumerate(f, 1):
                            if kw_lower in line.lower():
                                try:
                                    rel_path = entry.resolve().relative_to(root)
                                except ValueError:
                                    rel_path = entry
                                matches.append(f"{rel_path}:{line_num}: {line.strip()[:200]}")
                                if len(matches) >= max_matches:
                                    return
                except Exception:
                    continue

    _search(target, 1)

    if not matches:
        return f"未找到包含 '{keyword}' 的内容"

    result = f"找到 {len(matches)} 处匹配：\n\n"
    result += "\n".join(matches)
    if len(matches) >= max_matches:
        result += f"\n\n（已达最大匹配数 {max_matches}，可能还有更多）"
    return result


# === 写工具（永久关闭）===

def write_file(path: str, content: str) -> str:
    """写文件——永久关闭

    代码保留以预防日后需求，但 FILE_WRITE_ENABLED 默认 false 且不建议修改。
    如需启用，在 config/.env 中设置 FILE_WRITE_ENABLED=true。
    """
    if not settings.FILE_WRITE_ENABLED:
        return "写文件功能已禁用（永久关闭）。如需启用，在 config/.env 中设置 FILE_WRITE_ENABLED=true"

    try:
        target = _safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入：{target}"
    except Exception as e:
        return f"写入失败：{e}"


# === 辅助函数 ===

def _format_size(size: int) -> str:
    """格式化文件大小"""
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    else:
        return f"{size / (1024 * 1024):.1f}MB"


# === 工具描述（供 AI 调用时参考）===

FILE_TOOLS_DESCRIPTION = {
    "list_files": {
        "description": "列出目录下的文件和子目录（树状结构）",
        "params": {"path": "目录路径（默认根目录）", "max_depth": "最大深度（默认3）"},
        "returns": "树状结构文本"
    },
    "read_file": {
        "description": "读取单个文件内容（支持PDF/Excel/Word/txt/csv/json/md/py/yaml等）",
        "params": {
            "path": "文件路径",
            "max_chars": "最大字符数（默认8000）",
            "extract_tables": "PDF是否提取表格（默认True，False时快5-10倍）"
        },
        "returns": "文件内容文本"
    },
    "read_directory": {
        "description": "批量读取整个目录下的不同文件——自动按扩展名选择parser（PDF/Excel/Word/文本）",
        "params": {
            "path": "目录路径",
            "max_files": "最大文件数（默认20）",
            "max_total_chars": "总字符数上限（默认20000）",
            "file_pattern": "文件名模式（如*.pdf，默认*）",
            "extract_tables": "PDF是否提取表格（默认False，批量场景只需文本概览）"
        },
        "returns": "多文件合并内容（每段带文件名前缀）"
    },
    "search_in_files": {
        "description": "跨文件全文检索（grep风格）",
        "params": {"keyword": "搜索关键词", "path": "搜索目录（默认根目录）", "file_pattern": "文件名模式（如*.py）"},
        "returns": "匹配结果列表（文件名:行号:内容）"
    },
    "write_file": {
        "description": "写文件（永久关闭，需配置FILE_WRITE_ENABLED=true启用）",
        "params": {"path": "文件路径", "content": "文件内容"},
        "returns": "写入结果"
    }
}
