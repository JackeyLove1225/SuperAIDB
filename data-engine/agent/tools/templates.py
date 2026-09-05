"""模板与会话域——模板管理 + 会话清除工具 handler。

save_template / list_templates / import_template / drop_template /
clear_session。
"""
from core.tool_result import ToolResult


# ============ 模板 ============

def save_template(name="", database=""):
    if not name:
        return ToolResult.fail("请输入模板名称", code="VALIDATION",
                               reason="missing_params")
    from core.template_manager import save_template as _st
    return _st(name)


def list_templates(database=""):
    """列出模板（双轨）：data 带 {templates, count}（结构化核心负载）"""
    from core.template_manager import list_templates as _lt
    templates = _lt()
    if not templates:
        return ToolResult.ok("当前无可用模板", templates=[], count=0)
    lines = ["| 模板名 | 业务名 | 字段数 |", "|--------|--------|--------|"]
    for t in templates:
        lines.append(f"| {t['name']} | {t['business_name']} | {t['columns']} |")
    return ToolResult.ok("\n".join(lines), templates=templates, count=len(templates))


def import_template(name="", table="", database=""):
    if not name:
        return ToolResult.fail("请输入模板名称", code="VALIDATION",
                               reason="missing_params")
    if not table:
        return ToolResult.fail("请指定要导入的目标表名", code="VALIDATION",
                               reason="missing_params")
    from core.template_manager import import_template as _it
    return _it(name, table)


def drop_template(name="", database=""):
    if not name:
        return ToolResult.fail("请输入模板名称", code="VALIDATION",
                               reason="missing_params")
    from core.template_manager import delete_template as _dt
    return _dt(name)


# ============ 会话 ============

def clear_session(database=""):
    from core.session import clear_history
    clear_history()
    return ToolResult.ok("会话已清除")
