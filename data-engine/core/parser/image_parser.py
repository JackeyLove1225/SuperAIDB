"""图片/扫描件解析器——API 模式调用多模态 AI，预留本地 PaddleOCR 接口

开发阶段：使用多模态 AI API（GPT-4V / DeepSeek-VL）提取文字和理解内容。
客户部署时：可切换为本地 PaddleOCR，无需改核心代码。

内存优化：
- 图片大小预检（>10MB 拒绝，防止 base64 编码后 OOM）
- base64 分块读取+编码（避免一次性加载大图片到内存）
- OCR 调用后立即释放 base64_image 大字段
- OCR 返回内容长度限制（防止异常大响应）
"""

import base64
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

# OpenAI 改为懒导入——在 __init__ 中导入，节省模块导入内存

from .base import BaseParser, ParsedDocument


# 图片大小上限：超过此大小拒绝处理（base64 编码后会膨胀 ~33%）
_MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
# base64 分块读取大小
_BASE64_CHUNK_SIZE = 64 * 1024  # 64KB
# OCR 返回内容最大长度（防止异常大响应撑爆内存）
_MAX_OCR_TEXT_LENGTH = 50000


# ============================================================
# OCR 引擎抽象接口
# ============================================================

class BaseOcrEngine(ABC):
    """OCR 引擎抽象——客户可选 API 或本地部署"""

    @abstractmethod
    def extract_text(self, image_path: str) -> tuple[list[str], str]:
        """
        提取图片中的文字
        返回: (paragraphs, raw_text)
        """
        ...


class ApiOcrEngine(BaseOcrEngine):
    """
    多模态 AI API OCR 引擎（开发阶段使用）
    通过 GPT-4V / DeepSeek-VL 等视觉模型理解图片内容
    """

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: str = "gpt-4o"):
        self.api_key = api_key or os.getenv("AI_API_KEY", "")
        self.base_url = base_url or os.getenv("AI_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("AI_VL_MODEL", "gpt-4o")
        # 懒导入 OpenAI（节省模块导入内存）
        from openai import OpenAI
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _encode_image(self, image_path: str) -> str:
        """分块读取图片并 base64 编码

        内存优化：
        - 旧实现：f.read() 一次性读整个图片到内存（10MB 图片需 10MB+13.3MB=23.3MB）
        - 新实现：分块读取 64KB，base64 编码后追加到结果（峰值内存仅 ~64KB+输出）
        - 大小预检：超过 _MAX_IMAGE_SIZE_BYTES 直接拒绝
        """
        # 大小预检
        file_size = os.path.getsize(image_path)
        if file_size > _MAX_IMAGE_SIZE_BYTES:
            raise ValueError(
                f"图片过大（{file_size // 1024 // 1024}MB > {_MAX_IMAGE_SIZE_BYTES // 1024 // 1024}MB 上限），"
                f"建议压缩后重试或拆分为多张图片"
            )

        # 分块读取 + base64 编码
        chunks: list[str] = []
        with open(image_path, "rb") as f:
            while True:
                chunk = f.read(_BASE64_CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(base64.b64encode(chunk).decode("utf-8"))
        return "".join(chunks)

    def extract_text(self, image_path: str) -> tuple[list[str], str]:
        # 编码图片（分块读取，限制大小）
        base64_image = self._encode_image(image_path)

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请完整提取这张图片中的所有文字内容，"
                                                 "保留原始排版结构。如果是表格，"
                                                 "请用表格形式输出，每行用 | 分隔。"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}",
                                "detail": "high",
                            },
                        },
                    ],
                }],
                max_tokens=4096,
            )
        finally:
            # 立即释放 base64_image 大字段（10MB 图片→13.3MB base64 字符串）
            del base64_image

        content = response.choices[0].message.content or ""

        # OCR 内容长度保护（防止异常大响应）
        if len(content) > _MAX_OCR_TEXT_LENGTH:
            content = content[:_MAX_OCR_TEXT_LENGTH] + "\n...（OCR 响应过长，已截断）"

        paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
        return paragraphs, content


class LocalPaddleOcrEngine(BaseOcrEngine):
    """
    本地 PaddleOCR 引擎（客户部署时使用）
    需要安装 paddlepaddle + paddleocr
    """

    def __init__(self, lang: str = "ch"):
        self.lang = lang
        self._ocr = None

    @property
    def ocr(self):
        if self._ocr is None:
            from paddleocr import PaddleOCR
            self._ocr = PaddleOCR(use_angle_cls=True, lang=self.lang)
        return self._ocr

    def extract_text(self, image_path: str) -> tuple[list[str], str]:
        # 大小预检
        file_size = os.path.getsize(image_path)
        if file_size > _MAX_IMAGE_SIZE_BYTES:
            raise ValueError(
                f"图片过大（{file_size // 1024 // 1024}MB > {_MAX_IMAGE_SIZE_BYTES // 1024 // 1024}MB 上限）"
            )

        result = self.ocr.ocr(image_path, cls=True)
        paragraphs = []
        raw_lines = []

        if result and result[0]:
            for line_info in result[0]:
                # PaddleOCR 返回 [位置, (文字, 置信度)]
                try:
                    text = line_info[1][0] if line_info and len(line_info) > 1 else ""
                except (IndexError, TypeError):
                    continue
                if not text:
                    continue
                raw_lines.append(text)
                paragraphs.append(text)
                # 长度保护
                if sum(len(p) for p in paragraphs) > _MAX_OCR_TEXT_LENGTH:
                    paragraphs.append("...（OCR 结果过长，已截断）")
                    break

        raw_text = "\n".join(raw_lines)
        return paragraphs, raw_text


# ============================================================
# 图片解析器
# ============================================================

class ImageParser(BaseParser):
    """
    图片解析器——默认使用 API 多模态 OCR，可切换为本地部署

    用法：
        # 开发阶段（API）
        parser = ImageParser()  # 默认 ApiOcrEngine

        # 客户本地部署
        from .image_parser import LocalPaddleOcrEngine
        parser = ImageParser(ocr_engine=LocalPaddleOcrEngine(lang="ch"))
    """

    def __init__(self, ocr_engine: Optional[BaseOcrEngine] = None):
        self.engine = ocr_engine or ApiOcrEngine()

    def parse(self, file_path: str) -> ParsedDocument:
        paragraphs, raw_text = self.engine.extract_text(file_path)

        return ParsedDocument(
            raw_text=raw_text,
            paragraphs=paragraphs,
            metadata={
                "filename": Path(file_path).name,
                "ocr_engine": type(self.engine).__name__,
                "text_lines": len(paragraphs),
            }
        )
