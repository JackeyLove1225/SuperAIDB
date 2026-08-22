"""联邦数据库：数据源管理与联邦状态端点"""

from fastapi import APIRouter, HTTPException, Request

from core.contract.security_contract import safe_table_sql

router = APIRouter()


def _require_admin(request: "Request | None") -> None:
    """写端点仅限 admin（security_review 修复，与 routers/permissions.py 同款）

    中间件只校验"有无合法凭据"（Bearer 任意角色 或 API Key=system），
    不校验角色——配置级写端点此前普通 user 登录即可调用。
    本依赖强制：Bearer 必须是 admin；API Key（system）等同 admin
    （system 是可信系统级身份，见 server 中间件注释）。
    request=None：进程内直接调用（测试/内部），不经 HTTP 闸，放行。
    """
    from fastapi import HTTPException
    from core.auth import verify_token, verify_api_key
    from config.settings import settings
    if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
        return  # 本地开发模式不强制
    if request is None:
        return  # 进程内直接调用（测试/内部），非 HTTP 入口
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = verify_token(auth_header[7:])
        if not payload:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
        return
    api_key = request.headers.get("X-API-Key")
    if api_key and verify_api_key(api_key):
        return  # API Key = system 身份，等同 admin
    raise HTTPException(status_code=401, detail="未授权：需要 admin 凭据")


@router.get("/api/datasources")
def list_datasources():
    """列出所有已注册的数据源"""
    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()
    dsm.load_config()
    return {"datasources": dsm.list_datasources()}


@router.get("/api/datasources/{name}/test")
def test_datasource(name: str):
    """测试数据源连接"""
    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()
    dsm.load_config()
    return dsm.test_connection(name)


@router.get("/api/datasources/{name}/tables")
def list_datasource_tables(name: str):
    """列出指定数据源的所有表"""
    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()
    dsm.load_config()
    try:
        drv = dsm.get_driver(name)
        tables = drv.list_tables()
        # 获取每张表的行数
        result = []
        for tname in tables:
            try:
                rows = drv.query(f'SELECT COUNT(*) as c FROM {safe_table_sql(tname)}')
                count = rows[0]["c"] if rows else 0
            except Exception:
                count = -1
            result.append({"name": tname, "rows": count})
        return {"datasource": name, "tables": result, "total": len(result)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取表列表失败: {e}")


@router.post("/api/datasources/reload")
def reload_datasources(request: Request = None):
    """重新加载数据源配置（热重载，修改 datasources.yml 后调用）——仅 admin"""
    _require_admin(request)
    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()
    dsm.reload_config()
    # 重新注册表映射
    from core.schema_matcher import _load_schemas
    _load_schemas()
    return {
        "status": "ok",
        "message": "数据源配置已重新加载",
        "datasources": dsm.list_datasources(),
    }


@router.get("/api/federation/status")
def federation_status():
    """联邦数据库状态概览"""
    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()
    dsm.load_config()
    ds_list = dsm.list_datasources()

    # 统计表分布
    from core.schema_matcher import _load_schemas
    schemas = _load_schemas()
    table_distribution = {}
    for s in schemas:
        ds_name = s.get("datasource", dsm.get_default_name())
        table_distribution.setdefault(ds_name, []).append(s["name"])

    return {
        "enabled": len(ds_list) > 1,
        "datasource_count": len(ds_list),
        "default_datasource": dsm.get_default_name(),
        "datasources": ds_list,
        "table_distribution": {
            ds: {"tables": tables, "count": len(tables)}
            for ds, tables in table_distribution.items()
        },
        "total_tables": len(schemas),
    }
