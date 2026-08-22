"""文件即契约的公共实现（评审五轮代码质量：五份拷贝收敛于此）

一份 JSON 文件的跨进程共享需要三件事，此前各处各抄一遍且纪律不一：
- mtime 新鲜读（文件没变零读放大，变了原子重读）
- tmp+replace 原子写（读方永不看到半写状态）
- O_EXCL 互斥锁（读改写临界区；锁内写 PID，持有者死亡自动回收——
  os.kill(pid,0) 的 sig-0 语义在 Windows 3.13 以下不存在，统一用 psutil）

用法：
    c = JsonContract(Path("config/runtime/selections.json"))
    data = c.read()
    with c.lock():
        d = c.read(); d["k"] = v; c.write(d)
"""
import json
import os
import time
from pathlib import Path

from core.logger import get_logger

logger = get_logger(__name__)


def _pid_alive(pid: int) -> bool:
    """进程存活探测（跨版本一致原语：psutil.pid_exists）"""
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        return False


class FileLock:
    """文件互斥锁：Windows 用 msvcrt 字节区锁，POSIX 用 fcntl.flock——
    OS 级强制锁，进程死亡/崩溃由操作系统自动释放，永无残留锁
    （此前 O_EXCL 创建/删除式自旋锁在同进程线程并发下有 Windows 删除未决竞态，
    层 28 并发实测翻车；评审五轮）"""

    def __init__(self, path, timeout: float = 10.0):
        self._path = Path(path)
        self._timeout = timeout
        self._fd = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR)
        deadline = time.time() + self._timeout
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)  # 锁定区从文件头开始
                while True:
                    try:
                        # 锁文件首字节区（LK_NBLCK 非阻塞，拿不到立即 OSError）
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        self._fd = fd
                        return self
                    except OSError:
                        if time.time() > deadline:
                            os.close(fd)
                            raise TimeoutError(f"文件锁超时: {self._path.name}")
                        time.sleep(0.05)
            else:
                import fcntl
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self._fd = fd
                        return self
                    except OSError:
                        if time.time() > deadline:
                            os.close(fd)
                            raise TimeoutError(f"文件锁超时: {self._path.name}")
                        time.sleep(0.05)
        except Exception:
            if self._fd is None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(self._fd, 0, os.SEEK_SET)  # 解锁区与锁定区一致
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        return False


class JsonContract:
    """一份 JSON 文件的跨进程契约（mtime 新鲜读 + 原子写 + 互斥锁）"""

    def __init__(self, path, default_factory=dict):
        self._path = Path(path)
        self._default = default_factory
        self._cache = None
        self._mtime = -1.0

    @property
    def path(self) -> Path:
        return self._path

    def read(self):
        """mtime 新鲜读取；文件不存在/损坏返回 default（调用方语义决定 fail 方向）"""
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            self._cache, self._mtime = self._default(), -1.0
            return self._cache
        if mtime != self._mtime:
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._cache = data if isinstance(data, type(self._cache) or dict) else self._default()
            except Exception:
                self._cache = self._default()
            self._mtime = mtime
        return self._cache

    def write(self, data) -> None:
        """tmp+replace 原子写；写后同步缓存（此前只更 mtime 不同步缓存，
        写完读回旧数据——clear_all 清了文件的"假清"事故，评审自测发现）"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)
        self._cache = data
        try:
            self._mtime = os.path.getmtime(self._path)
        except OSError:
            pass

    def lock(self, timeout: float = 10.0) -> FileLock:
        """读改写临界区互斥锁（持有者死亡/损坏自动回收）"""
        return FileLock(self._path.with_suffix(self._path.suffix + ".lock"), timeout)
