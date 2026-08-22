"""
数据校验模块——业务层契约校验，依赖 YAML schema。

职责：写入前校验值是否满足 YAML 定义的字段规则（min/max/pattern/enum）

调用方：data_ops / schema_manager（业务层）
"""


def validate_row(table_config: dict, row: dict) -> str:
    """校验单行数据是否满足表定义中的约束。
    返回空字符串表示通过，否则返回错误描述。
    """
    for col in table_config.get("columns", []):
        cname = col["name"]
        if cname not in row:
            continue
        val = row[cname]
        if val is None:
            continue

        # min
        min_val = col.get("min")
        if min_val is not None:
            if isinstance(val, str):
                try:
                    val_num = float(val)
                except ValueError:
                    return f"字段 '{cname}' 要求最小值 {min_val}，但收到了非数值 '{val}'"
            else:
                val_num = float(val)
            if val_num < min_val:
                return f"字段 '{cname}' 的值 {val} 小于允许的最小值 {min_val}"

        # max
        max_val = col.get("max")
        if max_val is not None:
            if isinstance(val, str):
                try:
                    val_num = float(val)
                except ValueError:
                    return f"字段 '{cname}' 要求最大值 {max_val}，但收到了非数值 '{val}'"
            else:
                val_num = float(val)
            if val_num > max_val:
                return f"字段 '{cname}' 的值 {val} 大于允许的最大值 {max_val}"

        # pattern
        pattern = col.get("pattern")
        if pattern and isinstance(val, str):
            import re
            if not re.match(pattern, val):
                return f"字段 '{cname}' 的值 '{val}' 不匹配要求的格式"

        # enum
        enum_vals = col.get("enum")
        if enum_vals and isinstance(enum_vals, list) and isinstance(val, str):
            if val not in enum_vals:
                return f"字段 '{cname}' 的值 '{val}' 不在允许的枚举范围内: {enum_vals}"

    return ""
