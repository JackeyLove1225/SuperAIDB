"""
数据库大管家——仅管理表结构变动操作（YAML-first）。
数据查询、数据写入等不涉及 YAML 的操作，不走大管家。

联邦数据库支持：通过 DataSourceManager 管理多数据源，
_get_driver() 返回默认数据源的 Driver（向后兼容）。
"""

from config.settings import settings as s


class Steward:
    """表结构操作的大管家——先 YAML，后 DB，不可绕过"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._driver = None
        return cls._instance

    def _get_driver(self, datasource: str = None):
        """获取数据源的 Driver（已包装为 ContractDriver，自动应用所有契约保护）

        联邦数据库模式：从 DataSourceManager 获取
        单数据源模式：行为与原来完全一致

        返回的 ContractDriver 透明包装原始 Driver：
        - 所有 DML 操作经 DataCrudContract（类型校验 + WHERE 主键要求 + 批量上限）
        - 所有 DDL 操作经 SchemaChangeContract/TypeContract（差异分析 + 风险评估）
        - 所有异常经 ErrorTranslator 翻译为统一中文
        - 主键 id 字段绝对保护（不可改类型/删除/重命名）

        Args:
            datasource: 数据源名（None=默认数据源，向后兼容）
                        传入了非 None 值时绕过缓存，每次都从 DSM 获取
                        以支持联邦数据库下多数据源切换
        """
        from core.contract import ContractDriver
        if datasource is None:
            # 默认数据源：使用缓存（向后兼容）
            if self._driver is None:
                from core.datasource_manager import DataSourceManager
                dsm = DataSourceManager()
                dsm.load_config()
                raw_driver = dsm.get_driver()  # 获取默认数据源的 Driver
                # 避免重复包装
                if isinstance(raw_driver, ContractDriver):
                    self._driver = raw_driver
                else:
                    self._driver = ContractDriver(raw_driver)
            return self._driver
        else:
            # 指定数据源：不缓存，每次从 DSM 获取（多数据源场景）
            from core.datasource_manager import DataSourceManager
            dsm = DataSourceManager()
            dsm.load_config()
            raw_driver = dsm.get_driver(datasource)
            if isinstance(raw_driver, ContractDriver):
                return raw_driver
            return ContractDriver(raw_driver)

    def create_table(self, name: str, columns: str):
        from .schema_manager import create_table as _ct
        return _ct(name, columns)

    def drop_table(self, name: str):
        from .schema_manager import drop_table as _dt
        return _dt(name)

    def rename_table(self, old: str, new: str):
        from .schema_manager import rename_table as _rt
        return _rt(old, new)

    def add_column(self, table: str, column: str, col_type: str = "TEXT"):
        from .schema_manager import add_column as _ac
        return _ac(table, column, col_type)

    def drop_column(self, table: str, column: str, force: bool = False):
        from .schema_manager import drop_column as _dc
        return _dc(table, column, force)

    def modify_column(self, table: str, column: str, new_type: str):
        from .schema_manager import modify_column as _mc
        return _mc(table, column, new_type)

    def alter_precision(self, table: str, column: str, precision: str):
        from .schema_manager import alter_precision as _ap
        return _ap(table, column, precision)

    def create_index(self, table: str, columns: str, unique: bool = False):
        from .schema_manager import create_index as _ci
        return _ci(table, columns, unique)

    def drop_index(self, name: str):
        from .schema_manager import drop_index as _di
        return _di(name)

    def add_foreign_key(self, table: str, column: str, ref_table: str, ref_column: str = ""):
        from .schema_manager import add_foreign_key as _afk
        return _afk(table, column, ref_table, ref_column)

    def drop_foreign_key(self, table: str, constraint_name: str = "", force: bool = False):
        from .schema_manager import drop_foreign_key as _dfk
        return _dfk(table, constraint_name, force)

    def batch_create_tables(self, definitions: list):
        from .schema_manager import batch_create_tables as _bct
        return _bct(definitions)

    def clear_database(self, drop_tables: bool = False):
        from .schema_manager import clear_database as _cd
        return _cd(drop_tables)

    def repair_tables(self, table: str = ""):
        from .schema_manager import repair_tables as _rt
        return _rt(table)
