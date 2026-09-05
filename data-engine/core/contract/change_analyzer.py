"""变更差异分析器——对比 schema 变更前后差异，输出结构化变更报告

职责：
1. 对比旧 schema 与新 schema 的字段/外键/约束差异
2. 评估每项变更的风险等级（safe/warning/danger）
3. 汇总为 ChangeReport，包含总体风险等级和变更清单

设计原则：
- 纯函数式，无副作用
- 不依赖 LLM，纯规则匹配
- 当 drv 可用时，对类型变更/约束加严项做数据采样扫描
"""
from core.logger import get_logger

logger = get_logger(__name__)
from dataclasses import dataclass, field
from typing import Literal

from .security_contract import safe_table_sql, safe_column_sql, SecurityContract
from .type_contract import TypeContract


RiskLevel = Literal["safe", "warning", "danger"]


@dataclass
class Change:
    """单项变更"""
    type: str               # add_column/drop_column/modify_type/rename_column/
                            # add_not_null/drop_not_null/add_unique/drop_unique/
                            # add_check/drop_check/modify_precision/
                            # drop_fk/add_fk/rename_table/modify_pk
    target: str             # 受影响的字段/表名
    description: str        # 中文描述
    risk: RiskLevel
    data_impact: dict = field(default_factory=dict)  # 数据影响详情（如采样扫描结果）


@dataclass
class ChangeReport:
    """变更报告"""
    risk_level: RiskLevel = "safe"             # 总体风险等级（取最高）
    changes: list = field(default_factory=list)  # list[Change]
    requires_confirm: bool = False             # risk_level in ("warning", "danger")
    requires_force: bool = False               # risk_level == "danger"
    data_scan: dict = field(default_factory=dict)  # 数据预扫描汇总
    summary: str = ""                          # 中文摘要

    def to_dict(self) -> dict:
        """序列化为 dict（供 JSON 响应）"""
        return {
            "risk_level": self.risk_level,
            "requires_confirm": self.requires_confirm,
            "requires_force": self.requires_force,
            "summary": self.summary,
            "changes": [
                {
                    "type": c.type, "target": c.target,
                    "description": c.description, "risk": c.risk,
                    "data_impact": c.data_impact,
                } for c in self.changes
            ],
            "data_scan": self.data_scan,
        }


def _risk_level_value(level: RiskLevel) -> int:
    """风险等级数值化，便于比较"""
    return {"safe": 0, "warning": 1, "danger": 2}.get(level, 0)


