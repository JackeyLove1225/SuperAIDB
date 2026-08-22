"""类型变更契约——字段类型变更的风险评估与数据兼容性扫描

职责：
1. 类型族判断（int/float/text/date/datetime/blob/bool/other）
2. 类型变更风险评级（safe/warning/danger）
3. 数据兼容性采样扫描（查询前 N 行，尝试转换，统计成功率）

设计原则：
- 纯静态方法，无状态
- 风险评级基于类型族 + 转换方向，不依赖 LLM
- 数据扫描采样而非全表，避免大表性能问题
"""
from dataclasses import dataclass, field
from typing import Literal, Optional

from .security_contract import safe_table_sql, safe_column_sql, SecurityContract


RiskLevel = Literal["safe", "warning", "danger"]


@dataclass
class TypeChangeRisk:
    """类型变更风险评估结果"""
    level: RiskLevel                # safe/warning/danger
    message: str                    # 中文风险描述（空串=无风险）
    requires_force: bool            # 是否需要 force=True 才能执行
    requires_data_scan: bool        # 是否需要数据采样扫描
    old_family: str = ""            # 旧类型族
    new_family: str = ""            # 新类型族


class TypeContract:
    """类型变更契约——所有字段类型变更必须经过"""

    # 类型族定义（增强自 checks.py 的 family 函数）
    INT_FAMILY = {"INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT", "SERIAL"}
    FLOAT_FAMILY = {"FLOAT", "REAL", "DOUBLE", "NUMERIC", "DECIMAL"}
    TEXT_FAMILY = {"TEXT", "VARCHAR", "CHAR", "CLOB"}
    DATE_FAMILY = {"DATE"}
    DATETIME_FAMILY = {"DATETIME", "TIMESTAMP"}
    BLOB_FAMILY = {"BLOB"}
    BOOL_FAMILY = {"BOOLEAN", "BOOL"}

    # 数值类型无效值（从 sqlite_driver 迁移，共用）
    INVALID_NUMERIC = {"", "—", "－", "-", "─", "无", "N/A", "n/a", "null", "NULL", "None", "/"}

    # ── 类型族判断 ──

    @staticmethod
    def get_type_family(col_type: str) -> str:
        """返回类型所属族

        Args:
            col_type: 类型字符串（如 "INTEGER"/"VARCHAR(255)"/"DECIMAL(10,2)"）

        Returns:
            族名：int/float/text/date/datetime/blob/bool/other
        """
        if not col_type:
            return "other"
        # 提取基础类型名（去掉精度部分）：DECIMAL(10,2) → DECIMAL
        base = col_type.upper().split("(")[0].strip().split()[0]
        if base in TypeContract.INT_FAMILY:
            return "int"
        if base in TypeContract.FLOAT_FAMILY:
            return "float"
        if base in TypeContract.TEXT_FAMILY:
            return "text"
        if base in TypeContract.DATE_FAMILY:
            return "date"
        if base in TypeContract.DATETIME_FAMILY:
            return "datetime"
        if base in TypeContract.BLOB_FAMILY:
            return "blob"
        if base in TypeContract.BOOL_FAMILY:
            return "bool"
        return "other"

    @staticmethod
    def is_numeric_type(col_type: str) -> bool:
        """判断是否为数值类型（int/float/bool 族）"""
        family = TypeContract.get_type_family(col_type)
        return family in ("int", "float", "bool")

    # ── 风险评级 ──

    @staticmethod
    def classify_change_risk(old_type: str, new_type: str) -> TypeChangeRisk:
        """评估类型变更风险等级

        规则矩阵：
        - 同族 → safe（无损）
        - INT→FLOAT → safe（无损扩展）
        - FLOAT→INT → warning（丢小数部分）
        - TEXT→INT/FLOAT → danger（非数字文本会丢数据）
        - INT/FLOAT→TEXT → safe（无损，但提示上层查询可能需调整）
        - DATE→DATETIME → safe（无损扩展）
        - DATETIME→DATE → warning（丢时间部分）
        - TEXT↔BLOB → safe（SQLite 不区分，MySQL 兼容）
        - BOOL↔INT → safe（无损）
        - 其他跨族 → danger（差异大）

        Args:
            old_type: 旧类型
            new_type: 新类型

        Returns:
            TypeChangeRisk 评估结果
        """
        if not old_type or not new_type:
            return TypeChangeRisk(
                level="safe", message="", requires_force=False, requires_data_scan=False
            )

        old_family = TypeContract.get_type_family(old_type)
        new_family = TypeContract.get_type_family(new_type)

        # 同族：安全
        if old_family == new_family:
            return TypeChangeRisk(
                level="safe", message="",
                requires_force=False, requires_data_scan=False,
                old_family=old_family, new_family=new_family,
            )

        # INT → FLOAT：无损扩展
        if old_family == "int" and new_family == "float":
            return TypeChangeRisk(
                level="safe", message="",
                requires_force=False, requires_data_scan=False,
                old_family=old_family, new_family=new_family,
            )

        # FLOAT → INT：丢小数
        if old_family == "float" and new_family == "int":
            return TypeChangeRisk(
                level="warning",
                message=f"将 {old_type} 改为 {new_type} 可能导致小数部分丢失",
                requires_force=True, requires_data_scan=True,
                old_family=old_family, new_family=new_family,
            )

        # TEXT → INT/FLOAT：非数字文本会丢
        if old_family == "text" and new_family in ("int", "float"):
            return TypeChangeRisk(
                level="danger",
                message=f"将 {old_type} 改为 {new_type} 可能导致非数字文本数据丢失",
                requires_force=True, requires_data_scan=True,
                old_family=old_family, new_family=new_family,
            )

        # INT/FLOAT → TEXT：无损（数值转字符串）
        if old_family in ("int", "float") and new_family == "text":
            return TypeChangeRisk(
                level="safe",
                message="",  # 数据不丢失，但提示上层查询可能需调整
                requires_force=False, requires_data_scan=False,
                old_family=old_family, new_family=new_family,
            )

        # DATE → DATETIME：无损扩展
        if old_family == "date" and new_family == "datetime":
            return TypeChangeRisk(
                level="safe", message="",
                requires_force=False, requires_data_scan=False,
                old_family=old_family, new_family=new_family,
            )

        # DATETIME → DATE：丢时间部分
        if old_family == "datetime" and new_family == "date":
            return TypeChangeRisk(
                level="warning",
                message=f"将 {old_type} 改为 {new_type} 可能丢失时间部分",
                requires_force=True, requires_data_scan=True,
                old_family=old_family, new_family=new_family,
            )

        # TEXT ↔ BLOB：兼容
        if {old_family, new_family} == {"text", "blob"}:
            return TypeChangeRisk(
                level="safe", message="",
                requires_force=False, requires_data_scan=False,
                old_family=old_family, new_family=new_family,
            )

        # BOOL ↔ INT：兼容
        if {old_family, new_family} == {"bool", "int"}:
            return TypeChangeRisk(
                level="safe", message="",
                requires_force=False, requires_data_scan=False,
                old_family=old_family, new_family=new_family,
            )

        # 其他跨族：危险
        return TypeChangeRisk(
            level="danger",
            message=f"将 {old_type} 改为 {new_type} 类型差异较大，可能导致数据丢失或转换失败",
            requires_force=True, requires_data_scan=True,
            old_family=old_family, new_family=new_family,
        )

    # ── 数据兼容性采样扫描 ──

    @staticmethod
    def validate_data_compatibility(
        drv,
        table: str,
        column: str,
        new_type: str,
        sample_size: int = 100,
    ) -> dict:
        """采样数据预扫描——查询前 N 行非空值，尝试转换，统计成功率

        Args:
            drv: Driver 实例（用于查询数据）
            table: 表名
            column: 字段名
            new_type: 目标类型
            sample_size: 采样行数（默认 100）

        Returns:
            {
                ok_rate: float,           # 0.0-1.0 成功率
                fail_count: int,          # 失败行数
                fail_samples: list,       # 失败样本（最多 5 个）
                scanned: int,             # 实际扫描行数
                timeout: bool,            # 是否超时（预留，当前不实现）
            }
        """
        result = {
            "ok_rate": 1.0, "fail_count": 0, "fail_samples": [],
            "scanned": 0, "timeout": False,
        }

        new_family = TypeContract.get_type_family(new_type)

        # 数值类型才需要扫描（文本/日期类型转换一般不会失败）
        if new_family not in ("int", "float", "bool"):
            return result

        try:
            # 防御性校验标识符
            SecurityContract.validate_identifier(table, "表名")
            SecurityContract.validate_identifier(column, "字段名")
            # 查询前 N 行非空值
            sql = f'SELECT {safe_column_sql(column)} FROM {safe_table_sql(table)} WHERE {safe_column_sql(column)} IS NOT NULL LIMIT {int(sample_size)}'
            rows = drv.query(sql)
            result["scanned"] = len(rows)

            if not rows:
                return result

            fail_count = 0
            fail_samples = []

            for row in rows:
                val = row.get(column)
                if val is None:
                    continue
                ok = TypeContract._try_convert(val, new_family)
                if not ok:
                    fail_count += 1
                    if len(fail_samples) < 5:
                        fail_samples.append(str(val)[:50])

            result["fail_count"] = fail_count
            result["fail_samples"] = fail_samples
            result["ok_rate"] = 1.0 - (fail_count / len(rows)) if rows else 1.0

        except Exception:
            # 扫描失败（如表不存在/字段不存在）：不阻断，只记录
            result["timeout"] = True

        return result

    @staticmethod
    def _try_convert(value, target_family: str) -> bool:
        """尝试将值转换到目标类型族，返回是否成功"""
        try:
            if target_family == "int":
                # 清洗后再尝试
                if isinstance(value, str):
                    cleaned = value.strip()
                    if cleaned in TypeContract.INVALID_NUMERIC:
                        return True  # 无效值视为 None，转换成功
                    int(cleaned)
                else:
                    int(value)
                return True
            if target_family == "float":
                if isinstance(value, str):
                    cleaned = value.strip()
                    if cleaned in TypeContract.INVALID_NUMERIC:
                        return True
                    float(cleaned)
                else:
                    float(value)
                return True
            if target_family == "bool":
                # BOOL 转换较宽松
                return True
        except (ValueError, TypeError):
            return False
        return True

    # ── 数值清洗（共用工具，从 sqlite_driver 提取）──

    @staticmethod
    def clean_numeric_value(v):
        """数值类型清洗：空字符串/破折号等无效值转为 None，否则返回原值

        全角数字先做 NFKC 归一（１２３．４５ → 123.45）：中文文档/聊天粘贴
        的数字常是全角，不归一会被类型校验误杀（管线映射层同口径）。
        """
        if isinstance(v, str):
            import unicodedata
            nv = unicodedata.normalize("NFKC", v).strip()
            if nv in TypeContract.INVALID_NUMERIC:
                return None
            return nv
        return v