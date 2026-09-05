"""后端进程管理器——无窗口启动 Management API + Frontend

启动方式：
  桌面快捷方式 → pythonw.exe agent/management/launcher.py  （无任何窗口）
  调试模式     → python.exe agent/management/launcher.py    （有日志输出）

启动后写入 PID 文件（.backend_pids），供 stop.bat 和 /api/stop 端点使用
"""

import os
import sys
import time
import shutil
import subprocess
import json
import threading
import webbrowser
from pathlib import Path
from datetime import datetime

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PID_FILE = PROJECT_ROOT / ".backend_pids"

# 是否无控制台模式（pythonw.exe 启动时 sys.stdout 为 None）
_SILENT = sys.stdout is None

# 从 pythonw.exe 找到 python.exe
_python_exe = sys.executable
if _python_exe.endswith("pythonw.exe"):
    _python_exe = _python_exe.replace("pythonw.exe", "python.exe")

# Windows 进程创建标志：无窗口
_CREATE_NO_WINDOW = 0x08000000
# Windows 进程创建标志：脱离父进程的 Job Object（POSIX 无此值，取 0 不影响）
# 用于服务进程逃逸 MCP 客户端的 KILL_ON_JOB_CLOSE job（详见 _spawn_service_process）
_BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)

# 端口定义（Mgmt/Frontend 端口从 settings 读，可用 .env 覆盖；
# :2024 无独立服务进程，此常量仅为 status/health 兼容保留）
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from config.settings import settings as _svc_settings

PORT_LANGGRAPH = _svc_settings.LANGGRAPH_PORT  # :2024 无独立服务进程，保留定义兼容 status/health 逻辑
PORT_MGMT = _svc_settings.MGMT_PORT
PORT_FRONTEND = _svc_settings.FRONTEND_PORT


