"""文件即契约的公共实现（五份拷贝收敛于此）

一份 JSON 文件的跨进程共享需要三件事（各处不得各抄一遍致纪律不一）：
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
import threading
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
    （O_EXCL 创建/删除式自旋锁在同进程线程并发下有 Windows 删除未决竞态）

    同线程可重入（(路径, 线程) 计数注册表）：外层临界区内再取锁直通计数——
    msvcrt 字节区锁不分句柄主，同进程第二次 locking 必 PermissionError
    （context.save_selection 外层持锁 + JsonContract.write 内层持锁的
    嵌套死锁）。跨线程/跨进程仍走 OS 锁排队，语义不变。
    注册项 value 存持有者线程的 weakref：持有者死亡（被 kill/未走 __exit__）
    而注册未清时回收陈尸（关 fd 弹注册），防 OS ident 复用后新线程被误判
    "重入"共享死线程句柄（拿调用方自己的 ident 做存活核查
    恒真，即成不可达死代码）；进程级死亡仍由 OS 强制释放兜底。"""

    # (路径, 线程id) → (fd, 持有计数, 持有者线程 weakref)；进程内可重入注册表
    _REGISTRY: dict = {}
    _REG_LOCK = threading.Lock()

    def __init__(self, path, timeout: float = 10.0):
        self._path = Path(path)
        self._timeout = timeout
        self._fd = None
        self._key = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._key = (str(self._path), threading.get_ident())
        with FileLock._REG_LOCK:
            hit = FileLock._REGISTRY.get(self._key)
            if hit is not None:
                fd, count, holder_ref = hit
                # 持有者存活核查：weakref 解引用为 None = 持有线程已死亡
                #（ident 可被 OS 复用，拿 ident 问"活没活"在复用后恒真——
                # 旧实现是不可达死代码）
                if holder_ref() is not None:
                    # 同线程重入：只加计数，不重复取 OS 锁
                    FileLock._REGISTRY[self._key] = (fd, count + 1, holder_ref)
                    self._fd = fd
                    return self
                # 陈尸回收：关 fd 弹注册，随后按新取锁走
                FileLock._REGISTRY.pop(self._key, None)
                try:
                    os.close(fd)
                except OSError:
                    pass  # 陈尸 fd 关不掉无碍——OS 进程退出时兜底回收
        fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR)
        import weakref as _weakref
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
                        with FileLock._REG_LOCK:
                            FileLock._REGISTRY[self._key] = (
                                fd, 1, _weakref.ref(threading.current_thread()))
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
                        with FileLock._REG_LOCK:
                            FileLock._REGISTRY[self._key] = (
                                fd, 1, _weakref.ref(threading.current_thread()))
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
                    pass  # 锁未拿到时的 fd 回收失败——OS 在进程退出时兜底回收
            raise

    def __exit__(self, *exc):
        if self._fd is not None and self._key is not None:
            with FileLock._REG_LOCK:
                fd, count, holder_ref = FileLock._REGISTRY.pop(
                    self._key, (self._fd, 0, None))
                if count > 1:
                    # 重入层数未归零：只减计数，不释放 OS 锁
                    FileLock._REGISTRY[self._key] = (fd, count - 1, holder_ref)
                    return False
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(self._fd, 0, os.SEEK_SET)  # 解锁区与锁定区一致
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass  # 解锁失败（区段状态异常）随后 close 兜底；进程死亡由 OS 强制释放
            try:
                os.close(self._fd)
            except OSError:
                pass  # 关句柄失败——OS 进程退出时回收
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
                # 类型守卫以 default_factory 为准（曾写成
                # type(self._cache)——初始 None → NoneType，or dict 永不生效，
                # 新实例首读已存在文件恒吞真实内容，选择集跨进程静默清档的病根）
                self._cache = data if isinstance(data, type(self._default())) else self._default()
            except Exception:
                self._cache = self._default()
            self._mtime = mtime
        return self._cache

    def write(self, data) -> None:
        """tmp+replace 原子写；写后同步缓存（只更 mtime 不同步缓存会导致
        写完读回旧数据——clear_all 清了文件却读回旧值的"假清"问题）

        写路径全程持 FileLock（并发根修）：固定名的 tmp 文件在多线程/多进程
        并发写下会互踩（os.replace 撞 Windows 文件锁，reset_all 并发下
        WinError 32）——原子写若本身可被竞态破坏，"原子"二字就不成立"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock():
            tmp = self._path.with_suffix(".tmp")
            # tmp 创建即 0600（os.open 终态权限——tmp 本体默认权限窗口归零；
            # 挂起表/提权契约等含操作载荷的 JSON 与 daemon.json 同标准）
            _fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(_fd, "w", encoding="utf-8") as _fh:
                _fh.write(json.dumps(data, ensure_ascii=False))
            os.replace(tmp, self._path)
        self._cache = data
        try:
            self._mtime = os.path.getmtime(self._path)
        except OSError:
            pass  # mtime 读不到则下次全量重读（新鲜度取保守方向，不吃旧缓存）

    def lock(self, timeout: float = 10.0) -> FileLock:
        """读改写临界区互斥锁（持有者死亡/损坏自动回收）"""
        return FileLock(self._path.with_suffix(self._path.suffix + ".lock"), timeout)

    def delete(self) -> None:
        """删除契约文件并复位缓存（"文件不存在=default"契约的显式撤销通道）"""
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass  # 删除失败无碍——文件不存在本就是目标态
        self._cache, self._mtime = self._default(), -1.0
