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
import signal
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

# 端口定义（P2-9：Mgmt/Frontend 端口从 settings 读，可用 .env 覆盖；
# :2024 无独立服务进程，此常量仅为 status/health 兼容保留）
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from config.settings import settings as _svc_settings

PORT_LANGGRAPH = _svc_settings.LANGGRAPH_PORT  # :2024 无独立服务进程，保留定义兼容 status/health 逻辑
PORT_MGMT = _svc_settings.MGMT_PORT
PORT_FRONTEND = _svc_settings.FRONTEND_PORT


def _log(msg: str):
    """日志输出——无窗口时写文件，有窗口时打印到控制台"""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    if _SILENT:
        log_file = PROJECT_ROOT / "backend.log"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    else:
        print(line, flush=True)


def _wait_for_port(port: int, timeout: int = 30) -> bool:
    """等待端口就绪——纯 TCP 直连探测

    用 127.0.0.1 而非 localhost：避免 PowerShell/某些环境将 localhost 解析为
    IPv6 (::1)，而服务只监听 IPv4 127.0.0.1 导致检测失败。
    用 TCP 而非 urllib：urllib 默认读系统代理，企业代理环境会拦截
    127.0.0.1 请求造成误判（07:08 事故教训——同一文件内两个标准已统一）。
    """
    import socket as _sock
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _sock.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            pass
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
        pass


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
                pass
            if alive:
                return False
            try:
                _LAUNCHER_LOCK.unlink()
            except Exception:
                pass
    return False


def _release_single_instance_lock():
    """释放单实例锁（仅当锁里是自己的 PID，防误删新实例的锁）"""
    try:
        if _LAUNCHER_LOCK.exists() and \
                _LAUNCHER_LOCK.read_text(encoding="utf-8").strip() == str(os.getpid()):
            _LAUNCHER_LOCK.unlink()
    except Exception:
        pass


