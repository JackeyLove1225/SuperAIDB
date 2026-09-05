"""数据操作包（facade）——原子化的 UPDATE 和 DELETE，AI 写条件，代码执行

还提供多表 JOIN 查询和聚合统计查询能力。

联邦数据库支持：get_driver() 返回 FederatedDriver，
自动根据表名路由到对应数据源（单数据源时行为不变）。

拆分布局（20260830，facade 模式，先例 core/schema_manager/）：
原 core/data_ops.py（1461 行）按职责拆到本子包——
base_ops.py（基座：字段别名解析/单表改删/批量插入/SELECT 拼装与校验/查询错误翻译）、
join_sql.py（多表 JOIN 拼装与执行）、agg_sql.py（聚合统计拼装与执行）、
nl_mutate.py（自然语言改/删编排：候选探测/拓扑排序/合并确认闸/挂起登记）。
驱动缓存（_federated_driver / get_driver / reset_driver_cache /
close_driver_cache）与编排回调注入（register_tree_router /
register_mutation_extractor 及 _route_tool / _extract_mutation_ops
两个 DI 分发点）留在本 facade。

再导出面（__all__）保留外部实际经本 facade 取值的名字（含测试 patch
目标）；未列入的实现名请从对应子模块直取
（如 `from core.data_ops.join_sql import _build_join_sql`）。

patch 兼容约定（测试依赖，勿绕开）：get_driver / _load_table_schema /
_extract_mutation_ops / insert_row / delete_rows / _federated_driver 会被
tests 在 core.data_ops 上 patch/赋值（test_02/05/06/07/08/14/18/19/24/37）。
子模块内对这些名字的引用一律在调用时经本 facade 取值
（子模块 `from core import data_ops as _ops`，`_ops.X(...)`），
因此 patch 在导入完成后依然生效。共享状态 _federated_driver / _tree_router /
_extract_ops_impl 直接留在本 facade，注册、打桩与重置永远命中同一份。
"""
from core.logger import get_logger

logger = get_logger(__name__)

# ── 编排回调注入（依赖倒置，消灭 core→agent 反向 import 边）──
# mutate_natural 的工具路由（删→delete_data/改→edit_data）属于编排层职责；
# 引擎层只面向回调接口，由编排层（agent 包初始化）注册决策树路由。
_tree_router = None


def register_tree_router(fn):
    """编排层注册决策树路由：fn(behavior, category, constraint) -> tool_name"""
    global _tree_router
    _tree_router = fn


def _route_tool(behavior: str, category: str, constraint: str = "") -> str:
    if _tree_router is None:
        # 编排层未注册（mutate 只应经 agent/tools 流程到达，注册在 agent/__init__ 完成）——
        # 如实失败，不静默猜工具
        raise RuntimeError("决策树路由未注册（编排层未初始化）")
    return _tree_router(behavior, category, constraint)


# 联邦驱动单例（懒加载）
_federated_driver = None


def get_driver():
    """获取数据库驱动（公开门面——外部不再直引私有名）

    联邦数据库模式：返回 FederatedDriver，自动路由到表所属数据源
    单数据源模式：FederatedDriver 透明转发到默认 Driver，行为一致
    """
    global _federated_driver
    if _federated_driver is None:
        from core.datasource_manager import DataSourceManager
        from core.drivers.federated_driver import FederatedDriver
        _federated_driver = FederatedDriver(dsm=DataSourceManager())
    return _federated_driver


def reset_driver_cache() -> None:
    """驱动缓存公开重置入口（行业切换经 registry 调用；
    替代外部直戳 _federated_driver 私有全局——状态卫生）"""
    global _federated_driver
    _federated_driver = None


def close_driver_cache() -> None:
    """关闭并清空驱动缓存（备份/恢复等停写窗口用）——公开入口，
    替代外部直戳 _federated_driver.close()（状态卫生）"""
    global _federated_driver
    drv, _federated_driver = _federated_driver, None
    if drv is not None:
        try:
            drv.close()
        except Exception as e:
            logger.warning("驱动缓存关闭异常（继续清空）: %s", e)


# AI 改/删结构提取已上移编排层（core 只留确定性执行）。
# 本 facade 只保留 DI 分发点——实现由 agent/ai_extract.py 启动时注册
#（方向铁律：core 不 import agent）。测试 patch 面不变：
# patch.object(data_ops, "_extract_mutation_ops", ...) 依然接管整条提取。
_extract_ops_impl = None


def register_mutation_extractor(fn) -> None:
    """注册 NL→改/删结构提取实现（agent/ai_extract 启动时调用，DI 同款于
    register_tree_router——core 反向 import agent 的历史病型一律走注入）"""
    global _extract_ops_impl
    _extract_ops_impl = fn


def _extract_mutation_ops(instruction: str):
    """自然语言 → 结构化改/删操作（DI 分发；AI 只提取结构，不碰 SQL）"""
    if _extract_ops_impl is None:
        raise RuntimeError("改/删结构提取能力未注册（agent/ai_extract 未加载）")
    return _extract_ops_impl(instruction)


# ═══ 实现 re-export（仅外部实际经本 facade 取值的名字，见 __all__）═══
from .base_ops import (
    _find_fk_relation,
    _load_table_schema,
    _translate_query_error,
    build_select_sql,
    delete_rows,
    insert_row,
    insert_rows,
    resolve_field,
    update_rows,
    validate_group_by,
    validate_order_by,
    validate_select_fields,
)
from .join_sql import join_query
from .agg_sql import aggregate_query
from .nl_mutate import (
    _multi_ops_confirmed,
    _topo_sort_deletes,
    describe_table_mutation,
    mutate_natural,
)

__all__ = [
    # patch 契约与跨子模块共享助手
    "_extract_mutation_ops", "_find_fk_relation", "_load_table_schema",
    "_multi_ops_confirmed", "_route_tool", "_topo_sort_deletes",
    "_translate_query_error",
    # 公开操作面
    "aggregate_query", "build_select_sql", "close_driver_cache",
    "delete_rows", "describe_table_mutation", "get_driver", "insert_row",
    "insert_rows", "join_query", "mutate_natural",
    "register_mutation_extractor", "register_tree_router",
    "reset_driver_cache", "resolve_field", "update_rows",
    "validate_group_by", "validate_order_by", "validate_select_fields",
]

# 自注册到重置注册表
from core.registry import register_reset

register_reset("data_ops_driver_cache", reset_driver_cache)

# 能力注入 tool_registry（单向依赖：registry 需要的高危卡影响面能力
# 由本包注册，registry 不再反向 import data_ops——互耦环断开）
from core.tool_registry import register_nuke_card_capabilities

register_nuke_card_capabilities(describe_table_mutation, get_driver)

# import 子模块会在包命名空间留下同名属性（base_ops/join_sql/...），逐一移除，
# 保持 facade 命名空间只有 __all__ 面与上述约定名；sys.modules 中的子模块不受影响。
globals().pop("base_ops", None)
globals().pop("join_sql", None)
globals().pop("agg_sql", None)
globals().pop("nl_mutate", None)
