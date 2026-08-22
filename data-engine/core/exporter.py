"""数据导出模块——将查询结果导出为 CSV / Excel 文件

支持：
- 按表名导出全表数据（CSV 或 Excel）
- 按选择集导出查询结果（CSV）
- 自定义 WHERE 条件导出
"""

import csv
import os
from datetime import datetime
from pathlib import Path

from core.contract.security_contract import (
    safe_table_sql, safe_column_sql, safe_pragma_arg, SecurityContract,
    is_valid_identifier,
)


def _get_export_dir() -> Path:
    """获取导出目录（不存在则创建）"""
    export_dir = Path("exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    return export_dir


def export_table_to_csv(table: str, where: str = "", limit: int = 10000) -> dict:
    """将表数据导出为 CSV 文件

    Args:
        table: 表名
        where: 可选的 WHERE 条件（已通过安全校验）
        limit: 最大导出行数（防止内存溢出），默认 10000

    Returns:
        {"ok": bool, "path": str, "rows": int, "message": str}
    """
    from core.data_ops import _get_driver

    # 安全校验：表名必须是合法标识符（统一走安全契约的严格语义）
    if not is_valid_identifier(table):
        return {"ok": False, "path": "", "rows": 0, "message": f"非法表名: {table}"}

    drv = _get_driver()
    if not drv.table_exists(table):
        return {"ok": False, "path": "", "rows": 0, "message": f"表 {table} 不存在"}

    # 安全校验：WHERE 子句
    if where and not drv._safe_where(where):
        return {"ok": False, "path": "", "rows": 0, "message": "WHERE 条件不安全"}

    # 构建 SQL
    SecurityContract.validate_identifier(table, "表名")
    sql = f'SELECT * FROM {safe_table_sql(table)}'
    if where:
        sql += f" WHERE {where}"
    sql += f" LIMIT {int(limit)}"

    try:
        rows = drv.query(sql)
    except Exception as e:
        return {"ok": False, "path": "", "rows": 0, "message": f"查询失败: {e}"}

    if not rows:
        return {"ok": False, "path": "", "rows": 0, "message": "查询结果为空，无数据可导出"}

    # 写入 CSV
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{table}_{timestamp}.csv"
    filepath = _get_export_dir() / filename

    cols = list(rows[0].keys())
    try:
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            writer.writerows(rows)
    except Exception as e:
        return {"ok": False, "path": "", "rows": 0, "message": f"写入 CSV 失败: {e}"}

    return {
        "ok": True,
        "path": str(filepath),
        "rows": len(rows),
        "message": f"已导出 {len(rows)} 行到 {filepath.name}",
    }


def _fetch_table_rows(table: str, where: str = "", limit: int = 10000):
    """校验并查询表数据（export_table_to_csv / export_table_to_excel 共用）

    Returns:
        (rows, error_dict)：成功时 error_dict 为 None，失败时 rows 为 None
    """
    from core.data_ops import _get_driver

    # 安全校验：表名必须是合法标识符（统一走安全契约的严格语义）
    if not is_valid_identifier(table):
        return None, {"ok": False, "path": "", "rows": 0, "message": f"非法表名: {table}"}

    drv = _get_driver()
    if not drv.table_exists(table):
        return None, {"ok": False, "path": "", "rows": 0, "message": f"表 {table} 不存在"}

    # 安全校验：WHERE 子句
    if where and not drv._safe_where(where):
        return None, {"ok": False, "path": "", "rows": 0, "message": "WHERE 条件不安全"}

    # 构建 SQL
    SecurityContract.validate_identifier(table, "表名")
    sql = f'SELECT * FROM {safe_table_sql(table)}'
    if where:
        sql += f" WHERE {where}"
    sql += f" LIMIT {int(limit)}"

    try:
        rows = drv.query(sql)
    except Exception as e:
        return None, {"ok": False, "path": "", "rows": 0, "message": f"查询失败: {e}"}

    if not rows:
        return None, {"ok": False, "path": "", "rows": 0, "message": "查询结果为空，无数据可导出"}
    return rows, None


def export_table_to_excel(table: str, where: str = "", limit: int = 10000) -> dict:
    """将表数据导出为 Excel (.xlsx) 文件

    与 export_table_to_csv 接口一致，仅输出格式不同。
    xlsx 按 UTF-8 内部编码存储字符串，中文表头/中文数据不会乱码。

    Args:
        table: 表名
        where: 可选的 WHERE 条件（已通过安全校验）
        limit: 最大导出行数（防止内存溢出），默认 10000

    Returns:
        {"ok": bool, "path": str, "rows": int, "message": str}
    """
    rows, err = _fetch_table_rows(table, where, limit)
    if err:
        return err

    # 写入 xlsx
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{table}_{timestamp}.xlsx"
    filepath = _get_export_dir() / filename

    cols = list(rows[0].keys())
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        # sheet 名最长 31 字符，且不能含 []:*?/\\
        ws.title = "".join(c for c in table if c not in "[]:*?/\\")[:31] or "export"
        ws.append(cols)
        for row in rows:
            ws.append([row.get(c) for c in cols])
        wb.save(str(filepath))
    except Exception as e:
        return {"ok": False, "path": "", "rows": 0, "message": f"写入 Excel 失败: {e}"}

    return {
        "ok": True,
        "path": str(filepath),
        "rows": len(rows),
        "message": f"已导出 {len(rows)} 行到 {filepath.name}",
    }


def export_selection_to_csv(selection_id: int) -> dict:
    """将选择集数据导出为 CSV 文件

    Args:
        selection_id: 选择集 ID

    Returns:
        {"ok": bool, "path": str, "rows": int, "message": str}
    """
    from core.context import get_context

    sel = get_context().get_selection(selection_id)
    if not sel:
        return {"ok": False, "path": "", "rows": 0, "message": f"选择集 #{selection_id} 不存在"}

    rows = sel.get("sample", [])
    table = sel.get("table", "unknown")
    # 选择集只保存了 sample（前几行），需要重新查询完整数据
    # 通过 ids 重新查询
    ids = sel.get("ids", [])
    if not ids:
        return {"ok": False, "path": "", "rows": 0, "message": "选择集无数据"}

    from core.data_ops import _get_driver
    drv = _get_driver()
    # 类型感知 IN 子句（ids_in_clause 唯一实现；文本主键不再被 int 强转静默丢弃）
    from core.contract.security_contract import ids_in_clause
    try:
        rows = drv.query(f'SELECT * FROM {safe_table_sql(table)} WHERE {ids_in_clause(ids)}')
    except Exception as e:
        return {"ok": False, "path": "", "rows": 0, "message": f"查询失败: {e}"}

    if not rows:
        return {"ok": False, "path": "", "rows": 0, "message": "查询结果为空"}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{table}_selection{selection_id}_{timestamp}.csv"
    filepath = _get_export_dir() / filename

    cols = list(rows[0].keys())
    try:
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            writer.writerows(rows)
    except Exception as e:
        return {"ok": False, "path": "", "rows": 0, "message": f"写入 CSV 失败: {e}"}

    return {
        "ok": True,
        "path": str(filepath),
        "rows": len(rows),
        "message": f"已导出 {len(rows)} 行到 {filepath.name}",
    }


def list_exports() -> list[dict]:
    """列出所有已导出的文件"""
    export_dir = _get_export_dir()
    files = []
    for p in sorted([*export_dir.glob("*.csv"), *export_dir.glob("*.xlsx")],
                    key=lambda x: x.stat().st_mtime, reverse=True):
        stat = p.stat()
        files.append({
            "filename": p.name,
            "path": str(p),
            "size_kb": round(stat.st_size / 1024, 1),
            "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return files
