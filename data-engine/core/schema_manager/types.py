"""类型映射域：AI 可能输出的任何类型 → 系统内部类型
（20260822 拆包：core/schema_manager.py 同名片段纯搬家，逻辑零变化）
"""

# ── 类型映射：AI 可能输出的任何类型 → 系统内部类型 ──

TYPE_ALIASES = {
    "TEXT": "TEXT", "VARCHAR": "VARCHAR", "CHAR": "VARCHAR", "STRING": "VARCHAR",
    "INTEGER": "INTEGER", "INT": "INTEGER", "BIGINT": "INTEGER", "SMALLINT": "INTEGER",
    "FLOAT": "FLOAT", "REAL": "FLOAT", "DOUBLE": "FLOAT", "DECIMAL": "FLOAT", "NUMERIC": "FLOAT",
}

ALLOWED_TYPES = {"TEXT", "VARCHAR", "INTEGER", "FLOAT"}


def _normalize_type(raw: str) -> tuple:
    """将 AI 输出的类型名映射为 (类型, 精度元组)。
    返回 (类型, None) 或 (类型, (总长, 小数位))
    不认识的类型返回 (None, None)
    """
    clean = raw.strip().upper().split(" ")[0]
    precision = None
    # 提取精度：DECIMAL(12,2) → (12,2), VARCHAR(50) → (50,)
    if "(" in clean and clean.endswith(")"):
        base = clean.split("(")[0]
        prec_str = clean[len(base)+1:-1]
        try:
            prec_parts = [int(p.strip()) for p in prec_str.split(",")]
            precision = tuple(prec_parts)
        except Exception:
            precision = None
    else:
        base = clean
    internal = TYPE_ALIASES.get(base)
    return (internal, precision)
