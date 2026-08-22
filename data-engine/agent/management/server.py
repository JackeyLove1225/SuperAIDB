"""Management API 服务器——为前端控制台提供后端状态、指标和控制接口

运行在端口 2025
前端通过 http://localhost:2025/api/* 访问

端点按域拆分在 agent/management/routers/ 下（auth/preview/dashboard/tables/
datasources/backup_export/industry/schema_graph），本文件只负责应用组装：
app 创建、中间件、异常处理器、router 挂载与启动钩子。
共享的引导逻辑与辅助函数在 agent/management/deps.py。
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# deps 负责 sys.path 引导与日志捕获安装，须最先导入
from agent.management import deps  # noqa: F401
from agent.management.deps import settings
from agent.management.routers import (
    auth,
    preview,
    dashboard,
    tables,
    datasources,
    backup_export,
    industry,
    schema_graph,
    files,
    permissions as permissions_router,
    approvals,
    isolation,
)
from core.exceptions import AppError, RiskError, PrimaryKeyError, SecurityError

# 工具注册在组装点完成（评审四轮 P1）：core/health.py 只读注册表状态，
# 不再由健康检查副作用触发注册（core 反向 import agent 的耦合边消除）
import agent.tools  # noqa: F401

# 创建 FastAPI 应用
mgmt_app = FastAPI(
    title="SuperAIDB Management API",
    description="后端状态、指标和控制接口",
    version="1.0.0",
)

# CORS——来源从 settings.MGMT_CORS_ORIGINS 读（P2-9 配置化，不再写死）
mgmt_app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.mgmt_cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 全局异常处理器——确保 ContractDriver 抛出的契约异常能正确返回给前端 ──


@mgmt_app.exception_handler(SecurityError)
async def security_error_handler(request: Request, exc: SecurityError):
    """安全校验失败 → 400（客户端请求非法）"""
    return JSONResponse(status_code=400, content={"detail": exc.message, "ok": False})


@mgmt_app.exception_handler(PrimaryKeyError)
async def primary_key_error_handler(request: Request, exc: PrimaryKeyError):
    """主键保护 → 400（客户端试图操作 id 主键）"""
    return JSONResponse(status_code=400, content={"detail": exc.message, "ok": False})


@mgmt_app.exception_handler(RiskError)
async def risk_error_handler(request: Request, exc: RiskError):
    """破坏性变更需确认 → 409（Conflict，需 force=true 才能继续）"""
    return JSONResponse(
        status_code=409,
        content={"detail": exc.message, "ok": False, "report": exc.report},
    )


@mgmt_app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    """通用业务异常 → 400（数据库操作失败等）"""
    return JSONResponse(status_code=400, content={"detail": exc.message, "ok": False})

# 免认证路径（健康检查 + 认证端点本身供 StartupOverlay 轮询）
_MGMT_NO_AUTH_PATHS = {"/api/health", "/api/mcp-ok", "/api/auth/login", "/api/auth/register"}

# 签名媒体通道前缀（iframe/img/下载等浏览器原生资源无法带 Bearer；
# 带 sig 参数的中间件放行，端点内 fail-closed 验签）
_SIGNED_MEDIA_PREFIXES = ("/api/preview/pdf", "/api/files/raw", "/api/exports/")


@mgmt_app.middleware("http")
async def verify_api_key(request: Request, call_next):
    """API Key 认证 + 速率限制 + 指标收集中间件

    - API_KEY_ENABLED=false 时跳过 API Key 认证（本地开发模式）
    - 健康检查端点和认证端点免认证
    - OPTIONS 请求免认证（CORS 预检）
    - 所有请求都经过速率限制检查和指标收集
    """
    import time as _time
    from core.auth import get_rate_limiter
    from core.metrics import get_metrics_collector

    # 速率限制（所有请求都检查）
    client_ip = request.client.host if request.client else "unknown"
    allowed, limit_msg = get_rate_limiter().check(client_ip)
    if not allowed:
        # 记录 429 响应
        get_metrics_collector().record(
            request.method, request.url.path, 429, 0.1
        )
        return JSONResponse(
            status_code=429,
            content={"detail": limit_msg},
        )

    # 指标收集：记录请求开始时间
    start_time = _time.time()

    try:
        # 每请求显式归位角色上下文（contextvars 按 task 隔离，显式 set 防任何复用边界）
        from core.permission import set_current_role
        set_current_role("system")
        if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
            response = await call_next(request)
        else:
            # 免认证路径
            if request.url.path in _MGMT_NO_AUTH_PATHS:
                response = await call_next(request)
            elif request.method == "OPTIONS":
                response = await call_next(request)
            else:
                # 双模认证（20260804）：
                # ① Authorization: Bearer <token> → 用户身份，角色注入权限策略
                # ② X-API-Key → 系统级身份（兼容脚本/测试/内部调用），角色=system
                from core.auth import verify_token, verify_api_key as _verify_api_key
                payload = None
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    payload = verify_token(auth_header[7:])
                if payload:
                    set_current_role(payload.get("role") or "user")
                    response = await call_next(request)
                elif any(request.url.path.startswith(p) for p in _SIGNED_MEDIA_PREFIXES) \
                        and request.query_params.get("sig"):
                    # 签名媒体通道（评审五轮 S-3 资源面）：iframe/img/下载锚点无法携带
                    # Bearer——带 sig 参数的请求放行进端点，签名由端点 fail-closed 校验
                    response = await call_next(request)
                else:
                    # 检查 API Key
                    api_key = request.headers.get("X-API-Key")
                    if not _verify_api_key(api_key or ""):
                        duration_ms = (_time.time() - start_time) * 1000
                        get_metrics_collector().record(
                            request.method, request.url.path, 401, duration_ms
                        )
                        # 中间件里 raise HTTPException 不会被异常处理器捕获（会变成 500），
                        # 必须直接返回 JSONResponse
                        return JSONResponse(
                            status_code=401,
                            content={"detail": "未授权：请提供有效的 Bearer Token 或 API Key"},
                        )
                    response = await call_next(request)

        # 记录指标
        duration_ms = (_time.time() - start_time) * 1000
        get_metrics_collector().record(
            request.method, request.url.path, response.status_code, duration_ms
        )
        return response
    except HTTPException as e:
        # 记录 HTTP 异常
        duration_ms = (_time.time() - start_time) * 1000
        get_metrics_collector().record(
            request.method, request.url.path, e.status_code, duration_ms
        )
        raise
    except Exception as e:
        # 记录未捕获异常
        duration_ms = (_time.time() - start_time) * 1000
        get_metrics_collector().record(
            request.method, request.url.path, 500, duration_ms
        )
        raise


# ── 启动钩子 ──

@mgmt_app.on_event("startup")
def _init_auth():
    """启动时初始化 users 表"""
    from core.auth import init_users_table
    init_users_table()


@mgmt_app.on_event("startup")
def _disk_maintenance():
    """启动时执行磁盘保留策略（P2-10，失败不阻塞启动）"""
    try:
        from core.disk_maintenance import run_disk_maintenance
        run_disk_maintenance()
    except Exception:
        from core.logger import get_logger
        get_logger(__name__).warning("磁盘清理执行失败（不阻塞启动）", exc_info=True)


@mgmt_app.on_event("startup")
def _sync_schema_graph():
    """启动时同步 YAML → SQLite + Ladybug（表关系可视化图层）

    由 Management API 承担启动初始化：
    把当前行业的 schema YAML 同步到 SQLite 元数据表和 Ladybug 图库，
    保证画布数据就绪（幂等，失败不阻塞启动）。
    """
    try:
        from core.graph.schema_graph_service import SchemaGraphService
        SchemaGraphService.get_instance().sync_from_yaml()
    except Exception:
        from core.logger import get_logger
        get_logger(__name__).warning("Schema 图同步失败（不阻塞启动）", exc_info=True)


# ── 路由挂载（按域拆分于 agent/management/routers/）──

mgmt_app.include_router(auth.router)
mgmt_app.include_router(preview.router)
mgmt_app.include_router(dashboard.router)
mgmt_app.include_router(tables.router)
mgmt_app.include_router(datasources.router)
mgmt_app.include_router(backup_export.router)
mgmt_app.include_router(industry.router)
mgmt_app.include_router(schema_graph.router)
mgmt_app.include_router(files.router)
mgmt_app.include_router(permissions_router.router)
mgmt_app.include_router(approvals.router)
mgmt_app.include_router(isolation.router)
