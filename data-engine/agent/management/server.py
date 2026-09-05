"""Management API 服务器——为前端控制台提供后端状态、指标和控制接口

运行在端口 2025
前端通过 http://localhost:2025/api/* 访问

端点按域拆分在 agent/management/routers/ 下（auth/preview/dashboard/tables/
datasources/backup_export/industry/schema_graph），本文件只负责应用组装：
app 创建、中间件、异常处理器、router 挂载与启动钩子。
共享的引导逻辑与辅助函数在 agent/management/deps.py。
"""
import time as _time

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
    unrecognized as unrecognized_router,
)
from core.exceptions import AppError, RiskError, PrimaryKeyError, SecurityError

# 工具注册在组装点完成：core/health.py 只读注册表状态，
# 不再由健康检查副作用触发注册（core 反向 import agent 的耦合边消除）
import agent.tools  # noqa: F401

# 创建 FastAPI 应用
mgmt_app = FastAPI(
    title="SuperAIDB Management API",
    description="后端状态、指标和控制接口",
    version="1.0.0",
)

# CORS——来源从 settings.MGMT_CORS_ORIGINS 读（配置化，不写死）
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


async def _auth_enabled_response(request, call_next, start_time):
    """认证模式（API_KEY_ENABLED=true）分派：免认证路径 / OPTIONS /
    Bearer / 签名媒体 四路——返回最终响应或 401 JSONResponse

    X-API-Key 系统通道已废除（20260903）：该通道曾让持有 API_KEY 的脚本
    直接获得 system 角色（≈admin 等效、跳过全部角色/用户/自助规则），
    是绕过用户权限体系的旁门。脚本/测试请走真实用户通道——注册测试
    专用账号 + 登录换取 Bearer token（见 tests/_mgmt_auth.py）。
    """
    from core.auth import verify_token
    from core.permission import set_current_role
    # 免认证路径
    if request.url.path in _MGMT_NO_AUTH_PATHS:
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    # 唯一身份通道：Authorization: Bearer <token> → 用户身份，角色注入权限策略
    payload = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = verify_token(auth_header[7:])
    if payload:
        set_current_role(payload.get("role") or "user")
        from core.permission import set_current_user
        set_current_user(payload.get("username") or "")  # 用户级/自助规则的消费点
        return await call_next(request)
    if any(request.url.path.startswith(p) for p in _SIGNED_MEDIA_PREFIXES) \
            and request.query_params.get("sig"):
        # 签名媒体通道：iframe/img/下载锚点无法携带
        # Bearer——带 sig 参数的请求放行进端点，签名由端点 fail-closed 校验
        return await call_next(request)
    duration_ms = (_time.time() - start_time) * 1000
    from core.metrics import get_metrics_collector
    get_metrics_collector().record(
        request.method, request.url.path, 401, duration_ms
    )
    # 中间件里 raise HTTPException 不会被异常处理器捕获（会变成 500），
    # 必须直接返回 JSONResponse
    return JSONResponse(
        status_code=401,
        content={"detail": "未授权：请提供有效的 Bearer Token"},
    )


@mgmt_app.middleware("http")
async def verify_api_key(request: Request, call_next):
    """API Key 认证 + 速率限制 + 指标收集中间件

    - API_KEY_ENABLED=false 时跳过 API Key 认证（本地开发模式）
    - 健康检查端点和认证端点免认证
    - OPTIONS 请求免认证（CORS 预检）
    - 所有请求都经过速率限制检查和指标收集
    """
    from core.auth import get_rate_limiter
    from core.metrics import get_metrics_collector

    # 速率限制分层：爆破敏感端点（登录/注册/改密/自助规则）60 次/分/IP 严限；
    # 其余 API 流量 600 次/分（本地 SPA 页面加载连发是合法行为——曾把
    # "改库+刷新"的正常爆发打成 429，前端误判未登录隐藏用户区）
    client_ip = request.client.host if request.client else "unknown"
    _BRUTE_PATHS = ("/api/auth/login", "/api/auth/register", "/api/auth/change-password",
                    "/api/auth/my-rules")
    limiter = get_rate_limiter()
    if request.url.path in _BRUTE_PATHS:
        allowed, limit_msg = limiter.check(client_ip)
    else:
        allowed, limit_msg = limiter.check(client_ip, limit=600)
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
            response = await _auth_enabled_response(request, call_next, start_time)

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
    except Exception:
        # 记录未捕获异常
        duration_ms = (_time.time() - start_time) * 1000
        get_metrics_collector().record(
            request.method, request.url.path, 500, duration_ms
        )
        raise


