"""SQL 安全纯函数库——标识符校验与 SQL 片段安全拼装的中立模块

职责：
1. 标识符合法性校验（防 SQL 注入）
2. f-string SQL 内联校验便捷函数（safe_*_sql 系列）
3. SQL 表名提取（契约层读屏蔽与联邦路由共用的唯一实现）
4. SET 子句/字面值解析（权限判定与驱动执行同源）

设计原则：
- 纯函数，无状态，不依赖契约层/驱动层/图层的任何模块（可向下安全引用）
- 所有校验要么返回结果，要么抛 SecurityError
"""
import re

from core.exceptions import SecurityError


# ── 模块级便捷函数（用于 f-string SQL 内联校验）──
# 以下函数都是 validate_identifier 的薄包装，
# 旨在让 driver 层的 f-string SQL 能以最低成本获得注入防护。

# 标识符正则：字母/下划线开头，后跟字母/数字/下划线，长度 ≤ 64
# 全项目唯一标准定义：data_ops / mysql_driver 等模块一律 import 使用，
# 不再各自维护本地副本。
_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]{0,63}$')

# 公共别名，供其他模块 import（_IDENTIFIER_RE 保留以兼容既有引用）
IDENTIFIER_RE = _IDENTIFIER_RE


def is_valid_identifier(name: str) -> bool:
    """判断标识符是否合法（bool 版本，用于条件表达式）

    替代散落各处的 _is_valid_identifier 重复实现。

    Args:
        name: 表名/字段名/索引名等标识符

    Returns:
        True 若合法（字母/下划线开头，只含字母数字下划线，长度 ≤ 64）
    """
    return bool(name) and isinstance(name, str) and bool(_IDENTIFIER_RE.match(name))


def validate_identifier(name: str, kind: str = "标识符") -> str:
    """校验表名/字段名/索引名等标识符合法性（防 SQL 注入）

    Args:
        name: 标识符
        kind: 标识符类型描述（用于错误信息，如"表名"/"字段名"）

    Returns:
        校验通过的标识符（原值）

    Raises:
        SecurityError: 标识符非法
    """
    if not name or not isinstance(name, str):
        raise SecurityError(f"非法{kind}: 标识符为空或非字符串")
    if len(name) > 64:
        raise SecurityError(f"非法{kind}: '{name}' 长度超过 64 字符")
    if not _IDENTIFIER_RE.match(name):
        raise SecurityError(
            f"非法{kind}: '{name}'（必须以字母/下划线开头，只含字母数字下划线）"
        )
    return name


def safe_table_sql(table: str) -> str:
    """校验表名并返回带双引号的 SQL 片段

    用于 f-string SQL 中表名的安全拼接，例如：
        sql = f'SELECT * FROM {safe_table_sql(table)} WHERE id = ?'

    Args:
        table: 表名

    Returns:
        带双引号的 SQL 片段，如 '"users"'

    Raises:
        SecurityError: 表名非法
    """
    validate_identifier(table, "表名")
    return f'"{table}"'


def safe_column_sql(column: str) -> str:
    """校验字段名并返回带双引号的 SQL 片段

    用于 f-string SQL 中字段名的安全拼接，例如：
        sql = f'ALTER TABLE {safe_table_sql(table)} ADD COLUMN {safe_column_sql(column)} TEXT'

    Args:
        column: 字段名

    Returns:
        带双引号的 SQL 片段，如 '"name"'

    Raises:
        SecurityError: 字段名非法
    """
    validate_identifier(column, "字段名")
    return f'"{column}"'


# DEFAULT 允许的时间关键字（SQLite/MySQL 共同的常量默认值）
_DEFAULT_KEYWORDS = frozenset(
    {"NULL", "TRUE", "FALSE", "CURRENT_TIME", "CURRENT_DATE", "CURRENT_TIMESTAMP"})


def safe_default_sql(value) -> str:
    """DEFAULT 字面值安全拼装（此前两驱动直接插值，字符串连引号都不转义）

    规则：数值原样；时间/空值关键字白名单原样；已带单引号的兼容原样；
    其余按字符串字面值处理（单引号 doubling 转义）。
    """
    if value is None:
        return "NULL"
    sv = str(value).strip()
    if re.match(r'^-?\d+(?:\.\d+)?$', sv):
        return sv
    if sv.upper() in _DEFAULT_KEYWORDS:
        return sv.upper()
    if sv.startswith("'") and sv.endswith("'") and len(sv) >= 2:
        inner = sv[1:-1]
        # 已带引号且内部无未转义引号才原样（否则按字面值重新转义，防注入口）
        if "'" not in inner:
            return sv
    return "'" + sv.replace("'", "''") + "'"


