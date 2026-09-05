"""权限策略模块——独立的权限控制层（不耦合业务代码）

设计要点：
- 单栈收口：一切数据操作经 DataSourceManager 出厂包装的 ContractDriver 执行权限判定
  （20260822 由 FederatedDriver/ContractDriver 双栈收敛）；裸 SQL 由 sql_guard.
  guard_write_sql 统一映射
- 规则白盒外置：config/permissions.yml（git 可审计）；文件不存在=默认 full（向后兼容）
- 标识符规范形（casefold）在栈边界统一收口——SQL 引擎大小写不敏感，权限判定同样
- 按 数据源 × 操作类型 控制：同一 delete 操作可对 A 库禁止、B 库放行
- 拒绝语义：抛 PermissionDenied（AppError 子类，API 层自动 400），消息含库/操作/原因
- sudo 提权（escalation.json 文件契约）仅对置了通道旗标的 MCP 进程生效
"""
from .policy import (
    Operation, PermissionDenied, PermissionPolicy, METHOD_TO_OP,
    set_current_role, get_current_role,
    set_current_user, get_current_user,
    set_escalated_role, clear_escalation, get_escalated_role, get_effective_role,
    set_mcp_channel,
)

__all__ = [
    "Operation", "PermissionDenied", "PermissionPolicy", "METHOD_TO_OP",
    "set_current_role", "get_current_role",
    "set_current_user", "get_current_user",
    "set_escalated_role", "clear_escalation", "get_escalated_role", "get_effective_role",
    "set_mcp_channel",
]
