"""data_ops 子模块·基座：字段别名解析 / 单表改删 / 批量插入 / SELECT 拼装与校验
（20260830 拆包：core/data_ops.py 同名片段纯搬家，逻辑零变化）

patch 兼容（测试依赖，勿绕开）：get_driver / _load_table_schema 的引用一律
在调用时经 facade 取值（_ops.get_driver() / _ops._load_table_schema(...)），
使 patch("core.data_ops.get_driver") 之类的打桩保持有效。
"""
import json
import re as _re

from core.tool_result import ToolResult
from core.contract.security_contract import (
    safe_table_sql, SecurityContract, is_valid_identifier,
)

# facade 回旋引用：仅用于调用时取值（_ops.get_driver()），导入期不解引用
from core import data_ops as _ops


# ── 标识符与表 schema 辅助 ──

# 合法标识符正则统一从 core.contract.security_contract 导入（严格语义：
# 字母/下划线开头，只含字母数字下划线，长度 ≤ 64），本模块不再本地定义


def _validate_identifier(name: str) -> str:
    """校验标识符（表名/字段名）合法性，防止 SQL 注入"""
    if not name:
        raise ValueError("标识符不能为空")
    if not is_valid_identifier(name):
        raise ValueError(f"非法标识符: {name}")
    return name


def _load_table_schema(table: str) -> dict:
    """从 YAML 配置加载单张表的 schema（含外键关系）
    ——薄委托 schema_matcher.load_table_schema（加载收敛单点）"""
    from core.schema_matcher import load_table_schema
    return load_table_schema(table) or {}


def _find_fk_relation(table_a: str, table_b: str) -> tuple[str, str, str, str] | None:
    """查找两张表之间的外键关系

    Returns:
        (from_table, from_col, to_table, to_col) 或 None
    """
    # table_a 有 FK 指向 table_b
    schema_a = _ops._load_table_schema(table_a)
    for fk in schema_a.get("foreign_keys", []):
        if fk.get("references", "").lower() == table_b.lower():
            cols = fk.get("columns", [])
            ref_cols = fk.get("ref_columns", ["id"])
            if cols and ref_cols:
                return (table_a, cols[0], table_b, ref_cols[0])
    # table_b 有 FK 指向 table_a
    schema_b = _ops._load_table_schema(table_b)
    for fk in schema_b.get("foreign_keys", []):
        if fk.get("references", "").lower() == table_a.lower():
            cols = fk.get("columns", [])
            ref_cols = fk.get("ref_columns", ["id"])
            if cols and ref_cols:
                return (table_b, cols[0], table_a, ref_cols[0])
    return None


def resolve_field(expr: str, table: str = "") -> str:
    """将 SQL 表达式中的别名映射为真实字段名（基于 fields.yml 配置，不做模糊猜测）

    联邦数据库：当指定 table 时，仅替换目标表中实际存在的字段别名，
    避免跨数据源表的字段名冲突（如 price_history.price ≠ unit_price）

    已合并 db_chat._resolve_fields 的独有行为：
    别名同时注册"去掉空格"的变体（如 "人工 费" → labor_cost），
    用户/AI 输入省略空格时也能命中。
    """
    # 字段字典走行业加载器单源（industries.base 的 field_dict，ConfigHub 目录签名
    # 新鲜度）——此前本函数与 _extract_mutation_ops 各手搓一份 yaml 加载，
    # 同款两份必漂移（与权限层的教训同型）
    from industries.base import get_current_industry
    fd = get_current_industry().field_dict or {}
    if not fd:
        return expr
    aliases = {}
    for fname, finfo in fd.items():
        aliases[fname] = fname
        for alias in finfo.get("alias", []):
            aliases[alias] = fname
            # 也支持去掉空格的匹配（合并自 db_chat._resolve_fields）
            aliases[alias.replace(" ", "")] = fname
    # 联邦数据库：指定 table 时，仅替换目标表中存在的字段
    table_cols = set()
    if table:
        try:
            drv = _ops.get_driver()
            table_cols = {c["name"].lower() for c in drv.get_columns(table)}
        except Exception:
            pass  # 取列失败=该表字段候选缺省（字段解析按未知处理，不猜）
    # 精确别名替换（配置驱动，不猜测）
    for nick, real in sorted(aliases.items(), key=lambda x: -len(x[0])):
        if nick != real:
            # 指定了 table 时，仅当真实字段名存在于该表才替换
            if table_cols and real.lower() not in table_cols:
                continue
            expr = _re.sub(r'\b' + _re.escape(nick) + r'\b', real, expr)
    return expr


