"""
技术校验模块——所有 Driver 共享的通用安全校验，不依赖 YAML。
"""
import re
from typing import Optional

def validate_type_name(col_type: str, valid_types: set) -> str:
    ct = col_type.upper().strip()
    base = ct.split("(")[0].split(" ")[0]
    if base not in valid_types:
        return f"不支持的类型: {col_type}。支持: {', '.join(sorted(valid_types))}"
    return ""

def validate_where(where: str) -> bool:
    # 子查询/写关键字禁止（20260822 补）：值字符集含括号字母，`id IN (SELECT …)`
    # 形态的跨表子查询此前可穿透——SELECT/UNION 等关键字一律拦。
    # 函数形态布尔预言机同禁：CASE/SLEEP/BENCHMARK 等可经值域括号构造
    #（时间 DoS / 条件推断——无越权增益，但纵深不收这口子）
    # 已知边界：字段值恰为整词 'select' 会误伤（罕见，fail-closed 方向可接受）
    if re.search(r'\b(SELECT|UNION|INSERT|UPDATE|DELETE|DROP|ATTACH|PRAGMA|'
                 r'CASE|SLEEP|BENCHMARK|EXTRACTVALUE|LOAD_FILE|INTO\s+OUTFILE)\b',
                 where, re.IGNORECASE):
        return False
    # 标量函数形态禁止（值域含括号字母，`name = randomblob(999999999)` 形态的
    # 内存 DoS 可穿透——剔除引号字面量后，IN/NOT 之外的标识符紧贴 `(` 一律拦；
    # fail-closed 方向，合法 WHERE 无函数调用需求）
    _unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "", where)
    if re.search(r'\b(?!IN\b)(?!NOT\b)[a-zA-Z_]\w*\s*\(', _unquoted, re.IGNORECASE):
        return False
    return bool(re.match(
        r'^[a-zA-Z_][\w.]*\s*(?:IS\s+(?:NOT\s+)?NULL|'
        r'(?:[=!<>]+|IN|NOT\s+IN|LIKE|NOT\s+LIKE|BETWEEN)\s*[\w\'\"%\s.,()/-]+)'
        r'(?:\s+(AND|OR)\s+[a-zA-Z_][\w.]*\s*(?:IS\s+(?:NOT\s+)?NULL|'
        r'(?:[=!<>]+|IN|NOT\s+IN|LIKE|NOT\s+LIKE|BETWEEN)\s*[\w\'\"%\s.,()/-]+))*$',
        where.strip(), re.IGNORECASE))


# ============================================================
# CHECK 约束表达式安全校验
# ============================================================

# 危险关键字黑名单（词边界匹配，大小写不敏感）
_DANGEROUS_KEYWORDS = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "EXEC", "EXECUTE", "UNION", "JOIN", "INTO", "GRANT",
    "REVOKE", "CALL", "PRAGMA", "ATTACH", "DETACH", "REPLACE", "MERGE",
}

# 允许的运算符/关键字白名单（大小写不敏感）
_ALLOWED_KEYWORDS = {
    "AND", "OR", "NOT", "IN", "LIKE", "BETWEEN", "IS", "NULL",
    "TRUE", "FALSE",
}

# 允许的函数白名单（后跟左括号才认定为函数调用）
_ALLOWED_FUNCTIONS = {
    "LENGTH", "CHAR_LENGTH", "DATE", "DATETIME", "NOW", "CURDATE",
    "ABS", "LOWER", "UPPER", "TRIM", "COALESCE", "ROUND", "CAST",
}

# 危险字符序列（注释、语句结束符）
_DANGEROUS_SEQUENCES = [";", "--", "/*", "*/", "#", "&&", "||"]


