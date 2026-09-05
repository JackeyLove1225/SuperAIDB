"""管理与审批域——库级管理 / 占位路由 / 人审桥接 / sudo 提权工具 handler。

clear_db / unsupported_op / confirm_action / escalate_permission /
deescalate_permission。
"""
from core.tool_result import ToolResult

from agent.tools._shared import _schema_tool


# ============ 系统 ============

clear_db = _schema_tool(
    "core.schema_manager.clear_database",
    {"drop_tables": False, "database": ""},
)


def unsupported_op(operation=""):
    """统一"暂不支持"叶子：决策树中无对应工具的意图组合显式路由到这里。

    白盒原则：不支持就明说，不假装覆盖（替代原 l==r 假节点的占位路由）。
    双轨：ok=False + reason=unsupported_op——机器通道显式知晓"未执行"，
    agent loop 据此换路径而非当作成功。
    """
    return ToolResult.fail(
        "暂不支持该操作。目前支持：数据查询/聚合/多表关联、建表/改表结构、"
        "数据增改删、文件入库、导入导出等。您可以换种说法，"
        "或描述想达成的目标，我重新为您匹配。",
        code="VALIDATION", reason="unsupported_op", operation=operation)


# ============ 人审桥接 / sudo 提权（MCP 通道） ============

def _confirm_action(token: str, approve: bool = True) -> str:
    """高危操作批准结算（MCP 通道人审桥接，20260807；20260822 起结算收口到管理端）

    安全修复：token 曾随待批准消息回传 AI 通道，AI 可调本工具自助结算
    （人审闸形同虚设）。现全部高危挂起一律只能由 admin 在 Web 管理台
    「权限管理 → 待审批」结算（管理 API /api/approvals/*），AI 通道只转述。
    """
    from core.pending_ops import list_pending
    pend = list_pending()
    if pend:
        lines = "\n".join(f"- {p['name']}（{p['impact'][:60]}…）" for p in pend)
        return ("⛔ 本工具不再结算高危操作（安全修复 20260822：防 AI 自助结算）。"
                f"当前有 {len(pend)} 项待批准：\n{lines}\n"
                "请用户到 Web 管理台「权限管理 → 待审批」批准或拒绝。")
    return ("当前没有待批准的高危操作。高危操作的批准只在 Web 管理台"
            "「权限管理 → 待审批」进行（本工具自 20260822 起不再结算）。")


def _escalate_permission(role: str = "admin", ttl: int = 600) -> str:
    """临时提权为管理员（sudo 模式，20260809）

    AI 以 MCP_USER 绑定用户身份操作（未配置/用户不存在时拒绝启动——
    fail-closed，连只读都不给）；需要管理员全权限（drop 表/改结构/配权限）
    时调用本工具 → 登记提权请求（不暴露 token）→ 管理员在管理端确认 →
    进程级提权（admin + TTL），到期自动降回。
    提权 token 只经管理 API（需 admin 登录）结算，AI 无法自助确认。
    """
    from core.pending_ops import register_pending_dedup
    if role not in ("admin",):
        return f"仅支持提权为 admin，已拒绝: {role}"
    ttl = max(60, min(int(ttl), 3600))  # 1 分钟 ~ 1 小时
    # 登记提权请求（原子查重：重复调用堆叠重复卡——同请求在队列即复用，
    # find+register 同 FileLock 临界区，无 TOCTOU 窗口）
    token, _ = register_pending_dedup(
        "__escalate__", {"role": role, "ttl": ttl},
        f"提权为 {role}（{ttl // 60} 分钟）")
    # token 进 data（机器通道）而非 text：__str__ 只暴露 text，AI/LLM 可见
    # 通道永远看不到 token（防 AI 自助结算）；data 仅由 mcp_server 同步
    # 等待桥（MCP_APPROVAL_SYNC）消费
    return ToolResult.fail(
        f"⏸️ 已发起提权请求：升级为 {role}（有效期 {ttl // 60} 分钟）。"
        f"请管理员在管理端确认（token 不回传本通道）。"
        f"确认后本进程 AI 将获得管理员权限，到期自动降回。",
        code="CONTRACT", reason="pending_approval", approval_token=token)


def _deescalate_permission() -> str:
    """立即撤销提权，恢复默认角色（user/readonly）"""
    from core.permission import clear_escalation
    clear_escalation()
    return "已撤销提权，恢复 MCP_USER 绑定身份（未绑定则只读）"
