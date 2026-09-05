"""Management API 共享依赖——sys.path 引导、设置对象、日志捕获与通用辅助函数

routers/ 下的各路由模块只依赖本模块（以及 core/config 包），
server.py 负责应用组装，避免循环依赖。
"""

import os
import sys
import time
from core.logger import get_logger
from pathlib import Path
from fastapi import Request  # _require_user 签名注解用

# 模块级 logger
logger = get_logger(__name__)

# 确保项目根目录在 sys.path 中
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 隔离运行环境可能缺少 chromadb 依赖
_system_site = os.path.join(sys.base_prefix, 'Lib', 'site-packages')
if os.path.isdir(_system_site) and _system_site not in sys.path:
    sys.path.append(_system_site)

from config.settings import settings  # noqa: E402
from agent.management.log_handler import install_log_capture  # noqa: E402

# 安装日志捕获
install_log_capture()

# 启动时间
_start_time = time.time()

# ── 辅助函数 ──

_LOOPBACK_TOKEN_PATH = (_project_root and Path(_project_root) / "config" / "runtime" / "loopback.token")


def _loopback_token() -> str:
    """本机回环令牌：启动期轮换重铸、落 config/runtime/loopback.token、
    0600 权限收紧（对齐自家 daemon 令牌标准——
    daemon/runtime.py 每次启动重写令牌 + chmod 0o600；懒铸+永久有效
    +继承 ACL 会造成同仓两套口径）。

    用途：本地无密码模式（API_KEY_ENABLED=false）下，审批/权限写/备份/停机等
    敏感面不能被跨站网页/随手 curl 裸调（恶意网页 no-cors POST 直接打穿
    localhost 写面）。令牌只经前端 Next.js 代理在服务端注入（浏览器不可见）。
    效力边界如实说：同用户 shell 进程读得到令牌文件（0600 尽力而为）——
    防同用户本地进程的硬保证由系统级隔离模式（独立服务账号 + ACL）承接，
    提权契约另有 HMAC 验签兜底（core/permission/policy.py）。
    """
    from core.file_contract import JsonContract
    c = JsonContract(_LOOPBACK_TOKEN_PATH)
    tok = (c.read() or {}).get("token", "")
    if not tok:
        tok = mint_loopback_token()
    return tok