def validate_check_expr(expr: str, col_name: str = "",
                        col_type: str = "",
                        table_columns: Optional[list] = None) -> tuple:
    """校验 CHECK 约束表达式安全性

    参数:
        expr: CHECK 表达式字符串（如 "age >= 0 AND age <= 150"）
        col_name: 当前字段名（用于列名约束校验）
        col_type: 当前字段类型（暂未使用，预留）
        table_columns: 同表所有列名列表（可选，允许跨列约束如 end_date > start_date）

    返回: (ok: bool, message: str)
        ok=True 表示通过；ok=False 时 message 为中文错误说明

    校验规则（按顺序短路）:
        1. 空检查
        2. 危险字符序列（; -- /* */ # 等）
        3. 危险关键字黑名单（词边界）
        4. 括号配平
        5. token 化白名单校验（运算符/函数/字面量/列名）
        6. 列名约束：裸标识符必须 ∈ {col_name} ∪ table_columns
    """
    if not expr or not expr.strip():
        return (False, "CHECK 表达式不能为空")

    s = expr.strip()

    # 2. 危险字符序列
    for seq in _DANGEROUS_SEQUENCES:
        if seq in s:
            return (False, f"CHECK 表达式禁止包含字符序列 '{seq}'（防止 SQL 注入）")

    # 3. 危险关键字黑名单（词边界）
    for kw in _DANGEROUS_KEYWORDS:
        pattern = r'\b' + re.escape(kw) + r'\b'
        if re.search(pattern, s, re.IGNORECASE):
            return (False, f"CHECK 表达式禁止使用关键字 '{kw}'")

    # 4. 括号配平
    depth = 0
    for ch in s:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                return (False, "CHECK 表达式括号不配平：右括号多于左括号")
    if depth != 0:
        return (False, f"CHECK 表达式括号不配平：差 {depth} 个左括号")

    # 5. token 化白名单校验
    # 先剥离单引号字符串字面量，避免字符串内部的词被误判为标识符
    # 把 'xxx' 替换为空字符串占位（保留引号配平信息，但不提取内部词）
    s_without_strings = re.sub(r"'(?:[^']|'')*'", "''", s)

    # 允许的列名集合
    allowed_identifiers = set()
    if col_name:
        allowed_identifiers.add(col_name)
    if table_columns:
        allowed_identifiers.update(table_columns)

    # token 化：提取所有"词"（标识符/关键字/函数名）
    # 标识符正则：字母/下划线开头，后跟字母/数字/下划线/点
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_.]*", s_without_strings)

    for tok in tokens:
        tok_upper = tok.upper()
        # 跳过白名单关键字
        if tok_upper in _ALLOWED_KEYWORDS:
            continue
        # 跳过布尔字面量（已在 _ALLOWED_KEYWORDS）
        # 跳过函数名（需检查后面是否跟左括号）
        if tok_upper in _ALLOWED_FUNCTIONS:
            # 检查是否以函数形式调用（后面紧跟左括号）
            # 用正则查找该 token 后是否跟 (
            pattern = r'\b' + re.escape(tok) + r'\s*\('
            if re.search(pattern, s_without_strings, re.IGNORECASE):
                continue
            # 不是函数调用形式（如裸 NOW），允许 NOW/CURDATE 作为字面量
            if tok_upper in ("NOW", "CURDATE"):
                continue
            # 其他函数名不跟括号 → 当作列名处理，走列名校验
        # 跳过 NULL/TRUE/FALSE（已在 _ALLOWED_KEYWORDS）
        # 此时 tok 应该是列名
        if tok not in allowed_identifiers:
            # 允许是 col_name 的不同大小写
            if col_name and tok.lower() == col_name.lower():
                continue
            if table_columns and any(tok.lower() == c.lower() for c in table_columns):
                continue
            # 不是已知列名，且不是关键字/函数 → 拒绝
            return (False, f"CHECK 表达式中的标识符 '{tok}' 不合法：必须是当前字段名 '{col_name}'"
                          + (f" 或同表列名（{', '.join(table_columns)}）" if table_columns else "")
                          + "，或白名单关键字/函数")

    # 6. 引号字符串内部无未转义单引号（成对检查）
    # 简化检查：单引号数量必须为偶数
    quote_count = s.count("'")
    if quote_count % 2 != 0:
        return (False, "CHECK 表达式中单引号不配平（字符串字面量未闭合）")

    return (True, "")

