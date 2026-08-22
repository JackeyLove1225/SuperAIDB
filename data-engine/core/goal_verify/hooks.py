"""目标达成检测挂载钩子——执行层写操作后触发，工具层零感知（不 import 本模块）

挂载点（单向依赖，本模块不被工具层引用）：
- agent/open_layer/executor.py：execute_sub_task 成功返回前
- agent/open_layer/agent_loop.py：execute_tool 返回写操作结果后

审计口径与项目现有实践一致（logger 即审计轨）：
复查结果写日志——不符 warning、其余 info，形成"AI 为什么改 + 改后是否属实"双记录。
"""
from core.logger import get_logger

from core.goal_verify.report import VerifyReport
from core.goal_verify.verifier import rules_enabled, verify

logger = get_logger(__name__)


def after_write(data: dict, driver=None, rules: dict | None = None) -> list[VerifyReport]:
    """写操作双轨结果后处理：提取 effects → 独立复查 → 审计入日志

    Args:
        data: ToolResult.data（ok/effects/effects_list 通道）
        driver/rules: 可选注入（测试用）

    Returns:
        VerifyReport 列表。以下情形返回空列表（不产生报告）：
        非成功结果 / 无 effects / 总开关关闭 / effects 为挂起态（pending，操作未执行）
    """
    reports: list[VerifyReport] = []
    if not isinstance(data, dict) or data.get("ok") is not True:
        return reports
    if rules is None and not rules_enabled():
        return reports

    effects_list: list[dict] = []
    if isinstance(data.get("effects"), dict):
        effects_list.append(data["effects"])
    for e in data.get("effects_list") or []:
        if isinstance(e, dict):
            effects_list.append(e)

    for effects in effects_list:
        if effects.get("pending"):
            continue  # 挂起等人审：操作未执行，不复查
        try:
            report = verify(effects, driver=driver, rules=rules)
        except Exception as e:
            # 复查器自身故障绝不阻断主流程，但必须显式可见（不静默）
            report = VerifyReport(verified=None,
                                  table=effects.get("table", ""),
                                  action=effects.get("action", ""),
                                  skipped_reason=f"复查器异常: {e}")
        reports.append(report)
        # 审计：验证结果入日志（不符 warning 其余 info）
        if report.verified is False:
            logger.warning("goal_verify 复查不符: %s", report.mismatch_detail)
        else:
            logger.info("goal_verify: %s", report.render())
    return reports


def attach(result, driver=None, rules: dict | None = None) -> list[VerifyReport]:
    """把复查报告附到 ToolResult：text 追加呈现行，data["verify"] 挂机器通道

    供 executor/agent_loop 在写操作成功后调用。result 必须是 ToolResult。
    """
    reports = after_write(result.data, driver=driver, rules=rules)
    if not reports:
        return reports
    result.text += "\n" + "\n".join(r.render() for r in reports)
    result.data["verify"] = [r.to_dict() for r in reports]
    return reports
