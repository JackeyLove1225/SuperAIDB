"""DaemonDriver——同一套 29 驱动接口的 RPC 代理实现（上层零感知切换）

29 接口抽象的设计回报：数据层进 daemon 进程后，上层（MCP/管理 API/工具层）
只需把 DataSourceManager.get_driver 的实现从直连换成 DaemonDriver——
业务代码一行不改。

角色随调用传递：调用方进程的生效角色（get_effective_role）随每个 RPC 带到
daemon，权限判定在 daemon 侧以真实角色进行（contextvar 不过进程）。
"""
from core.logger import get_logger
import uuid

from core.daemon.protocol import rpc_call
from core.daemon.runtime import ensure_daemon
from core.drivers.base import Driver

logger = get_logger(__name__)


class DaemonDriver(Driver):
    """RPC 代理驱动：继承 Driver ABC 实现 29 接口（编译期符合性校验），
    全部转发到 daemon 进程。"""

    def __init__(self, datasource: str = "primary"):
        self._ds = datasource
        # session 亲和按线程生成（threading.local）：管理端多线程共享一个
        # DaemonDriver 实例时，跨线程共用 session 会让 daemon 侧单连接交叉
        # commit（A 线程在途写被 B 线程连带提交，评审四轮 N8）
        import threading as _th
        self._local = _th.local()
        rt = ensure_daemon()
        self._port, self._token = rt["port"], rt["token"]

    @property
    def _session(self) -> str:
        """本线程的 session id（懒生成）——同线程内保持事务亲和"""
        s = getattr(self._local, "session", None)
        if s is None:
            s = uuid.uuid4().hex
            self._local.session = s
        return s

    # ── RPC 主通道 ──
    def _call(self, method: str, **kwargs):
        from core.permission import get_effective_role
        from core.daemon.runtime import ensure_daemon, in_maintenance, read_runtime
        if method != "close" and in_maintenance():
            raise RuntimeError("系统维护中（数据恢复/迁移），请稍后重试")
        args = {"datasource": self._ds, "_role": get_effective_role(), **kwargs}
        try:
            return rpc_call(self._port, self._token, method, args,
                            session=self._session).get("result")
        except ConnectionRefusedError as e:
            conn_err = e  # 对面真死（拒连）——进入自愈；发送前失败，无重复执行面
        except (ConnectionError, TimeoutError, OSError):
            # 超时不重试（评审四轮 N1）：写操作可能仍在 daemon 工作线程执行，
            # 重试=重复执行；重置/断管同样可能已执行——如实上抛，不猜
            raise
        # 自愈（daemon 重启后端口/令牌换新）：重读运行文件重连一次；
        # 运行文件未变（真死）→ 按需重拉。旧实现把 port/token 固化在
        # 构造期——daemon 一旦重启，管理端全部数据调用打旧端口直到进程重启
        rt = read_runtime()
        if rt and (rt["port"] != self._port or rt["token"] != self._token):
            self._port, self._token = rt["port"], rt["token"]
        elif in_maintenance():
            raise conn_err  # 维护窗口期不重拉（恢复流程正在独占数据层）
        else:
            rt = ensure_daemon()
            self._port, self._token = rt["port"], rt["token"]
        return rpc_call(self._port, self._token, method, args,
                        session=self._session).get("result")

    # ── 29 接口转发 ──
    def insert(self, table, rows, overwrite=False):
        return self._call("insert", table=table, rows=rows, overwrite=overwrite)

    def query(self, sql):
        return self._call("query", sql=sql)

    def update(self, table, set_clause, where=""):
        return self._call("update", table=table, set_clause=set_clause, where=where)

    def delete(self, table, where):
        return self._call("delete", table=table, where=where)

    def create_table(self, table_config):
        return self._call("create_table", table_config=table_config)

    def delete_by_pk(self, table, pk_column, pk_value):
        return self._call("delete_by_pk", table=table, pk_column=pk_column,
                          pk_value=pk_value)

    def _get_unique_key_column(self, table):
        return self._call("_get_unique_key_column", table=table)

    def drop_table(self, table):
        return self._call("drop_table", table=table)

    def rename_table(self, table, new_name):
        return self._call("rename_table", table=table, new_name=new_name)

    def add_column(self, table, column, col_type, precision=None, not_null=False):
        return self._call("add_column", table=table, column=column,
                          col_type=col_type, precision=precision, not_null=not_null)

    def drop_column(self, table, column):
        return self._call("drop_column", table=table, column=column)

    def modify_column(self, table, column, new_type):
        return self._call("modify_column", table=table, column=column,
                          new_type=new_type)

    def alter_precision(self, table, column, new_precision):
        return self._call("alter_precision", table=table, column=column,
                          new_precision=list(new_precision) if new_precision else None)

    def add_foreign_key(self, table, column, ref_table, ref_column="id"):
        return self._call("add_foreign_key", table=table, column=column,
                          ref_table=ref_table, ref_column=ref_column)

    def drop_foreign_key(self, table, constraint_name):
        return self._call("drop_foreign_key", table=table,
                          constraint_name=constraint_name)

    def create_index(self, table, columns, unique=False):
        return self._call("create_index", table=table, columns=columns,
                          unique=unique)

    def recreate_table(self, table_config):
        return self._call("recreate_table", table_config=table_config)

    def execute(self, sql):
        return self._call("execute", sql=sql)

    def drop_index(self, name):
        return self._call("drop_index", name=name)

    def list_tables(self):
        return self._call("list_tables")

    def get_referencing_tables(self, table):
        return self._call("get_referencing_tables", table=table)

    def table_exists(self, table):
        return self._call("table_exists", table=table)

    def column_exists(self, table, column):
        return self._call("column_exists", table=table, column=column)

    def get_columns(self, table):
        return self._call("get_columns", table=table)

    def ping(self):
        return self._call("ping")

    def close(self):
        return self._call("close")

    def begin(self, name=""):
        return self._call("begin", name=name)

    def commit(self):
        return self._call("commit")

    def rollback(self, name=""):
        return self._call("rollback", name=name)
