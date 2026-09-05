"""兼容 shim——本模块已迁移到 core.checks

checks 是契约层（core.contract.security_contract）也在消费的共享校验，
drivers/ 不是它的家。此处保留再导出，既有 import 路径不受影响。
"""
from core.checks import (
    validate_type_name,
    validate_where,
    validate_check_expr,
)

__all__ = [
    "validate_type_name",
    "validate_where",
    "validate_check_expr",
]
