"""daemon 服务端——唯一持有数据库句柄与主密钥的进程

启动：python -m core.daemon.server（通常由 runtime.ensure_daemon 自动拉起）
行为：
- 监听 127.0.0.1 随机空闲端口；令牌随机生成，随端口写入运行文件（600 权限）
- 方法白名单 = BaseDriver 29 接口 + ping；调用按数据源名路由到真实驱动
  （DataSourceManager 在 daemon 进程内自持，ConfigHub 配置它自己读）
- session 亲和：同一 session 的调用落在同一驱动连接（事务正确性）
- 主密钥从 keyring 取出后只驻本进程内存（其他任何进程拿不到明文库能力）
"""
import logging
import secrets
import socket
import threading

from core.daemon import runtime as _rt
from core.daemon.protocol import decode, encode
from core.logger import get_logger

logger = get_logger(__name__)

# 允许的方法白名单（与 BaseDriver 接口一致 + ping）
_ALLOWED = {
    "ping", "insert", "query", "update", "delete", "create_table", "delete_by_pk",
    "drop_table", "rename_table", "add_column", "drop_column", "modify_column",
    "alter_precision", "add_foreign_key", "drop_foreign_key", "create_index",
    "recreate_table", "execute", "drop_index", "list_tables",
    "get_referencing_tables", "table_exists", "column_exists", "get_columns",
    "close", "begin", "commit", "rollback", "_get_unique_key_column",
}


