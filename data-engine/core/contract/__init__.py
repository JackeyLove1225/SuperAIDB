"""契约模块——所有数据库操作的横切关注点集中管理

架构：
- SecurityContract: 安全校验（标识符/WHERE/主键/CHECK）
- TypeContract: 类型变更风险评估 + 数据兼容性扫描
- ChangeAnalyzer: Schema 变更差异分析（12 类变更检测）
- SchemaChangeContract: DDL 操作前置分析（precheck + assert_can_*）
- DataCrudContract: DML 操作前置校验（insert/update/delete）
- ErrorTranslator: 异常翻译为统一中文（按 driver 类型路由）
- ContractDriver: 包装原始 Driver，自动应用所有契约

使用方式：
    from core.contract import ContractDriver
    raw_drv = DataSourceManager().get_driver()
    drv = ContractDriver(raw_drv)  # 自动获得所有契约保护

新增 driver 时：
    1. 实现 core/drivers/xxx_driver.py（继承 Driver，实现 27 个原子操作）
    2. 在 DataSourceManager 配置 type: xxx
    3. 可选：ErrorTranslator.register("xxx", [...]) 注册错误翻译
    无需关心任何契约逻辑——ContractDriver 自动应用所有保护。
"""
from .base import ContractDriver
from .security_contract import (
    SecurityContract,
    is_valid_identifier,
    safe_table_sql,
    safe_column_sql,
    safe_index_sql,
    safe_pragma_arg,
    safe_savepoint_name,
)
from .type_contract import TypeContract, TypeChangeRisk
from .change_analyzer import ChangeAnalyzer, ChangeReport, Change
from .schema_change_contract import SchemaChangeContract
from .data_crud_contract import DataCrudContract
from .error_translator import ErrorTranslator

__all__ = [
    "ContractDriver",
    "SecurityContract",
    # 便捷校验函数（用于 f-string SQL 内联校验）
    "is_valid_identifier",
    "safe_table_sql",
    "safe_column_sql",
    "safe_index_sql",
    "safe_pragma_arg",
    "safe_savepoint_name",
    "TypeContract",
    "TypeChangeRisk",
    "ChangeAnalyzer",
    "ChangeReport",
    "Change",
    "SchemaChangeContract",
    "DataCrudContract",
    "ErrorTranslator",
]