def mint_loopback_token() -> str:
    """重铸回环令牌（启动期轮换：每次后端启动旧令牌即失效）"""
    import secrets as _secrets
    from core.file_contract import JsonContract
    tok = _secrets.token_urlsafe(32)
    JsonContract(_LOOPBACK_TOKEN_PATH).write(
        {"token": tok, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    try:
        import os as _os
        _os.chmod(_LOOPBACK_TOKEN_PATH, 0o600)  # 仅属主读写（对齐 daemon 令牌）
    except OSError:
        pass  # Windows ACL 语义下 chmod 尽力而为（隔离模式有 ACL 收紧兜底）
    return tok


def check_loopback(request) -> None:
    """本地无密码模式的敏感面防伪闸：要求 X-Loopback-Token 与落盘令牌一致

    request=None（进程内直接调用/测试）放行——不经 HTTP 面无伪造面。
    API_KEY_ENABLED=true 时本闸不参与（走正常认证）。
    令牌比对用常量时间比较（与 daemon 令牌同标准，防计时侧信道）。
    """
    if request is None:
        return
    import secrets as _secrets
    from fastapi import HTTPException
    got = request.headers.get("X-Loopback-Token", "")
    if not got or not _secrets.compare_digest(got, _loopback_token()):
        raise HTTPException(
            status_code=403,
            detail="敏感操作需要本机回环令牌（经管理控制台前端访问自动携带）；"
                   "直接调用请开启 API_KEY_ENABLED 并携带合法凭据")


def _get_driver():
    """获取数据库驱动实例"""
    try:
        from core.data_ops import get_driver as get_drv
        return get_drv()
    except Exception:
        return None


def _get_vector_store():
    """获取向量数据库实例"""
    try:
        from core.vector_store import get_vector_store as get_vs
        return get_vs()
    except Exception:
        return None


def _get_db_path() -> str:
    """获取 SQLite 数据库文件路径"""
    path = settings.SQLITE_DB_PATH
    if not os.path.isabs(path):
        path = os.path.join(_project_root, path)
    return path


def _get_chroma_path() -> str:
    """获取 ChromaDB 数据目录"""
    path = settings.CHROMA_PATH
    if not os.path.isabs(path):
        path = os.path.join(_project_root, path)
    return path


def _dir_size_mb(path: str) -> float:
    """计算目录总大小（MB）"""
    if not os.path.isdir(path):
        return 0.0
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass  # 单文件取大小失败则不计入（体积统计尽力而为）
    return round(total / 1024 / 1024, 2)


def _get_cached_dir_size(path: str, ttl: int = 60) -> float:
    """带 TTL 缓存的目录大小计算（避免频繁 os.walk 大目录）"""
    if not hasattr(_get_cached_dir_size, "_cache"):
        _get_cached_dir_size._cache = {}
    now = time.time()
    cached = _get_cached_dir_size._cache.get(path)
    if cached and now - cached[1] < ttl:
        return cached[0]
    size = _dir_size_mb(path)
    _get_cached_dir_size._cache[path] = (size, now)
    return size


def _format_uptime(seconds: int) -> str:
    """格式化运行时间"""
    if seconds < 60:
        return f"{seconds}秒"
    elif seconds < 3600:
        return f"{seconds // 60}分{seconds % 60}秒"
    elif seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}小时{m}分"
    else:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        return f"{d}天{h}小时"


def _require_user(request: "Request | None") -> None:
    """读/取回端点至少要求登录用户（readonly 拒）——自助注册的
    user 不得下载 admin 导出的全表 CSV（列权限的二次扩散面）。
    request=None：进程内直接调用（测试/内部），不经 HTTP 闸，放行。
    签名媒体通道（sig 由端点 fail-closed 验过才到角色判定）视为 user 级已认证。
    X-API-Key 系统通道已废除（20260903）——脚本/测试走真实用户 Bearer。"""
    from fastapi import HTTPException
    from core.auth import verify_token
    from config.settings import settings
    if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
        return  # 本地开发模式不强制
    if request is None:
        return
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = verify_token(auth_header[7:])
        if not payload:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
        if payload.get("role") == "readonly":
            raise HTTPException(status_code=403, detail="只读角色不可执行此操作")
        return
    if request.query_params.get("sig"):
        return  # 签名媒体通道（端点已验签）
    raise HTTPException(status_code=401, detail="未授权：需要登录")


def _upload_root() -> Path:
    """上传文件根目录（uploads；首次使用自动创建）——routers 共享实现
    （P-F：曾散在 files.py 被 preview.py 跨路由私引）"""
    root = Path(getattr(settings, "UPLOAD_DIR", "") or "uploads")
    if not root.is_absolute():
        root = Path(_project_root) / root
    return root


def require_operator_password(request: "Request | None", body: dict) -> None:
    """管理端写操作的人因第二因子：body 必须携带**当前登录用户本人**的密码
    （谁的会话谁确认——users 表慢哈希比对，按身份分桶连续失败锁定）。
    无用户身份的通道（API Key/系统）回退为任一 admin 密码。
    验证通过后本进程持有 10 分钟能力凭证：该进程是可信应用代码，
    后续契约层直调闸（drop_table 等）由此放行；凭证不出进程、不落盘。"""
    from fastapi import HTTPException as _HE
    from core.operator_gate import unlock as _unlock
    # 从 Bearer token 解析当前用户身份（无/无效 token → 空串走 admin 回退）
    username = ""
    if request is not None:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            from core.auth import verify_token
            payload = verify_token(auth_header[7:])
            if payload:
                username = payload.get("username", "")
    pwd = str((body or {}).get("operator_password", ""))
    if not _unlock(pwd, username=username):
        raise _HE(status_code=403,
                  detail="操作密码错误或未提供（本操作需要输入操作密码）")