def update_rows(table: str, set_clause: str, where: str = "") -> "ToolResult":
    """安全执行 UPDATE（自动纠正字段名）。双轨：text 文案不变，data 带 affected/table"""
    drv = _ops.get_driver()
    if not drv.table_exists(table):
        return ToolResult.fail(f"表 {table} 不存在", code="NOT_FOUND",
                               reason="table_not_found", table=table)
    set_clause = resolve_field(set_clause, table)
    where = resolve_field(where, table) if where else where
    r = drv.update(table, set_clause, where)
    if not r["ok"]:
        return ToolResult.fail(r["message"], table=table)
    drv.commit()
    return ToolResult.ok(f"已更新 {table} 中 {r['count']} 条记录",
                         table=table, action="UPDATE", affected=r["count"])


def delete_rows(table: str, where: str = "") -> "ToolResult":
    """安全执行 DELETE（必须带 WHERE）。双轨：text 文案不变，data 带 affected/table"""
    drv = _ops.get_driver()
    if not drv.table_exists(table):
        return ToolResult.fail(f"表 {table} 不存在", code="NOT_FOUND",
                               reason="table_not_found", table=table)
    r = drv.delete(table, where)
    if not r["ok"]:
        return ToolResult.fail(r["message"], table=table)
    drv.commit()
    return ToolResult.ok(f"已从 {table} 删除 {r['count']} 条记录",
                         table=table, action="DELETE", affected=r["count"])


def insert_row(table: str, data_json: str) -> "ToolResult":
    """插入一行数据到指定表。data_json 是 JSON 格式的字段值字典。

    双轨：text 文案不变；data 带 ok/table/effects（values=插入行，供目标达成检测复查）。
    """
    try:
        row = json.loads(data_json)
    except Exception:
        return ToolResult.fail("data 格式错误，请使用 JSON 格式",
                               code="VALIDATION", reason="data_format")
    if not isinstance(row, dict):
        return ToolResult.fail("data 须为 JSON 对象", code="VALIDATION",
                               reason="data_format")
    from core.schema_manager import _guard_sys_column as _gsc
    if any(_gsc(k, "手动插入值") for k in row.keys()):
        return ToolResult.fail(
            "id 是系统主键，由系统自动生成，不允许手动指定。请去掉 id 字段后重新插入",
            code="VALIDATION", reason="primary_key", table=table)
    # 数据校验（CHECK 约束）
    from core.schema_manager import _load_config
    cfg = _load_config()
    tbl_cfg = next((t for t in cfg.get("tables", []) if t["name"].lower() == table.lower()), None)
    if tbl_cfg:
        from core.validator import validate_row
        err = validate_row(tbl_cfg, row)
        if err:
            return ToolResult.fail(err, code="VALIDATION", reason="check_constraint",
                                   table=table)
    drv = _ops.get_driver()
    r = drv.insert(table, [row])
    drv.commit()
    if r.get("ok"):
        cols = "; ".join(f"{k}={v}" for k, v in row.items())
        return ToolResult.ok(
            f"已插入{table}数据：{cols}", table=table, action="INSERT", affected=1,
            effects={"table": table, "action": "INSERT", "affected": 1,
                     "values": [row]})
    return ToolResult.fail(f"插入失败: {r.get('message','')}", table=table,
                           action="INSERT")


def insert_rows(table: str, rows: list, overwrite: bool = False, auto_commit: bool = True) -> dict:
    """批量插入数据到指定表（带校验，供 pipeline 调用）

    与 insert_row 的区别：
    - 接收 list[dict] 而非 JSON 字符串
    - 返回结构化 dict 而非字符串（pipeline 需要 ok/conflict 判断）
    - 复用同样的校验逻辑：系统字段保护 + CHECK 约束
    - auto_commit=False 时不在内部 commit，供 pipeline 事务控制

    Returns:
        {"ok": bool, "conflict": bool, "message": str, "count": int}
    """
    if not rows:
        return {"ok": True, "conflict": False, "message": "无数据", "count": 0}

    from core.schema_manager import _guard_sys_column as _gsc, _load_config

    # 系统字段保护（id 由系统自动生成）
    for row in rows:
        if any(_gsc(k, "批量插入") for k in row.keys()):
            return {"ok": False, "conflict": False,
                    "message": "id 是系统主键，不允许手动指定", "count": 0}

    # 数据校验（CHECK 约束）
    cfg = _load_config()
    tbl_cfg = next((t for t in cfg.get("tables", [])
                    if t["name"].lower() == table.lower()), None)
    if tbl_cfg:
        from core.validator import validate_row
        for i, row in enumerate(rows):
            err = validate_row(tbl_cfg, row)
            if err:
                return {"ok": False, "conflict": False,
                        "message": f"第{i+1}行校验失败: {err}", "count": 0}

    drv = _ops.get_driver()
    r = drv.insert(table, rows, overwrite=overwrite)
    if auto_commit:
        drv.commit()
    return {
        "ok": r.get("ok", False),
        "conflict": r.get("conflict", False),
        "message": r.get("message", ""),
        "count": r.get("count", len(rows)),
    }


