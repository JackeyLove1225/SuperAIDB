"""表结构变更契约——所有 DDL 操作的前置分析

职责：
1. 表结构变更预校验（precheck_update）：差异分析 + 主键保护
2. 执行前断言（assert_can_update）：force=False 时阻断高危变更
3. 删表/删字段前断言：外键引用检查 + 主键保护
4. 加外键前断言：类型一致性 + 数据采样扫描

设计原则：
- 静态方法，无状态
- 不执行 SQL，只做校验和风险分析
- 高危变更抛 RiskError（携带 report），主键操作抛 PrimaryKeyError
"""
from typing import Optional

from .change_analyzer import ChangeAnalyzer, ChangeReport
from .security_contract import safe_table_sql, safe_column_sql, SecurityContract
from .type_contract import TypeContract
from core.exceptions import RiskError, PrimaryKeyError

# 主键标记常见别名——列定义归一化时统一映射为 is_pk
_PK_ALIASES = ("primary_key", "primaryKey", "is_primary", "isPrimary", "isPrimaryKey")


def _get_id_type(columns: list) -> str:
    """从 columns 列表中提取 id 字段的类型

    Args:
        columns: 字段配置列表

    Returns:
        id 字段的类型字符串（大写），不存在则返回空串
    """
    if not columns:
        return ""
    for c in columns:
        if not isinstance(c, dict):
            continue
        if c.get("name", "").lower() == "id":
            return (c.get("type") or "").upper()
    return ""


