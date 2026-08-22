"""联邦驱动——Driver 接口的路由代理

核心设计：
1. 实现 Driver 基类的全部 27 个抽象方法，满足 LSP（里氏替换原则）
2. 单库操作：解析表名 → 路由到对应物理 Driver（透明转发）
3. 跨库查询：直接 SQL 抛异常并引导 join_query（跨库 JOIN 由应用层编排）
4. 元数据操作：聚合所有数据源的结果

配置驱动：
- 路由依据来自 schema YAML 的 datasource 字段（通过 DataSourceManager 的表映射）
- 不在代码中硬编码任何表到数据源的映射关系

权限单栈（20260822 收口）：本层不再自带权限检查——DataSourceManager.get_driver
发出的每个驱动都已包 ContractDriver，表级/列级/裸 SQL 护栏在契约层统一执行。
历史上本层另写一份同款检查，列名大小写绕过、子查询星号泄露两次同步漂移
（权限矩阵复盘），故收口。本层只保留职责本体：路由 + 脏驱动事务广播。

用法：
    from core.drivers.federated_driver import FederatedDriver
    fed = FederatedDriver()
    rows = fed.query('SELECT * FROM quota_item')  # 自动路由到 quota_item 所属数据源
"""

from .base import Driver


class FederatedDriver(Driver):
    """联邦驱动——Driver 接口的路由代理

    对调用方完全透明：调用方不需要知道数据在哪个库
    内部根据表名 → DataSourceManager 的表映射 → 对应物理 Driver
    （每个物理 Driver 均已由 DataSourceManager 包装 ContractDriver——权限在契约层）
    """

    def __init__(self):
        from core.datasource_manager import DataSourceManager
        self._dsm = DataSourceManager()
        self._dsm.load_config()
        # 脏驱动集合：本事务/本操作链中执行过写操作的物理 Driver
        # commit/rollback 时对这些驱动逐一提交/回滚（见 commit() 注释）
        self._dirty_drivers: set = set()

    def _mark_dirty(self, drv: Driver):
        """登记执行过写操作的物理驱动，供 commit/rollback 逐一处理"""
        self._dirty_drivers.add(drv)

    def _get_driver_for_table(self, table: str) -> Driver:
        """根据表名获取对应的物理 Driver"""
        return self._dsm.get_driver_for_table(table)

    def _get_default_driver(self) -> Driver:
        """获取默认数据源的 Driver"""
        return self._dsm.get_driver()

    # ── DML：数据操作（按表名路由；权限在契约层单栈执行）──

    def insert(self, table: str, rows: list[dict], overwrite: bool = False) -> dict:
        """批量插入——按表名路由"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.insert(table, rows, overwrite)

    def query(self, sql: str) -> list[dict]:
        """SELECT 查询——自动路由

        读权限（表级校验 + 列级屏蔽）在 ContractDriver.query 单栈执行；
        本方法只做路由：单表/同库多表直接转发，跨库抛异常引导 join_query。
        """
        from core.contract.security_contract import extract_tables_from_sql
        tables = extract_tables_from_sql(sql)
        if len(tables) == 0:
            # 无法识别表名（如 PRAGMA 查询），走默认数据源
            return self._get_default_driver().query(sql)
        elif len(tables) == 1:
            # 单表查询——直接路由
            return self._get_driver_for_table(tables[0]).query(sql)
        else:
            # 多表——检查是否在同一数据源
            datasources = set()
            for t in tables:
                datasources.add(self._dsm.get_datasource_for_table(t))
            if len(datasources) == 1:
                # 同库多表——直接转发
                ds_name = datasources.pop()
                return self._dsm.get_driver(ds_name).query(sql)
            else:
                raise RuntimeError(
                    f"跨库查询不支持直接 SQL：表 {tables} 分布在不同数据源 {datasources}。"
                    f"请使用 join_query() 进行跨库关联查询。"
                )

    def update(self, table: str, set_clause: str, where: str = "") -> dict:
        """条件更新——按表名路由"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.update(table, set_clause, where)

    def delete(self, table: str, where: str) -> dict:
        """条件删除——按表名路由"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.delete(table, where)

    def delete_by_pk(self, table: str, pk_column: str, pk_value) -> dict:
        """按主键删除——按表名路由，参数化绑定（P1-10）"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.delete_by_pk(table, pk_column, pk_value)

    def _get_unique_key_column(self, table: str) -> str:
        """表的唯一业务键列名（P1-2 接口契约）——按表名路由"""
        drv = self._get_driver_for_table(table)
        return drv._get_unique_key_column(table)

    # ── DDL：表结构变更（按表名路由；权限在契约层单栈执行）──

    def create_table(self, table_config: dict) -> str:
        """建表——根据 table_config 中的 datasource 字段路由（缺省用默认数据源）

        跨数据源外键处理：如果外键引用的表在不同数据源中，
        则跳过该外键的物理创建（仅在 schema 层面记录关系）。
        DDL 权限在契约层按目标数据源校验。
        """
        import copy
        ds_name = table_config.get("datasource", "")
        if ds_name and ds_name != self._dsm.get_default_name():
            drv = self._dsm.get_driver(ds_name)
        else:
            drv = self._get_default_driver()
        self._mark_dirty(drv)

        # 过滤跨数据源外键——避免物理驱动建表时因引用表不存在而失败
        table_name = table_config.get("name", "")
        fks = table_config.get("foreign_keys", [])
        if fks and ds_name:
            filtered_config = copy.deepcopy(table_config)
            kept_fks = []
            skipped_fks = []
            for fk in fks:
                ref_table = fk.get("references", "")
                ref_ds = self._dsm.get_datasource_for_table(ref_table)
                if ref_ds == ds_name:
                    kept_fks.append(fk)
                else:
                    skipped_fks.append(fk)
            filtered_config["foreign_keys"] = kept_fks
            if skipped_fks:
                print(f"  [联邦] 跳过跨数据源外键: {table_name} → {[(fk.get('references'), fk.get('columns')) for fk in skipped_fks]}（仅在schema层面记录）")
            result = drv.create_table(filtered_config)
        else:
            result = drv.create_table(table_config)

        # 注册表到数据源映射
        if table_name:
            self._dsm.register_table(table_name, ds_name or self._dsm.get_default_name())
        return result

    def drop_table(self, table: str) -> str:
        """删表——按表名路由"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.drop_table(table)

    def rename_table(self, table: str, new_name: str) -> str:
        """重命名表——路由并更新表映射"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        result = drv.rename_table(table, new_name)
        # 更新表映射
        ds_name = self._dsm.get_datasource_for_table(table)
        self._dsm.register_table(new_name, ds_name)
        return result

    def add_column(self, table: str, column: str, col_type: str, precision=None, not_null=False) -> dict:
        """加字段——按表名路由"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.add_column(table, column, col_type, precision, not_null)

    def drop_column(self, table: str, column: str) -> dict:
        """删字段——按表名路由"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.drop_column(table, column)

    def modify_column(self, table: str, column: str, new_type: str) -> dict:
        """改字段类型——按表名路由"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.modify_column(table, column, new_type)

    def alter_precision(self, table: str, column: str, new_precision: tuple) -> dict:
        """改字段精度——按表名路由"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.alter_precision(table, column, new_precision)

    def add_foreign_key(self, table: str, column: str, ref_table: str, ref_column: str = "id") -> dict:
        """添加外键——按表名路由

        注意：跨库外键在数据库层面不生效，仅在 schema 层面记录关系
        """
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.add_foreign_key(table, column, ref_table, ref_column)

    def drop_foreign_key(self, table: str, constraint_name: str) -> dict:
        """删除外键约束——按表名路由"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.drop_foreign_key(table, constraint_name)

    def create_index(self, table: str, columns: str, unique: bool = False) -> str:
        """创建索引——按表名路由"""
        drv = self._get_driver_for_table(table)
        self._mark_dirty(drv)
        return drv.create_index(table, columns, unique)

    def drop_index(self, name: str) -> str:
        """删除索引——走默认数据源（索引名全局唯一；DROP 权限在契约层判定）"""
        drv = self._get_default_driver()
        self._mark_dirty(drv)
        return drv.drop_index(name)

    def recreate_table(self, table_config: dict) -> dict:
        """重建表——按表名路由"""
        table_name = table_config.get("name", "")
        if table_name:
            drv = self._get_driver_for_table(table_name)
        else:
            drv = self._get_default_driver()
        self._mark_dirty(drv)
        return drv.recreate_table(table_config)

    def execute(self, sql: str):
        """执行任意 SQL——走默认数据源

        注意：此方法用于内部多步骤操作，只在默认数据源上执行。
        纵深校验（单语句/禁注释/首关键字白名单/裸 SQL 权限护栏/审计日志）
        全部在 ContractDriver.execute 单栈执行。
        """
        drv = self._get_default_driver()
        self._mark_dirty(drv)
        drv.execute(sql)

    # ── 元数据（聚合所有数据源）──

    def list_tables(self) -> list[str]:
        """列出所有数据源的所有表（聚合）"""
        all_tables = []
        for ds_info in self._dsm.list_datasources():
            ds_name = ds_info["name"]
            try:
                drv = self._dsm.get_driver(ds_name)
                tables = drv.list_tables()
                all_tables.extend(tables)
            except Exception:
                # 某个数据源不可用时跳过，不影响其他数据源
                continue
        return sorted(set(all_tables))

    def get_referencing_tables(self, table: str) -> list[dict]:
        """返回外键引用了本表的所有表——按表名路由"""
        return self._get_driver_for_table(table).get_referencing_tables(table)

    def table_exists(self, table: str) -> bool:
        """检查表是否存在（跨所有数据源）"""
        # 先检查表映射中是否已注册
        ds_name = self._dsm.get_datasource_for_table(table)
        drv = self._dsm.get_driver(ds_name)
        if drv.table_exists(table):
            return True
        # 如果映射中没有，遍历所有数据源查找
        for ds_info in self._dsm.list_datasources():
            if ds_info["name"] == ds_name:
                continue
            try:
                drv = self._dsm.get_driver(ds_info["name"])
                if drv.table_exists(table):
                    # 找到了，注册映射
                    self._dsm.register_table(table, ds_info["name"])
                    return True
            except Exception:
                continue
        return False

    def column_exists(self, table: str, column: str) -> bool:
        """检查字段是否存在——按表名路由"""
        return self._get_driver_for_table(table).column_exists(table, column)

    def get_columns(self, table: str) -> list[dict]:
        """获取字段列表——按表名路由"""
        return self._get_driver_for_table(table).get_columns(table)

    # ── 辅助方法（委托给默认 Driver）──

    def _safe_where(self, where: str) -> bool:
        """WHERE 条件安全校验——委托给默认 Driver"""
        return self._get_default_driver()._safe_where(where)

    # ── 事务/连接（commit/rollback 广播到所有"脏"数据源）──

    def ping(self) -> bool:
        """检查默认数据源连接"""
        return self._get_default_driver().ping()

    def close(self):
        """关闭所有数据源的连接"""
        for ds_info in self._dsm.list_datasources():
            ds_name = ds_info["name"]
            try:
                drv = self._dsm.get_driver(ds_name)
                drv.close()
            except Exception:
                pass

    def _collect_tx_drivers(self) -> list:
        """收集需要提交/回滚的物理驱动：默认数据源 + 全部脏驱动（去重，默认在前）"""
        default_drv = self._get_default_driver()
        drivers = [default_drv]
        drivers.extend(d for d in self._dirty_drivers if d is not default_drv)
        return drivers

    def begin(self, name: str = ""):
        """开启事务——作用于默认数据源

        注意：跨库事务无法原子协调（SQLite 跨库本来就不能一个事务，
        两阶段提交超出本层范围），跨库操作请使用幂等设计 + 补偿事务。
        带 name 的 SAVEPOINT 只在默认数据源上生效（SAVEPOINT 是连接级概念）。
        """
        self._get_default_driver().begin(name)

    def commit(self):
        """提交事务——对默认数据源 + 所有脏驱动逐一提交

        语义说明：跨库提交不是原子的，也不可能原子（各库是独立连接，
        SQLite 跨库无法共用一个事务）。目标语义是"每个涉及过的库都正确
        提交"，而非假原子。旧实现只提交默认数据源，导致跨库配置下
        非默认数据源的写入（如 insert 到 secondary）永远不落盘。

        某个数据源提交失败时，其余数据源仍会尝试提交，最后抛出第一个错误。
        """
        drivers = self._collect_tx_drivers()
        self._dirty_drivers.clear()
        first_err = None
        for drv in drivers:
            try:
                drv.commit()
            except Exception as e:
                if first_err is None:
                    first_err = e
        if first_err is not None:
            raise first_err

    def rollback(self, name: str = ""):
        """回滚事务——带 name 时回滚默认数据源的 SAVEPOINT；
        不带 name 时对默认数据源 + 所有脏驱动逐一回滚

        与 commit() 同理：跨库回滚不保证原子，只保证每个涉及过的库
        都被回滚。SAVEPOINT 是连接级概念，只存在于默认数据源上。
        """
        if name:
            self._get_default_driver().rollback(name)
            return
        drivers = self._collect_tx_drivers()
        self._dirty_drivers.clear()
        first_err = None
        for drv in drivers:
            try:
                drv.rollback()
            except Exception as e:
                if first_err is None:
                    first_err = e
        if first_err is not None:
            raise first_err
