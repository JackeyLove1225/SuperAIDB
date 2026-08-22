"""行业管理端点

行业 CRUD / 导出导入 / 切换 / 配置（config.yml）/ 提示词（prompts.yml）/
字段别名（fields.yml）/ 表结构 schemas CRUD / AI 配置向导。
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from core.contract.security_contract import is_valid_identifier
from agent.management.deps import _project_root

router = APIRouter()


def _require_admin(request: "Request | None") -> None:
    """写端点仅限 admin（security_review 修复，与 routers/permissions.py 同款）

    中间件只校验"有无合法凭据"（Bearer 任意角色 或 API Key=system），
    不校验角色——文件/配置级写端点此前普通 user 登录即可调用。
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


@router.get("/api/industries")
def api_list_industries():
    """列出所有可用行业"""
    from core.industry_manager import list_industries
    return {"industries": list_industries()}


@router.post("/api/industries/export/{industry_name}")
def api_export_industry(industry_name: str, request: Request = None):
    """导出行业配置为 ZIP——仅 admin"""
    _require_admin(request)
    from core.industry_manager import export_industry
    if not is_valid_identifier(industry_name):
        raise HTTPException(status_code=400, detail="非法行业名")
    result = export_industry(industry_name)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/api/industries/import")
def api_import_industry(body: dict, request: Request = None):
    """从 ZIP 导入行业配置——仅 admin

    Body: {"zip_path": str, "overwrite": bool (可选)}
    """
    _require_admin(request)
    from core.industry_manager import import_industry
    zip_path = body.get("zip_path", "")
    overwrite = body.get("overwrite", False)
    if not zip_path:
        raise HTTPException(status_code=400, detail="请提供 zip_path")
    result = import_industry(zip_path, overwrite=overwrite)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/api/industries/create")
def api_create_industry(body: dict, request: Request = None):
    """从模板创建新行业——仅 admin

    Body: {"name": str, "display_name": str, "description": str, "template": str}
    """
    _require_admin(request)
    from core.industry_manager import create_industry_from_template, DEFAULT_TEMPLATE
    name = body.get("name", "")
    if not is_valid_identifier(name):
        raise HTTPException(status_code=400, detail="行业名只能包含字母、数字、下划线")
    result = create_industry_from_template(
        name,
        display_name=body.get("display_name", ""),
        description=body.get("description", ""),
        template=body.get("template") or DEFAULT_TEMPLATE,
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.delete("/api/industries/{industry_name}")
def api_delete_industry(industry_name: str, request: Request = None):
    """删除行业（不允许删除内置行业）——仅 admin"""
    _require_admin(request)
    from core.industry_manager import delete_industry
    if not is_valid_identifier(industry_name):
        raise HTTPException(status_code=400, detail="非法行业名")
    result = delete_industry(industry_name)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/api/industries/exports")
def api_list_industry_exports():
    """列出所有已导出的行业配置 ZIP"""
    from core.industry_manager import list_exports
    return {"exports": list_exports()}


@router.get("/api/industries/exports/{filename}/download")
def api_download_industry_export(filename: str):
    """下载行业配置 ZIP 文件"""
    from fastapi.responses import FileResponse
    from core.industry_manager import INDUSTRIES_DIR, DEFAULT_TEMPLATE
    safe_name = Path(filename).name
    export_dir = INDUSTRIES_DIR.parent / "exports"
    filepath = export_dir / safe_name
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(filepath), filename=safe_name, media_type="application/zip")


# ── 行业配置管理端点（前端自助适配行业）──

def _get_industry_dir(industry_name: str) -> Path:
    """获取行业目录路径"""
    from core.industry_manager import INDUSTRIES_DIR
    p = INDUSTRIES_DIR / industry_name
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"行业 '{industry_name}' 不存在")
    return p


