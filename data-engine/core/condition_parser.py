"""
条件解析模块——从用户指令中提取筛选条件
统一管理条件格式和 AI FC 调用

支持的操作：
  =, !=, <>, >, <, >=, <=
  LIKE, NOT LIKE
  IN, NOT IN
  BETWEEN, NOT BETWEEN
  IS NULL, IS NOT NULL
"""

from core.contract.security_contract import is_valid_identifier


def _escape_str(v: str) -> str:
    """转义字符串值中的单引号（' → ''），防止闭合字符串注入"""
    return v.replace("'", "''")


def extract_conditions(instruction: str, ai_client=None) -> list:
    """从用户指令中提取筛选条件
    
    返回: [{"field": "x", "op": "LIKE", "value": "s%", "link": "AND"}]
    """
    if ai_client is None:
        return []
    func = [{
        "type": "function",
        "function": {
            "name": "extract_conditions",
            "description": "提取筛选条件",
            "parameters": {
                "type": "object",
                "properties": {
                    "conditions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "description": "字段名"},
                                "op": {
                                    "type": "string",
                                    "enum": ["=", "!=", "<>", ">", "<", ">=", "<=",
                                             "LIKE", "NOT LIKE", "IN", "NOT IN",
                                             "BETWEEN", "NOT BETWEEN",
                                             "IS NULL", "IS NOT NULL"],
                                    "description": "操作符"
                                },
                                "value": {"type": "string", "description": "值（BETWEEN 用逗号分隔两个值）"},
                                "link": {"type": "string", "enum": ["AND", "OR"], "description": "与上个条件的连接符（第一个条件的link被忽略）"}
                            },
                            "required": ["field", "op"]
                        }
                    }
                },
                "required": ["conditions"]
            }
        }
    }]
    try:
        from core.llm_usage import set_role as _usage_role
        with _usage_role("extract_param"):
            _, args = ai_client.call_function(func, instruction,
                system_prompt="从指令中提取筛选条件。示例：'大于100'→>=100，'以s开头'→LIKE 's%'，'在10到20之间'→BETWEEN 10,20。没有条件就返回空数组。")
        return args.get("conditions", []) if args else []
    except Exception as e:
        # AI 调用失败必须显式报错——静默返回 [] 会退化为无 WHERE 全表查询
        raise ValueError(f"查询条件解析失败: {e}") from e


def build_where(conditions: list) -> str:
    """将条件列表拼接为 WHERE 子句

    每个条件的 link 表示与上一个条件的连接符（AND/OR）。
    第一个条件的 link 被忽略（前面没有条件可连接）。

    同字段多个 "=" 以 AND 相连且值不同 → 归并为 IN（确定性）：
    "X 是 A 或 B" 常被提取成 [X=A AND X=B]——永真为空，唯一合理解是 IN；
    AI 未标 link=OR 时也不翻车（S7 批量删事故）。
    """
    merged = []
    by_field_eq: dict = {}
    for c in conditions or []:
        if (c.get("op", "=") == "=" and c.get("field")
                and str(c.get("link", "AND")).upper() == "AND"):
            key = c["field"]
            if key in by_field_eq:
                by_field_eq[key]["values"].append(str(c.get("value", "")))
                continue
            by_field_eq[key] = {"cond": dict(c), "values": [str(c.get("value", ""))]}
            merged.append(by_field_eq[key]["cond"])
            continue
        merged.append(c)
    for f, grp in by_field_eq.items():
        vals = [v for v in grp["values"] if v]
        if len(set(vals)) >= 2:
            grp["cond"]["op"] = "IN"
            grp["cond"]["value"] = ",".join(vals)

    if not merged:
        return ""
    parts = []
    for i, c in enumerate(merged):
        f = c.get("field", "")
        v = str(c.get("value", ""))
        op = c.get("op", "=")
        link = c.get("link", "AND").upper()
        # 值归一：剥离 AI 预加的首尾成对引号（'X'/"X" → X）——
        # 组装层统一负责加引号；不去掉会双重引号导致永远查空（S7 更新 0 条事故）
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if not f:
            continue
        if not is_valid_identifier(f):
            raise ValueError(f"非法字段名: {f!r}（必须以字母/下划线开头，只含字母数字下划线）")
        if link not in ("AND", "OR"):
            raise ValueError(f"非法条件连接符: {link!r}（只允许 AND/OR）")

        op_upper = op.upper()

        if op_upper in ("IS NULL", "IS NOT NULL"):
            cond = f"{f} {op_upper}"
        elif op_upper in ("IN", "NOT IN"):
            if not v:
                continue
            vals = v.split(",") if isinstance(v, str) else (v if isinstance(v, list) else [str(v)])
            quoted = ",".join(f"'{_escape_str(x.strip())}'" for x in vals)
            cond = f"{f} {op_upper} ({quoted})"
        elif op_upper in ("BETWEEN", "NOT BETWEEN"):
            if not v:
                continue
            vals = v.split(",") if isinstance(v, str) else v
            if len(vals) >= 2:
                cond = f"{f} {op_upper} '{_escape_str(vals[0].strip())}' AND '{_escape_str(vals[1].strip())}'"
            else:
                continue
        elif op_upper in ("LIKE", "NOT LIKE"):
            if not v:
                continue
            cond = f"{f} {op_upper} '{_escape_str(v)}'"
        elif op in (">", "<", ">=", "<="):
            if not v:
                continue
            cond = f"{f} {op} '{_escape_str(v)}'"
        elif op == "!=" or op == "<>":
            if not v:
                continue
            cond = f"{f}<>'{_escape_str(v)}'"
        else:  # "="
            if not v:
                continue
            # 多值拆分（自然语言枚举）：'A 或 B'、'A、B'、'A，B'、'A,B'
            # 单个 "=" 装不下枚举，确定性转 IN——AI 提取常把 "TEST-902 或 TEST-903"
            # 塞成一个字面值，不拆则永远查空（S7 批量删事故）
            import re as _re
            mv = [x.strip().strip("'\"") for x in _re.split(r"\s*或\s*|[、，,]", v) if x.strip()]
            if len(mv) >= 2:
                quoted = ",".join(f"'{_escape_str(x)}'" for x in mv)
                cond = f"{f} IN ({quoted})"
            else:
                cond = f"{f} = '{_escape_str(v)}'"

        # 第一个条件不加连接符，后续条件用自己的 link 与上一个连接
        if i == 0:
            parts.append(cond)
        else:
            parts.append(f"{link} {cond}")

    if not parts:
        return ""
    return " WHERE " + " ".join(parts)
