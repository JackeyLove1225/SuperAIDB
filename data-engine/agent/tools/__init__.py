"""工具层——所有 Agent 工具的 handler 和注册。

每个工具 = 一个 handler 函数 + 一个 register_tool() 调用。

facade（20260822 拆包）：实现按域搬到子模块——
query.py（查询）/ records.py（记录写）/ ddl.py（表结构）/
files.py（文件）/ templates.py（模板与会话）/ admin.py（管理与审批）；
公共助手在 _shared.py；38 个 register_tool + _TOOL_METADATA 集中在
_registry.py（注册顺序与拆包前一致）。
本模块只做 re-export：`from agent.tools import X` / `agent.tools.X` /
`import agent.tools` 的调用方零感知。
"""
from core.logger import get_logger

logger = get_logger(__name__)

# 拆包前的模块级别名（兼容 exec 加载方与反射访问）
import json as _json
import re as _re

from core.tool_registry import Tool, Param, register_tool, execute_tool
from core.tool_result import ToolResult
from pipeline.constants import TIER2_BATCH_UNITS
from core.condition_parser import extract_conditions, build_where
from core.contract.security_contract import safe_table_sql, is_valid_identifier, SecurityContract

from agent.tools._shared import (
    _validate_table_name,
    _msg_result,
    _require_params,
    _schema_tool,
    _guard_file_path,
)
from agent.tools.query import (
    list_databases,
    describe_schema,
    _query_with_fallback,
    join_query_tool,
    aggregate_query_tool,
    list_selections_tool,
)
from agent.tools.records import (
    _set_fields_of,
    insert_data,
    batch_insert_data,
    edit_data,
    delete_data,
    mutate_data,
)
from agent.tools.ddl import (
    batch_create_tables,
    create_standard_tables,
    drop_table_tool,
    _default_col_type,
    add_column_tool,
    drop_column_tool,
    modify_column_tool,
    _precision_arg,
    alter_precision_tool,
    set_not_null_tool,
    add_foreign_key_tool,
    _fk_constraint_arg,
    drop_foreign_key_tool,
    _index_columns_arg,
    create_index_tool,
    drop_index_tool,
)
from agent.tools.templates import (
    save_template,
    list_templates,
    import_template,
    drop_template,
    clear_session,
)
from agent.tools.files import (
    process_file,
    upload_file,
    search_documents,
    export_data_tool,
    list_vector_collections,
)
from agent.tools.admin import (
    clear_db,
    unsupported_op,
    _confirm_action,
    _escalate_permission,
    _deescalate_permission,
)

# 导入即触发全部 38 个 register_tool + apply_metadata（顺序与拆包前一致）
from agent.tools._registry import (
    _load_tool_examples,
    _tool_exs,
    _join_ex,
    _agg_ex,
    _TABLE_DEFINITION_SCHEMA,
    _apply_tool_meta,
    _TOOL_METADATA,
)
