"""安全契约——所有 driver 操作的前置安全校验

职责：
1. 标识符合法性校验（防 SQL 注入）
2. WHERE 子句安全校验（防 SQL 注入）
3. 主键保护（id 字段禁止修改/删除/重命名）
4. CHECK 表达式安全校验（委托给 core.checks）

设计原则：
- 纯静态方法，无状态，便于并发和测试
- 所有方法要么返回校验结果，要么抛 SecurityError/PrimaryKeyError
- 不直接执行 SQL，只做校验

模块级便捷函数（safe_*_sql、表名提取、SET 解析等）已下沉到中立模块
core.sql_safe（驱动层直接消费，拆除 drivers→contract 依赖边）；
此处显式再导出，既有 import 路径保持不变。
"""
from typing import Optional

from core.checks import validate_check_expr as _checks_validate_check_expr
from core.checks import validate_where as _checks_validate_where
from core.exceptions import PrimaryKeyError, SecurityError

# ── 兼容再导出：以下名称的真实定义在 core.sql_safe，此 import 路径保持不变 ──
from core.sql_safe import (
    _IDENTIFIER_RE,  # SecurityContract._IDENTIFIER_RE 类属性用
    IDENTIFIER_RE,
    is_valid_identifier,
    validate_identifier,
    safe_table_sql,
    safe_column_sql,
    safe_default_sql,
    TABLE_REF_FRAGMENT,
    extract_table_aliases,
    extract_tables_from_sql,
    split_top_commas,
    split_set_pairs,
    decode_sql_literal,
    split_update_set_where,
    safe_index_sql,
    safe_pragma_arg,
    safe_savepoint_name,
)

__all__ = [  # 再导出清单（pyflakes 据此认定 import 被使用；兼容面机器可见）
    "IDENTIFIER_RE", "is_valid_identifier", "validate_identifier",
    "safe_table_sql", "safe_column_sql", "safe_default_sql",
    "TABLE_REF_FRAGMENT", "extract_table_aliases", "extract_tables_from_sql",
    "split_top_commas", "split_set_pairs", "decode_sql_literal",
    "split_update_set_where", "safe_index_sql", "safe_pragma_arg",
    "safe_savepoint_name", "SecurityContract", "ids_in_clause",
]


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

        复用 core.checks.validate_where 的正则逻辑，
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
        # 复用 checks 的正则校验
        if not _checks_validate_where(where):
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
        """CHECK 表达式安全校验（委托给 core.checks）

        Args:
            expr: CHECK 表达式字符串
            col_name: 当前字段名
            col_type: 当前字段类型
            table_columns: 同表所有列名列表

        Returns:
            (ok: bool, message: str)
        """
        return _checks_validate_check_expr(expr, col_name, col_type, table_columns)

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
    收口为唯一实现）。列名过标识符校验。
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