@router.post("/api/industries/switch")
def api_switch_industry(body: dict, request: Request = None):
    """切换当前行业——仅 admin

    修改 config/.env 中的 INDUSTRY 值，并重置所有缓存单例（热生效，无需重启服务）。
    """
    _require_admin(request)
    industry_name = body.get("industry", "").strip()
    if not industry_name or not is_valid_identifier(industry_name):
        raise HTTPException(status_code=400, detail="非法行业名")

    from core.industry_manager import INDUSTRIES_DIR
    if not (INDUSTRIES_DIR / industry_name).exists():
        raise HTTPException(status_code=404, detail=f"行业 '{industry_name}' 不存在")

    # 修改 config/.env（统一写通道；settings 热键使全行业切换免重启——
    # 本进程靠 override 立即生效，其他进程靠 .env 新鲜读取下次操作即生效）
    from core.config_hub import write_text_atomic
    env_path = Path(_project_root) / "data-engine" / "config" / ".env"
    if not env_path.exists():
        env_path = Path(_project_root) / "config" / ".env"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith("INDUSTRY="):
                lines[i] = f"INDUSTRY={industry_name}"
                found = True
                break
        if not found:
            lines.append(f"INDUSTRY={industry_name}")
        write_text_atomic(env_path, "\n".join(lines) + "\n", backup=True)

    # 更新 settings 单例（进程内覆盖，即时生效）
    try:
        from config.settings import settings
        settings.INDUSTRY = industry_name
    except Exception:
        pass

    # 重置缓存单例（U-9 状态卫生：全部走公开入口，不戳私有属性）
    try:
        from core.datasource_manager import DataSourceManager
        DataSourceManager.reset_instance()
        import core.data_ops as _data_ops
        _data_ops._federated_driver = None
    except Exception:
        pass

    # 重置行业注册表
    try:
        import industries.base as _base
        _base._industries.clear()
    except Exception:
        pass

    # MetaDB 重置（按行业独立文件，重建时指向新行业元数据库）
    try:
        from core.graph.meta_db import MetaDB
        MetaDB.reset_instance()
    except Exception:
        pass

    # SchemaGraphService 重置（其缓存的 MetaDB 引用同样按行业，漏重置会串台）
    try:
        from core.graph.schema_graph_service import SchemaGraphService
        SchemaGraphService.reset_instance()
    except Exception:
        pass

    # 会话态清理（暂存上下文+选择集引用旧行业表，必清）
    try:
        from core.context import get_context
        get_context().clear_all()
    except Exception:
        pass

    # 切换后自检：新行业声明的表在 DB 中是否存在（不存在≠错误——可能尚未建表，如实上报）
    selfcheck = {"tables_in_db": [], "tables_missing": []}
    try:
        from core.schema_matcher import _load_schemas
        from core.data_ops import _get_driver
        _drv = _get_driver()
        for t in _load_schemas():
            name = t.get("name", "")
            if not name:
                continue
            (selfcheck["tables_in_db"] if _drv.table_exists(name)
             else selfcheck["tables_missing"]).append(name)
    except Exception as e:
        selfcheck["error"] = str(e)[:100]

    return {"ok": True,
            "message": f"已切换到行业 '{industry_name}'（热生效，无需重启）",
            "selfcheck": selfcheck}


@router.get("/api/industries/{industry_name}/config")
def api_get_industry_config(industry_name: str):
    """获取行业完整配置（config.yml + prompts.yml 概要 + 表列表）"""
    p = _get_industry_dir(industry_name)
    import yaml

    # config.yml
    config_data = {}
    config_path = p / "config" / "config.yml"
    if config_path.exists():
        config_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # prompts.yml
    prompts_data = {}
    prompts_path = p / "prompts" / "prompts.yml"
    if prompts_path.exists():
        prompts_data = yaml.safe_load(prompts_path.read_text(encoding="utf-8")) or {}

    # schemas 列表
    schema_dir = p / "schemas"
    schemas = []
    if schema_dir.exists():
        for sf in sorted(schema_dir.glob("*.yaml")):
            try:
                sd = yaml.safe_load(sf.read_text(encoding="utf-8")) or {}
                schemas.append({
                    "name": sd.get("name", sf.stem),
                    "business_name": sd.get("business_name", ""),
                    "description": sd.get("description", ""),
                    "datasource": sd.get("datasource", ""),
                    "column_count": len(sd.get("columns", [])),
                })
            except Exception:
                continue

    return {
        "config": config_data,
        "prompts": {
            "classification_hints": prompts_data.get("classification_hints", ""),
            "decompose_examples": prompts_data.get("decompose_examples", []),
            "router_examples": prompts_data.get("router_examples", []),
            "tool_examples": prompts_data.get("tool_examples", {}),
        },
        "schemas": schemas,
    }


