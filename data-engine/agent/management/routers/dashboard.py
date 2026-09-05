"""系统状态与控制台聚合端点

健康检查、指标、运行状态、数据库概览、日志、配置、Dashboard 聚合、
向量库概览、停止服务、前端可见设置。
"""

import os
import sys
import time
import platform
import threading
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from core.contract.security_contract import safe_table_sql
from agent.management.log_handler import get_log_buffer
from agent.management.deps import (
    settings,
    _project_root,
    _start_time,
    _get_driver,
    _get_vector_store,
    _get_db_path,
    _get_chroma_path,
    _dir_size_mb,
    _get_cached_dir_size,
    _format_uptime,
)

router = APIRouter()


def _require_admin(request: "Request | None") -> None:
    """写端点仅限 admin（security_review 修复，与 routers/permissions.py 同款）

    中间件只校验"有无合法凭据"（Bearer 任意角色），不校验角色——
    系统级/配置级写端点若普通 user 登录即可调用即成越权。
    本依赖强制：Bearer 必须是 admin。
    X-API-Key 系统通道已废除（20260903）——脚本/测试走真实用户 Bearer。
    request=None：进程内直接调用（测试/内部），不经 HTTP 闸，放行。
    """
    from fastapi import HTTPException
    from core.auth import verify_token
    from config.settings import settings
    if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
        return
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
    raise HTTPException(status_code=401, detail="未授权：需要 admin 凭据")


@router.get("/api/health")
def health_check():
    """健康检查——前端用于探测后端是否在线"""
    return {"status": "ok", "service": "management-api", "timestamp": datetime.now().isoformat()}


@router.get("/api/mcp-ok")
def check_mcp():
    """轻量级 MCP 能力面状态检查（工具注册表加载探测，响应快）

    供前端状态展示轮询使用，避免 /api/dashboard 的 2-3 秒延迟
    """
    try:
        from core.health import check_mcp_health
        return {"ok": check_mcp_health(timeout=3)}
    except Exception:
        return {"ok": False}


@router.get("/api/metrics")
def get_metrics():
    """获取系统监控指标——请求计数/响应时间/错误率/告警"""
    from core.metrics import get_metrics_collector
    return get_metrics_collector().get_summary()


@router.post("/api/metrics/reset")
def reset_metrics(request: Request = None):
    """重置监控指标——仅 admin"""
    _require_admin(request)
    from core.metrics import get_metrics_collector
    get_metrics_collector().reset()
    return {"status": "ok", "message": "指标已重置"}


