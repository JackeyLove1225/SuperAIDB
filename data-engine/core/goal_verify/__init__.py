"""目标达成检测——写操作声明与数据库真实状态的独立对账（完整体系，零耦合）

设计原则（路线图迭代 4.5）：
- 工具层零感知：工具不 import 本模块；执行层（executor/agent_loop）写操作后调钩子
- 单向依赖：hooks → verifier → report；verifier 走驱动标准 query（自身过权限/契约）
- 失败显式：复查不符如实报告（"声明删 N 条但复查仍在"），不静默、不自动回滚
- 可配置：config/goal_verify.yml 整体/按操作类型开关，规则白盒可审计
"""