@router.put("/api/industries/{industry_name}/config")
def api_update_industry_config(industry_name: str, body: dict, request: Request = None):
    """更新行业基本配置（config.yml）——仅 admin"""
    _require_admin(request)
    p = _get_industry_dir(industry_name)
    import yaml

    config_path = p / "config" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有配置，合并更新
    existing = {}
    if config_path.exists():
        existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # 允许更新的字段
    for key in ["name", "description", "expert_role", "hierarchy_desc", "default_table_name"]:
        if key in body:
            existing[key] = body[key]

    config_path.write_text(
        yaml.dump(existing, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8"
    )
    return {"ok": True, "message": f"行业 '{industry_name}' 配置已更新"}


@router.get("/api/industries/{industry_name}/prompts")
def api_get_industry_prompts(industry_name: str):
    """获取行业 AI 提示词配置（prompts.yml）"""
    p = _get_industry_dir(industry_name)
    import yaml

    prompts_path = p / "prompts" / "prompts.yml"
    prompts_data = {}
    if prompts_path.exists():
        prompts_data = yaml.safe_load(prompts_path.read_text(encoding="utf-8")) or {}

    return {"prompts": prompts_data}


@router.put("/api/industries/{industry_name}/prompts")
def api_update_industry_prompts(industry_name: str, body: dict, request: Request = None):
    """更新行业 AI 提示词配置（prompts.yml）——仅 admin

    支持更新 decompose_examples、router_examples、tool_examples、classification_hints
    """
    _require_admin(request)
    p = _get_industry_dir(industry_name)
    import yaml

    prompts_path = p / "prompts" / "prompts.yml"
    prompts_path.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有配置
    existing = {}
    if prompts_path.exists():
        existing = yaml.safe_load(prompts_path.read_text(encoding="utf-8")) or {}

    # 合并更新（支持部分更新）——覆盖文件处理相关提示词与 few-shot 示例
    for key in [
        "classification_hints", "schema_hints",
        "decompose_examples", "router_examples", "tool_examples",
        "terminology",
    ]:
        if key in body:
            existing[key] = body[key]

    # custom_prompts（含 extraction_prompt / extraction_rules）作为嵌套 dict 合并更新
    if "custom_prompts" in body:
        cp = existing.get("custom_prompts") or {}
        if not isinstance(cp, dict):
            cp = {}
        for k, v in body["custom_prompts"].items():
            cp[k] = v
        existing["custom_prompts"] = cp

    prompts_path.write_text(
        yaml.dump(existing, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8"
    )
    return {"ok": True, "message": f"行业 '{industry_name}' 提示词配置已更新"}


@router.get("/api/industries/{industry_name}/fields")
def api_get_industry_fields(industry_name: str):
    """获取行业字段别名配置（fields.yml）"""
    p = _get_industry_dir(industry_name)
    import yaml

    fields_path = p / "fields" / "fields.yml"
    fields_data = {}
    if fields_path.exists():
        fields_data = yaml.safe_load(fields_path.read_text(encoding="utf-8")) or {}

    return {"fields": fields_data}


@router.put("/api/industries/{industry_name}/fields")
def api_update_industry_fields(industry_name: str, body: dict, request: Request = None):
    """更新行业字段别名配置（fields.yml）——仅 admin"""
    _require_admin(request)
    p = _get_industry_dir(industry_name)
    import yaml

    fields_path = p / "fields" / "fields.yml"
    fields_path.parent.mkdir(parents=True, exist_ok=True)

    fields_data = body.get("fields", body)
    fields_path.write_text(
        yaml.dump(fields_data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8"
    )
    return {"ok": True, "message": f"行业 '{industry_name}' 字段别名已更新"}


# ============================================================
# 表结构（schemas）CRUD API——前端可视化编辑器使用
# ============================================================

def _get_schemas_dir(industry_name: str) -> Path:
    """获取行业 schemas 目录"""
    p = _get_industry_dir(industry_name) / "schemas"
    p.mkdir(parents=True, exist_ok=True)
    return p


@router.get("/api/industries/{industry_name}/schemas")
def api_list_schemas(industry_name: str):
    """列出行业所有表结构定义"""
    import yaml
    p = _get_schemas_dir(industry_name)
    tables = []
    for f in sorted(p.glob("*.yaml")) + sorted(p.glob("*.yml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            tables.append({
                "name": data.get("name", f.stem),
                "business_name": data.get("business_name", ""),
                "description": data.get("description", ""),
                "column_count": len(data.get("columns", [])),
                "fk_count": len(data.get("foreign_keys", [])),
                "filename": f.name,
            })
        except Exception:
            tables.append({"name": f.stem, "filename": f.name, "error": "解析失败"})
    return {"schemas": tables, "count": len(tables)}


@router.get("/api/industries/{industry_name}/schemas/{table_name}")
def api_get_schema(industry_name: str, table_name: str):
    """获取单张表的完整结构定义"""
    import yaml
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    p = _get_schemas_dir(industry_name)
    # 优先 yaml，其次 yml
    for ext in [".yaml", ".yml"]:
        f = p / f"{table_name}{ext}"
        if f.exists():
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            return {"schema": data, "filename": f.name}
    raise HTTPException(status_code=404, detail=f"表 '{table_name}' 不存在")


@router.put("/api/industries/{industry_name}/schemas/{table_name}")
def api_update_schema(industry_name: str, table_name: str, body: dict, request: Request = None):
    """创建或更新表结构定义——仅 admin

    body 格式：
    {
        "name": "表名",
        "business_name": "业务名称",
        "description": "描述",
        "columns": [{"name":"id","type":"INTEGER","is_pk":true,"autoincrement":true}, ...],
        "foreign_keys": [{"columns":["region_id"],"references":"region","ref_columns":["id"]}, ...],
        "indexes": [{"name":"idx_xxx","columns":["xxx"],"unique":true}, ...]
    }
    """
    _require_admin(request)
    import yaml
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")

    # 校验并规范化 schema
    schema = body.get("schema", body)
    if not isinstance(schema, dict):
        raise HTTPException(status_code=400, detail="schema 必须是对象")

    # 确保 name 字段与路径一致
    schema["name"] = table_name

    # 校验列定义
    columns = schema.get("columns", [])
    if not columns:
        raise HTTPException(status_code=400, detail="表至少需要一个字段")
    for col in columns:
        if not is_valid_identifier(col.get("name", "")):
            raise HTTPException(status_code=400, detail=f"非法字段名: {col.get('name')}")

    # 校验外键引用
    for fk in schema.get("foreign_keys", []):
        ref_table = fk.get("references", "")
        if not is_valid_identifier(ref_table):
            raise HTTPException(status_code=400, detail=f"非法外键引用表名: {ref_table}")

    p = _get_schemas_dir(industry_name)
    # 删除可能存在的旧文件（yaml/yml）
    for ext in [".yaml", ".yml"]:
        old = p / f"{table_name}{ext}"
        if old.exists():
            old.unlink()
    # 写入新文件
    f = p / f"{table_name}.yaml"
    f.write_text(
        yaml.dump(schema, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8"
    )
    return {"ok": True, "message": f"表 '{table_name}' 结构已保存", "filename": f.name}


@router.delete("/api/industries/{industry_name}/schemas/{table_name}")
def api_delete_schema(industry_name: str, table_name: str, request: Request = None):
    """删除表结构定义——仅 admin"""
    _require_admin(request)
    if not is_valid_identifier(table_name):
        raise HTTPException(status_code=400, detail="非法表名")
    p = _get_schemas_dir(industry_name)
    deleted = False
    for ext in [".yaml", ".yml"]:
        f = p / f"{table_name}{ext}"
        if f.exists():
            f.unlink()
            deleted = True
    if not deleted:
        raise HTTPException(status_code=404, detail=f"表 '{table_name}' 不存在")
    return {"ok": True, "message": f"表 '{table_name}' 已删除"}


# ============================================================
