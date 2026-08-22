"""权限管理端点：规则读取 / 校验写入（ConfigHub 原子写）/ 规则演练

全部经 X-API-Key 保护（server 统一中间件）。保存即生效（ConfigHub mtime 热生效），
不需要重启任何进程。
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from core.permission import PermissionPolicy, Operation, PermissionDenied

router = APIRouter()

_OPS = {op.value for op in Operation}
_MODES = {"full", "read_only", "custom"}


def _policy() -> PermissionPolicy:
    return PermissionPolicy.get_instance()


def _perm_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "permissions.yml"


def _require_admin(request: "Request") -> None:
    """权限管理端点仅限 admin（security_review 修复，20260809）

    中间件只校验"有无合法凭据"（Bearer 任意角色 或 API Key=system），
    不校验角色——此前普通 user 登录即可改权限规则（自提权漏洞）。
    本依赖强制：Bearer 必须是 admin；API Key（system）等同 admin
    （system 是可信系统级身份，见 server 中间件注释）。
    """
    from fastapi import HTTPException
    from core.auth import verify_token, verify_api_key
    from config.settings import settings
    if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
        return  # 本地开发模式不强制
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = verify_token(auth_header[7:])
        if not payload:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可访问权限管理")
        return
    api_key = request.headers.get("X-API-Key")
    if api_key and verify_api_key(api_key):
        return  # API Key = system 身份，等同 admin
    raise HTTPException(status_code=401, detail="未授权：需要 admin 凭据")


# ── 读取 ──

@router.get("/api/permissions")
def get_permissions(request: Request):
    """当前规则快照 + UI 渲染选项（数据源/表/列清单）——仅 admin"""
    _require_admin(request)
    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()
    datasources = []
    for ds in dsm.list_datasources():
        tables = []
        try:
            drv = dsm.get_driver(ds["name"])
            tables = sorted(drv.list_tables())
        except Exception:
            pass
        columns = {}
        for t in tables:
            try:
                columns[t] = [c["name"] for c in drv.get_columns(t)]
            except Exception:
                columns[t] = []
        datasources.append({
            "name": ds["name"], "type": ds["type"],
            "is_default": ds["is_default"], "tables": tables, "columns": columns,
        })
    return {
        "rules": _policy().describe(),
        "datasources": datasources,
        "operations": sorted(_OPS),
    }


# ── 写入（结构校验 + 试载 + 原子写 + 自动备份）──

def _validate_rules(rules: dict) -> None:
    if not isinstance(rules, dict):
        raise HTTPException(status_code=400, detail="规则必须是对象")
    default = rules.get("default", "full")
    if default not in ("full", "read_only"):
        raise HTTPException(status_code=400, detail=f"default 只能是 full/read_only: {default}")
    for scope_name, scope_rules in (rules.get("datasources") or {}).items():
        _validate_scope(f"datasources.{scope_name}", scope_rules)
        for tname, trules in (scope_rules.get("tables") or {}).items():
            _validate_scope(f"datasources.{scope_name}.tables.{tname}", trules)
            for cname, crules in (trules.get("columns") or {}).items():
                if not isinstance(crules, dict):
                    raise HTTPException(status_code=400, detail=f"列规则必须是对象: {cname}")
                _validate_op_list(f"列 {cname} 的 deny", crules.get("deny"))
    for rname, rrules in (rules.get("roles") or {}).items():
        if not isinstance(rrules, dict):
            raise HTTPException(status_code=400, detail=f"角色规则必须是对象: {rname}")
        if rrules.get("allow") is not None and rrules.get("deny") is not None:
            raise HTTPException(status_code=400, detail=f"角色 {rname} 的 allow/deny 不能同时配置")
        _validate_op_list(f"角色 {rname} 的 allow", rrules.get("allow"))
        _validate_op_list(f"角色 {rname} 的 deny", rrules.get("deny"))
        # 角色内表级规则（用户级专属权限：roles.<role>.tables.<表>[.columns.<列>]）
        for tname, trules in (rrules.get("tables") or {}).items():
            _validate_scope(f"角色 {rname}.tables.{tname}", trules)
            for cname, crules in (trules.get("columns") or {}).items():
                if not isinstance(crules, dict):
                    raise HTTPException(status_code=400, detail=f"角色 {rname} 列规则必须是对象: {cname}")
                _validate_op_list(f"角色 {rname} 列 {cname} 的 deny", crules.get("deny"))


def _validate_scope(where: str, scope: dict) -> None:
    if not isinstance(scope, dict):
        raise HTTPException(status_code=400, detail=f"{where} 规则必须是对象")
    mode = scope.get("mode")
    if mode is not None and mode not in _MODES:
        raise HTTPException(status_code=400, detail=f"{where}.mode 非法: {mode}（可选 {sorted(_MODES)}）")
    if scope.get("allow") is not None and scope.get("deny") is not None:
        raise HTTPException(status_code=400, detail=f"{where} 的 allow/deny 不能同时配置")
    _validate_op_list(f"{where} 的 allow", scope.get("allow"))
    _validate_op_list(f"{where} 的 deny", scope.get("deny"))


def _validate_op_list(where: str, ops) -> None:
    if ops is None:
        return
    if not isinstance(ops, list) or not all(isinstance(o, str) for o in ops):
        raise HTTPException(status_code=400, detail=f"{where} 必须是字符串数组")
    bad = [o for o in ops if o not in _OPS]
    if bad:
        raise HTTPException(status_code=400, detail=f"{where} 含非法操作 {bad}（可选 {sorted(_OPS)}）")


@router.put("/api/permissions")
def put_permissions(body: dict, request: Request):
    """写入权限规则：结构校验 → 试载 → 原子写（自动备份）→ 立即生效。仅 admin"""
    _require_admin(request)
    rules = body.get("rules")
    if rules is None:
        raise HTTPException(status_code=400, detail="缺少 rules 字段")
    _validate_rules(rules)
    # 试载：用临时实例验证规则可被策略层消费（fail-closed 防线）
    try:
        PermissionPolicy.new_instance(rules)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"规则试载失败: {e}")
    from core.config_hub import write_yaml_atomic
    write_yaml_atomic(_perm_path(), rules, backup=True)
    return {"ok": True, "message": "权限规则已保存并立即生效（无需重启）"}


# ── 规则演练 ──

@router.post("/api/permissions/test")
def test_permission(body: dict, request: Request):
    """干跑一条权限判定：{datasource, table?, op, role?} → 允许/禁止 + 命中层级。仅 admin"""
    _require_admin(request)
    ds = body.get("datasource") or ""
    op = body.get("op") or ""
    table = body.get("table") or ""
    role = body.get("role") or ""
    if not ds:
        raise HTTPException(status_code=400, detail="缺少 datasource")
    if op not in _OPS:
        raise HTTPException(status_code=400, detail=f"op 非法: {op}（可选 {sorted(_OPS)}）")
    pol = _policy()
    operation = Operation(op)
    # 角色先行
    try:
        if role:
            pol._check_role(role, operation)
    except PermissionDenied as e:
        return {"allowed": False, "scope": "角色级", "reason": str(e)}
    mode, allow, deny, scope = pol._resolve(ds, table)
    allowed = pol._allowed(mode, allow, deny, operation)
    reason = ""
    if not allowed:
        reason = (f"数据源 '{ds}'" + (f" 的表 '{table}'" if table else "")
                  + f"（{scope}规则）权限不足：禁止 {op} 操作"
                  + f"（{mode} 模式{pol._mode_hint(mode, allow, deny)}）")
    return {"allowed": allowed, "scope": scope, "mode": mode, "reason": reason}


# ── 备份管理 ──

@router.get("/api/permissions/backups")
def list_backups(request: Request):
    """列出权限规则的自动备份（倒序）——仅 admin"""
    _require_admin(request)
    bdir = _perm_path().parent / "backups"
    if not bdir.exists():
        return {"backups": []}
    files = sorted((p.name for p in bdir.glob("permissions_*.yml")), reverse=True)
    return {"backups": files[:20]}


@router.post("/api/permissions/restore")
def restore_backup(body: dict, request: Request):
    """回滚到指定备份——仅 admin"""
    _require_admin(request)
    name = body.get("name") or ""
    if not name or "/" in name or ".." in name:
        raise HTTPException(status_code=400, detail="非法备份名")
    src = _perm_path().parent / "backups" / name
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"备份不存在: {name}")
    import yaml as _yaml
    try:
        data = _yaml.safe_load(src.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"备份文件损坏: {e}")
    _validate_rules(data)
    from core.config_hub import write_yaml_atomic
    write_yaml_atomic(_perm_path(), data, backup=True)
    return {"ok": True, "message": f"已回滚到 {name} 并立即生效"}
