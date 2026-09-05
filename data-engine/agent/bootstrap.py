"""DI 装配（自 agent/__init__ 拆出）

导入本模块即完成编排层启动装配：
- agent.tools 全量工具注册（导入副作用）
- 决策树路由注入 core.data_ops（register_tree_router——core 不反向 import agent）
- 改/删结构提取注册（agent/ai_extract 导入即注册）
"""
from agent import tools  # noqa: F401  # 导入即触发全部 register_tool
from agent import ai_extract  # noqa: F401  # 导入即注册改/删提取能力

# 依赖倒置注册：core/data_ops.mutate_natural 的工具路由
# 由编排层在此注入——core 不再反向 import agent.router，分层方向恢复单向。
# 注意 get_tree 必须调用期动态解析（函数内 import）：否则 from-import 绑死
# 原函数，测试对 agent.router.get_tree 的 patch 会失效（层 29 实测拦截）
from core.data_ops import register_tree_router as _register_tree_router


def _route_via_tree(behavior: str, category: str, constraint: str = "") -> str:
    from agent.router import get_tree
    return get_tree().route(behavior, category, constraint)


_register_tree_router(_route_via_tree)
