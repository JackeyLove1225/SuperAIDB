"""系统级数据隔离端点（三期产品化）：状态探测 / 一键开启 / 一键关闭——admin 专属

开启/关闭是特权动作（建服务账号/注册服务级任务）：经 UAC 提权一次后由
 scripts/isolation_setup.ps1 完成，此后开机自启、用户零操作。
状态探测无提权（只读查询）。
"""
import json
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "isolation_setup.ps1"


def _require_admin(request: Request) -> None:
    """与 permissions.py 同款：Bearer 必须 admin；API Key（system）等同 admin；
    本地开发模式（API_KEY_ENABLED=false）不强制。"""
    from core.auth import verify_token, verify_api_key
    from config.settings import settings
    if settings.API_KEY_ENABLED.lower() not in ("true", "1", "yes"):
        return
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = verify_token(auth_header[7:])
        if not payload:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可操作数据隔离")
        return
    api_key = request.headers.get("X-API-Key")
    if api_key and verify_api_key(api_key):
        return
    raise HTTPException(status_code=401, detail="未授权：需要 admin 凭据")


def _run_script(mode: str) -> dict:
    import sys
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(_SCRIPT), "-Mode", mode, "-PythonExe", sys.executable],
        capture_output=True, text=True, timeout=60)
    out = (r.stdout or "").strip().splitlines()
    try:
        return json.loads(out[-1]) if out else {"ok": False, "message": r.stderr[:300]}
    except json.JSONDecodeError:
        return {"ok": r.returncode == 0, "message": (out[-1] if out else r.stderr[:300])}


@router.get("/api/isolation/status")
def isolation_status(request: Request):
    """隔离状态（active=服务账号+任务已注册；daemon_as_service=daemon 正以服务账号运行）"""
    _require_admin(request)
    if not _SCRIPT.exists():
        return {"active": False, "daemon_as_service": False, "available": False}
    return {**_run_script("status"), "available": True}


@router.post("/api/isolation/switch")
def isolation_switch(request: Request, body: dict):
    """开启/关闭系统级数据隔离（admin；UAC 提权一次后全自动）。

    提权方式：拉起一个提权的 PowerShell 子进程执行安装脚本（Windows 弹 UAC
    授权框，用户点"是"即完成）。异步执行——前端随后轮询 status 即可。
    """
    _require_admin(request)
    enable = bool(body.get("enable", True))
    if not _SCRIPT.exists():
        raise HTTPException(status_code=500, detail="隔离脚本缺失: scripts/isolation_setup.ps1")
    mode = "enable" if enable else "disable"
    # Start-Process -Verb RunAs 触发系统 UAC 授权框；安装日志落 logs/isolation_setup.log。
    # 传 -PythonExe（当前进程真实解释器）——提权环境 PATH 不同，脚本内
    # Get-Command python 可能命中商店别名存根（评审四轮 R5 断链修复）
    import sys
    log_file = Path(__file__).resolve().parents[3] / "logs" / "isolation_setup.log"
    ps = ("Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "
          f"'-NoProfile','-ExecutionPolicy','Bypass','-File',''{_SCRIPT}'',''-Mode',''{mode}'',"
          f"'-PythonExe',''{sys.executable}'' "
          f"*>> ''{log_file}''")
    subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"ok": True,
            "message": ("已请求系统授权（UAC）——请在弹出的窗口点「是」完成"
                        + ("开启" if enable else "关闭") + "，稍后刷新状态查看"),
            "pending": True}