class SchemaChangeContract:
    """表结构变更契约——所有 DDL 操作的前置分析"""

    # ── 建表列定义归一化与主键校验 ──

    @staticmethod
    def normalize_pk_aliases(columns: list) -> list:
        """列定义归一化：primary_key/primaryKey/is_primary 等主键别名统一映射为 is_pk

        就地修改 columns 列表并返回。别名为真值时置 is_pk=True，别名键本身移除，
        避免下游只认 is_pk/pk 时主键标记被静默忽略。

        Args:
            columns: 字段配置列表

        Returns:
            归一化后的 columns（同一对象）
        """
        for c in columns or []:
            if not isinstance(c, dict):
                continue
            for alias in _PK_ALIASES:
                if alias in c:
                    if c.pop(alias):
                        c["is_pk"] = True
        return columns

    @staticmethod
    def assert_id_pk_declared(columns: list) -> None:
        """校验收紧：存在名为 id 的列但未标记 is_pk → 拒绝（不放行无主键表）

        应在 normalize_pk_aliases 之后调用（别名已归一化为 is_pk）。

        Args:
            columns: 字段配置列表

        Raises:
            PrimaryKeyError: id 列未声明 is_pk
        """
        for c in columns or []:
            if not isinstance(c, dict):
                continue
            if c.get("name", "").lower() == SecurityContract.PRIMARY_KEY_COLUMN \
                    and not (c.get("is_pk") or c.get("pk")):
                raise PrimaryKeyError(
                    "id 列必须声明 is_pk: true（项目硬约束：每张表只有一个默认主键 id）"
                )

    # ── 表结构变更（update_table）──

    @staticmethod
    def precheck_update(
        old_schema: dict,
        new_schema: dict,
        drv=None,
    ) -> ChangeReport:
        """预校验表结构变更（不执行）——供 server.py /precheck 路由调用

        步骤：
        1. 主键存在性校验：new_schema 必须包含 id 字段
        2. 主键类型不可变：id 字段类型变更 → PrimaryKeyError
        3. 差异分析：ChangeAnalyzer.analyze 输出 ChangeReport

        Args:
            old_schema: 旧 schema dict（含 name/columns/foreign_keys）
            new_schema: 新 schema dict
            drv: 可选 Driver 实例（用于数据采样扫描）

        Returns:
            ChangeReport

        Raises:
            PrimaryKeyError: 主键字段被删除或类型被修改
        """
        # 1. 主键存在性
        SecurityContract.assert_primary_key_exists(new_schema.get("columns", []))
        SecurityContract.assert_no_duplicate_primary_key(new_schema.get("columns", []))

        # 2. 主键类型不可变
        old_id_type = _get_id_type(old_schema.get("columns", []))
        new_id_type = _get_id_type(new_schema.get("columns", []))
        if old_id_type and new_id_type and old_id_type != new_id_type:
            raise PrimaryKeyError(
                f"主键 id 字段类型不允许修改（{old_id_type} → {new_id_type}）"
            )

        # 3. 差异分析
        return ChangeAnalyzer.analyze(old_schema, new_schema, drv)

    @staticmethod
    def assert_can_update(
        old_schema: dict,
        new_schema: dict,
        force: bool,
        drv=None,
    ) -> ChangeReport:
        """执行前断言——force=False 时阻断高风险变更

        Args:
            old_schema: 旧 schema
            new_schema: 新 schema
            force: 是否强制执行高危变更
            drv: 可选 Driver 实例

        Returns:
            ChangeReport

        Raises:
            PrimaryKeyError: 主键操作
            RiskError: 高危变更且 force=False
        """
        report = SchemaChangeContract.precheck_update(old_schema, new_schema, drv)
        if report.risk_level == "safe":
            return report
        if report.requires_force and not force:
            raise RiskError(
                f"变更包含高危操作（{report.summary}），需确认后带 force=True 执行",
                report=report.to_dict(),
            )
        return report

    # ── 删表 ──

    @staticmethod
    def assert_can_drop_table(table: str, drv) -> None:
        """删表前断言——检查外键引用

        被其他表外键引用的表不能直接删除，需先解除引用关系。

        Args:
            table: 表名
            drv: Driver 实例

        Raises:
            SecurityError: 表名非法
            RiskError: 表被外键引用
        """
        SecurityContract.validate_identifier(table, "表名")
        try:
            refs = drv.get_referencing_tables(table) or []
        except Exception:
            refs = []
        if refs:
            ref_desc = ", ".join(
                f"{r.get('table', '?')}.{r.get('from_col', '?')}" for r in refs
            )
            raise RiskError(
                f"表 '{table}' 被以下外键引用，无法删除：{ref_desc}。请先解除引用关系",
                report={"referenced_by": refs},
                forceable=False,
            )

    # ── 删字段 ──

    @staticmethod
    def assert_can_drop_column(table: str, column: str, drv) -> None:
        """删字段前断言——主键保护 + 外键引用检查

        Args:
            table: 表名
            column: 字段名
            drv: Driver 实例

        Raises:
            PrimaryKeyError: 字段是主键 id
            SecurityError: 标识符非法
            RiskError: 字段被外键引用
        """
        SecurityContract.validate_identifier(table, "表名")
        SecurityContract.validate_identifier(column, "字段名")
        # 主键保护（绝对规则，不可被 force 绕过）
        SecurityContract.assert_not_primary_key(table, column, "删除")
        # 外键引用检查
        try:
            refs = drv.get_referencing_tables(table) or []
        except Exception:
            refs = []
        for ref in refs:
            if ref.get("from_col", "").lower() == column.lower():
                raise RiskError(
                    f"字段 '{table}.{column}' 被外键引用"
                    f"（{ref.get('table', '?')}.{ref.get('from_col', '?')}），无法删除",
                    report={"referenced_by": [ref]},
                    forceable=False,
                )

    # ── 加外键 ──

    @staticmethod
    def assert_can_add_foreign_key(
        table: str,
        column: str,
        ref_table: str,
        drv,
        ref_column: str = "id",
        force: bool = False,
    ) -> None:
        """加外键前断言——类型一致性 + 数据采样扫描

        检查：
        1. 标识符合法性
        2. 列类型与被引用列类型一致（不一致 → RiskError，需 force）
        3. 现有数据采样扫描：table.column 的值是否都在 ref_table.ref_column 中存在
           失败率高 → RiskError，需 force

        Args:
            table: 本表名
            column: 本表外键列名
            ref_table: 被引用表名
            drv: Driver 实例
            ref_column: 被引用列名（默认 id）
            force: 是否强制执行

        Raises:
            SecurityError: 标识符非法
            RiskError: 类型不一致或数据不满足约束
        """
        SecurityContract.validate_table_and_column(table, column)
        SecurityContract.validate_identifier(ref_table, "被引用表名")
        SecurityContract.validate_identifier(ref_column, "被引用列名")

        # 被引用表必须存在
        if not drv.table_exists(ref_table):
            raise RiskError(
                f"被引用表 '{ref_table}' 不存在，无法创建外键",
                report={"ref_table": ref_table},
                forceable=False,
            )

        # 类型一致性检查
        local_type = ""
        ref_type = ""
        try:
            for c in drv.get_columns(table):
                if c.get("name", "").lower() == column.lower():
                    local_type = (c.get("type") or "").upper()
                    break
        except Exception:
            pass
        try:
            for c in drv.get_columns(ref_table):
                if c.get("name", "").lower() == ref_column.lower():
                    ref_type = (c.get("type") or "").upper()
                    break
        except Exception:
            pass

        if local_type and ref_type:
            local_family = TypeContract.get_type_family(local_type)
            ref_family = TypeContract.get_type_family(ref_type)
            if local_family != ref_family:
                msg = (
                    f"外键列类型不一致：{table}.{column}={local_type}（{local_family}族），"
                    f"{ref_table}.{ref_column}={ref_type}（{ref_family}族）"
                )
                if not force:
                    raise RiskError(msg, report={"type_mismatch": True})
                # force=True 时放行，但仍记录

        # 数据采样扫描：检查现有数据是否满足 FK 约束
        if drv.table_exists(table):
            try:
                # 防御性校验标识符（validate_table_and_column 已在前面调用，此处冗余但安全）
                # 构造安全的 SQL 片段
                scan_sql = (
                    f'SELECT COUNT(*) AS c FROM {safe_table_sql(table)} '
                    f'WHERE {safe_column_sql(column)} IS NOT NULL '
                    f'AND {safe_column_sql(column)} NOT IN '
                    f'(SELECT {safe_column_sql(ref_column)} FROM {safe_table_sql(ref_table)})'
                )
                rows = drv.query(scan_sql)
                orphan_count = rows[0].get("c", 0) if rows else 0
                if orphan_count > 0 and not force:
                    raise RiskError(
                        f"外键约束失败：{table}.{column} 有 {orphan_count} 行数据"
                        f"在 {ref_table}.{ref_column} 中不存在",
                        report={"orphan_count": orphan_count, "type_mismatch": False},
                    )
            except RiskError:
                raise
            except Exception:
                # 扫描失败不阻断（如表或字段不存在），让 driver 自己报错
                pass

    # ── 重命名表 ──

    @staticmethod
    def assert_can_rename_table(table: str, new_name: str, drv) -> None:
        """重命名表前断言

        检查：
        1. 标识符合法性
        2. 新表名不与现有表冲突

        Args:
            table: 旧表名
            new_name: 新表名
            drv: Driver 实例

        Raises:
            SecurityError: 标识符非法或新表名已存在
        """
        SecurityContract.validate_identifier(table, "旧表名")
        SecurityContract.validate_identifier(new_name, "新表名")
        if drv.table_exists(new_name):
            raise RiskError(
                f"表名 '{new_name}' 已存在，无法重命名",
                report={"conflict_table": new_name},
                forceable=False,
            )

    # ── 重命名字段 ──

    @staticmethod
    def assert_can_rename_column(
        table: str,
        old_column: str,
        new_column: str,
        drv,
    ) -> None:
        """重命名字段前断言

        检查：
        1. 主键保护：id 字段不允许重命名
        2. 标识符合法性
        3. 新字段名不与现有字段冲突

        Args:
            table: 表名
            old_column: 旧字段名
            new_column: 新字段名
            drv: Driver 实例

        Raises:
            PrimaryKeyError: 字段是主键 id
            SecurityError: 标识符非法或新字段名已存在
        """
        SecurityContract.validate_identifier(table, "表名")
        SecurityContract.validate_identifier(old_column, "旧字段名")
        SecurityContract.validate_identifier(new_column, "新字段名")
        # 主键保护
        SecurityContract.assert_not_primary_key(table, old_column, "重命名")
        # 新字段名冲突检查
        try:
            existing_cols = {
                c.get("name", "").lower() for c in drv.get_columns(table)
            }
        except Exception:
            existing_cols = set()
        if new_column.lower() in existing_cols:
            raise RiskError(
                f"字段名 '{new_column}' 在表 '{table}' 中已存在",
                report={"conflict_column": new_column},
                forceable=False,
            )