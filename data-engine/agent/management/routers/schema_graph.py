"""表结构可视化设计器（Schema Graph Designer）API

基于 YAML ↔ SQLite 元数据 ↔ Ladybug 图库三层存储架构
前端用 React Flow 渲染可拖拽卡片节点 + 外键连线
"""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core.contract.security_contract import is_valid_identifier
from core.contract.schema_change_contract import SchemaChangeContract
from core.exceptions import PrimaryKeyError

router = APIRouter()


def _get_schema_graph_service():
    """获取 SchemaGraphService 单例（懒加载，避免启动时初始化图库）"""
    from core.graph import SchemaGraphService
    return SchemaGraphService.get_instance()


@router.get("/api/schema-graph")
def schema_graph_get():
    """获取完整图——所有表节点 + 所有外键边

    前端画布初始化时调用，返回 {nodes, edges} 供 React Flow 渲染。
    节点含坐标（来自 Ladybug 图库），边含外键关系信息。
    """
    svc = _get_schema_graph_service()
    try:
        return svc.get_graph()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取图数据失败: {e}")


@router.get("/api/schema-graph/table/{table_name}")
def schema_graph_get_table(table_name: str):
    """获取单张表的完整信息（结构 + 字段 + 关系 + 位置）"""
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    svc = _get_schema_graph_service()
    try:
        result = svc.get_table(table_name)
        if result is None:
            raise HTTPException(status_code=404, detail=f"表 '{table_name}' 不存在")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取表信息失败: {e}")


@router.post("/api/schema-graph/table")
def schema_graph_create_table(body: dict, request: Request):
    """创建表——同步四层（YAML + SQLite + Ladybug 图库 + 实际建表）

    Body:
        table_schema: 表结构（name, business_name, columns, foreign_keys, ...）
        x, y: 画布初始坐标（可选，默认 0,0）
        create_real_table: 是否实际建表（默认 true）
        operator_password: 操作密码（写皆密码）
    """
    from agent.management.deps import require_operator_password
    require_operator_password(request, body)
    table_schema = body.get("table_schema") or body.get("schema") or {}
    if not isinstance(table_schema, dict):
        raise HTTPException(status_code=400, detail="table_schema 必须是对象")

    name = table_schema.get("name", "")
    if not is_valid_identifier(name):
        raise HTTPException(status_code=400, detail=f"非法表名: {name}")

    # 主键键名归一化：primary_key/primaryKey/is_primary 等别名统一映射为 is_pk，
    # 避免前端传别名时主键标记被静默忽略、建出无主键表
    columns = table_schema.get("columns", [])
    if isinstance(columns, list):
        SchemaChangeContract.normalize_pk_aliases(columns)
        # 校验收紧：存在 id 列但未标 is_pk → 明确拒绝，不放行无主键表
        try:
            SchemaChangeContract.assert_id_pk_declared(columns)
        except PrimaryKeyError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # 校验字段名
    for col in table_schema.get("columns", []):
        if not is_valid_identifier(col.get("name", "")):
            raise HTTPException(status_code=400, detail=f"非法字段名: {col.get('name')}")

    # 校验外键引用
    for fk in table_schema.get("foreign_keys", []):
        ref_table = fk.get("references", "")
        if not is_valid_identifier(ref_table):
            raise HTTPException(status_code=400, detail=f"非法外键引用表名: {ref_table}")

    x = float(body.get("x", 0))
    y = float(body.get("y", 0))
    create_real_table = body.get("create_real_table", True)

    svc = _get_schema_graph_service()
    try:
        result = svc.create_table(table_schema, x=x, y=y, create_real_table=create_real_table)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("message", "创建失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建表失败: {e}")