def _translate_query_error(e: Exception) -> str:
    """翻译常见 SQL 查询错误为中文提示（薄委托 core.contract.ErrorTranslator：
    ambiguous 规则已并入 ErrorTranslator，本地无规则副本）"""
    from core.contract.error_translator import ErrorTranslator
    drv = _ops.get_driver()
    # get_driver() 恒为 FederatedDriver（模块级单例）：解包到默认驱动以推断
    # driver_type（FederatedDriver 本身无翻译规则；解包失败则沿用联邦驱动按类名推断）
    try:
        drv = drv._get_default_driver()
    except Exception:
        pass  # 解包默认驱动失败则沿用联邦驱动（翻译规则按类名推断）
    # 契约包装驱动（ContractDriver）自带 driver_type；否则按类名推断
    driver_type = getattr(drv, "driver_type", "") or ErrorTranslator.get_driver_type(drv)
    result = ErrorTranslator.translate(driver_type, e)
    # 兜底文案与旧实现保持一致（直接返回截断的原始错误，不加前缀）
    if result.message.startswith("数据库操作失败:"):
        return str(e)[:200]
    return result.message


# ── SELECT 子句拼装与校验（查询路径唯一拼装点，自 db_chat 迁入）──
# 字段：标识符、table.column、聚合函数包裹（COUNT(*)/SUM(x) 等）、可选 AS 别名
_FIELD_ITEM_RE = _re.compile(
    r'^\s*(?:(?:COUNT|SUM|AVG|MIN|MAX|DISTINCT)\s*\(\s*(?:[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?|\*)\s*\)'
    r'|[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?)'
    r'(?:\s+AS\s+[a-zA-Z_]\w*)?\s*$',
    _re.IGNORECASE)
# 排序项：标识符 + 可选 ASC/DESC
_ORDER_ITEM_RE = _re.compile(
    r'^\s*[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?(?:\s+(?:ASC|DESC))?\s*$',
    _re.IGNORECASE)
# 分组项：纯标识符
_GROUP_ITEM_RE = _re.compile(r'^\s*[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?\s*$')


def validate_select_fields(fields: str) -> bool:
    """校验 SELECT 字段列表：*, 标识符, table.column, 聚合(field), field AS alias"""
    if not fields or not fields.strip():
        return False
    if fields.strip() == "*":
        return True
    return all(_FIELD_ITEM_RE.match(p) for p in fields.split(","))


def validate_order_by(order_by: str) -> bool:
    """校验 ORDER BY 子句：标识符列表，每项可选 ASC/DESC"""
    if not order_by or not order_by.strip():
        return False
    return all(_ORDER_ITEM_RE.match(p) for p in order_by.split(","))


def validate_group_by(group_by: str) -> bool:
    """校验 GROUP BY 子句：纯标识符列表"""
    if not group_by or not group_by.strip():
        return False
    return all(_GROUP_ITEM_RE.match(p) for p in group_by.split(","))


def build_select_sql(table: str, fields: str, where: str = "",
                     order_by: str = "", group_by: str = "",
                     limit: int = 0) -> str:
    """构建 SELECT SQL——查询路径唯一拼装点（自 db_chat 迁入）。

    所有子句先做安全校验，任一子句不安全时抛 ValueError。
    """
    if not validate_select_fields(fields):
        raise ValueError(f"SELECT 字段不安全，已拒绝执行: '{fields[:80]}'")
    if where:
        # 归一化：剥离 AI 可能预加的 "WHERE" 前缀与前导空格
        # （db_chat.py L320 仅对 where_conditions 分支剥离，where 字符串
        # 直传路径会带 " WHERE ..." 进入校验导致误拒——query 归一化补丁 20260803）
        where = where.strip()
        if where.upper().startswith("WHERE "):
            where = where[6:]
        try:
            SecurityContract.validate_where(where)
        except Exception as e:
            raise ValueError(f"WHERE 条件不安全，已拒绝执行: {e}")
    sql = f'SELECT {fields} FROM {safe_table_sql(table)}'
    if where:
        sql += f" WHERE {where}"
    if order_by:
        if not validate_order_by(order_by):
            raise ValueError(f"ORDER BY 子句不安全，已拒绝执行: '{order_by[:80]}'")
        sql += f" ORDER BY {order_by}"
    if group_by:
        if not validate_group_by(group_by):
            raise ValueError(f"GROUP BY 子句不安全，已拒绝执行: '{group_by[:80]}'")
        sql += f" GROUP BY {group_by}"
    if limit and isinstance(limit, int) and limit > 0:
        sql += f" LIMIT {limit}"
    return sql
