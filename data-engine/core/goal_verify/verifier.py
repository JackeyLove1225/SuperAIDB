"""目标达成验证执行器——读 effects → 独立复查查询 → 比对预期状态

复查查询走驱动标准 query()（FederatedDriver 自动路由数据源，权限/契约层全程在位）。
验证器自身权限受限时显式返回 verified=None + skipped_reason，不装死、不越权。
"""
from core.logger import get_logger
from pathlib import Path

import yaml

from core.goal_verify.report import VerifyReport

logger = get_logger(__name__)

# 配置缺失时的内置默认（与 config/goal_verify.yml 一致；文件优先）
_DEFAULT_RULES = {
    "INSERT": {"enabled": True, "checks": ["records_exist", "row_count"]},
    "UPDATE": {"enabled": True, "checks": ["records_exist", "fields_match"]},
    "DELETE": {"enabled": True, "checks": ["records_absent", "row_count"]},
}

# 单条 effects 复查的 id/行数上限（防御：巨型选择集不全量 IN）
_MAX_IDS = 100
_MAX_VALUE_ROWS = 5


def rules_enabled() -> bool:
    """总开关：config/goal_verify.yml 的 enabled（缺失默认开）"""
    cfg = _load_config()
    return bool(cfg.get("enabled", True))


def _load_config() -> dict:
    path = Path(__file__).parent.parent.parent / "config" / "goal_verify.yml"
    try:
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("goal_verify 配置读取失败，使用内置默认规则: %s", e)
    return {"enabled": True, "rules": _DEFAULT_RULES}


def _load_rules() -> dict:
    return _load_config().get("rules") or _DEFAULT_RULES


def _sql_literal(v) -> str:
    """值字面量（仅用于复查查询的等值对账；id 列之外的值经转义）"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _values_equal(actual, expected) -> bool:
    """字段值对账：字符串化严格相等；不等时按数值语义兜底比对
    （expected_values 来自指令解析多为 str，DB 数值列读出为 int/float，
    '88' vs 88.0 应判达成而非误报不符）"""
    if str(actual) == str(expected):
        return True
    try:
        return float(actual) == float(expected)
    except (TypeError, ValueError):
        return False


def verify(effects: dict, driver=None, rules: dict | None = None) -> VerifyReport:
    """对一条写操作 effects 做目标达成复查

    Args:
        effects: 写工具双轨 data 的 effects——
                 {table, action: INSERT/UPDATE/DELETE, affected,
                  affected_ids?, changed_fields?, expected_values?, values?}
        driver:  可选注入（测试用）；缺省走 core.data_ops._get_driver()（联邦路由）
        rules:   可选规则覆盖（测试用）；缺省读 config/goal_verify.yml

    Returns:
        VerifyReport（verified True/False/None 三态）
    """
    from core.contract.security_contract import safe_table_sql, safe_column_sql

    table = str(effects.get("table", "") or "")
    action = str(effects.get("action", "") or "").upper()
    if not table or action not in ("INSERT", "UPDATE", "DELETE"):
        return VerifyReport(verified=None, table=table, action=action,
                            skipped_reason="effects 缺少 table/action，无法复查")

    rules = rules if rules is not None else _load_rules()
    rule = rules.get(action) or {}
    if not rule.get("enabled", True):
        return VerifyReport(verified=None, table=table, action=action,
                            skipped_reason=f"{action} 复查规则已关闭")
    checks = set(rule.get("checks") or [])

    if driver is None:
        from core.data_ops import _get_driver
        driver = _get_driver()

    ids = []
    for i in (effects.get("affected_ids") or [])[:_MAX_IDS]:
        try:
            ids.append(int(i))
        except (TypeError, ValueError):
            continue
    affected = effects.get("affected")
    expected: dict = {"action": action}
    actual: dict = {}
    mismatches: list[str] = []

    def _count(where_sql: str) -> int:
        rows = driver.query(
            f"SELECT COUNT(*) AS c FROM {safe_table_sql(table)} WHERE {where_sql}")
        return int(rows[0]["c"]) if rows else 0

    try:
        # ── DELETE：目标 id 应全部不存在 ──
        if action == "DELETE" and "records_absent" in checks:
            if not ids:
                return VerifyReport(verified=None, table=table, action=action,
                                    skipped_reason="缺少 affected_ids，无法独立复查")
            expected["absent_ids"] = ids
            remaining = _count("id IN (%s)" % ",".join(str(i) for i in ids))
            actual["remaining"] = remaining
            if remaining != 0:
                mismatches.append(
                    f"声明删除 {table} 的 {len(ids)} 条记录，但复查仍有 {remaining} 条存在")

        # ── UPDATE：目标 id 应全部存在 ──
        if action == "UPDATE" and "records_exist" in checks:
            if not ids:
                return VerifyReport(verified=None, table=table, action=action,
                                    skipped_reason="缺少 affected_ids，无法独立复查")
            expected["exist_ids"] = ids
            existing = _count("id IN (%s)" % ",".join(str(i) for i in ids))
            actual["existing"] = existing
            if existing != len(ids):
                mismatches.append(
                    f"声明更新 {table} 的 {len(ids)} 条记录，但复查仅 {existing} 条存在")

        # ── UPDATE：字段值对账（effects 带 expected_values 才做）──
        if action == "UPDATE" and "fields_match" in checks and ids:
            ev = effects.get("expected_values") or {}
            if ev:
                cols = ", ".join(safe_column_sql(k) for k in ev)
                rows = driver.query(
                    f"SELECT {cols} FROM {safe_table_sql(table)} WHERE id = {ids[0]}")
                if rows:
                    for k, v in ev.items():
                        actual_v = rows[0].get(k)
                        expected[f"field_{k}"] = v
                        actual[f"field_{k}"] = actual_v
                        if not _values_equal(actual_v, v):
                            mismatches.append(
                                f"字段 {k} 期望 {v!r}，复查实际 {actual_v!r}")

        # ── INSERT：插入行按字段等值复查存在 ──
        if action == "INSERT" and "records_exist" in checks:
            values = effects.get("values") or []
            if not values:
                return VerifyReport(verified=None, table=table, action=action,
                                    skipped_reason="缺少 values，无法独立复查")
            for row in values[:_MAX_VALUE_ROWS]:
                conds = " AND ".join(
                    f"{safe_column_sql(k)} = {_sql_literal(v)}" for k, v in row.items())
                if _count(conds) < 1:
                    mismatches.append(f"声明插入的行复查不存在: {row}")

        # ── 行数对账：声明 affected 与目标集规模一致 ──
        if "row_count" in checks and ids and affected is not None:
            expected["affected"] = len(ids)
            actual["affected"] = affected
            if int(affected) != len(ids):
                mismatches.append(
                    f"声明影响 {affected} 行，但目标集为 {len(ids)} 行")

    except Exception as e:
        # 权限拒绝（PermissionDenied）与其他查询异常同口径：
        # 验证器自身过不了权限/契约时显式报告"无法复查"，不装死、不越权
        from core.permission import PermissionDenied
        if isinstance(e, PermissionDenied):
            return VerifyReport(verified=None, table=table, action=action,
                                expected=expected, actual=actual,
                                skipped_reason=f"权限受限，无法复查: {e}")
        raise

    if mismatches:
        return VerifyReport(verified=False, table=table, action=action,
                            expected=expected, actual=actual,
                            mismatch_detail="；".join(mismatches))
    return VerifyReport(verified=True, table=table, action=action,
                        expected=expected, actual=actual)