# 浏览器防伪中间件：默认桌面模式无认证——CORS 白名单只挡
# 跨域"读取"，不挡 no-cors 简单请求的副作用（任意恶意网页可 POST /api/stop
# 关停整套后端——攻击者能力远低于已声明的威胁模型却能造成完整可用性打击）。
# 判定：写方法（GET/HEAD/OPTIONS 以外）且声明了跨站来源即拒；
# 无 Origin/Sec-Fetch-Site（curl/本地脚本/同源导航）放行——本机正常使用零影响。
@mgmt_app.middleware("http")
async def anti_csrf_middleware(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sfs = (request.headers.get("sec-fetch-site") or "").lower()
        if sfs == "cross-site":
            return JSONResponse(status_code=403,
                                content={"detail": "跨站请求已拒绝（浏览器防伪）",
                                         "ok": False})
        origin = request.headers.get("origin", "")
        if origin:
            from urllib.parse import urlparse
            try:
                host = (urlparse(origin).hostname or "").lower()
            except ValueError:
                host = ""  # 畸形 Origin（如方括号不配对）按跨站拒（fail-closed）
            if host not in ("localhost", "127.0.0.1", "::1"):
                return JSONResponse(status_code=403,
                                    content={"detail": f"非常地来源已拒绝（Origin: {host or '畸形'}）",
                                             "ok": False})
    return await call_next(request)

# ── 启动钩子 ──

# 敏感面默认拒：本地无密码模式下，一切写方法
#（POST/PUT/DELETE/PATCH）要求本机回环令牌——按端点枚举防护会漏掉
# 整个数据/DDL 面（schema_graph 删表/tables 行级写/files 上传/preview 转换
# 若无闸，默认部署下本机任意进程 curl 即可 DROP 真实表）。
# 豁免：登录/注册（认证机制本身）+ 读方法。前端代理在服务端注入令牌，
# 浏览器不可见；本机其他进程读不到 config/runtime/loopback.token（0600）。
_NO_AUTH_OPEN_WRITE = frozenset({"/api/auth/login", "/api/auth/register"})


@mgmt_app.middleware("http")
async def loopback_gate_middleware(request: Request, call_next):
    if settings.API_KEY_ENABLED.lower() in ("true", "1", "yes"):
        return await call_next(request)  # 认证模式走正常 RBAC/认证链
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)
    if request.url.path in _NO_AUTH_OPEN_WRITE:
        return await call_next(request)
    from agent.management.deps import check_loopback
    try:
        check_loopback(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code,
                            content={"detail": e.detail, "ok": False})
    return await call_next(request)

@mgmt_app.on_event("startup")
def _init_auth():
    """启动时初始化 users 表"""
    from core.auth import init_users_table
    init_users_table()
    # 回环令牌启动期轮换重铸（每次后端启动旧令牌即失效——
    # 与 daemon 令牌每次启动重写同标准；懒铸会让首个敏感请求 403 一次）
    try:
        from agent.management.deps import mint_loopback_token
        mint_loopback_token()
    except Exception:
        from core.logger import get_logger
        get_logger(__name__).warning("回环令牌重铸失败（首个敏感请求将懒铸自愈）",
                                     exc_info=True)


@mgmt_app.on_event("startup")
def _disk_maintenance():
    """启动时执行磁盘保留策略（失败不阻塞启动）"""
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


@mgmt_app.on_event("startup")
def _resume_saga_journals():
    """启动时续滚未补偿的 saga（崩溃续滚闭环——机制有实现必有调用方）

    上次进程在跨库 saga 中途崩溃时，db/saga_journal/ 留有
    failed_uncompensated journal；本钩子按逆序补偿收拾干净。
    结果如实记日志（每个续滚项成功/失败+原因），失败不阻塞启动。
    """
    try:
        from core.federation.saga import Saga
        from core.logger import get_logger
        results = Saga.resume_pending()
        for r in results:
            if r.get("ok"):
                get_logger(__name__).warning("saga 崩溃续滚成功: %s", r["saga_id"])
            else:
                get_logger(__name__).error(
                    "saga 崩溃续滚失败: %s — %s（journal 保留，下次启动重试）",
                    r["saga_id"], r.get("error", ""))
    except Exception:
        from core.logger import get_logger
        get_logger(__name__).warning("saga 崩溃续滚执行失败（不阻塞启动）", exc_info=True)


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
mgmt_app.include_router(unrecognized_router.router)
