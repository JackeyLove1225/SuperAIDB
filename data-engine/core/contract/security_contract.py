"""安全契约——所有 driver 操作的前置安全校验

职责：
1. 标识符合法性校验（防 SQL 注入）
2. WHERE 子句安全校验（防 SQL 注入）
3. 主键保护（id 字段禁止修改/删除/重命名）
4. CHECK 表达式安全校验（委托给 core.drivers.checks）

设计原则：
- 纯静态方法，无状态，便于并发和测试
- 所有方法要么返回校验结果，要么抛 SecurityError/PrimaryKeyError
- 不直接执行 SQL，只做校验
"""
import re
from typing import Optional

from core.exceptions import PrimaryKeyError, SecurityError


# ── 模块级便捷函数（用于 f-string SQL 内联校验，T1.1 新增）──
# 以下函数都是 SecurityContract.validate_identifier 的薄包装，
# 旨在让 driver 层的 f-string SQL 能以最低成本获得注入防护。

# 标识符正则：字母/下划线开头，后跟字母/数字/下划线，长度 ≤ 64
# 全项目唯一标准定义（T1.3 收敛）：data_ops / mysql_driver 等模块一律 import 使用，
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
    SecurityContract.validate_identifier(table, "表名")
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
    SecurityContract.validate_identifier(column, "字段名")
    return f'"{column}"'


# DEFAULT 允许的时间关键字（SQLite/MySQL 共同的常量默认值）
_DEFAULT_KEYWORDS = frozenset(
    {"NULL", "TRUE", "FALSE", "CURRENT_TIME", "CURRENT_DATE", "CURRENT_TIMESTAMP"})


