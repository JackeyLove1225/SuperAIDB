"""daemon 运行文件管理 + 按需拉起

运行文件 config/runtime/daemon.json：{port, token, pid, started_at}——
每次启动重写（令牌不跨启动复用），文件权限 600（仅当前用户可读）。
客户端连接不上时自动拉起 daemon（detached 子进程），轮询等就绪。
"""
import json
from core.logger import get_logger
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

logger = get_logger(__name__)

RUNTIME_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "runtime"
RUNTIME_FILE = RUNTIME_DIR / "daemon.json"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_runtime() -> dict | None:
    try:
        return json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_runtime(port: int, token: str, pid: int) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_FILE.with_suffix(".tmp")
    # tmp 创建即 0600（os.open 终态权限——tmp 本体默认权限窗口归零）
    _fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(_fd, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps({"port": port, "token": token, "pid": pid,
                              "started_at": time.time()}))
    os.replace(tmp, RUNTIME_FILE)


def clear_runtime() -> None:
    try:
        RUNTIME_FILE.unlink(missing_ok=True)
    except OSError:
        pass  # 运行文件删除失败无碍（下次启动按 PID 身份校验回收）


# 维护窗口旗标（恢复/迁移期间禁止 daemon 业务调用——防自愈在窗口期
# 把 daemon 拉回来抢库）
MAINTENANCE_FILE = RUNTIME_DIR / "maintenance.flag"
MAINTENANCE_TTL_SECONDS = 30 * 60  # 维护窗最长 30 分钟


def set_maintenance(on: bool) -> None:
    """维护模式开关（文件即契约，跨进程生效）"""
    if on:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        MAINTENANCE_FILE.write_text(str(time.time()), encoding="utf-8")
    else:
        try:
            MAINTENANCE_FILE.unlink(missing_ok=True)
        except OSError:
            pass  # 维护窗标记删除失败无碍（mtime 语义已失效即自动忽略）


