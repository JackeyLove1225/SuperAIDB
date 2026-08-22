"""表数据端点——数据库概览、表结构、表数据分页浏览与 CRUD"""

import os

from fastapi import APIRouter, HTTPException

from core.exceptions import AppError
from core.contract.security_contract import safe_table_sql, is_valid_identifier
from agent.management.deps import _get_driver, _get_db_path

router = APIRouter()


@router.get("/api/database/overview")
def get_database_overview():
    """数据库概览——所有表名 + 每表行数 + 数据库文件大小"""
    drv = _get_driver()
    if not drv:
        raise HTTPException(status_code=500, detail="数据库驱动未初始化")

    tables = []
    try:
        table_names = drv.list_tables()
        for tname in table_names:
            try:
                rows = drv.query(f'SELECT COUNT(*) as c FROM {safe_table_sql(tname)}')
                count = rows[0]["c"] if rows else 0
            except Exception:
                count = -1
            tables.append({"name": tname, "rows": count})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询表列表失败: {e}")

    # 数据库文件大小
    db_path = _get_db_path()
    db_size_mb = round(os.path.getsize(db_path) / 1024 / 1024, 2) if os.path.exists(db_path) else 0

    total_rows = sum(t["rows"] for t in tables if t["rows"] > 0)

    return {
        "tables": tables,
        "table_count": len(tables),
        "total_rows": total_rows,
        "db_path": db_path,
        "db_size_mb": db_size_mb,
    }


@router.get("/api/database/schema/{table_name}")
def get_table_schema(table_name: str):
    """获取指定表的字段结构"""
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    drv = _get_driver()
    if not drv:
        raise HTTPException(status_code=500, detail="数据库驱动未初始化")

    try:
        columns = drv.get_columns(table_name)
        # 尝试获取行数
        rows = drv.query(f'SELECT COUNT(*) as c FROM {safe_table_sql(table_name)}')
        row_count = rows[0]["c"] if rows else 0

        # 尝试获取前 5 行数据作为预览
        preview = drv.query(f'SELECT * FROM {safe_table_sql(table_name)} LIMIT 5')
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询表结构失败: {e}")

    return {
        "table_name": table_name,
        "columns": columns,
        "row_count": row_count,
        "preview": preview,
    }