def _open_service_log(name: str):
    """打开子服务日志文件（logs/ 目录不存在则创建；>10MB 时滚动为 .1 旧档）

    子进程 stdout/stderr 重定向到 logs/<name>，便于排查后端 traceback
    打开失败时回退 DEVNULL（不阻止服务启动）
    滚动策略：启动时超 10MB 即归档（append 无上限曾致长跑膨胀，评审三轮收口）
    """
    try:
        logs_dir = PROJECT_ROOT / "logs"
        logs_dir.mkdir(exist_ok=True)
        log_path = logs_dir / name
        if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
            try:
                old = log_path.with_suffix(log_path.suffix + ".1")
                old.unlink(missing_ok=True)
                log_path.replace(old)
            except OSError:
                pass
        return open(log_path, "a", encoding="utf-8")
    except Exception as e:
        _log(f"  [警告] 无法打开日志文件 logs/{name}: {e}，该服务日志将被丢弃")
        return subprocess.DEVNULL


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
        pass
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
        proc = subprocess.Popen(
            cmd,
            cwd=str(frontend_dir),
            env={**os.environ, "FORCE_COLOR": "1", "CI": "false", "PORT": str(PORT_FRONTEND)},
            creationflags=_CREATE_NO_WINDOW,
            # 前端崩溃零现场曾是排障黑洞（评审三轮）——与后端同标准落 logs/
            stdout=_open_service_log("frontend.log"),
            stderr=subprocess.STDOUT,
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
    - langgraph / agent.management / launcher.py 特征关键字
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
        "langgraph",
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
                pass
        proc.kill()
        _log(f"  {log_prefix}已终止 PID {pid} 及其子进程")
    except psutil.NoSuchProcess:
        pass
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
            pass
    # 也清理旧 launcher 进程
    if PID_FILE.exists():
        try:
            data = json.loads(PID_FILE.read_text(encoding="utf-8"))
            old_launcher = data.get("launcher_pid")
            if old_launcher and old_launcher != os.getpid():
                try:
                    proc = psutil.Process(old_launcher)
                    # 校验身份，防 PID 复用误杀无关进程
                    if not _is_project_process(old_launcher):
                        _log(f"  旧 launcher PID {old_launcher} 已被其他进程复用，跳过清理")
                    else:
                        _log(f"  旧 launcher 进程 (PID: {old_launcher})，正在清理...")
                        _kill_process_tree(old_launcher)
                except psutil.NoSuchProcess:
                    pass
        except Exception:
            pass
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


def start():
    """启动 Management API + Frontend（全部无窗口，并行启动）

    服务同时启动，总等待时间 = max(各服务启动时间) 而非 sum
    Ladybug 为嵌入式图库（进程内），无需启动外部服务
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

    _log("=" * 50)
    _log("SuperAIOffice 后端启动中（并行模式）...")
    _log(f"项目目录: {PROJECT_ROOT}")
    _log(f"Python: {_python_exe}")
    _log(f"无窗口模式: {_SILENT}")
    _log("")

    # ── 0. 清理残留进程（避免端口冲突）──
    _log("[0/4] 检查残留进程...")
    _cleanup_residual()
    _log("  残留清理完成")

    # ── 0.5. 自动备份数据库 ──
    _log("[0.5/4] 自动备份数据库...")
    try:
        from core.backup import backup_database
        result = backup_database()
        if result["ok"]:
            _log(f"  数据库已备份: {result['message']}")
        else:
            _log(f"  数据库备份跳过: {result['message']}")
    except Exception as e:
        _log(f"  数据库备份异常: {e}")

    # ── 同时启动服务（Popen 不阻塞，立即返回）──
    # Ladybug 为嵌入式图库（进程内），无需启动外部服务
    _log("[1/3] 无独立编排服务（:2024 不启动）")

    # LibreOffice 检测（用于 Office 文件预览转 PDF）
    _log("[2/3] 检测 LibreOffice (Office 文件预览)...")
    _check_libreoffice()

    _log(f"[3/3] 启动 Management API (端口 {PORT_MGMT})...")
    mgmt_cmd = [
        _python_exe, "-m", "uvicorn",
        "agent.management.server:mgmt_app",
        "--port", str(PORT_MGMT), "--host", _svc_settings.MGMT_HOST,
        "--no-access-log",
    ]
    mgmt_proc = subprocess.Popen(
        mgmt_cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        creationflags=_CREATE_NO_WINDOW,
        stdout=_open_service_log("management_api.log"),
        stderr=subprocess.STDOUT,  # stderr 合并进同一日志文件
    )
    _log(f"  Management API PID: {mgmt_proc.pid}")
    _log(f"  Management API 日志: logs/management_api.log")

    _log("[5/5] 启动前端 (Agent Chat UI)...")
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

    # ── 并行等待所有端口就绪（总时间 = max 而非 sum）──
    # 超时按模式调整：生产模式 15s 足够，开发模式需要 90s（冷启动）
    _log("")
    _log("等待所有服务就绪（并行等待）...")

    ready = {}

    def _wait_service(name: str, port: int, timeout: int):
        ok = _wait_for_port(port, timeout=timeout)
        ready[name] = ok
        # 前端就绪后立即打开浏览器——不等后端服务
        # 用户在前端会看到启动进度页面，后端就绪后自动消失
        if name == "Frontend" and ok:
            _log("  前端就绪，立即打开浏览器（后端仍在启动中）...")
            try:
                webbrowser.open(f"http://localhost:{port}")
            except Exception:
                pass

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

    return True


def stop():
    """停止所有服务（包括进程树）"""
    import psutil

    _log("正在停止所有服务...")

    # 读取 PID 文件
    pids = []
    if PID_FILE.exists():
        try:
            data = json.loads(PID_FILE.read_text(encoding="utf-8"))
            pids = data.get("pids", [])
        except Exception:
            pass

    # net_connections() 全量枚举一次快照，5 个端口共用——
    # 原实现每个端口重新枚举（5 次全量扫描，Windows 上单次数秒，
    # 托盘消息循环线程被冻结 30s+，表现为"点退出没反应"）
    listen_conns = []
    try:
        listen_conns = [c for c in psutil.net_connections() if c.status == "LISTEN"]
    except Exception:
        pass

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
            pass

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
        pass

    # 等待进程退出
    time.sleep(1)

    _clear_pid_file()
    _log("所有服务已停止 ✅")


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

        try/finally 兜底：stop() 任何异常都不能阻止 icon.stop()——
        否则消息循环线程卡住，托盘图标残留、进程永不退出（"点退出没反应"事故）。
        """
        _log("用户从托盘菜单退出应用...")
        try:
            stop()
        except Exception as e:
            _log(f"退出清理异常（仍继续退出）: {e}")
        finally:
            icon.stop()

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
    按 PID 判死会把健康运行的系统整体误杀（05:16 事故）。
    也不用 urllib 发 HTTP：Windows 系统代理会拦截 127.0.0.1 请求造成误判（07:08 事故）。
    TCP 直连不经过任何代理，只验证监听者存活——这正是"进程健在"的语义。
    backend_pids 保留签名兼容，健康语义只看服务端口。
    frontend_expected=False（前端未装/未启动，可选组件）时不盯前端端口——
    否则客户机缺前端约 26s 后健康后端被绞杀（评审五轮 D5 修复）
    """
    import socket

    # 容忍窗口：3 次尝试 × (3s 超时 + 4s 间隔) ≈ 21s
    # 覆盖前端冷编译 / 系统瞬时资源高峰的不可达，防误杀服务
    ports = (PORT_MGMT, PORT_FRONTEND) if frontend_expected else (PORT_MGMT,)
    for port in ports:
        ok = False
        for attempt in range(3):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=3):
                    ok = True
                    break
            except OSError:
                if attempt == 2:
                    _log(f"警告: 端口 {port} 连续 3 次不可达（约 21s），判定服务退出")
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
            # 监控随之不盯前端端口（启动与看门语义对齐，评审五轮 D5）
            frontend_expected = bool(data.get("frontend_pid"))
        except Exception:
            pass

    # 后台线程：监控进程状态
    monitor_stop = threading.Event()
    tray_icon_ref = [None]  # 用 list 包装以便闭包内修改

    def _monitor():
        """后台监控进程——发现异常退出时停止托盘"""
        try:
            while not monitor_stop.is_set():
                if not _check_processes_alive(backend_pids, frontend_expected):
                    _log("检测到子进程异常退出，正在清理...")
                    stop()
                    if tray_icon_ref[0]:
                        tray_icon_ref[0].stop()
                    break
                monitor_stop.wait(5)  # 可被中断的等待
        except Exception as e:
            _log(f"监控线程异常: {e}")

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
                if not _check_processes_alive(backend_pids):
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
        # 检查端口状态（健康检查收敛 core.health，P2-9）
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
