"""工具层——所有 Agent 工具的 handler 和注册。

每个工具 = 一个 handler 函数 + 一个 register_tool() 调用。

facade（20260822 拆包）：实现按域搬到子模块——
query.py（查询）/ records.py（记录写）/ ddl.py（表结构）/
files.py（文件）/ templates.py（模板与会话）/ admin.py（管理与审批）；
公共助手在 _shared.py；全部 register_tool + _TOOL_METADATA 集中在
_registry.py（注册顺序与拆包前一致）。

再导出面（__all__）只保留外部实际经本 facade 取值的名字
（含测试 patch 目标，如 agent.tools.list_databases）；
其余 handler/常量请从对应子模块直取。
"""
from core.logger import get_logger

logger = get_logger(__name__)

from agent.tools._shared import _msg_result
from agent.tools.query import list_databases, list_selections_tool, _query_with_fallback
from agent.tools.records import delete_data
from agent.tools.files import process_file, export_data_tool

# 导入即触发全部 register_tool + apply_metadata（副作用本身是关键，
# 名字无人经 facade 取值，故不 re-export）
# 注意：必须保持绝对 import——test_02 用 exec(空 globals) 加载本文件统计注册数，
# 相对 import（from . import）在那种上下文里没有 __name__ 会直接炸
from agent.tools import _registry  # noqa: F401

__all__ = [
    "_msg_result",
    "_query_with_fallback",
    "delete_data",
    "export_data_tool",
    "list_databases",
    "list_selections_tool",
    "process_file",
]