# ── SQL 表名提取（契约层读屏蔽与联邦路由共用的唯一实现）──
# 曾在 FederatedDriver 与契约层各写一份，列名大小写绕过、子查询星号泄露两次
# 同步失败（权限矩阵复盘）——20260822 收口于此，只此一份。
# 方括号 [users]、schema 前缀 main.users、AS 别名、
# 大小写错位星号（USERS.*）全部归一——读权限唯一实现不得有形态盲区。
# schema 前缀形态归一提取为本片段（读写两侧共用——
# 写护栏 sql_guard._GUARDS 同一片段）。前缀任意段、引号/方括号/空白任意混排：
# main.users / "main"."users" / [main]. [users] / main . users / `main`.`users` /
# 不对称 "main".users / main."users"——捕获组恒为末段表名。
TABLE_REF_FRAGMENT = r'(?:["`\[]?[\w$]+["`\]]?\s*\.\s*)*["`\[]?([\w$]+)["`\]]?'

_TABLE_REF_RE = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+' + TABLE_REF_FRAGMENT +
    r'(?:\s+(?:AS\s+)?(\w+))?',
    re.IGNORECASE)

_SQL_KEYWORDS = frozenset({"select", "where", "and", "or", "as", "on", "group",
                           "order", "limit", "offset", "having"})
# 子句/连接词：不得被误捕为别名
_CLAUSE_WORDS = frozenset({"left", "right", "inner", "outer", "cross", "join",
                           "union", "where", "on", "group", "order", "limit",
                           "values", "set", "select"}) | _SQL_KEYWORDS


def _strip_sql_comments(sql: str, dialect: str = "sqlite") -> str:
    """剥除 SQL 注释——薄委托 core.sql_lex.strip_comments（
    全仓唯一词法模型；dialect=mysql 时支持 # 注释与 \\' 转义）"""
    from core.sql_lex import strip_comments
    return strip_comments(sql, dialect)


def extract_table_aliases(sql: str) -> dict:
    """{别名或表名（原样）: 规范表名}——形态全归一：
    方括号 [users]、schema 前缀全混排形态（TABLE_REF_FRAGMENT 覆盖，含空白/
    不对称引号）、AS 别名、逗号连接（FROM t1, users）、大小写任意。
    （读屏蔽唯一实现不得有形态盲区）
    """
    sql = _strip_sql_comments(sql)  # 注释先剥（A2：FROM/**/users 曾让提取落空）
    out = {}

    def _add(raw: str, alias: str | None):
        table = raw.strip('`"[]').split(".")[-1]  # schema 前缀取末段
        if not table or table.lower() in _CLAUSE_WORDS:
            return
        out[table] = table
        if alias and alias.lower() not in _CLAUSE_WORDS:
            out[alias] = table

    for m in _TABLE_REF_RE.finditer(sql):
        _add(m.group(1), m.group(2))
        # 逗号连接的续表：FROM t1 a, users u, ...（只在 FROM 语境、遇子句词即停）
        # 续表同样走 TABLE_REF_FRAGMENT（schema 前缀混排形态不打折）
        rest = sql[m.end():]
        while True:
            cm = re.match(r'\s*,\s*' + TABLE_REF_FRAGMENT + r'(?:\s+(?:AS\s+)?(\w+))?', rest)
            if not cm:
                break
            cand = cm.group(1)
            if cand.lower() in _CLAUSE_WORDS:
                break
            _add(cm.group(1), cm.group(2))
            rest = rest[cm.end():]
    return out


def extract_tables_from_sql(sql: str) -> list:
    """从 SQL 语句中提取所有涉及的规范表名（SELECT FROM/JOIN、INSERT INTO、UPDATE、DELETE FROM）"""
    return sorted(set(extract_table_aliases(sql).values()))