def _log(msg: str):
    """日志输出——始终写 backend.log；控制台可见时同时打印到 stderr

    始终落盘的原因：原设计"无窗口写文件 / 有窗口打印"的假设在
    stdout 被重定向到管道时会失效（pythonw + 管道 → sys.stdout 非
    None → 只打印不落盘，backend.log 断档）。管道断裂后 print 还会
    抛 OSError——曾把托盘退出回调第一行炸死（异常被 pystray 窗口
    过程静默吞掉，表现为"点退出没反应"），并让监控线程静默消失。
    日志是运维通道，任何失败都不允许波及调用方。

    打印走 stderr（不走 stdout）：launcher 被 MCP 同步人审桥进程内
    调用时，本进程 stdout 是 MCP stdio 协议通道——print 到 stdout
    会污染 JSON-RPC 流（客户端逐行解析报错）。stderr 在控制台同样
    可见，用户体验零变化。
    """
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        with open(PROJECT_ROOT / "backend.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # 日志写失败不阻断启动（运维通道降级，非数据面）
    if not _SILENT:
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass  # stderr 是断掉的管道（重定向宿主已退出）——不能炸调用方


def _http_identity_ok(port: int, timeout: float = 3.0,
                      marker: bytes = b'"management-api"') -> bool:
    """HTTP 身份校验：GET 且响应含服务标识才算"本服务活着"。

    纯 TCP 只证明"端口有人听"：外部进程占用端口时假绿、看门狗永不自愈。
    裸 socket 手写 HTTP：urllib 默认读系统代理，
    企业代理环境会拦截 127.0.0.1（同一标准贯彻到
    就绪/存活两面）。marker=None 时只要求合法 HTTP 响应（用于前端端口）。
    """
    import socket as _sock
    try:
        with _sock.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            path = "/api/health" if marker else "/"
            s.sendall(f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
            s.settimeout(timeout)
            buf = b""
            try:
                while len(buf) < 262144:
                    data = s.recv(65536)
                    if not data:
                        break
                    buf += data
                    if marker and marker in buf:
                        return True
            except OSError:
                pass  # 读超时也按已收内容判定
        if marker:
            return marker in buf
        return buf.startswith(b"HTTP/1.")
    except OSError:
        return False


def _wait_for_port(port: int, timeout: int = 30, frontend: bool = False) -> bool:
    """等待服务就绪——HTTP 身份校验（实现见 _http_identity_ok）

    用 127.0.0.1 而非 localhost：避免 PowerShell/某些环境将 localhost 解析为
    IPv6 (::1)，而服务只监听 IPv4 127.0.0.1 导致检测失败。
    """
    marker = None if frontend else b'"management-api"'
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _http_identity_ok(port, timeout=2, marker=marker):
            return True
        time.sleep(0.5)
    return False


def _write_pid_file(backend_pids: list[int], frontend_pid: int | None = None):
    """写入 PID 文件

    backend_pids: Management API 的 PID（watch() 监控这些）
    frontend_pid: 前端 dev server 的 PID（不监控，因为 npm/pnpm 进程行为不同）
    """
    all_pids = list(backend_pids)
    if frontend_pid:
        all_pids.append(frontend_pid)
    data = {
        "pids": all_pids,
        "backend_pids": backend_pids,
        "frontend_pid": frontend_pid,
        "started_at": datetime.now().isoformat(),
        "python_exe": _python_exe,
        "launcher_pid": os.getpid(),
    }
    try:
        PID_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        _log(f"警告: 写入 PID 文件失败: {e}")


def _clear_pid_file():
    """删除 PID 文件"""
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass  # PID 残留清理失败无碍——下次启动按锁/PID 校验回收


_LAUNCHER_LOCK = PROJECT_ROOT / ".launcher.lock"


def _acquire_single_instance_lock() -> bool:
    """单实例锁：拿到返回 True；检测到存活的已有实例返回 False

    文件锁 + PID 身份校验（防 PID 复用误判）：
    - open('x') 原子创建——两个实例同时双击只一个能拿到锁
    - 锁已存在时校验其中 PID：进程存活且命令行含 launcher.py → 真有实例在跑；
      进程已死 / PID 被复用为其他程序 → 残留锁，删除重试一次
    """
    for _ in range(2):
        try:
            with open(_LAUNCHER_LOCK, "x", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            return True
        except FileExistsError:
            alive = False
            try:
                old_pid = int(_LAUNCHER_LOCK.read_text(encoding="utf-8").strip())
                info = _get_process_info(old_pid)
                alive = bool(info and "launcher.py" in info)
            except Exception:
                pass  # 旧锁读不出/解析不了按残留处理（下方删除重试一次）
            if alive:
                return False
            try:
                _LAUNCHER_LOCK.unlink()
            except Exception:
                pass  # 删残留锁失败：重试次数已尽，留给下次启动回收
    return False


def _release_single_instance_lock():
    """释放单实例锁（仅当锁里是自己的 PID，防误删新实例的锁）"""
    try:
        if _LAUNCHER_LOCK.exists() and \
                _LAUNCHER_LOCK.read_text(encoding="utf-8").strip() == str(os.getpid()):
            _LAUNCHER_LOCK.unlink()
    except Exception:
        pass  # 释放失败无碍——锁内含 PID，下次启动身份校验后回收


def _prep_service_log(name: str) -> Path:
    """准备子服务日志路径（logs/ 目录不存在则创建；>10MB 时滚动为 .1 旧档）

    返回路径供两种打开方共用：本进程句柄（Popen 重定向）或 WMI 中转
    进程自行打开（Job 逃逸路径，见 _spawn_via_wmi）。
    滚动策略：启动时超 10MB 即归档（append 无上限曾致长跑膨胀）
    """
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / name
    if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
        try:
            old = log_path.with_suffix(log_path.suffix + ".1")
            old.unlink(missing_ok=True)
            log_path.replace(old)
        except OSError:
            pass  # 滚动失败则直接追加写，旧档留存交给下次滚动
    return log_path


def _open_service_log(name: str):
    """打开子服务日志文件句柄（Popen stdout/stderr 重定向用）

    打开失败时回退 DEVNULL（不阻止服务启动）
    """
    try:
        return open(_prep_service_log(name), "a", encoding="utf-8")
    except Exception as e:
        _log(f"  [警告] 无法打开日志文件 logs/{name}: {e}，该服务日志将被丢弃")
        return subprocess.DEVNULL


class _DetachedProcHandle:
    """WMI 逃逸拉起的服务进程句柄替身——只持有真实 PID，无进程对象

    launcher 后续只消费 .pid（启动日志/PID 文件）；进程存活性检查
    走 PID 文件 + 端口探测（watch），不依赖本句柄，poll() 恒视为运行中。
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return None  # 无句柄可查——真实死亡由端口检查/看门狗兜底

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        _log(f"  [提示] PID {self.pid} 为 WMI 逃逸进程，无句柄直控——由 stop() 端口清理兜底")


# WMI 中转代理（单行 -c 代码，字符串一律单引号——外层命令行用双引号包裹，
# 内部不得再出现双引号；参数经 base64 传递，规避引号/空格/中文的全套转义地狱）
_BROKER_CODE = (
    "import sys,json,base64,subprocess;"
    "p=json.loads(base64.b64decode(sys.argv[1]));"
    "f=open(p['log'],'ab');"
    "q=subprocess.Popen(p['args'],cwd=p['cwd'],env=p['env'],"
    "stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT,"
    f"creationflags={_CREATE_NO_WINDOW});"
    "open(p['pid_out'],'w').write(str(q.pid));"
    "f.close()"
)


def _spawn_via_wmi(args, cwd, env, log_path: Path) -> _DetachedProcHandle:
    """经 WMI 中转拉起子进程——逃逸限制性 Job Object 的通路

    Windows Job 规则：job 内进程的后代自动继承 job 成员身份；
    CREATE_BREAKAWAY_FROM_JOB 需 job 显式允许——MCP 客户端（mcp SDK）
    的 KILL_ON_JOB_CLOSE job 不允许，直接 Popen 无法逃逸。
    WMI Win32_Process.Create 由系统进程 WmiPrvSE.exe 创建子进程，
    不在本进程的 job 内——服务因此独立于 MCP 会话生命周期存活。

    中转代理只做三件事：打开日志 → Popen 真实服务（env 完整回放）→
    回写真实服务 PID；本函数轮询取回 PID（通常 <1s）。
    """
    import base64
    import tempfile
    fd, pid_out = tempfile.mkstemp(suffix=".pid")
    os.close(fd)
    payload = {
        "args": [str(a) for a in args],
        "cwd": str(cwd),
        "env": {k: str(v) for k, v in env.items()},
        "log": str(log_path),
        "pid_out": pid_out,
    }
    b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    try:
        # 静态方法调用走 Methods_ + ExecMethod_（官方姿势）——win32com 动态
        # 派发下直接 cls.Create(...) 会把 Create 解析成属性值而不可调用
        import win32com.client
        svc = win32com.client.GetObject("winmgmts:root\\cimv2")
        cls = svc.Get("Win32_Process")
        params = cls.Methods_("Create").InParameters.SpawnInstance_()
        params.CommandLine = f'"{sys.executable}" -c "{_BROKER_CODE}" {b64}'
        params.CurrentDirectory = str(cwd)
        r = cls.ExecMethod_("Create", params)
        if r.ReturnValue != 0:
            raise RuntimeError(f"WMI Create 返回码 {r.ReturnValue}")
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                txt = Path(pid_out).read_text(encoding="utf-8").strip()
                if txt.isdigit():
                    return _DetachedProcHandle(int(txt))
            except Exception:
                pass  # 代理尚未回写
            time.sleep(0.2)
        raise RuntimeError("WMI 中转超时：服务 PID 未回写（WmiPrvSE 或代理异常）")
    finally:
        try:
            os.unlink(pid_out)
        except OSError:
            pass


def _spawn_service_process(args, cwd, env, log_name: str):
    """拉起服务子进程（mgmt/前端共用）——保证服务独立于拉起者进程树存活

    背景：launcher 被 MCP 同步人审桥进程内调用时，MCP 客户端（mcp SDK）
    把 server 进程放进 KILL_ON_JOB_CLOSE 的 Job Object——直接 Popen 的
    服务是该 job 的孙成员，AI 客户端退出/重启 MCP 时整树被株连（实测：
    用户正在用的前端当场死掉，mgmt 日志无任何停机记录）。

    逃逸策略（三段）：
    ① 常规 Popen 带 CREATE_BREAKAWAY_FROM_JOB——不在 job 内则标志
       无效（零行为变化），job 允许 breakaway 则直接脱离；
    ② PermissionError（job 禁止 breakaway，如 mcp SDK 的 job）→
       经 WMI 系统进程创建（不在 job 内，彻底独立）；
    ③ WMI 也失败（环境异常）→ 退回常规 Popen 并大声告警——服务可用
       但会随 MCP 会话退出被终止（保可用性优先，不静默）。

    stdin 一律隔断（与 daemon 拉起同款）：launcher 被 MCP 进程内调用时，
    本进程 stdin 是 MCP 协议管道——不隔断则子进程共享协议管道即死
    （实测卡死：进程存活但零 CPU，端口永不绑定）。
    """
    try:
        return subprocess.Popen(
            args,
            cwd=str(cwd),
            env=env,
            creationflags=_CREATE_NO_WINDOW | _BREAKAWAY,
            stdin=subprocess.DEVNULL,
            stdout=_open_service_log(log_name),
            stderr=subprocess.STDOUT,
        )
    except PermissionError:
        _log("  [Job 逃逸] breakaway 被拒（处于限制性 Job 内），改经 WMI 中转拉起")
        try:
            return _spawn_via_wmi(args, cwd, env, _prep_service_log(log_name))
        except Exception as e:
            _log(f"  [警告] WMI 中转拉起失败（{e}）——退回常规拉起，"
                 f"服务将随当前会话（MCP 客户端）退出而被终止")
            return subprocess.Popen(
                args,
                cwd=str(cwd),
                env=env,
                creationflags=_CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,
                stdout=_open_service_log(log_name),
                stderr=subprocess.STDOUT,
            )


def _find_frontend_dir() -> Path | None:
    """查找前端目录（agent-chat-ui）"""
    candidates = [
        PROJECT_ROOT.parent / "agent-chat-ui",  # 同级目录 d:\...\SuperAIOffice\agent-chat-ui
        PROJECT_ROOT / "agent-chat-ui",         # 子目录
    ]
    for c in candidates:
        if c.is_dir() and (c / "package.json").exists():
            return c
    return None


def _read_dev_mode_flag() -> bool:
    """读取前端开发模式开关

    从 config/.env 读取 FRONTEND_DEV_MODE：
    - true/1/yes → 开发模式（next dev，支持热更新）
    - false/0/no/未设置 → 生产模式（next start，启动快）

    开发者只需修改 config/.env 中的 FRONTEND_DEV_MODE=true 即可切换
    """
    env_file = PROJECT_ROOT / "config" / ".env"
    if not env_file.exists():
        return False
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            if line.lower().startswith("frontend_dev_mode="):
                val = line.split("=", 1)[1].strip().lower()
                return val in ("true", "1", "yes")
    except Exception:
        pass  # 读不到 .env 按非 dev 模式（默认行为不变）
    return False


def _start_frontend(frontend_dir: Path) -> tuple[subprocess.Popen | None, str]:
    """启动前端 server（无窗口）

    模式切换（通过 config/.env 的 FRONTEND_DEV_MODE 控制）：
    - FRONTEND_DEV_MODE=true → 强制开发模式 (next dev，支持热更新)
    - FRONTEND_DEV_MODE=false（默认）→ 生产模式 (next start，~5s 启动)
      - 若无生产构建产物 (.next/BUILD_ID)，回退到开发模式

    用 shutil.which 找到 pnpm.cmd/npm.cmd 完整路径，避免 shell=True 导致
    Popen 返回的 cmd.exe 进程立即退出（会使 watch() 误判为异常退出）

    返回 (proc, mode)，mode 为 "prod" 或 "dev"
    """
    use_pnpm = (frontend_dir / "pnpm-lock.yaml").exists()
    exe = (shutil.which("pnpm") or shutil.which("pnpm.cmd")) if use_pnpm else \
          (shutil.which("npm") or shutil.which("npm.cmd"))

    if not exe:
        _log("  [错误] 未找到 pnpm 或 npm，请确认 Node.js 已安装")
        return None, "dev"

    # 读取开发模式开关
    dev_mode = _read_dev_mode_flag()

    if dev_mode:
        # 开发者模式：强制 next dev
        cmd = [exe, "dev"] if use_pnpm else [exe, "run", "dev"]
        mode = "dev"
        _log(f"  模式: 开发模式 (FRONTEND_DEV_MODE=true)")
    else:
        # 生产模式：检查是否有构建产物
        build_id = frontend_dir / ".next" / "BUILD_ID"
        if build_id.exists():
            cmd = [exe, "start"] if use_pnpm else [exe, "run", "start"]
            mode = "prod"
            _log(f"  模式: 生产模式 (next start) — 启动快 (~5s)")
        else:
            # 无构建产物，回退到 dev 模式
            cmd = [exe, "dev"] if use_pnpm else [exe, "run", "dev"]
            mode = "dev"
            _log(f"  模式: 开发模式 (无构建产物，回退) — 首次较慢 (~30-70s)")
            _log(f"  [提示] 运行 build_frontend.bat 构建生产版本可大幅加速启动")

    _log(f"  启动命令: {' '.join(cmd)} (cwd: {frontend_dir})")

    try:
        # 前端崩溃零现场曾是排障黑洞——与后端同标准落 logs/
        proc = _spawn_service_process(
            cmd,
            cwd=str(frontend_dir),
            env={**os.environ, "FORCE_COLOR": "1", "CI": "false", "PORT": str(PORT_FRONTEND)},
            log_name="frontend.log",
        )
        return proc, mode
    except Exception as e:
        _log(f"  启动前端失败: {e}")
        return None, mode


def _get_process_info(pid: int) -> str | None:
    """获取进程命令行 + 工作目录（小写拼接），用于身份校验；失败返回 None"""
    import psutil
    try:
        proc = psutil.Process(pid)
        parts = [str(p) for p in proc.cmdline()]
        try:
            parts.append(proc.cwd())
        except Exception:
            pass  # 权限不足时拿不到 cwd，仅用命令行判断
        return " ".join(parts).lower()
    except Exception:
        return None


def _is_project_process(pid: int) -> bool:
    """校验 PID 对应进程是否属于本项目（防误杀无关进程 / PID 复用）

    匹配特征（命令行或工作目录）：
    - 本项目路径或工作区路径（覆盖同级 agent-chat-ui 前端）
    - agent.management / launcher.py / core.daemon.server 特征关键字
      （langgraph 特征已于图编排下线后移除——项目不再运行 langgraph 进程，
      留着会把命令行含该词的无关进程误判为本项目而错杀）
    进程不存在或信息获取失败时一律视为非本项目（宁可不杀，不可错杀）
    """
    if not pid or pid <= 0:
        return False
    info = _get_process_info(pid)
    if info is None:
        return False
    signatures = [
        str(PROJECT_ROOT).lower(),
        str(PROJECT_ROOT.parent).lower(),
        "agent.management",
        "launcher.py",
        "core.daemon.server",
    ]
    return any(sig in info for sig in signatures)


def _kill_process_tree(pid: int, log_prefix: str = ""):
    """杀掉进程及其所有子进程"""
    import psutil
    # 防御：pid 无效（None/0/负数）或为自身时绝不执行
    # psutil.Process(None) 等于当前进程——直接调用会杀掉 launcher 自己
    if not pid or pid <= 0 or pid == os.getpid():
        _log(f"  {log_prefix}跳过无效 PID: {pid}")
        return
    try:
        proc = psutil.Process(pid)
        # 先杀子进程
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass  # 子进程退出竞态（已死/权限），继续杀其余
        proc.kill()
        _log(f"  {log_prefix}已终止 PID {pid} 及其子进程")
    except psutil.NoSuchProcess:
        pass  # 进程已死，无需处理
    except Exception as e:
        _log(f"  {log_prefix}终止 PID {pid} 失败: {e}")


def _cleanup_residual():
    """清理残留进程——避免端口冲突导致启动失败"""
    import psutil
    found = False
    for port in [PORT_MGMT, PORT_FRONTEND]:
        try:
            for conn in psutil.net_connections():
                if conn.laddr.port == port and conn.status == "LISTEN":
                    found = True
                    if not conn.pid:
                        _log(f"  端口 {port} 被占用，但无法获取 PID（权限不足），请手动检查")
                        continue
                    if not _is_project_process(conn.pid):
                        _log(f"  端口 {port} 被非本项目进程占用 (PID: {conn.pid})，未处理")
                        continue
                    _log(f"  端口 {port} 被占用 (PID: {conn.pid})，正在清理...")
                    _kill_process_tree(conn.pid)
        except Exception:
            pass  # 枚举连接失败（权限/平台差异）跳过本项端口清理
    # 也清理旧 launcher 进程
    if PID_FILE.exists():
        try:
            data = json.loads(PID_FILE.read_text(encoding="utf-8"))
            old_launcher = data.get("launcher_pid")
            if old_launcher and old_launcher != os.getpid():
                try:
                    # 校验身份，防 PID 复用误杀无关进程
                    if not _is_project_process(old_launcher):
                        _log(f"  旧 launcher PID {old_launcher} 已被其他进程复用，跳过清理")
                    else:
                        _log(f"  旧 launcher 进程 (PID: {old_launcher})，正在清理...")
                        _kill_process_tree(old_launcher)
                except psutil.NoSuchProcess:
                    pass  # 进程已死，无需处理
        except Exception:
            pass  # 旧 launcher 探测失败不阻断本次启动
    _clear_pid_file()
    if found:
        time.sleep(2)  # 等待端口释放


def _check_libreoffice() -> bool:
    """检测 LibreOffice (soffice) 是否可用

    用于 Office 文件预览（docx/xlsx/pptx → PDF 转换）。
    失败时不阻止启动，仅打印提示。
    """
    try:
        from core.parser.office_converter import find_soffice, get_cache_stats
        soffice = find_soffice()
        if soffice:
            stats = get_cache_stats()
            _log(f"  ✓ LibreOffice 可用: {soffice}")
            if stats["file_count"] > 0:
                _log(f"    预览缓存: {stats['file_count']} 个文件, {stats['total_size_mb']} MB")
            return True
        else:
            _log("  [提示] LibreOffice 未安装，Office 文件预览不可用")
            _log("  [提示] 安装: winget install TheDocumentFoundation.LibreOffice")
            return False
    except Exception as e:
        _log(f"  [警告] LibreOffice 检测失败: {e}")
        return False


def _start_preflight():
    """启动前置：启动横幅 + 残留进程清理 + 数据库自动备份"""
    _log("=" * 50)
    _log("SuperAIOffice 后端启动中（并行模式）...")
    _log(f"项目目录: {PROJECT_ROOT}")
    _log(f"Python: {_python_exe}")
    _log(f"无窗口模式: {_SILENT}")
    _log("")

    # ── 0. 清理残留进程（避免端口冲突）──
    _log("[1/5] 检查残留进程...")
    _cleanup_residual()
    _log("  残留清理完成")

    # ── 0.5. 自动备份数据库 ──
    _log("[2/5] 自动备份数据库...")
    try:
        from core.backup import backup_database
        result = backup_database()
        if result["ok"]:
            _log(f"  数据库已备份: {result['message']}")
        else:
            _log(f"  数据库备份跳过: {result['message']}")
        # 备份面如实提示：当前只覆盖 primary 主库——
        # 注册的其余 SQLite 数据源不在自动备份面，避免"已备份"被误读为全覆盖
        try:
            from core.datasource_manager import DataSourceManager
            _extra = [n for n, c in (DataSourceManager()._config or {}).items()
                      if not c.get("is_default") and c.get("type", "sqlite") == "sqlite"]
            if _extra:
                _log(f"  提示: 自动备份面仅覆盖默认主库（primary）——"
                     f"其余数据源未纳入: {', '.join(_extra)}")
        except Exception:
            pass  # 提示失败不影响备份主流程
    except Exception as e:
        _log(f"  数据库备份异常: {e}")


def _launch_services(env):
    """拉起服务进程：Management API（必起）+ 前端（可选，缺失仅告警）

    Popen 不阻塞立即返回；LibreOffice 检测只提示不阻断。
    """
    # ── 同时启动服务（Popen 不阻塞，立即返回）──
    # Ladybug 为嵌入式图库（进程内），无需启动外部服务
    _log("[3/5] 无独立编排服务（已收口进程内）")

    # LibreOffice 检测（用于 Office 文件预览转 PDF）
    _log("[4/5] 检测 LibreOffice（Office 文件预览）+ 启动 Management API...")
    _check_libreoffice()

    _log(f"  启动 Management API（端口 {PORT_MGMT}）...")
    mgmt_cmd = [
        _python_exe, "-m", "uvicorn",
        "agent.management.server:mgmt_app",
        "--port", str(PORT_MGMT), "--host", _svc_settings.MGMT_HOST,
        "--no-access-log",
    ]
    mgmt_proc = _spawn_service_process(
        mgmt_cmd,
        cwd=str(PROJECT_ROOT),
        env={**env, "SUPERAIDB_ROLE": "backend"},  # 轮转文件日志按角色分名
        log_name="management_api.log",  # stderr 合并进同一日志文件
    )
    _log(f"  Management API PID: {mgmt_proc.pid}")
    _log(f"  Management API 日志: logs/management_api.log")

    _log("[5/5] 启动前端（Agent Chat UI）...")
    frontend_dir = _find_frontend_dir()
    frontend_proc = None
    frontend_mode = "dev"
    if frontend_dir:
        _log(f"  前端目录: {frontend_dir}")
        frontend_proc, frontend_mode = _start_frontend(frontend_dir)
        if frontend_proc:
            _log(f"  Frontend PID: {frontend_proc.pid} ({frontend_mode})")
        else:
            _log("  [警告] 前端启动失败")
    else:
        _log("  [警告] 未找到 agent-chat-ui 目录")

    return mgmt_proc, frontend_dir, frontend_proc, frontend_mode


def _wait_services_ready(frontend_proc, frontend_mode):
    """并行等待所有端口就绪（总时间 = max 而非 sum），返回就绪表

    超时按模式调整：生产模式 15s 足够，开发模式需要 90s（冷启动）。
    前端就绪后立即打开浏览器，不等后端服务。
    """
    _log("")
    _log("等待所有服务就绪（并行等待）...")

    ready = {}

    def _wait_service(name: str, port: int, timeout: int):
        ok = _wait_for_port(port, timeout=timeout, frontend=(name == "Frontend"))
        ready[name] = ok
        # 前端就绪后立即打开浏览器——不等后端服务
        # 用户在前端会看到启动进度页面，后端就绪后自动消失
        if name == "Frontend" and ok:
            _log("  前端就绪，立即打开浏览器（后端仍在启动中）...")
            try:
                webbrowser.open(f"http://localhost:{port}")
            except Exception:
                pass  # 浏览器打开失败不影响服务，用户可手动访问

    frontend_timeout = 15 if frontend_mode == "prod" else 90
    threads = [
        # :2024 无独立服务进程，无需启动/等待
        threading.Thread(target=_wait_service, args=("Management API", PORT_MGMT, 30)),
    ]
    # Ladybug 为嵌入式图库（进程内），无需启动/等待外部服务
    if frontend_proc:
        threads.append(threading.Thread(target=_wait_service, args=("Frontend", PORT_FRONTEND, frontend_timeout)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 输出就绪状态
    for name in ["Management API", "Frontend"]:
        ok = ready.get(name, False)
        _log(f"  {name}: {'就绪 ✅' if ok else '超时 ❌'}")

    return ready


def _finalize_start(mgmt_proc, frontend_dir, frontend_proc, ready):
    """启动收尾：核心服务失败判 False、写 PID 文件、输出启动摘要"""
    # Management API 是核心服务，启动失败则返回 False
    if not ready.get("Management API"):
        _log("  [错误] Management API 启动失败！")
        _log("  请查看日志 logs/management_api.log 排查原因")
        _log("  如需排查，请用控制台模式：python agent/management/launcher.py")
        return False

    # 写入 PID 文件
    backend_pids = [mgmt_proc.pid]
    frontend_pid = frontend_proc.pid if frontend_proc else None
    _write_pid_file(backend_pids, frontend_pid)

    # 如果前端未就绪，提供备选方案
    if not ready.get("Frontend"):
        if frontend_dir:
            _log("  [提示] 前端未就绪，可手动打开 http://localhost:3000")

    _log("")
    _log("=" * 50)
    _log("SuperAIDB 启动完成！")
    _log(f"  Management API:    http://localhost:{PORT_MGMT}  [{'✅' if ready.get('Management API') else '❌'}]")
    _log(f"  Frontend (展示板): http://localhost:{PORT_FRONTEND}  [{'✅' if ready.get('Frontend') else '❌'}]")
    _log(f"  API 文档:          http://localhost:{PORT_MGMT}/docs")
    _log("=" * 50)

    # 清除 stop() 留下的维护窗旗标——stop 注释承诺"文件随下次启动清理"，
    # 但此前启动路径从未清除：每次 stop 后启动，系统被旗标挡在维护态
    # 长达 30 分钟（TTL），用户一切写操作报"系统维护中"。服务已就绪，
    # 维护窗语义结束，必须在此封口。
    try:
        from core.daemon.runtime import set_maintenance
        set_maintenance(False)
    except Exception:
        pass  # 旗标清除失败不阻断启动（TTL 30 分钟兜底自愈）

    return True


def start():
    """启动 Management API + Frontend（全部无窗口，并行启动）

    服务同时启动，总等待时间 = max(各服务启动时间) 而非 sum
    Ladybug 为嵌入式图库（进程内），无需启动外部服务
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

    _start_preflight()
    mgmt_proc, frontend_dir, frontend_proc, frontend_mode = _launch_services(env)
    ready = _wait_services_ready(frontend_proc, frontend_mode)
    return _finalize_start(mgmt_proc, frontend_dir, frontend_proc, ready)


def stop():
    """停止所有服务（包括进程树）"""
    import psutil

    _log("正在停止所有服务...")

    # 先置维护窗旗标：stop 进行中若 mgmt 内某请求正在
    # ensure_daemon（最长 30s 等就绪），stop 按旧运行文件杀完后新 daemon
    # 才写运行文件——孤儿 daemon 永驻（持库句柄+主密钥）。
    # 旗标让 ensure_daemon/业务调用即刻拒入，窗口封死；文件随下次启动清理。
    try:
        from core.daemon.runtime import set_maintenance
        set_maintenance(True)
    except Exception:
        pass  # 旗标置失败不阻断停止（停机优先）

    # 读取 PID 文件
    pids = []
    if PID_FILE.exists():
        try:
            data = json.loads(PID_FILE.read_text(encoding="utf-8"))
            pids = data.get("pids", [])
        except Exception:
            pass  # PID 文件损坏按无运行实例处理

    # net_connections() 全量枚举一次快照，2 个端口共用——
    # 原实现每个端口重新枚举（2 次全量扫描，Windows 上单次数秒，
    # 托盘消息循环线程被冻结 30s+，表现为"点退出没反应"）
    listen_conns = []
    try:
        listen_conns = [c for c in psutil.net_connections() if c.status == "LISTEN"]
    except Exception:
        pass  # 枚举失败则跳过本项清理（下轮 watchdog 再试）

    # 杀掉端口占用的进程（确保不遗漏）——校验身份，防误杀无关进程
    for port in [PORT_MGMT, PORT_FRONTEND]:
        try:
            for conn in listen_conns:
                if conn.laddr.port == port:
                    if not conn.pid:
                        _log(f"  端口 {port} 被占用，但无法获取 PID（权限不足），请手动检查")
                        continue
                    if not _is_project_process(conn.pid):
                        _log(f"  端口 {port} 被非本项目进程占用 (PID: {conn.pid})，未处理")
                        continue
                    _kill_process_tree(conn.pid, f"端口 {port}: ")
        except Exception:
            pass  # 单端口清理失败不影响其余端口

    # 杀掉 PID 文件中的进程——校验身份，防 PID 复用误杀
    for pid in pids:
        if pid and _is_project_process(pid):
            _kill_process_tree(pid, "PID 文件: ")
        else:
            _log(f"  PID {pid} 已不存在或不属于本项目，跳过")

    # 数据守护进程：读运行文件按 pid 终止（端口动态分配，不能走端口通道）
    try:
        from core.daemon.runtime import read_runtime, clear_runtime
        rt = read_runtime()
        if rt and rt.get("pid"):
            dpid = rt["pid"]
            if _is_project_process(dpid):
                _kill_process_tree(dpid, "daemon: ")
            else:
                _log(f"  daemon PID {dpid} 已不存在或不属于本项目，跳过")
        clear_runtime()
    except Exception:
        pass  # daemon 状态清理失败不影响 launcher 退出

    # 等待进程退出
    time.sleep(1)

    _clear_pid_file()
    _log("所有服务已停止 ✅")


def _start_force_exit_watchdog(delay: float = 15.0):
    """硬退出看门狗——用户请求退出后，清理路径若卡死则强制收尾

    正常退出路径（on_quit → stop() → icon.stop() → 主循环退出 →
    watch() 收尾）通常数秒完成，看门狗随之失效（进程已退）。
    任何一步挂死（进程树终止无响应、psutil 枚举卡顿等）时，
    到时强制退出——先释放单实例锁和 PID 文件再 os._exit，
    避免残留状态影响下次启动。
    """
    def _watchdog():
        time.sleep(delay)
        try:
            _log(f"退出流程超过 {delay}s 未完成，强制退出（清理路径疑似卡死）")
        except Exception:
            pass
        try:
            _release_single_instance_lock()
        except Exception:
            pass  # 锁含 PID，下次启动身份校验后自动回收
        try:
            _clear_pid_file()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_watchdog, daemon=True).start()


def _create_tray_icon():
    """创建系统托盘图标

    功能：
    - 左键点击 → 打开前端（default 菜单项）
    - 右键菜单 → 打开前端/控制台/设置、退出应用

    需要 pystray 和 Pillow
    """
    import pystray
    from PIL import Image, ImageDraw, ImageFont

    # 生成图标——蓝色圆形 + 白色 "S"
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, 62, 62], fill=(59, 130, 246, 255))  # 蓝色圆
    try:
        font = ImageFont.truetype("arial.ttf", 42)
    except Exception:
        font = ImageFont.load_default()
    # 居中绘制 "S"
    bbox = draw.textbbox((0, 0), "S", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        (32 - tw / 2 - bbox[0], 32 - th / 2 - bbox[1]),
        "S",
        fill="white",
        font=font,
    )

    def on_open_frontend(icon, item):
        """打开前端控制台页面"""
        webbrowser.open(f"http://localhost:{PORT_FRONTEND}")

    def on_open_dashboard(icon, item):
        """打开控制台"""
        webbrowser.open(f"http://localhost:{PORT_FRONTEND}/dashboard")

    def on_open_settings(icon, item):
        """打开设置"""
        webbrowser.open(f"http://localhost:{PORT_FRONTEND}/settings")

    def on_quit(icon, item):
        """退出应用——停止所有服务并退出

        三层防护：
        1. 全身 try/except：托盘回调里抛出的异常会被 pystray 的窗口
           过程静默吞掉（表现为"点了没反应"），任何清理失败都不得
           阻止退出——包括 _log（日志通道曾因断管道抛 OSError 把
           本函数第一行炸死）
        2. finally icon.stop()：无论如何都终止托盘消息循环
        3. 硬退出看门狗：stop()/icon.stop() 若因任何原因卡死（进程
           树终止挂起等），到时强制收尾——用户点了退出，进程必须消失
        """
        _start_force_exit_watchdog(15)
        try:
            _log("用户从托盘菜单退出应用...")
            stop()
        except Exception as e:
            try:
                _log(f"退出清理异常（仍继续退出）: {e}")
            except Exception:
                pass  # 日志通道自身故障不阻断退出
        finally:
            try:
                icon.stop()
            except Exception:
                pass  # 托盘停止失败也不阻断——看门狗兜底强制退出

    menu = pystray.Menu(
        pystray.MenuItem("打开前端", on_open_frontend, default=True),
        pystray.MenuItem("打开控制台", on_open_dashboard),
        pystray.MenuItem("打开设置", on_open_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出应用", on_quit),
    )

    icon = pystray.Icon("SuperAIOffice", img, "SuperAIOffice", menu)
    return icon


def _check_processes_alive(backend_pids: list[int], frontend_expected: bool = True) -> bool:
    """服务存活判定：以端口 TCP 可达为准

    不能按 tracked PID 判死：uvicorn/next 会 re-exec（原 PID 退出、服务健在），
    按 PID 判死会把健康运行的系统整体误杀。
    也不用 urllib 发 HTTP：Windows 系统代理会拦截 127.0.0.1 请求造成误判。
    存活判据收敛为 _http_identity_ok（裸 socket HTTP 身份校验）——外部进程占用
    端口时纯 TCP 假绿且永不自愈。
    backend_pids 保留签名兼容，健康语义只看服务端口。
    frontend_expected=False（前端未装/未启动，可选组件）时不盯前端端口——
    否则客户机缺前端约 26s 后健康后端被绞杀
    """
    # 容忍窗口：3 次尝试 × (3s 超时 + 4s 间隔) ≈ 21s
    # 覆盖前端冷编译 / 系统瞬时资源高峰的不可达，防误杀服务
    ports = (PORT_MGMT, PORT_FRONTEND) if frontend_expected else (PORT_MGMT,)
    for port in ports:
        ok = False
        # 前端端口没有 /api/health——身份标记退化为"合法 HTTP 响应"
        marker = None if port == PORT_FRONTEND else b'"management-api"'
        for attempt in range(3):
            if _http_identity_ok(port, timeout=3, marker=marker):
                ok = True
                break
            if attempt == 2:
                _log(f"警告: 端口 {port} 连续 3 次身份校验失败（约 21s），判定服务退出")
                return False
            time.sleep(4)  # 资源高峰退避，给服务恢复时间
        if not ok:
            return False
    return True


def watch():
    """启动后持续监控，进程退出时自动清理

    集成系统托盘（如果 pystray 可用）：
    - 左键点击托盘图标 → 打开前端
    - 右键菜单 → 打开前端/控制台/设置、退出应用
    - 如果 pystray 不可用，回退到命令行监控模式

    单实例：已有实例在跑时不再启动第二套服务（两个实例的启动清理会
    互杀对方服务），直接打开前端页面并退出。
    """
    if not _acquire_single_instance_lock():
        _log("检测到已有实例正在运行——直接打开前端，不再重复启动")
        webbrowser.open(f"http://localhost:{PORT_FRONTEND}")
        return 0
    try:
        return _watch_main()
    finally:
        _release_single_instance_lock()


def _watch_main():
    """watch 主流程（单实例锁内执行）"""
    if not start():
        _log("启动失败，退出")
        return 1

    _log("")

    # 读取 PID 文件——只监控后端 PID（前端 npm/pnpm 进程行为不同，不监控）
    backend_pids = []
    frontend_expected = True
    if PID_FILE.exists():
        try:
            data = json.loads(PID_FILE.read_text(encoding="utf-8"))
            backend_pids = data.get("backend_pids", data.get("pids", [])[:2])
            # 前端是可选组件：PID 文件里没有 frontend_pid = 本次未启动——
            # 监控随之不盯前端端口（启动与看门语义对齐）
            frontend_expected = bool(data.get("frontend_pid"))
        except Exception:
            pass  # 读不到 PID 数据则本轮不调整监控面

    # 后台线程：监控进程状态
    monitor_stop = threading.Event()
    tray_icon_ref = [None]  # 用 list 包装以便闭包内修改

    def _monitor():
        """后台监控进程——发现异常退出时停止托盘

        每轮检查独立兜底：单次瞬时异常只跳过本轮，不终结看门狗——
        监控线程一旦静默消失，服务死亡就无人收拾，应用沦为僵尸
        （托盘还在、服务已死）。stop()/icon.stop() 均可安全重试：
        前者内部全兜底，后者检查 _running 幂等。
        """
        while not monitor_stop.is_set():
            try:
                if not _check_processes_alive(backend_pids, frontend_expected):
                    _log("检测到子进程异常退出，正在清理...")
                    stop()
                    if tray_icon_ref[0]:
                        tray_icon_ref[0].stop()
                    break
            except Exception as e:
                try:
                    _log(f"监控本轮异常（跳过继续）: {e}")
                except Exception:
                    pass
            monitor_stop.wait(5)

    monitor_thread = threading.Thread(target=_monitor, daemon=True)
    monitor_thread.start()

    # 尝试使用系统托盘
    use_tray = True
    try:
        icon = _create_tray_icon()
        tray_icon_ref[0] = icon
        _log("系统托盘已就绪——左键点击图标打开前端，右键菜单可退出应用")
        icon.run()  # 阻塞主线程，直到 icon.stop()
    except ImportError:
        use_tray = False
        _log("pystray 未安装，使用命令行监控模式")
        _log("安装 pystray 可启用系统托盘: pip install pystray")
    except Exception as e:
        use_tray = False
        _log(f"托盘初始化失败: {e}，使用命令行监控模式")

    # 回退模式：命令行监控
    if not use_tray:
        _log("进入监控模式（关闭后端请用前端控制台或 stop.bat）...")
        try:
            while True:
                if not _check_processes_alive(backend_pids, frontend_expected):
                    _log("检测到子进程异常退出，正在清理...")
                    stop()
                    _log("建议：请重新启动系统")
                    break
                time.sleep(5)
        except KeyboardInterrupt:
            _log("\n收到中断信号，正在停止...")
            stop()

    # 清理
    monitor_stop.set()
    _log("应用已退出")
    return 0


if __name__ == "__main__":
    # 解析命令行参数
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        stop()
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        # 检查端口状态（健康检查收敛 core.health）
        from core.health import check_http_ok
        for port, name in [(PORT_MGMT, "Management API"), (PORT_FRONTEND, "Frontend")]:
            path = 'api/health' if port == PORT_MGMT else ''
            url = f"http://localhost:{port}/{path}"
            ok = check_http_ok(url, timeout=2)
            print(f"{name} (:{port}): {'运行中 ✅' if ok else '已停止 ❌'}")
        print("无独立编排服务（:2024 不启动）")
    else:
        # 默认：启动 + 监控
        sys.exit(watch())