def in_maintenance() -> bool:
    """维护窗判定：flag 存在且未超 TTL（30 分钟，D3 断电恢复闭环）

    超期旗标=上次维护异常断电/崩溃的残留——系统重启后服务被它永久挡死
    即"断电后假死"形态。超期自动清除并按非维护放行（保守方向取
    可用性：维护是人工短窗操作，30 分钟足够；残留旗标永挡是致命伤）。
    """
    try:
        ts = float(MAINTENANCE_FILE.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False  # 不存在/读不出/内容坏 = 非维护
    if time.time() - ts > MAINTENANCE_TTL_SECONDS:
        try:
            MAINTENANCE_FILE.unlink(missing_ok=True)
        except OSError:
            pass  # 清不掉无碍——本次已按超期放行，下次读到再试
        return False
    return True



def ping(port: int, token: str, timeout: float = 1.5) -> bool:
    """探测 daemon 是否活着且令牌正确"""
    try:
        from core.daemon.protocol import rpc_call
        r = rpc_call(port, token, "ping", {}, timeout=timeout)
        return r.get("ok") is True
    except Exception:
        return False


from core.file_contract import _pid_alive  # 唯一实现（三处拷贝收敛，自查清单第 7 项）


def ensure_daemon(timeout: float = 30.0) -> dict:
    """确保 daemon 在跑，返回 {port, token}。不在/失联则自动拉起。

    幂等：并发调用时后生者读到先者的运行文件（文件即契约）。
    判死纪律：pid 已死 → 直接重拉；pid 活着 → ping 退避重试 3 次才判失联
    （单次 ping 超时不即判死——长查询占工作线程时假死曾拉出双 daemon 孤儿；
    ping 快速道（IO 线程直答）已让忙态 daemon 即时应答，重试是瞬时抖动保险）。
    """
    rt = read_runtime()
    if rt:
        if _pid_alive(rt.get("pid", 0)):
            for attempt in range(3):
                if ping(rt["port"], rt["token"], timeout=1.5 * (attempt + 1)):
                    return rt
                time.sleep(0.5)
            logger.warning("daemon pid 存活但连续 3 次 ping 不应，按失联处理")
        else:
            logger.info("daemon 运行文件残留（pid 已死），重新拉起")

    logger.info("daemon 未运行或失联，自动拉起…")
    root = Path(__file__).resolve().parent.parent.parent
    log_dir = root / "logs"

    # 拉起互斥（O_EXCL 自旋）：并发 ensure_daemon 只许一个进程真拉起——
    # 否则双 daemon 抢写运行文件，输家是永不退出的孤儿。
    # 锁内写 PID 并校验身份：强杀/断电残留的死锁自动回收（与 launcher
    # 单实例锁同纪律，残留死锁无需手动删文件）
    lock_file = RUNTIME_DIR / "daemon.spawn.lock"
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                holder = int(lock_file.read_text(encoding="utf-8").strip() or "0")
                if holder and not _pid_alive(holder):
                    lock_file.unlink(missing_ok=True)  # 持有者已死：回收残留锁
                    continue
            except (ValueError, OSError):
                lock_file.unlink(missing_ok=True)  # 锁内容损坏：回收
                continue
            rt2 = read_runtime()
            if rt2 and ping(rt2["port"], rt2["token"]):
                return rt2
            if time.time() > deadline:
                raise RuntimeError("daemon 拉起互斥超时——删 config/runtime/daemon.spawn.lock 重试")
            time.sleep(0.3)
    log_dir.mkdir(exist_ok=True)
    # 日志滚动（与 launcher._open_service_log 同标准）：>10MB 归档一档
    # 注意目标文件是 daemon_stdout.log（裸 stdout 捕获：traceback/print 兜底）——
    # daemon.log 由 core/logger 的轮转 handler 独占（角色分名），两个写入方
    # 共用同一文件在 Windows 上 rename 撞锁 + 双 fd 交错撕行
    daemon_log = log_dir / "daemon_stdout.log"
    if daemon_log.exists() and daemon_log.stat().st_size > 10 * 1024 * 1024:
        try:
            old = daemon_log.with_suffix(".log.1")
            old.unlink(missing_ok=True)
            daemon_log.replace(old)
        except OSError:
            pass  # 日志滚动失败则追加写，旧档留存交给下次滚动
    log_f = open(daemon_log, "a", encoding="utf-8")
    # Windows 完全脱离：新进程组 + 无窗口 + 脱离会话——父进程（MCP server/测试）
    # 退出时 Job Object 连坐杀不死 daemon（实测：stdio_client 退出会清理子树）
    cflags = 0
    if os.name == "nt":
        cflags = (subprocess.CREATE_NEW_PROCESS_GROUP
                  | subprocess.CREATE_NO_WINDOW
                  | getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    # daemon 自身必须跑本地驱动（DAEMON_MODE=false）——否则它建驱动时又走
    # DaemonDriver 再起 daemon，无限自举死循环（20260822）
    daemon_env = dict(os.environ)
    daemon_env["DAEMON_MODE"] = "false"
    daemon_env["SUPERAIDB_ROLE"] = "daemon"  # 轮转文件日志按角色分名
    subprocess.Popen(
        [sys.executable, "-m", "core.daemon.server"],
        cwd=str(root),
        env=daemon_env,
        stdin=subprocess.DEVNULL,   # 关键：MCP 父进程的 stdin 是协议管道，
                                    # 不隔断则孙子进程共享管道即死
        stdout=log_f, stderr=log_f,
        creationflags=cflags,
    )
    try:
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(0.5)
            rt = read_runtime()
            if rt and ping(rt["port"], rt["token"]):
                logger.info("daemon 已就绪（端口 %s）", rt["port"])
                return rt
        raise RuntimeError(f"daemon 拉起超时（{timeout}s）——请检查 Python 环境与依赖")
    finally:
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass  # 锁文件清理由 OS 字节区锁兜底，残留文件下次启动回收
