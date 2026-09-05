"""兼容 shim——本模块已迁移到 core.check_templates

check_templates 是纯模板注册表（仅 re/typing 依赖），graph 不是它的家。
此处保留再导出，既有 import 路径不受影响。
"""
from core.check_templates import (
    PARAM_INT,
    PARAM_FLOAT,
    PARAM_STR,
    PARAM_INT_LIST,
    PARAM_FLOAT_LIST,
    PARAM_STR_LIST,
    STANDARD_TYPES,
    TEMPLATES_BY_TYPE,
    normalize_type,
    get_templates_by_type,
    get_all_templates_flat,
    get_template_by_key,
    render_expr,
    translate_for_dialect,
)

__all__ = [
    "PARAM_INT",
    "PARAM_FLOAT",
    "PARAM_STR",
    "PARAM_INT_LIST",
    "PARAM_FLOAT_LIST",
    "PARAM_STR_LIST",
    "STANDARD_TYPES",
    "TEMPLATES_BY_TYPE",
    "normalize_type",
    "get_templates_by_type",
    "get_all_templates_flat",
    "get_template_by_key",
    "render_expr",
    "translate_for_dialect",
]