class _Daemon:
    def __init__(self):
        self._dsm = None          # DataSourceManager（惰性，首调用时加载配置）
        self._drivers = {}        # ds_name → 真实驱动
        self._cfg_mtime = -1.0    # 加载配置时 datasources.yml 的 mtime
        self._sessions = {}       # session_id → ds_name（session 亲和）
        self._lock = threading.Lock()
        self._busy = False        # 工作线程执行中标志（ping 应答携带：忙≠死可区分，
                                  # 评审四轮 N2——快速道不能让卡死检测彻底消失）

    def _driver(self, ds_name: str, session: str = ""):
        # session 亲和（真实现）：驱动缓存按 （数据源， 会话） 分键——
        # 每个客户端会话在 daemon 侧独占驱动实例（独占连接集），
        # 事务状态互不串扰（A 的 rollback 不会吞 B 已提交的写）
        # 本函数由工作线程在持锁区调用（handle :103 的 with self._lock）——
        # 不许再持同一把锁（非重入锁自锁死锁的实测事故 20260822）
        # 配置新鲜度：datasources.yml 变了就整体重载并清驱动缓存
        #（防旧路径句柄复活——跨启动重注册同名数据源的实测事故）
        try:
            import os as _os
            mtime = _os.path.getmtime(
                _os.path.join(_rt.RUNTIME_DIR.parent, "datasources.yml"))
        except OSError:
            mtime = -1.0
        if self._dsm is not None and mtime != self._cfg_mtime:
            # 配置变更：清缓存前先逐个 close（Windows 文件锁不靠 GC 兜底）——
            # 进行中事务随连接关闭回滚（配置变更=管理员动作，属预期语义，如实注释）
            for d in self._drivers.values():
                try:
                    d.close()
                except Exception:
                    pass
            self._dsm = None
            self._drivers.clear()
            self._sessions.clear()
        key = (ds_name, session or "default")
        if key in self._drivers:
            return self._drivers[key]
        if self._dsm is None:
            from core.datasource_manager import DataSourceManager
            # 显式实例化（P2-4）：绕开单例——单例的驱动缓存按名字命中旧路径句柄
            #（跨启动重注册同名数据源的"unable to open"实测事故根因）
            self._dsm = DataSourceManager.new_instance()
            self._dsm.load_config()
            self._cfg_mtime = mtime
        drv = self._dsm.get_driver(ds_name)
        self._drivers[key] = drv
        return drv

    def handle(self, req: dict) -> dict:
        if not secrets.compare_digest(str(req.get("token") or ""), self.token):
            return {"ok": False, "error": "令牌无效", "error_kind": "auth"}
        method = req.get("method", "")
        if method not in _ALLOWED:
            return {"ok": False, "error": f"方法不允许: {method}", "error_kind": "method"}
        if method == "ping":
            return {"ok": True, "result": True}
        args = req.get("args") or {}
        session = req.get("session", "")
        logger.info("收到调用: %s(%s)", method, args.get("datasource", ""))
        import time as _t
        _t0 = _t.time()
        # 调用方角色随调用传递（contextvar 不过进程，权限判定在 daemon 侧以真实角色进行）。
        # 信任模型（20260822 定稿）：daemon 是「加密数据的进程隔离边界」，不是认证边界——
        # 认证/授权在 mgmt（Bearer/API-Key 路由级校验）与 MCP（MCP_ROLE + 提权审批契约）层。
        # 令牌文件由系统级隔离脚本 ACL 锁给当前用户，跨用户窃取在 OS 层拦截；
        # 同用户本地进程属威胁模型外（可读令牌者同样可读提权契约，钳制无实益反而
        # 拦截管理端自身已鉴权的 admin/system 流——实测：daemon 默认模式下 UI 建表被拒）。
        role = args.pop("_role", "")
        ds_name = args.pop("datasource", "") or args.pop("_ds", "")
        if not ds_name:
            return {"ok": False, "error": "缺 datasource", "error_kind": "args"}
        try:
            drv = self._driver(ds_name, session)
            from core.permission import set_current_role
            if role:
                set_current_role(role)
            try:
                # session 亲和：同 session 的调用在同一驱动实例内串行（事务正确性）
                self._busy = True
                with self._lock:
                    result = getattr(drv, method)(**args)
                if method == "close":
                    # 释放本会话的全部驱动（文件句柄+缓存），防 Windows 文件锁残留
                    for k in [k for k in self._drivers if k[1] == (session or "default")]:
                        try:
                            self._drivers[k].close()
                        except Exception:
                            pass
                        self._drivers.pop(k, None)
            finally:
                self._busy = False
                if role:
                    set_current_role("system")
            logger.info("完成: %s 耗时 %.1fs", method, _t.time() - _t0)
            return {"ok": True, "result": result}
        except Exception as e:
            import traceback as _tb
            logger.warning("调用失败 %s(%s): %s\n%s", method, ds_name, e,
                           _tb.format_exc()[:800])
            return {"ok": False, "error": str(e)[:500],
                    "error_kind": type(e).__name__}

    def serve(self):
        self.token = secrets.token_urlsafe(24)
        port = _rt._find_free_port()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(16)
        srv.settimeout(2.0)
        _rt.write_runtime(port, self.token, __import__("os").getpid())
        logger.info("daemon 就绪：127.0.0.1:%s（pid 文件已写）", port)

        # 单工作线程 + 队列：全部驱动调用落在同一线程 → 驱动的线程局部连接
        # 唯一稳定（事务/锁语义正确；管理级流量，串行开销可忽略）
        import queue as _q
        self._jobs = _q.Queue()
        worker = threading.Thread(target=self._work_loop, daemon=True)
        worker.start()
        try:
            while True:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._read_conn, args=(conn,),
                                 daemon=True).start()
        finally:
            _rt.clear_runtime()
            srv.close()

    def _read_conn(self, conn: socket.socket):
        """读请求入队（IO 线程），响应由工作线程写回"""
        try:
            buf = b""
            while b"\n" not in buf and len(buf) < (64 << 20):
                chunk = conn.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                conn.close()
                return
            req = decode(buf.split(b"\n", 1)[0])
            # ping 快速道：活性探测不排工作线程的队（单工作线程被长查询占用时，
            # ping 排队超时曾被误判为失联→拉起第二个 daemon 孤儿）。ping 不碰驱动、
            # 无事务语义，IO 线程验令牌后直接应答。
            if req.get("method") == "ping":
                if secrets.compare_digest(str(req.get("token") or ""), self.token):
                    conn.sendall(encode({"ok": True, "result": True,
                                         "busy": self._busy}))
                else:
                    conn.sendall(encode({"ok": False, "error": "令牌无效",
                                         "error_kind": "auth"}))
                conn.close()
                return
            self._jobs.put((req, conn))
        except Exception as e:
            logger.warning("连接读取失败: %s", e)
            conn.close()

    def _work_loop(self):
        while True:
            req, conn = self._jobs.get()
            try:
                conn.sendall(encode(self.handle(req)))
            except Exception as e:
                logger.warning("响应写回失败: %s", e)
            finally:
                conn.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [daemon] %(levelname)s %(message)s")
    # 环境契约自检（评审五轮 D2，fail-fast）：daemon 必须在 DAEMON_MODE=false 下
    # 运行——否则它建驱动时会走 DaemonDriver 自指 RPC：ping 快速道照常应答
    # （健康全绿）而业务调用自锁死（假活）。启动即失败，让人/计划任务重试机制
    # 立刻看到，而不是假活在那里。
    import os as _os
    if _os.environ.get("DAEMON_MODE", "").lower() != "false":
        logger.error("DAEMON_MODE 必须为 false（守护进程环境契约）——当前: %r，退出",
                     _os.environ.get("DAEMON_MODE"))
        raise SystemExit(2)
    # daemon 侧的契约层是数据面实现细节：force 确认闸（人审语义）已在调用方
    #（意图边界）评估过，内层重评会把已批准操作再次拦死——置类级直通开关
    from core.contract.base import ContractDriver
    ContractDriver.force_passthrough = True
    _Daemon().serve()


if __name__ == "__main__":
    main()