@router.get("/api/database/table/{table_name}/data")
def get_table_data(table_name: str, page: int = 1, page_size: int = 50):
    """获取表数据（分页）——供前端表浏览页面使用

    Args:
        table_name: 表名
        page: 页码（1-indexed）
        page_size: 每页条数（默认50，最大200）
    """
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    drv = _get_driver()
    if not drv:
        raise HTTPException(status_code=500, detail="数据库驱动未初始化")

    # 参数规范化
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size

    try:
        # 总行数
        count_rows = drv.query(f'SELECT COUNT(*) as c FROM {safe_table_sql(table_name)}')
        total = count_rows[0]["c"] if count_rows else 0

        # 分页数据
        data = drv.query(
            f'SELECT * FROM {safe_table_sql(table_name)} LIMIT {page_size} OFFSET {offset}'
        )

        # 字段结构
        columns = drv.get_columns(table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询表数据失败: {e}")

    import math
    total_pages = math.ceil(total / page_size) if total > 0 else 0

    return {
        "table_name": table_name,
        "columns": columns,
        "rows": data,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_more": page * page_size < total,
    }


@router.post("/api/database/table/{table_name}/data")
def insert_table_data(table_name: str, body: dict):
    """新增数据行——经 DataCrudContract 契约校验

    Body: {"rows": [{...}, ...], "overwrite": false}
    - rows: 要插入的行列表（每行为字段名→值的 dict）
    - overwrite: 是否覆盖冲突行（默认 false）

    契约保护：
    - 标识符校验（防 SQL 注入）
    - 字段存在性校验
    - 类型匹配校验 + 数值清洗
    - 批量上限 1000 行
    - 主键 id 自动移除（让自增）
    """
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    drv = _get_driver()
    if not drv:
        raise HTTPException(status_code=500, detail="数据库驱动未初始化")

    rows = body.get("rows") or []
    overwrite = bool(body.get("overwrite", False))
    if not rows:
        raise HTTPException(status_code=400, detail="rows 不能为空")

    return _write_tx(drv, lambda: drv.insert(table_name, rows, overwrite), "插入数据")


def _write_tx(drv, fn, what: str):
    """管理端写端点事务纪律（chat 路径在 data_ops 显式 commit；此前管理端点无 commit，
    默认隔离级别下同线程可见、跨线程/重启即丢——评审三轮架构复核发现）：
    成功 commit 落盘；失败（异常或 ok=False）一律 rollback 后原样上抛。"""
    try:
        result = fn()
    except HTTPException:
        raise
    except AppError:
        _safe_rollback(drv)
        raise
    except Exception as e:
        _safe_rollback(drv)
        raise HTTPException(status_code=500, detail=f"{what}失败: {e}")
    if isinstance(result, dict) and not result.get("ok", True):
        _safe_rollback(drv)
        raise HTTPException(status_code=400, detail=result.get("message", f"{what}失败"))
    drv.commit()
    return result


def _safe_rollback(drv):
    try:
        drv.rollback()
    except Exception:
        pass


@router.put("/api/database/table/{table_name}/data")
def update_table_data(table_name: str, body: dict):
    """更新数据行——经 DataCrudContract 契约校验

    Body: {"set": "name='张三', age=30", "where": "id=1"}
    - set: SET 子句（字段=值，逗号分隔）
    - where: WHERE 子句（必须包含主键 id 或唯一索引列）

    契约保护：
    - WHERE 必填且必须含主键（防误更新全表）
    - WHERE 安全校验（防 SQL 注入）
    - SET 子句类型校验
    - 影响行数上限 10000
    """
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    drv = _get_driver()
    if not drv:
        raise HTTPException(status_code=500, detail="数据库驱动未初始化")

    set_clause = body.get("set") or body.get("set_clause") or ""
    where = body.get("where") or body.get("where_clause") or ""
    if not set_clause:
        raise HTTPException(status_code=400, detail="set 子句不能为空")

    return _write_tx(drv, lambda: drv.update(table_name, set_clause, where), "更新数据")


@router.post("/api/database/table/{table_name}/data/delete-by-pk")
def delete_table_data_by_pk(table_name: str, body: dict):
    """按主键删除数据行——参数化绑定，前端不再手拼 SQL WHERE（P1-10）。

    body: {"pk_column": str, "pk_value": Any}
    """
    pk_column = str(body.get("pk_column", "")).strip()
    pk_value = body.get("pk_value")
    if not is_valid_identifier(table_name) or not is_valid_identifier(pk_column):
        raise HTTPException(status_code=400, detail="非法表名或主键列名")
    if pk_value is None:
        raise HTTPException(status_code=400, detail="pk_value 不能为空（禁止无键删除）")
    drv = _get_driver()
    if not drv:
        raise HTTPException(status_code=500, detail="数据库驱动未初始化")
    return _write_tx(drv, lambda: drv.delete_by_pk(table_name, pk_column, pk_value), "删除数据")


@router.post("/api/database/table/{table_name}/data/update-by-pk")
def update_table_data_by_pk(table_name: str, body: dict):
    """按主键更新数据行——服务端字面值安全拼装（JSON 类型驱动），
    前端不再手拼 SQL SET/WHERE（对称 delete-by-pk；评审指出的"拼装渗进展示层"收口）。

    body: {"pk_column": str, "pk_value": Any, "values": {列名: 值, ...}}
    值类型语义：null→NULL；bool→1/0；number→裸数值；string→单引号+doubling 转义。
    """
    pk_column = str(body.get("pk_column", "")).strip()
    pk_value = body.get("pk_value")
    values = body.get("values")
    if not is_valid_identifier(table_name) or not is_valid_identifier(pk_column):
        raise HTTPException(status_code=400, detail="非法表名或主键列名")
    if pk_value is None:
        raise HTTPException(status_code=400, detail="pk_value 不能为空（禁止无键更新）")
    if not isinstance(values, dict) or not values:
        raise HTTPException(status_code=400, detail="values 不能为空")
    for c in values:
        if not is_valid_identifier(c):
            raise HTTPException(status_code=400, detail=f"非法列名: {c}")
    if pk_column in values:
        raise HTTPException(status_code=400, detail="主键列不可作为更新字段")

    def _literal(v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    set_clause = ", ".join(f"{c}={_literal(v)}" for c, v in values.items())
    where = f"{pk_column}={_literal(pk_value)}"
    drv = _get_driver()
    if not drv:
        raise HTTPException(status_code=500, detail="数据库驱动未初始化")
    return _write_tx(drv, lambda: drv.update(table_name, set_clause, where), "更新数据")


@router.delete("/api/database/table/{table_name}/data")
def delete_table_data(table_name: str, where: str = ""):
    """删除数据行——经 DataCrudContract 契约校验

    Query: ?where=id=1
    - where: WHERE 子句（必填，必须包含主键 id 或唯一索引列）

    契约保护：
    - WHERE 必填且必须含主键（防误删除全表）
    - WHERE 安全校验（防 SQL 注入）
    - 影响行数上限 10000
    """
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    drv = _get_driver()
    if not drv:
        raise HTTPException(status_code=500, detail="数据库驱动未初始化")

    if not where:
        raise HTTPException(status_code=400, detail="where 参数不能为空（禁止全表删除）")

    return _write_tx(drv, lambda: drv.delete(table_name, where), "删除数据")