@router.put("/api/schema-graph/table/{table_name}")
def schema_graph_update_table(table_name: str, body: dict, request: Request):
    """更新表结构——契约校验 + 同步三层 + 实际变更表结构

    Body: 可包含 business_name, description, columns, foreign_keys, force
    - force: bool，是否强制执行高危变更（默认 False）
             高危变更且 force=False 时返回 409 + report 供前端二次确认
    """
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")

    updates = body.get("updates") or body
    force = bool(body.get("force", False))
    # 写皆密码：结构保存不分风险级一律要操作密码（不再只对 force 高危变更收）；
    # 低危编辑（改业务名/描述）也过闸——"改结构"本身就是高危类别
    from agent.management.deps import require_operator_password
    require_operator_password(request, body)

    # 校验字段名（如果提供了 columns）
    for col in updates.get("columns", []) or []:
        if not is_valid_identifier(col.get("name", "")):
            raise HTTPException(status_code=400, detail=f"非法字段名: {col.get('name')}")
    # 校验外键引用
    for fk in updates.get("foreign_keys", []) or []:
        ref_table = fk.get("references", "")
        if not is_valid_identifier(ref_table):
            raise HTTPException(status_code=400, detail=f"非法外键引用表名: {ref_table}")

    svc = _get_schema_graph_service()
    try:
        result = svc.update_table(table_name, updates, force=force)
        if not result.get("ok"):
            # 高危变更需确认：返回 409 + 完整报告
            if result.get("need_confirm"):
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "need_confirm": True,
                        "report": result.get("report", {}),
                        "message": result.get("message", "变更包含高危操作，需确认"),
                    },
                )
            raise HTTPException(status_code=400, detail=result.get("message", "更新失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新表失败: {e}")


@router.post("/api/schema-graph/table/{table_name}/precheck")
def schema_graph_precheck_update(table_name: str, body: dict):
    """预校验表结构变更（不执行）——返回变更风险评估报告

    Body: 可包含 business_name, description, columns, foreign_keys
    返回: {ok, report: {risk_level, requires_confirm, requires_force, changes, summary}, message}
    """
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")

    updates = body.get("updates") or body
    # 校验字段名
    for col in updates.get("columns", []) or []:
        if not is_valid_identifier(col.get("name", "")):
            raise HTTPException(status_code=400, detail=f"非法字段名: {col.get('name')}")

    svc = _get_schema_graph_service()
    try:
        result = svc.precheck_update(table_name, updates)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("message", "预校验失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"预校验失败: {e}")


@router.delete("/api/schema-graph/table/{table_name}")
def schema_graph_delete_table(table_name: str, drop_real_table: bool = True, body: dict = None, request: Request = None):
    """删除表——同步四层 + 操作密码（删表不可逆）

    Query: ?drop_real_table=false 仅删除元数据保留实际表（默认 true 删除实际表）
    Body: {"operator_password": str}
    """
    from agent.management.deps import require_operator_password
    require_operator_password(request, body)
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    svc = _get_schema_graph_service()
    try:
        result = svc.delete_table(table_name, drop_real_table=drop_real_table)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("message", "删除失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除表失败: {e}")


@router.post("/api/schema-graph/table/{table_name}/delete/precheck")
def schema_graph_precheck_delete_table(table_name: str):
    """删表预检：影响面报告（行数/正向外键/反向引用计数）供前端确认弹窗，不执行删除"""
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    svc = _get_schema_graph_service()
    try:
        report = svc.delete_table_precheck(table_name)
        if not report.get("ok"):
            raise HTTPException(status_code=404, detail=report.get("message", "预检失败"))
        return {"ok": True, "report": report}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删表预检失败: {e}")


@router.post("/api/schema-graph/relationship/delete/precheck")
def schema_graph_precheck_delete_relationship(body: dict):
    """删外键预检：受影响行数供前端确认弹窗，不执行删除"""
    from_table = body.get("from_table", "")
    from_column = body.get("from_column", "")
    if not is_valid_identifier(from_table) or not is_valid_identifier(from_column):
        raise HTTPException(status_code=400, detail="非法表名或字段名")
    svc = _get_schema_graph_service()
    try:
        report = svc.delete_relationship_precheck(from_table, from_column)
        if not report.get("ok"):
            raise HTTPException(status_code=404, detail=report.get("message", "预检失败"))
        return {"ok": True, "report": report}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删外键预检失败: {e}")


@router.put("/api/schema-graph/table/{table_name}/position")
def schema_graph_update_position(table_name: str, body: dict):
    """更新表节点的画布坐标——仅 Ladybug 图库（高频拖拽操作，不同步其他层）

    Body: {x: float, y: float}
    """
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    try:
        x = float(body.get("x", 0))
        y = float(body.get("y", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="x, y 必须是数字")

    svc = _get_schema_graph_service()
    try:
        return svc.update_position(table_name, x, y)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新位置失败: {e}")


@router.put("/api/schema-graph/layout")
def schema_graph_update_layout(body: dict):
    """批量更新表节点画布坐标——整理布局一次提交，替代逐表 N+1 写请求

    Body: {positions: [{table, x, y, datasource(可选，保留扩展位)}, ...]}
    与单位置端点同一存储：复用 svc.update_position 逐条落库（仅 Ladybug 图库，
    不同步其他层）；单条失败不影响其余，failed 明细随响应返回。
    """
    positions = body.get("positions")
    if not isinstance(positions, list):
        raise HTTPException(status_code=400, detail="positions 必须是数组")

    svc = _get_schema_graph_service()
    updated = 0
    failed = []
    for item in positions:
        if not isinstance(item, dict):
            failed.append({"table": None, "message": "位置项必须是对象"})
            continue
        table_name = item.get("table", "")
        if not is_valid_identifier(table_name):
            failed.append({"table": table_name, "message": "非法表名"})
            continue
        try:
            x = float(item.get("x", 0))
            y = float(item.get("y", 0))
        except (TypeError, ValueError):
            failed.append({"table": table_name, "message": "x, y 必须是数字"})
            continue
        try:
            svc.update_position(table_name, x, y)
            updated += 1
        except Exception as e:
            failed.append({"table": table_name, "message": str(e)[:100]})
    return {"ok": not failed, "updated": updated, "failed": failed}


@router.post("/api/schema-graph/relationship")
def schema_graph_create_relationship(body: dict, request: Request):
    """创建外键关系——同步三层 + 实际 ALTER TABLE

    Body: {table_name, column_name, ref_table_name, ref_column_name(默认 id),
           operator_password（写皆密码）}
    """
    from agent.management.deps import require_operator_password
    require_operator_password(request, body)
    table_name = body.get("table_name", "")
    column_name = body.get("column_name", "")
    ref_table_name = body.get("ref_table_name", "")
    ref_column_name = body.get("ref_column_name", "id")

    for n in (table_name, column_name, ref_table_name, ref_column_name):
        if not is_valid_identifier(n):
            raise HTTPException(status_code=400, detail=f"非法标识符: {n}")

    svc = _get_schema_graph_service()
    try:
        result = svc.create_relationship(table_name, column_name, ref_table_name, ref_column_name)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("message", "创建外键失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建外键关系失败: {e}")


@router.delete("/api/schema-graph/relationship")
def schema_graph_delete_relationship(body: dict, request: Request):
    """删除外键关系——同步三层 + 操作密码（结构变更）

    Body: {table_name, column_name, operator_password}
    """
    from agent.management.deps import require_operator_password
    require_operator_password(request, body)
    table_name = body.get("table_name", "")
    column_name = body.get("column_name", "")
    if not is_valid_identifier(table_name) or not is_valid_identifier(column_name):
        raise HTTPException(status_code=400, detail="非法标识符")

    svc = _get_schema_graph_service()
    try:
        result = svc.delete_relationship(table_name, column_name)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("message", "删除外键失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除外键关系失败: {e}")


@router.get("/api/schema-graph/meta/search")
def schema_graph_search(q: str = ""):
    """搜索表名/字段名（利用 SQLite 索引，毫秒级响应）

    Query: ?q=关键字
    """
    if not q:
        return {"tables": [], "columns": []}
    svc = _get_schema_graph_service()
    try:
        return svc.search(q)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索失败: {e}")


@router.get("/api/schema-graph/verify")
def schema_graph_verify():
    """三层对账端点：YAML ↔ MetaDB ↔ Ladybug 图库漂移检测。

    ok=False 时 drift 各字段给出漂移内容（缺表/缺边/多余节点/写失败记录），
    dashboard 据此显示漂移告警。
    """
    svc = _get_schema_graph_service()
    return svc.verify_reconciliation()


@router.get("/api/schema-graph/meta/stats")
def schema_graph_stats():
    """获取统计信息——表数/字段数/关系数"""
    svc = _get_schema_graph_service()
    try:
        return svc.get_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取统计失败: {e}")


@router.get("/api/schema-graph/datasources")
def schema_graph_datasources():
    """列出所有已注册的联邦数据源——前端建表时选择目标数据库

    返回: [{name, type, is_default, description}, ...]
    """
    try:
        from core.datasource_manager import DataSourceManager
        dsm = DataSourceManager()
        dsm.load_config()
        return dsm.list_datasources()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取数据源列表失败: {e}")


@router.get("/api/schema-graph/check-templates")
def schema_graph_check_templates(col_type: str = Query("", alias="type")):
    """获取 CHECK 约束模板列表——按字段类型分类

    Query: ?type=INTEGER  返回该类型的模板列表
           无 type        返回所有模板的扁平列表（去重）
    """
    from core.check_templates import get_templates_by_type, get_all_templates_flat, normalize_type
    try:
        if col_type:
            nt = normalize_type(col_type)
            templates = get_templates_by_type(nt)
            return {"type": nt, "templates": templates}
        else:
            return {"type": "_all", "templates": get_all_templates_flat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取 CHECK 模板失败: {e}")


@router.post("/api/schema-graph/validate-check")
def schema_graph_validate_check(body: dict):
    """校验 CHECK 约束表达式安全性 + 双方言预演

    Body: {expr, col_name, col_type, table_columns?}
    Returns: {ok, message, dialects: {sqlite, mysql}}
    """
    from core.checks import validate_check_expr
    from core.check_templates import translate_for_dialect

    expr = (body.get("expr") or "").strip()
    col_name = body.get("col_name") or ""
    col_type = body.get("col_type") or ""
    table_columns = body.get("table_columns") or []

    if not is_valid_identifier(col_name) and col_name:
        raise HTTPException(status_code=400, detail="非法字段名")

    try:
        ok, msg = validate_check_expr(expr, col_name, col_type, table_columns)
        return {
            "ok": ok,
            "message": msg,
            "dialects": {
                "sqlite": translate_for_dialect(expr, "sqlite") if ok else "",
                "mysql": translate_for_dialect(expr, "mysql") if ok else "",
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"校验失败: {e}")


@router.post("/api/schema-graph/sync")
def schema_graph_sync():
    """从 YAML 同步到 SQLite + Ladybug 图库（启动时或手动触发）

    遍历当前行业的所有 schema YAML 文件，将缺失的表同步到元数据库。
    """
    svc = _get_schema_graph_service()
    try:
        return svc.sync_from_yaml()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"同步失败: {e}")


@router.get("/api/schema-graph/status")
def schema_graph_status():
    """图库状态端点（图库为嵌入式 Ladybug，进程内运行）

    返回: {enabled: 图库连接是否可用, pending: 是否预热中}
    嵌入式无外部服务懒启动语义，pending 恒 False（保留键位供前端轮询协议一致）。
    """
    from core.graph import LadybugStore
    return {"enabled": bool(LadybugStore.is_available()), "pending": False}
