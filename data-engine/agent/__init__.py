"""Agent 编排层包（20260824 图路径下线后的薄门面）

- agent/bootstrap.py：DI 装配（工具注册触发 / 树路由注入 / 改删提取注册）
- agent/router.py + decision_tree/：确定性路由规则资产
- agent/tools/：39 个原子工具（MCP 能力面唯一实现方）
- agent/management/：FastAPI 管理 API + 启动器

历史：进程内图编排（open_layer/ LangGraph 主链 + agent.py 单步执行器）已于
20260824 随"对话全量 MCP 化"下线——上层 AI 客户端即编排器，本仓不再有
进程内聊天循环（git 历史可查）。
"""
from agent import bootstrap  # noqa: F401  # 导入即完成 DI 装配与工具注册