@router.get("/api/status")
def get_status():
    """后端运行状态——进程信息、资源占用、服务状态"""
    import psutil

    process = psutil.Process()
    uptime = int(time.time() - _start_time)

    # 检查 MCP 能力面是否可用（30秒缓存，工具注册表加载探测）
    from core.health import check_mcp_health
    mcp_ok = check_mcp_health()

    # 内存信息
    mem_info = process.memory_info()
    vm = psutil.virtual_memory()

    return {
        "status": "running",
        "pid": process.pid,
        "uptime_seconds": uptime,
        "uptime_human": _format_uptime(uptime),
        "memory_mb": round(mem_info.rss / 1024 / 1024, 1),
        # interval=None：非阻塞，返回上次调用后的平均值（首次返回 0）
        "cpu_percent": process.cpu_percent(interval=None),
        "thread_count": process.num_threads(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "services": {
            "mcp_server": {"transport": "stdio", "status": "ready" if mcp_ok else "error"},
            "management_api": {"port": settings.MGMT_PORT, "status": "running"},
        },
        "system": {
            # interval=None：非阻塞
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_total_gb": round(vm.total / 1024 / 1024 / 1024, 1),
            "memory_used_gb": round(vm.used / 1024 / 1024 / 1024, 1),
            "memory_percent": vm.percent,
        },
    }


@router.get("/api/logs")
def get_logs(limit: int = 50):
    """获取最近 N 条系统日志"""
    buffer = get_log_buffer()
    return {
        "logs": buffer.get_recent(limit),
        # O(1) 计数，避免原先 buffer.get_recent(9999) 的全量拷贝
        "total": buffer.count(),
    }


@router.post("/api/logs/clear")
def clear_logs(request: Request = None):
    """清空日志缓冲——仅 admin"""
    _require_admin(request)
    buffer = get_log_buffer()
    buffer.clear()
    return {"status": "ok", "message": "日志已清空"}


@router.get("/api/config")
def get_config():
    """当前系统配置（脱敏——不返回 API Key）"""
    return {
        "ai": {
            "model": settings.AI_MODEL,
            "base_url": settings.AI_BASE_URL,
            "api_key_configured": bool(settings.AI_API_KEY),
        },
        "database": {
            "type": "sqlite",
            "path": _get_db_path(),
        },
        "vector_store": {
            "type": settings.VECTOR_STORE_TYPE,
            "path": _get_chroma_path(),
        },
        "industry": settings.INDUSTRY,
    }


def _dashboard_cache_lookup(now: float):
    """TTL 缓存（15 秒）命中返回聚合响应（日志实时、uptime 动态），未命中返回 None"""
    cache_ttl = 15
    if (hasattr(get_dashboard, "_cache")
            and now - get_dashboard._cache_time < cache_ttl):
        cached = get_dashboard._cache
        # 日志实时获取（不缓存）
        buffer = get_log_buffer()
        return {
            **cached,
            "logs": buffer.get_recent(20),
            "status": {
                **cached["status"],
                "uptime_seconds": int(now - _start_time),
                "uptime_human": _format_uptime(int(now - _start_time)),
            },
        }
    return None


def _dashboard_service_probes():
    """服务探测：MCP 能力面状态 + 前端端口直连（0.5s 超时），返回 (mcp_ok, frontend_ok)"""
    # MCP 能力面状态（30 秒缓存，工具注册表加载探测）
    from core.health import check_mcp_health
    mcp_ok = check_mcp_health()

    # 前端状态（TCP 直连 + settings 端口——urllib 读系统代理会误判，
    # 写死 3000 在改 FRONTEND_PORT 后永假；与 launcher._wait_for_port 同标准）
    frontend_ok = False
    try:
        import socket as _sock
        with _sock.create_connection(("127.0.0.1", settings.FRONTEND_PORT), timeout=0.5):
            frontend_ok = True
    except OSError:
        pass  # 前端端口探测失败=未就绪，如实显示未就绪

    return mcp_ok, frontend_ok


def _dashboard_storage_info():
    """存储面聚合：数据库表/行数/体积 + 向量库集合/向量数（单项失败仅降级该项）"""
    # 数据库
    db_info = {"tables": [], "table_count": 0, "total_rows": 0, "db_size_mb": 0}
    drv = _get_driver()
    if drv:
        try:
            table_names = drv.list_tables()
            tables = []
            for tname in table_names:
                try:
                    rows = drv.query(f'SELECT COUNT(*) as c FROM {safe_table_sql(tname)}')
                    count = rows[0]["c"] if rows else 0
                except Exception:
                    count = -1
                tables.append({"name": tname, "rows": count})
            db_info = {
                "tables": tables,
                "table_count": len(tables),
                "total_rows": sum(t["rows"] for t in tables if t["rows"] > 0),
                "db_size_mb": round(os.path.getsize(_get_db_path()) / 1024 / 1024, 2)
                    if os.path.exists(_get_db_path()) else 0,
            }
        except Exception:
            pass  # 磁盘/体积统计失败则不展示该项（面板信息降级，非数据面）

    # 向量数据库（chroma_size_mb 缓存 60s，避免频繁 os.walk）
    chroma_size = _get_cached_dir_size(_get_chroma_path(), ttl=60)
    vector_info = {"collections": [], "total_collections": 0, "total_vectors": 0,
                   "chroma_size_mb": chroma_size}
    vs = _get_vector_store()
    if vs:
        try:
            col_names = vs.list_collections()
            collections = []
            for name in col_names:
                try:
                    count = vs.count(name)
                except Exception:
                    count = -1
                collections.append({"name": name, "count": count})
            vector_info = {
                "collections": collections,
                "total_collections": len(collections),
                "total_vectors": sum(c["count"] for c in collections if c["count"] > 0),
                "chroma_size_mb": chroma_size,
            }
        except Exception:
            pass  # 向量库统计失败则不展示该项（同上，面板降级）

    return db_info, vector_info


def _dashboard_schema_drift():
    """Schema 三层漂移摘要：漂移存在时 dashboard 显示告警（对账失败不阻塞）"""
    schema_drift = {"ok": True, "checked": False}
    try:
        from core.graph.schema_graph_service import SchemaGraphService
        _drift = SchemaGraphService.get_instance().verify_reconciliation()
        schema_drift = {
            "ok": _drift.get("ok", True),
            "checked": True,
            "graph_available": _drift.get("graph_available", False),
            "items": {k: v for k, v in _drift.items()
                      if k.endswith("not_in_yaml") or k.endswith("not_in_meta")
                      or k.endswith("not_in_graph")},
            "write_failure_count": len(_drift.get("write_failures", [])),
        }
    except Exception:
        pass  # 对账失败不阻塞 dashboard
    return schema_drift


@router.get("/api/dashboard")
def get_dashboard():
    """聚合数据——一次请求获取控制台所需的全部数据

    性能优化：
    - TTL 缓存 15 秒（避免前端频繁刷新触发重负载聚合）
    - cpu_percent 用 interval=None（非阻塞，返回上次采样后的平均值）
    - Frontend 健康检查超时缩短到 0.5s
    - 日志单独获取（不缓存，保持实时性）
    """
    import psutil

    # ── TTL 缓存（15 秒）──
    now = time.time()
    cached_response = _dashboard_cache_lookup(now)
    if cached_response is not None:
        return cached_response

    process = psutil.Process()
    uptime = int(now - _start_time)

    mcp_ok, frontend_ok = _dashboard_service_probes()
    db_info, vector_info = _dashboard_storage_info()
    schema_drift = _dashboard_schema_drift()

    # 缓存聚合结果（不含日志和动态 uptime）
    cached_payload = {
        "status": {
            "running": True,
            "pid": process.pid,
            "uptime_seconds": uptime,
            "uptime_human": _format_uptime(uptime),
            "memory_mb": round(process.memory_info().rss / 1024 / 1024, 1),
            # interval=None：非阻塞，返回上次调用后的平均值（首次返回 0）
            "cpu_percent": process.cpu_percent(interval=None),
            "mcp_ok": mcp_ok,
            "frontend_ok": frontend_ok,
        },
        "database": db_info,
        "vector_store": vector_info,
        "schema_drift": schema_drift,
    }
    get_dashboard._cache = cached_payload
    get_dashboard._cache_time = now

    # 日志实时获取
    buffer = get_log_buffer()
    recent_logs = buffer.get_recent(20)

    return {**cached_payload, "logs": recent_logs}


@router.get("/api/vector/collections")
def get_vector_collections():
    """向量数据库概览——所有集合 + 每集合向量数"""
    vs = _get_vector_store()
    if not vs:
        return {"collections": [], "total_collections": 0, "total_vectors": 0,
                "chroma_path": _get_chroma_path(), "chroma_size_mb": 0,
                "message": "向量数据库未初始化"}

    collections = []
    try:
        col_names = vs.list_collections()
        for name in col_names:
            try:
                count = vs.count(name)
            except Exception:
                count = -1
            collections.append({"name": name, "count": count})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询向量数据库失败: {e}")

    total_vectors = sum(c["count"] for c in collections if c["count"] > 0)
    chroma_path = _get_chroma_path()
    chroma_size = _dir_size_mb(chroma_path)

    return {
        "collections": collections,
        "total_collections": len(collections),
        "total_vectors": total_vectors,
        "chroma_path": chroma_path,
        "chroma_size_mb": chroma_size,
    }


@router.post("/api/stop")
def stop_all_services(request: Request = None):
    """停止所有服务（Management API + Frontend + Launcher）——仅 admin

    异步执行：先返回 HTTP 响应，再杀进程（避免响应未发出就被杀）
    """
    _require_admin(request)

    def _do_stop():
        time.sleep(0.5)  # 等待 HTTP 响应返回客户端
        # 唯一实现收口：端口/PID 双通道 + 身份校验 + daemon 退场全部
        # 在 launcher.stop()；此处不再手抄一份（曾缺 daemon 清理+枚举放大）
        from agent.management.launcher import stop as _launcher_stop
        _launcher_stop()

    threading.Thread(target=_do_stop, daemon=True).start()
    return {"status": "ok", "message": "正在停止所有服务..."}


# ── 设置管理 ──

# 可被前端修改的设置项白名单（key → .env 中的变量名）
_SETTINGS_WHITELIST = {
    "frontend_dev_mode": "FRONTEND_DEV_MODE",
}


def _read_env_file() -> dict:
    """读取 config/.env 返回 {KEY: value} 字典"""
    env_file = Path(_project_root) / "config" / ".env"
    result = {}
    if not env_file.exists():
        return result
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip()
    return result


def _write_env_value(env_key: str, new_value: str) -> bool:
    """修改 config/.env 中某个变量的值，若不存在则追加

    保留注释和原有顺序。返回是否成功。
    """
    env_file = Path(_project_root) / "config" / ".env"
    if not env_file.exists():
        return False

    lines = env_file.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.upper().startswith(f"{env_key}="):
            lines[i] = f"{env_key}={new_value}"
            found = True
            break
    if not found:
        lines.append(f"{env_key}={new_value}")

    from core.config_hub import write_text_atomic
    write_text_atomic(env_file, "\n".join(lines) + "\n")  # 原子写且权限不降级（0600 保持）
    return True


@router.get("/api/settings")
def get_settings():
    """获取前端可见的系统设置

    只返回白名单中的设置项，避免泄露敏感信息（如 API Key）。
    """
    env = _read_env_file()
    return {
        "frontend_dev_mode": env.get("FRONTEND_DEV_MODE", "false").lower()
        in ("true", "1", "yes"),
        # 预留：后续可扩展更多设置项
        "industry": env.get("INDUSTRY", settings.INDUSTRY),
        "ai_model": env.get("AI_MODEL", ""),
    }


@router.post("/api/settings")
def update_settings(payload: dict, request: Request = None):
    """更新系统设置（修改 config/.env）——仅 admin

    目前支持：
    - frontend_dev_mode (bool): 前端开发模式开关

    注意：修改 .env 后需要重启后端才能生效。
    """
    _require_admin(request)
    updated = []
    for setting_key, env_key in _SETTINGS_WHITELIST.items():
        if setting_key not in payload:
            continue
        value = payload[setting_key]
        # 布尔值转字符串
        if isinstance(value, bool):
            new_val = "true" if value else "false"
        else:
            new_val = str(value)
        if _write_env_value(env_key, new_val):
            updated.append(setting_key)

    if not updated:
        raise HTTPException(status_code=400, detail="没有可更新的设置项")

    return {
        "status": "ok",
        "message": "设置已保存，重启后端生效",
        "updated": updated,
        "restart_required": True,
    }