def safe_default_sql(value) -> str:
    """DEFAULT 字面值安全拼装（此前两驱动直接插值，字符串连引号都不转义——评审 L3）

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
# 评审四轮 H-3 加固：方括号 [users]、schema 前缀 main.users、AS 别名、
# 大小写错位星号（USERS.*）全部归一——读权限唯一实现不得有形态盲区。
_TABLE_REF_RE = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+["`\[]?([\w.]+)["`\]]?'
    r'(?:\s+(?:AS\s+)?(\w+))?',
    re.IGNORECASE)

_SQL_KEYWORDS = frozenset({"select", "where", "and", "or", "as", "on", "group",
                           "order", "limit", "offset", "having"})
# 子句/连接词：不得被误捕为别名
_CLAUSE_WORDS = frozenset({"left", "right", "inner", "outer", "cross", "join",
                           "union", "where", "on", "group", "order", "limit",
                           "values", "set", "select"}) | _SQL_KEYWORDS


def extract_table_aliases(sql: str) -> dict:
    """{别名或表名（原样）: 规范表名}——形态全归一：
    方括号 [users]、schema 前缀 main.users、引号/方括号包裹前缀（"main"."users"、
    [main].[users]）、AS 别名、逗号连接（FROM t1, users）、大小写任意。
    （评审五轮安全 H-3 续：读屏蔽唯一实现不得有形态盲区）
    """
    # 前置归一：引号包裹的 schema 连接点拍平（"main"."users"/[main].[users]→main.users）
    norm = re.sub(r'(["`\]])\s*\.\s*(["`\[])', '.', sql)
    out = {}

    def _add(raw: str, alias: str | None):
        table = raw.strip('`"[]').split(".")[-1]  # schema 前缀取末段
        if not table or table.lower() in _CLAUSE_WORDS:
            return
        out[table] = table
        if alias and alias.lower() not in _CLAUSE_WORDS:
            out[alias] = table

    for m in _TABLE_REF_RE.finditer(norm):
        _add(m.group(1), m.group(2))
        # 逗号连接的续表：FROM t1 a, users u, ...（只在 FROM 语境、遇子句词即停）
        rest = norm[m.end():]
        while True:
            cm = re.match(r'\s*,\s*[`"\[]?([\w.]+)["`\]]?(?:\s+(?:AS\s+)?(\w+))?', rest)
            if not cm:
                break
            cand = cm.group(1).strip('`"[]').split(".")[-1]
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

    评审四轮 S-2：契约层权限判定与驱动层执行曾各用一条引号模型不同的解析器，
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
    m = re.match(r'^\s*UPDATE\s+[`"\[]?\w+[`"\]]?\s+SET\s+', sql, re.I)
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
    SecurityContract.validate_identifier(name, "索引名")
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
    SecurityContract.validate_identifier(name, "PRAGMA参数")
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
    SecurityContract.validate_identifier(name, "SAVEPOINT名称")
    return name


class SecurityContract:
    """安全契约——所有 driver 操作的前置安全校验"""

    # 主键字段名（项目硬约束：每张表都有且只有一个默认的主键 id）
    PRIMARY_KEY_COLUMN = "id"

    # 标识符正则：字母/下划线开头，后跟字母/数字/下划线，长度 ≤ 64
    # （模块级也有一份 _IDENTIFIER_RE，供便捷函数使用）
    _IDENTIFIER_RE = _IDENTIFIER_RE

    # ── 标识符校验 ──

    @staticmethod
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
        if not SecurityContract._IDENTIFIER_RE.match(name):
            raise SecurityError(
                f"非法{kind}: '{name}'（必须以字母/下划线开头，只含字母数字下划线）"
            )
        return name

    # ── WHERE 子句校验 ──

    @staticmethod
    def validate_where(where: str) -> str:
        """WHERE 子句安全校验（防 SQL 注入）

        复用 core.drivers.checks.validate_where 的正则逻辑，
        但抛 SecurityError 而非返回 bool。

        Args:
            where: WHERE 子句字符串

        Returns:
            校验通过的 WHERE 子句

        Raises:
            SecurityError: WHERE 子句不安全
        """
        if not where or not where.strip():
            raise SecurityError("WHERE 子句为空")
        # 复用现有 checks.py 的正则校验
        from core.drivers.checks import validate_where as _check_where
        if not _check_where(where):
            raise SecurityError(
                f"WHERE 子句不安全: '{where[:50]}'（格式不合法或含注入风险）"
            )
        # 额外检查：禁止分号、注释（双重防线）
        w = where.strip()
        if ";" in w:
            raise SecurityError("WHERE 子句禁止包含分号（防止多语句注入）")
        if "--" in w or "/*" in w or "*/" in w:
            raise SecurityError("WHERE 子句禁止包含注释符")
        return where

    # ── 主键保护 ──

    @staticmethod
    def assert_not_primary_key(table: str, column: str, operation: str = "修改") -> None:
        """主键保护——禁止对 id 字段做破坏性操作

        项目硬约束：每张表都有且只有一个默认的主键 id。
        此检查不可被 force=True 绕过。

        Args:
            table: 表名
            column: 字段名
            operation: 操作描述（用于错误信息，如"修改类型"/"删除"/"重命名"）

        Raises:
            PrimaryKeyError: 字段是主键 id
        """
        if column and column.lower() == SecurityContract.PRIMARY_KEY_COLUMN:
            raise PrimaryKeyError(
                f"主键字段 'id' 不允许{operation}（项目硬约束：每张表只有一个默认主键 id）"
            )

    @staticmethod
    def assert_primary_key_exists(columns: list[dict]) -> None:
        """校验 columns 列表必须包含 id 主键字段

        用于建表/改表时确保 id 主键存在。

        Args:
            columns: 字段配置列表，每个 dict 含 name 字段

        Raises:
            PrimaryKeyError: 列表不含 id 字段
        """
        if not columns:
            raise PrimaryKeyError("字段列表为空，必须包含 id 主键字段")
        if not any(
            isinstance(c, dict) and c.get("name", "").lower() == SecurityContract.PRIMARY_KEY_COLUMN
            for c in columns
        ):
            raise PrimaryKeyError(
                "表必须包含 id 主键字段（项目硬约束：每张表只有一个默认主键 id）"
            )

    @staticmethod
    def assert_no_duplicate_primary_key(columns: list[dict]) -> None:
        """校验 columns 列表不能有多个主键标记

        项目硬约束：不允许联合主键，id 是唯一主键。

        Args:
            columns: 字段配置列表

        Raises:
            PrimaryKeyError: 存在非 id 字段被标记为主键，或多个主键
        """
        pk_count = 0
        for c in columns:
            if not isinstance(c, dict):
                continue
            if c.get("is_pk") or c.get("pk"):
                pk_count += 1
                if c.get("name", "").lower() != SecurityContract.PRIMARY_KEY_COLUMN:
                    raise PrimaryKeyError(
                        f"字段 '{c.get('name', '?')}' 被标记为主键，但项目硬约束只允许 id 作为主键"
                    )
        if pk_count > 1:
            raise PrimaryKeyError(
                f"检测到 {pk_count} 个主键标记，项目硬约束不允许联合主键"
            )

    # ── CHECK 表达式校验 ──

    @staticmethod
    def validate_check_expr(
        expr: str,
        col_name: str = "",
        col_type: str = "",
        table_columns: Optional[list] = None,
    ) -> tuple:
        """CHECK 表达式安全校验（委托给 core.drivers.checks）

        Args:
            expr: CHECK 表达式字符串
            col_name: 当前字段名
            col_type: 当前字段类型
            table_columns: 同表所有列名列表

        Returns:
            (ok: bool, message: str)
        """
        from core.drivers.checks import validate_check_expr as _check
        return _check(expr, col_name, col_type, table_columns)

    # ── 表名/字段名联合校验 ──

    @staticmethod
    def validate_table_and_column(table: str, column: str = None) -> tuple:
        """联合校验表名和字段名

        Args:
            table: 表名
            column: 字段名（可选）

        Returns:
            (table, column) 校验通过的值

        Raises:
            SecurityError: 标识符非法
        """
        table = SecurityContract.validate_identifier(table, "表名")
        if column is not None:
            column = SecurityContract.validate_identifier(column, "字段名")
        return table, column

def ids_in_clause(ids, column: str = "id") -> str:
    """选择集 id 集 → 'id IN (...)' 子句——类型驱动字面量拼装。

    整型/浮点裸拼，其余类型单引号 + doubling 转义（文本主键表的脏 id
    不会变成注入文本；此前三处调用方两种防御标准——str 直拼与 int 强转——
    评审四轮收口为唯一实现）。列名过标识符校验。
    """
    def _lit(v):
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            return repr(v)
        return "'" + str(v).replace("'", "''") + "'"
    SecurityContract.validate_identifier(column, "主键列名")
    return f"{column} IN (%s)" % ",".join(_lit(i) for i in ids)
