"""pipeline 跨阶段共享纯函数（无阶段归属的文本/编码归一助手收于此——
提取层与入库层同用，杜绝跨阶段互私下划线私有函数）"""
import unicodedata


def norm_code_value(v) -> str:
    """业务编码值归一（白盒幂等的前提：同一编码必须有同一字节形态）

    文本层 PDF/OCR 常产出全角编码（Ａ１⁃１９）或带空格（A 1-25）——
    不归一会让同一条目以不同码形重复入库（唯一键形同虚设）。
    """
    s = unicodedata.normalize("NFKC", str(v or ""))
    for dash in ("⁃", "—", "−", "–", "一"):
        s = s.replace(dash, "-")
    return "".join(s.split()).strip()
