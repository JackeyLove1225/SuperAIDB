"""服务健康检查——唯一实现点

deps.py / dashboard.py / launcher.py 的 urllib 检查收敛到这里。

check_http_ok 供 management/launcher.py 端口探测调用；check_mcp_health 供
management/routers/dashboard.py 的 /api/health 与仪表盘接口调用；
check_langgraph_health 仅为兼容旧部署保留（:2024 无独立服务进程）。
"""
import time as _time
from config.settings import settings


def check_http_ok(url: str, timeout: float = 3.0) -> bool:
    """HTTP GET 健康检查：200 返回 True，其余一律 False

    显式空代理（ProxyHandler({})）：urllib 默认读系统代理，企业代理环境
    会拦截 127.0.0.1 请求造成"服务已停止"误判。
    """
    import urllib.request
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, method="GET")
        with opener.open(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_langgraph_health(timeout: float = 3.0) -> bool:
    """兼容健康检查（:2024 /ok 端点）

    :2024 无独立服务进程，保留仅为兼容旧部署。
    """
    return check_http_ok(f"http://127.0.0.1:{settings.LANGGRAPH_PORT}/ok", timeout)


_mcp_health_cache: dict = {"result": None, "time": 0.0}


def check_mcp_health(timeout: float = 3.0) -> bool:
    """MCP 能力面健康检查——stdio 无端口，探测方式为快速加载工具注册表

    30 秒缓存，避免仪表盘轮询时反复 import。
    """
    now = _time.time()
    if _mcp_health_cache["result"] is not None and now - _mcp_health_cache["time"] < 30:
        return _mcp_health_cache["result"]
    ok = False
    try:
        # 只读注册表状态（注册在各进程组装点完成：mcp_server.py / management/server.py）
        from core.tool_registry import get_tools
        ok = len(get_tools()) > 0
    except Exception:
        ok = False
    _mcp_health_cache["result"] = ok
    _mcp_health_cache["time"] = now
    return ok