def split_top_commas(s: str) -> list:
    """顶层逗号切段：单/双引号字符串内的逗号不切（''/"" doubling 转义正确处理）。

    SET 子句/列清单的安全分词——朴素 split(',') 会被值里的逗号/等号
    误切出幻影列名（"name='a,b=c'"）。
    """
    parts, buf = [], []
    i, n = 0, len(s)
    quote = None  # 当前字符串的引号字符（' 或 "）
    while i < n:
        ch = s[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and s[i + 1] == quote:
                    buf.append(quote)
                    i += 1  # doubling 转义：留在字符串内
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def split_set_pairs(set_clause: str) -> list:
    """SET 子句 → [(列名, 原始值文本)]——全仓唯一的 SET 解析器。

    契约层权限判定与驱动层执行曾各用一条引号模型不同的解析器，
    'name="O''Neil", password_hash=...' 类载荷在契约层吞逗号只见 name 放行、
    驱动层正确解析照写——判定与执行必须同源。三处消费方：契约权限判定、
    sqlite/mysql 驱动执行、sql_guard execute 列级闸。
    """
    pairs = []
    for seg in split_top_commas(set_clause):
        if "=" not in seg:
            continue
        col, _, val = seg.partition("=")
        pairs.append((col.strip().strip('`"[]'), val.strip()))
    return pairs


def decode_sql_literal(raw: str):
    """SET 字面值文本 → Python 值（引号剥离 + doubling 还原；NULL/数值归一）"""
    s = raw.strip()
    if not s:
        return ""
    if s.upper() == "NULL":
        return None
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        inner = s[1:-1]
        # 严格性：内部存在未转义（非 doubling）引号 = 畸形，拒绝而非猜测
        if "'" in inner.replace("''", ""):
            raise SecurityError(f"SET 字面值畸形（未转义的引号）: {raw[:40]!r}")
        return inner.replace("''", "'")
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        inner = s[1:-1]
        if '"' in inner.replace('""', ""):
            raise SecurityError(f"SET 字面值畸形（未转义的引号）: {raw[:40]!r}")
        return inner.replace('""', '"')
    if re.match(r'^-?\d+$', s):
        return int(s)
    if re.match(r'^-?\d+\.\d+$', s):
        return float(s)
    return s


def split_update_set_where(sql: str) -> tuple:
    """UPDATE 语句 → (set_clause, where)——引号感知：字面量内的 WHERE 不切
   （"SET note='含 WHERE 字样'" 曾在裸正则处提前截断，后续列逃出列级闸）"""
    # 表名走 TABLE_REF_FRAGMENT（schema 前缀混排全兼容——
    # 旧单段标识符形态让 UPDATE main.users 的 SET 解析静默失败、列级闸空转）
    m = re.match(r'^\s*UPDATE\s+' + TABLE_REF_FRAGMENT + r'\s+SET\s+', sql, re.I)
    if not m:
        return "", ""
    rest = sql[m.end():]
    quote = None
    i, cut = 0, len(rest)
    while i < len(rest):
        ch = rest[i]
        if quote:
            if ch == quote:
                if i + 1 < len(rest) and rest[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in " \t\r\n" and re.match(r'\s*WHERE\b', rest[i:], re.I):
            cut = i
            break
        i += 1
    where = rest[cut:].strip()
    if where[:5].upper() == "WHERE":
        where = where[5:].strip()
    return rest[:cut].strip(), where


def safe_index_sql(name: str) -> str:
    """校验索引名并返回带双引号的 SQL 片段

    Args:
        name: 索引名

    Returns:
        带双引号的 SQL 片段，如 '"idx_users_id"'

    Raises:
        SecurityError: 索引名非法
    """
    validate_identifier(name, "索引名")
    return f'"{name}"'


def safe_pragma_arg(name: str) -> str:
    """校验标识符并返回裸形式（用于 PRAGMA 语句）

    SQLite PRAGMA 语法不支持引号包裹（部分版本会报错），必须用裸标识符。
    因此此处只做校验，不加引号。

    用法：
        sql = f"PRAGMA table_info({safe_pragma_arg(table)})"

    Args:
        name: 表名/索引名等

    Returns:
        校验通过的裸标识符（无引号）

    Raises:
        SecurityError: 标识符非法
    """
    validate_identifier(name, "PRAGMA参数")
    return name


def safe_savepoint_name(name: str) -> str:
    """校验 SAVEPOINT 名称并返回裸形式

    SAVEPOINT 名称遵循标识符规则，不加引号。

    Args:
        name: savepoint 名称

    Returns:
        校验通过的裸标识符

    Raises:
        SecurityError: 名称非法
    """
    validate_identifier(name, "SAVEPOINT名称")
    return name
