"""多数据源管理器——联邦数据库的核心

配置驱动原则：
1. 所有数据源定义在 config/datasources.yml 中
2. 本模块只负责加载配置、创建 Driver 实例、路由查找
3. 不硬编码任何数据库连接信息

核心能力：
- 从 YAML 加载数据源配置
- 按数据源名创建对应的 Driver 实例（懒加载 + 缓存）
- 根据表名查找所属数据源（通过 schema 的 datasource 字段）
- 向后兼容：单数据源时行为与原 Steward.get_driver() 一致
"""

import os
import threading
from pathlib import Path


def resolve_sqlite_path(path: str) -> str:
    """sqlite 相对路径锚定项目根（data-engine/）——唯一实现。

    _create_driver 与联邦挂载写（pipeline/ingestion）消费同一实现：
    相对路径不靠 CWD 隐式约定（CWD 不同的进程会读到错误位置/新建空库）。
    """
    if not path:
        return path
    if not os.path.isabs(path):
        path = str(Path(__file__).resolve().parent.parent / path)
    return path


class DataSourceManager:
    """多数据源管理器（单例 + 线程安全）

    用法：
        dsm = DataSourceManager()          # 获取单例
        drv = dsm.get_driver()             # 获取默认数据源的 Driver
        drv = dsm.get_driver("remote_mysql")  # 获取指定数据源的 Driver
        drv = dsm.get_driver_for_table("quota_item")  # 按表名路由
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._config: dict = {}          # 数据源配置 {name: config_dict}
        self._default_name: str = ""     # 默认数据源名
        self._drivers: dict = {}         # 已创建的 Driver 实例 {name: Driver}
        self._table_map: dict = {}       # 表名(casefold) → 数据源名（键全小写规范形）
        self._driver_lock = threading.Lock()
        self._config_path = None         # 显式指定过的配置文件路径（None=默认生产配置）
        # 依赖倒置注册：契约层 drop_index 的对象归属解析钩子（契约层不向上
        # 感知本类——边方向 dsm→contract 单向）
        from core.contract.base import register_object_ds_resolver
        register_object_ds_resolver(self.resolve_object_datasource)
        # 同型注册：驱动面的唯一业务键解析与联邦驱动 dsm 提供者
        #（drivers ↛ schema_matcher/datasource_manager，边方向单向向下）
        from core.drivers.base import register_unique_key_resolver, \
            register_table_schema_loader
        from core.schema_matcher import get_unique_key_column, load_table_schema
        register_unique_key_resolver(get_unique_key_column)
        register_table_schema_loader(load_table_schema)
        from core.drivers.federated_driver import register_dsm_provider
        register_dsm_provider(DataSourceManager)
        # 同型注册：权限层（sql_guard）的表→数据源映射解析钩子
        from core.permission.sql_guard import register_table_ds_resolver
        register_table_ds_resolver(self.get_datasource_for_table,
                                   self.get_default_name)

    @classmethod
    def new_instance(cls, config_path=None) -> "DataSourceManager":
        """显式实例化入口（测试注入用）——绕过单例返回全新实例。

        与单例（DataSourceManager()）互不干扰，测试结束无需 reset _instance。
        """
        inst = super().__new__(cls)
        inst._initialized = False
        inst.__init__()
        if config_path:
            inst.load_config(config_path)
        return inst

    @classmethod
    def reset_instance(cls):
        """单例重置的公开入口（替代直接戳 _instance 私有属性）"""
        with cls._lock:
            cls._instance = None

    # ── 配置加载 ──

    def _get_config_path(self) -> Path:
        """获取数据源配置文件路径"""
        return Path(__file__).resolve().parent.parent / "config/datasources.yml"

    def _ensure_fresh(self):
        """ConfigHub 新鲜度守卫：datasources.yml 变了就增量重建（免 reload/restart）

        真解耦语义：文件即契约。配置变化时只重建受影响数据源的 Driver
        （未变的连接保留），表映射清空等下次注册。
        """
        from core.config_hub import load_yaml
        p = self._config_path or self._get_config_path()
        try:
            m = p.stat().st_mtime
        except OSError:
            return  # 文件不存在：保持现状（load_config 的 settings 兜底路径已覆盖）
        if m == getattr(self, "_config_mtime", None):
            return
        data = load_yaml(p, default={}, fail_policy="last_good")
        new_cfg = (data or {}).get("datasources", {})
        if not new_cfg:
            self._config_mtime = m
            return
        if new_cfg != self._config:
            removed = set(self._config) - set(new_cfg)
            changed = {n for n in new_cfg if new_cfg[n] != self._config.get(n)}
            for n in removed | changed:
                drv = self._drivers.pop(n, None)
                if drv is not None:
                    try:
                        drv.close()
                    except Exception:
                        pass  # 清理/关闭失败不影响主流程（OS 兜底回收）
            self._table_map.clear()
            self._config = new_cfg
            self._default_name = next(
                (n for n, c in new_cfg.items() if c.get("is_default")),
                next(iter(new_cfg), ""))
            if self._default_name and not any(
                    c.get("is_default") for c in new_cfg.values()):
                from core.logger import get_logger as _gl
                _gl(__name__).warning(
                    "数据源配置未声明 is_default，回退取第一个: %s"
                    "（yaml.dump sort_keys 会按字母序重排键——请显式声明 is_default）",
                    self._default_name)
        self._config_mtime = m

    def load_config(self, config_path=None):
        """从数据源配置文件加载配置

        Args:
            config_path: 可选配置文件路径（str 或 Path）。
                默认 None = config/datasources.yml（生产配置）。
                测试可传入 tests/fixtures/datasources.yml 实现测试/生产配置隔离。
                显式传入后会被记住：后续无参 load_config() 沿用同一路径，
                避免框架内部的裸 load_config()（如 FederatedDriver 初始化）
                把测试配置覆盖回生产配置。

        如果配置文件不存在，自动从 settings 创建默认单数据源配置（向后兼容）
        """
        if config_path:
            self._config_path = Path(config_path)
        config_path = self._config_path or self._get_config_path()

        if config_path.exists():
            from core.config_hub import load_yaml
            data = load_yaml(config_path, default={}, fail_policy="last_good") or {}
            self._config = data.get("datasources", {})
            try:
                self._config_mtime = config_path.stat().st_mtime
            except OSError:
                self._config_mtime = None
        else:
            # 向后兼容：无配置文件时，从 settings 创建默认数据源
            from config.settings import settings as s
            if s.db_type == "mysql":
                self._config = {
                    "primary": {
                        "type": "mysql",
                        "host": s.MYSQL_HOST,
                        "port": int(s.MYSQL_PORT),
                        "user": s.MYSQL_USER,
                        "password": s.MYSQL_PASSWORD,
                        "database": s.MYSQL_DATABASE,
                        "is_default": True,
                    }
                }
            else:
                self._config = {
                    "primary": {
                        "type": "sqlite",
                        "path": s.SQLITE_DB_PATH,
                        "is_default": True,
                    }
                }

        # 确定默认数据源
        self._default_name = ""
        for name, cfg in self._config.items():
            if cfg.get("is_default", False):
                self._default_name = name
                break
        # 如果没有标记 is_default，取第一个作为默认
        if not self._default_name and self._config:
            self._default_name = list(self._config.keys())[0]
            from core.logger import get_logger as _gl
            _gl(__name__).warning(
                "数据源配置未声明 is_default，回退取第一个: %s"
                "（yaml.dump sort_keys 会按字母序重排键——字母序靠前者并非"
                "业务默认库时路由将静默错库，请显式声明 is_default）",
                self._default_name)

        # 数据源名加载期校验：非法标识符若迟爆到 ATTACH 别名/
        # saga 校验才炸，排错链路长；名字一进配置就如实拒绝
        from core.contract.security_contract import is_valid_identifier
        for _n in self._config:
            if not is_valid_identifier(_n):
                raise ValueError(
                    f"数据源名 '{_n}' 非法（须为合法 SQL 标识符：字母/数字/下划线，"
                    "字母开头）——ATTACH 别名与联邦路由都消费它，加载期即拒绝")

    def reload_config(self):
        """重新加载配置（热重载，清除已缓存的 Driver 实例）"""
        with self._driver_lock:
            # 关闭所有已创建的 Driver
            for drv in self._drivers.values():
                try:
                    drv.close()
                except Exception:
                    pass  # 清理/关闭失败不影响主流程（OS 兜底回收）
            self._drivers.clear()
            self._table_map.clear()
        self.load_config()

    # ── Driver 创建 ──

    @staticmethod
    def _build_sqlite(cfg: dict):
        from core.drivers.sqlite_driver import SqliteDriver
        path = resolve_sqlite_path(cfg.get("path", "./db/data_engine.db"))
        return SqliteDriver(db_path=path)

    @staticmethod
    def _build_mysql(cfg: dict):
        from core.drivers.mysql_driver import MysqlDriver
        return MysqlDriver(
            host=cfg.get("host", "localhost"),
            port=int(cfg.get("port", 3306)),
            user=cfg.get("user", "root"),
            password=cfg.get("password", ""),
            database=cfg.get("database", ""),
        )

    # 驱动类型注册表：新增数据库种类 = 实现 Driver 接口 + 在此登记工厂，
    # 调用面零改动（datasources.yml 的 type 字段即注册键）
    _DRIVER_FACTORIES = None  # 类级懒初始化（避免模块级硬引用全部驱动）

    @classmethod
    def _factories(cls) -> dict:
        if cls._DRIVER_FACTORIES is None:
            cls._DRIVER_FACTORIES = {
                "sqlite": cls._build_sqlite,
                "mysql": cls._build_mysql,
                # "postgresql": cls._build_postgresql,  # 驱动尚未实现——实现后在此登记
            }
        return cls._DRIVER_FACTORIES

    def _create_driver(self, name: str, cfg: dict):
        """根据配置创建 Driver 实例（注册表分派，未知类型如实报错）"""
        db_type = cfg.get("type", "sqlite").lower()
        factory = self._factories().get(db_type)
        if factory is None:
            known = "、".join(sorted(self._factories()))
            raise ValueError(f"不支持的数据源类型: {db_type}（已注册: {known}）")
        return factory(cfg)

    def get_driver(self, name: str = None):
        """获取 Driver 实例（已包装为 ContractDriver，自动应用所有契约保护）

        契约保护包括：
        - 标识符/WHERE/主键/CHECK 安全校验（SecurityContract）
        - 类型变更风险评估（TypeContract）
        - 表结构变更差异分析（SchemaChangeContract）
        - 数据 CRUD 类型校验 + 批量上限 + WHERE 主键要求（DataCrudContract）
        - 异常翻译为统一中文（ErrorTranslator）

        Args:
            name: 数据源名（None=默认数据源）

        Returns:
            ContractDriver 实例（透明包装原始 Driver）
        """
        self._ensure_fresh()
        if not self._config:
            self.load_config()

        if name is None:
            name = self._default_name

        if name not in self._config:
            raise ValueError(f"数据源 '{name}' 未注册。已注册: {list(self._config.keys())}")

        # 懒加载 + 缓存
        if name not in self._drivers:
            with self._driver_lock:
                if name not in self._drivers:
                    from config.settings import settings as _st
                    if _st.DAEMON_MODE_EFFECTIVE == "true":
                        # 守护进程模式：驱动调用经 daemon（密钥驻其内存，本进程只见令牌）
                        from core.daemon.client import DaemonDriver
                        raw_driver = DaemonDriver(name)
                    else:
                        raw_driver = self._create_driver(name, self._config[name])
                    # 用 ContractDriver 包装（契约层保护）
                    from core.contract import ContractDriver
                    if not isinstance(raw_driver, ContractDriver):
                        db_type = self._config[name].get("type", "sqlite").lower()
                        raw_driver = ContractDriver(raw_driver, db_type)
                    self._drivers[name] = raw_driver

        return self._drivers[name]

    def get_driver_type(self, name: str = None) -> str:
        """获取数据源的 driver 类型名

        Args:
            name: 数据源名（None=默认数据源）

        Returns:
            driver 类型字符串（如 "sqlite"/"mysql"）
        """
        self._ensure_fresh()
        if not self._config:
            self.load_config()
        if name is None:
            name = self._default_name
        return self._config.get(name, {}).get("type", "sqlite").lower()

    # ── 表级路由 ──

    def register_table(self, table: str, datasource: str = None):
        """注册表到数据源的映射

        Args:
            table: 表名
            datasource: 数据源名（None=默认数据源）
        """
        if datasource is None:
            datasource = self._default_name
        self._table_map[table.casefold()] = datasource

    def register_tables_from_schemas(self, schemas: list[dict]):
        """从 schema 列表批量注册表到数据源的映射

        Args:
            schemas: schema 字典列表，每个含 name 和可选 datasource 字段
        """
        if not self._config:
            self.load_config()
        for schema in schemas:
            table_name = schema.get("name", "")
            ds_name = schema.get("datasource", "")
            if table_name:
                if ds_name and ds_name in self._config:
                    self._table_map[table_name.casefold()] = ds_name
                else:
                    self._table_map[table_name.casefold()] = self._default_name

    def get_driver_for_table(self, table: str):
        """根据表名查找所属数据源的 Driver

        如果表未注册，遍历所有数据源查找并缓存映射（向后兼容）
        """
        if not self._config:
            self.load_config()

        ds_name = self._table_map.get(table.casefold())
        if ds_name:
            return self.get_driver(ds_name)
        # 联邦数据库：表未注册时遍历所有数据源查找（一次查找，缓存结果）
        for ds_info in self.list_datasources():
            try:
                drv = self.get_driver(ds_info["name"])
                if drv.table_exists(table):
                    self._table_map[table.casefold()] = ds_info["name"]  # 缓存映射
                    return drv
            except Exception:
                continue
        # 所有数据源都未找到，返回默认数据源（让调用方收到正常的错误信息）
        return self.get_driver(self._default_name)

    def get_datasource_for_table(self, table: str) -> str:
        """获取表所属的数据源名"""
        self._ensure_fresh()
        if not self._config:
            self.load_config()
        return self._table_map.get(table.casefold(), self._default_name)

    def resolve_object_datasource(self, name: str, kind: str = "index") -> tuple:
        """目标描述符：跨数据源定位无表归属的对象
        （index/trigger/view），返回 (数据源名 | None, 状态)。

        判定与执行同源的根基：drop_index 等无表维度的操作，先解析对象在
        哪个数据源，权限判定与路由执行都消费同一个结果——不再各自猜默认库。
        状态："ok"（唯一命中）/ "not_found"（无处可判，调用方按默认语义走）/
        "ambiguous"（多数据源同名，如实报错，绝不静默选一个）。
        """
        from core.exceptions import AppError
        hits = []
        for ds_info in self.list_datasources():
            ds_name = ds_info["name"]
            cached = ds_name in self._drivers
            try:
                drv = self.get_driver(ds_name)
                rows = self._query_object(drv, ds_info.get("type", "sqlite"), name, kind)
                if rows:
                    hits.append(ds_name)
            except Exception:
                continue  # 单源探测失败不排除其余源（尽力定位）
            finally:
                if not cached:
                    # 探测新铸的驱动用完即关（它有独立 session，无人替它收尾——
                    # 不关则 daemon 侧长期握着该库文件句柄，Windows 锁残留）
                    d = self._drivers.pop(ds_name, None)
                    if d is not None:
                        try:
                            d.close()
                        except Exception:
                            pass  # 关闭失败——会话 idle TTL 兜底回收
        if len(hits) > 1:
            raise AppError(f"{kind} '{name}' 在多个数据源同名（{', '.join(hits)}），"
                           "无法确定操作目标，请显式指定数据源")
        return (hits[0] if hits else None, "ok" if hits else "not_found")

    @staticmethod
    def _query_object(drv, ds_type: str, name: str, kind: str) -> int:
        """在单个数据源内查对象存在数（方言分支：sqlite_master / information_schema）。
        drv.query 只收 SQL 文本——名字按字面值 doubling 转义内联（字符集来自标识符校验上游）"""
        kind_map = {"index": ("index", "STATISTICS", "INDEX_NAME"),
                    "trigger": ("trigger", "TRIGGERS", "TRIGGER_NAME"),
                    "view": ("view", "VIEWS", "TABLE_NAME")}
        sqlite_type, is_schema, is_col = kind_map.get(kind, kind_map["index"])
        lit = "'" + str(name).replace("'", "''") + "'"
        if ds_type == "mysql":
            sql = (f"SELECT COUNT(*) AS c FROM information_schema.{is_schema} "
                   f"WHERE {is_col} = {lit}")
        else:
            sql = f"SELECT COUNT(*) AS c FROM sqlite_master WHERE name = {lit} AND type = '{sqlite_type}'"
        rows = drv.query(sql)
        if not rows:
            return 0
        first = rows[0]
        return int(next(iter(first.values())))

    # ── 元数据查询 ──

    def list_datasources(self) -> list[dict]:
        """列出所有已注册数据源"""
        self._ensure_fresh()
        if not self._config:
            self.load_config()
        result = []
        for name, cfg in self._config.items():
            result.append({
                "name": name,
                "type": cfg.get("type", "sqlite"),
                "is_default": name == self._default_name,
                "host": cfg.get("host", ""),
                "database": cfg.get("database", cfg.get("path", "")),
                "table_count": sum(1 for ds in self._table_map.values() if ds == name),
            })
        return result

    def get_default_name(self) -> str:
        """获取默认数据源名"""
        self._ensure_fresh()
        if not self._config:
            self.load_config()
        return self._default_name

    def test_connection(self, name: str) -> dict:
        """测试数据源连接

        Returns:
            {"ok": bool, "message": str, "tables": int}
        """
        try:
            if not self._config:
                self.load_config()
            if name not in self._config:
                return {"ok": False, "message": f"数据源 '{name}' 未注册"}

            # 与生产同通道：get_driver 在 daemon 模式发
            # DaemonDriver（密钥只驻 daemon 进程）、本地模式发契约包装驱动；
            # 此前 _create_driver 裸建直连——密钥进管理端进程内存、且 ping 完
            # 不 close 泄漏句柄。缓存驱动由单例复用，无泄漏面
            drv = self.get_driver(name)

            ok = drv.ping()
            if ok:
                tables = drv.list_tables()
                return {"ok": True, "message": f"连接成功（{len(tables)} 张表）", "tables": len(tables)}
            else:
                return {"ok": False, "message": "连接失败：ping 返回 False"}
        except Exception as e:
            return {"ok": False, "message": f"连接失败: {e}"}


# ── 全局单例快捷函数 ──

def get_datasource_manager() -> DataSourceManager:
    """获取 DataSourceManager 单例"""
    return DataSourceManager()


# 自注册到重置注册表：行业切换遍历即覆盖
from core.registry import register_reset

register_reset("datasource_manager", DataSourceManager.reset_instance)
