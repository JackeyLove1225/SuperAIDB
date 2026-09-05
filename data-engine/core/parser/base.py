"""解析器抽象基类——所有格式解析器继承此类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Union


@dataclass
class PhysicalCell:
    """带物理坐标的单元格"""
    row: int
    col: int
    value: str
    rowspan: int = 1
    colspan: int = 1
    style: Optional[dict] = None


@dataclass
class ParsedDocument:
    """解析后的统一数据结构

    tables 字段支持两种格式（to_structured_text 会自动识别）：
    - list[list[list[PhysicalCell]]]：带物理坐标的旧格式
    - list[list[list[str]]]：紧凑存储的新格式（PdfParser/WordParser 使用，节省内存）
    """
    raw_text: str                                   # 纯文本内容
    tables: list[list[list[Union[PhysicalCell, str]]]] = field(default_factory=list)  # 表格列表
    structured_tables: list["StructuredTable"] = field(default_factory=list)  # 结构化多表
    paragraphs: list[str] = field(default_factory=list)  # 段落文本
    images: list[bytes] = field(default_factory=list)    # 嵌入图片
    metadata: dict = field(default_factory=dict)         # 元数据（文件名、页数等）

    def to_structured_text(self) -> str:
        """转为带坐标的结构化文本供 AI 分析

        兼容两种表格存储格式：
        - list[list[PhysicalCell]]：旧格式，每个 cell 有 row/col/value
        - list[list[str]]：紧凑格式，仅存值（节省内存，476页PDF省8-12MB）
        """
        lines = []
        lines.append(f"【文件元数据】{self.metadata}")
        lines.append(f"\n【段落文本】\n" + "\n".join(self.paragraphs))

        if self.structured_tables:
            # 输出结构化多表（含层级关系）
            for st in self.structured_tables:
                lines.append(f"\n【{st.name}】(类型:{st.table_type})")
                if st.foreign_key:
                    lines.append(f"  关联: {st.foreign_key} -> {st.references}")
                for row in st.rows:
                    cells = [f"{c.value}(行{c.row+1}列{c.col+1})" for c in row]
                    lines.append(" | ".join(cells))
        else:
            # 兼容两种表格格式
            for ti, table in enumerate(self.tables):
                lines.append(f"\n【表格 {ti+1}】")
                for ri, row in enumerate(table):
                    # 检测元素类型：PhysicalCell or str
                    if row and hasattr(row[0], "value"):
                        # PhysicalCell 格式
                        cells = [f"{c.value}(行{c.row+1}列{c.col+1})" for c in row]
                    else:
                        # 紧凑 str 格式
                        cells = [f"{v}(行{ri+1}列{ci+1})" for ci, v in enumerate(row)]
                    lines.append(" | ".join(cells))

        lines.append(f"\n【原始文本】\n{self.raw_text}")
        return "\n".join(lines)


@dataclass
class StructuredTable:
    """带语义信息的结构化子表"""
    name: str                              # 业务名称（如"定额主表"）
    table_type: str                        # "category" / "main" / "detail" / "metadata"
    rows: list[list[PhysicalCell]]         # 数据行
    headers: list[str] = field(default_factory=list)  # 表头
    foreign_key: Optional[str] = None      # 外键字段名（子表指向主表）
    references: Optional[str] = None       # 关联的主表名
    description: str = ""                  # 业务描述
    level: int = 0                         # 分类层级（category表用）


class BaseParser(ABC):
    """解析器抽象基类"""

    @abstractmethod
    def parse(self, file_path: str) -> ParsedDocument:
        """解析文件，返回统一结构"""
        ...