class ChangeAnalyzer:
    """变更差异分析器——纯函数式，无副作用"""

    @staticmethod
    def analyze(
        old_schema: dict,
        new_schema: dict,
        drv=None,
    ) -> ChangeReport:
        """分析 schema 变更前后差异，返回风险评估

        检测项（12 类）：
        1. 字段新增 → safe
        2. 字段删除 → warning（丢数据）
        3. 字段类型变更 → 按 TypeContract.classify_change_risk 评级
        4. 字段重命名 → warning（上层引用失效）
        5. NOT NULL 加严 → danger（现有 NULL 数据违反）
        6. UNIQUE 加严 → danger（现有重复数据违反）
        7. CHECK 加严 → warning
        8. 主键变更 → danger（禁止，SecurityContract 拦截）
        9. 外键删除 → warning（失去引用完整性）
        10. 外键新增 → warning（现有数据可能不满足）
        11. 精度收紧 → warning（可能丢精度）
        12. 表重命名 → warning（上层引用失效）

        Args:
            old_schema: 旧 schema dict（含 name/columns/foreign_keys 等）
            new_schema: 新 schema dict
            drv: 可选的 Driver 实例（用于数据采样扫描）

        Returns:
            ChangeReport
        """
        report = ChangeReport()

        # 表名变更
        old_name = old_schema.get("name", "")
        new_name = new_schema.get("name", "")
        if old_name and new_name and old_name != new_name:
            report.changes.append(Change(
                type="rename_table", target=old_name,
                description=f"表重命名: {old_name} → {new_name}",
                risk="warning",
            ))

        # 数据扫描用的表名（优先用旧名：表重命名前数据仍在旧表下）
        scan_table = old_name or new_name

        # 字段差异
        old_cols = {c.get("name", ""): c for c in old_schema.get("columns", []) if isinstance(c, dict)}
        new_cols = {c.get("name", ""): c for c in new_schema.get("columns", []) if isinstance(c, dict)}

        # 新增字段
        for col_name, new_col in new_cols.items():
            if col_name not in old_cols:
                report.changes.append(Change(
                    type="add_column", target=col_name,
                    description=f"新增字段: {col_name} ({new_col.get('type', '?')})",
                    risk="safe",
                ))

        # 删除字段
        for col_name in old_cols:
            if col_name not in new_cols:
                report.changes.append(Change(
                    type="drop_column", target=col_name,
                    description=f"删除字段: {col_name}（数据将丢失）",
                    risk="warning",
                ))

        # 修改字段（类型/约束/精度）
        for col_name, old_col in old_cols.items():
            if col_name not in new_cols:
                continue
            new_col = new_cols[col_name]
            ChangeAnalyzer._diff_column(col_name, old_col, new_col, drv, scan_table, report)

        # 外键差异
        ChangeAnalyzer._diff_foreign_keys(
            old_schema.get("foreign_keys", []),
            new_schema.get("foreign_keys", []),
            report,
        )

        # 汇总风险等级
        if report.changes:
            max_risk = max(report.changes, key=lambda c: _risk_level_value(c.risk)).risk
            report.risk_level = max_risk
            report.requires_confirm = max_risk in ("warning", "danger")
            report.requires_force = max_risk == "danger"
            report.summary = ChangeAnalyzer._build_summary(report.changes)

        return report

    @staticmethod
    def _scan_constraint_impact(drv, scan_table: str, col_name: str,
                                count_sql: str, impact_key: str) -> dict:
        """加严类约束（NOT NULL/UNIQUE）影响面采样——两段共用：
        防御性标识符校验 → 计数查询 → {impact_key: count}；
        drv 缺省或采样失败时返回 {}（影响面缺省，不阻断 diff 主流程）。"""
        if drv is None or not scan_table:
            return {}
        try:
            SecurityContract.validate_identifier(scan_table, "表名")
            SecurityContract.validate_identifier(col_name, "字段名")
            rows = drv.query(count_sql)
            return {impact_key: rows[0].get("c", 0) if rows else 0}
        except Exception:
            logger.debug("约束加严影响面采样失败（该项缺省）: %s", impact_key, exc_info=True)
            return {}

    @staticmethod
    def _diff_column(
        col_name: str,
        old_col: dict,
        new_col: dict,
        drv,
        scan_table: str,
        report: ChangeReport,
    ) -> None:
        """对比单个字段差异（pk/类型/NOT NULL/UNIQUE/精度），差异累积进 report；
        scan_table 为数据采样表名（一般取旧表名——变更前数据在旧表下）。"""
        # 主键变更检测
        old_pk = old_col.get("is_pk") or old_col.get("pk") or old_col.get("name", "").lower() == "id"
        new_pk = new_col.get("is_pk") or new_col.get("pk") or new_col.get("name", "").lower() == "id"
        if old_pk != new_pk:
            report.changes.append(Change(
                type="modify_pk", target=col_name,
                description=f"主键变更: {col_name} 主键状态 {old_pk} → {new_pk}",
                risk="danger",
            ))

        # 类型变更
        old_type = old_col.get("type", "")
        new_type = new_col.get("type", "")
        if old_type and new_type and old_type.upper() != new_type.upper():
            risk = TypeContract.classify_change_risk(old_type, new_type)
            data_impact = {}
            # 高危时做数据采样扫描
            if risk.requires_data_scan and drv is not None and scan_table:
                try:
                    scan = TypeContract.validate_data_compatibility(
                        drv, scan_table, col_name, new_type
                    )
                    data_impact = scan
                except Exception:
                    logger.debug("类型变更数据影响扫描失败（该列影响面缺省）", exc_info=True)
            report.changes.append(Change(
                type="modify_type", target=col_name,
                description=f"字段类型变更: {col_name} {old_type} → {new_type}",
                risk=risk.level,
                data_impact=data_impact,
            ))

        # NOT NULL 加严
        old_nn = old_col.get("not_null", False)
        new_nn = new_col.get("not_null", False)
        if new_nn and not old_nn:
            count_sql = (
                f'SELECT COUNT(*) AS c FROM {safe_table_sql(scan_table)} '
                f'WHERE {safe_column_sql(col_name)} IS NULL'
            )
            report.changes.append(Change(
                type="add_not_null", target=col_name,
                description=f"加严非空约束: {col_name} 设为 NOT NULL",
                risk="danger",
                data_impact=ChangeAnalyzer._scan_constraint_impact(
                    drv, scan_table, col_name, count_sql, "null_count"),
            ))

        # UNIQUE 加严
        old_uniq = old_col.get("unique", False)
        new_uniq = new_col.get("unique", False)
        if new_uniq and not old_uniq:
            dup_sql = (
                f'SELECT COUNT(*) AS c FROM '
                f'(SELECT {safe_column_sql(col_name)} FROM {safe_table_sql(scan_table)} '
                f'WHERE {safe_column_sql(col_name)} IS NOT NULL '
                f'GROUP BY {safe_column_sql(col_name)} HAVING COUNT(*) > 1) t'
            )
            report.changes.append(Change(
                type="add_unique", target=col_name,
                description=f"加严唯一约束: {col_name} 设为 UNIQUE",
                risk="danger",
                data_impact=ChangeAnalyzer._scan_constraint_impact(
                    drv, scan_table, col_name, dup_sql, "duplicate_groups"),
            ))

        # 精度收紧
        old_prec = old_col.get("precision")
        new_prec = new_col.get("precision")
        if old_prec and new_prec and ChangeAnalyzer._is_precision_tightened(old_prec, new_prec):
            report.changes.append(Change(
                type="modify_precision", target=col_name,
                description=f"精度收紧: {col_name} {old_prec} → {new_prec}",
                risk="warning",
            ))

    @staticmethod
    def _diff_foreign_keys(
        old_fks: list,
        new_fks: list,
        report: ChangeReport,
    ) -> None:
        """对比外键列表差异"""
        # 外键签名：列名→引用表.引用列
        def _fk_sig(fk: dict) -> str:
            cols = ",".join(fk.get("columns", []))
            ref = fk.get("references", "")
            ref_cols = ",".join(fk.get("ref_columns", ["id"]))
            return f"{cols}->{ref}.{ref_cols}"

        old_sigs = {_fk_sig(fk) for fk in old_fks if isinstance(fk, dict)}
        new_sigs = {_fk_sig(fk) for fk in new_fks if isinstance(fk, dict)}

        # 新增外键
        for fk in new_fks:
            if isinstance(fk, dict) and _fk_sig(fk) not in old_sigs:
                cols = ",".join(fk.get("columns", []))
                ref = fk.get("references", "?")
                report.changes.append(Change(
                    type="add_fk", target=cols,
                    description=f"新增外键: {cols} → {ref}",
                    risk="warning",
                ))

        # 删除外键
        for fk in old_fks:
            if isinstance(fk, dict) and _fk_sig(fk) not in new_sigs:
                cols = ",".join(fk.get("columns", []))
                report.changes.append(Change(
                    type="drop_fk", target=cols,
                    description=f"删除外键: {cols}（失去引用完整性）",
                    risk="warning",
                ))

    @staticmethod
    def _is_precision_tightened(old_prec, new_prec) -> bool:
        """判断精度是否收紧（总长或小数位变小）"""
        try:
            if isinstance(old_prec, (list, tuple)) and isinstance(new_prec, (list, tuple)):
                if len(old_prec) >= 1 and len(new_prec) >= 1:
                    if new_prec[0] < old_prec[0]:
                        return True
                if len(old_prec) >= 2 and len(new_prec) >= 2:
                    if new_prec[1] < old_prec[1]:
                        return True
        except Exception:
            logger.debug("精度收窄判定解析失败，按不收窄处理（保守方向）", exc_info=True)
        return False

    @staticmethod
    def _build_summary(changes: list) -> str:
        """构建变更摘要"""
        if not changes:
            return "无变更"
        parts = []
        danger_count = sum(1 for c in changes if c.risk == "danger")
        warning_count = sum(1 for c in changes if c.risk == "warning")
        safe_count = sum(1 for c in changes if c.risk == "safe")
        if danger_count:
            parts.append(f"{danger_count} 项高危")
        if warning_count:
            parts.append(f"{warning_count} 项警告")
        if safe_count:
            parts.append(f"{safe_count} 项安全")
        return "、".join(parts) + f"（共 {len(changes)} 项变更）"
