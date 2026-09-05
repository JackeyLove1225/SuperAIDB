"""结构化指令契约工具——execute_structured（MCP 面唯一数据通道，20260905）

上游 AI 把用户指令翻译成结构化契约后调用，不再转述自然语言——仓内
跳过 P1/P2 的 LLM 翻译环节（上游 AI 本身是全功能 LLM，翻译质量更高），
树路由与全部安全闸原样在位：

  自然语言链（execute_instruction，保留在注册表服务仓内/Web 测试）：
      原话 → P1 LLM → 树 → P2 LLM → execute_tool
  结构化链（本工具）：
      契约 → 枚举校验 → 树 → args 适配 → execute_tool

安全不变量（与通道无关）：AI 无 SQL 执行入口——SQL 由代码从封闭枚举
的填空组装（condition_parser FC schema + build_where 白名单）。上游
AI 填的"空"与 P2 给仓内 LLM 填的"空"同形状，闸门位置不变：
树路由强制（不能指定工具名）→ 边界闸（表/字段存在性）→ 系统表拦截
→ 高危人审闸（operator_gate + MCP 同步桥）。

契约顶层 5 字段（冻结稳定）：behavior / object / constraint / args /
database。枚举真源 = 决策树 yml（_contract_enums 从树推导，不设第二
真源——树演进契约自动跟进）。
"""
import json

from core.logger import get_logger
from core.tool_result import ToolResult

logger = get_logger(__name__)


def _contract_enums() -> tuple[list, list, list]:
    """契约枚举推导（真源=决策树 yml 节点，加载期已合并为 _NODES）

    behavior：根链（l1 起沿 r 链）behavior 节点的单值 m（查/增/改/删/
    导入/上传/导出——顺序即链序）。
    object / constraint：全树对应维度节点 m 值的并集（含同义词组
    "库/数据库"、"暂存/选择集"等，sorted 稳定排序；constraint 的
    空串缺省不算枚举值——缺省即空）。
    """
    from agent.router import _NODES
    behaviors: list[str] = []
    nid = "l1"
    while nid in _NODES:
        node = _NODES[nid]
        if "tool" in node or node["c"] != "behavior":
            break
        m = node["m"]
        behaviors.append(next(iter(m)) if isinstance(m, set) else m)
        nid = node["r"]
    objects: list[str] = []
    constraints: list[str] = []
    for node in _NODES.values():
        if "tool" in node or node["c"] not in ("db", "constraint"):
            continue
        m = node["m"]
        vals = m if isinstance(m, set) else {m}
        target = objects if node["c"] == "db" else constraints
        for v in sorted(vals):
            if v and v not in target:
                target.append(v)
    return behaviors, objects, constraints


def _enum_fail(field: str, val: str, legal: list, route: str = "") -> "ToolResult":
    """枚举校验失败（fail-closed）：如实报 + 附合法值清单"""
    return ToolResult.fail(
        f"结构化契约字段 {field} 的值 {val!r} 不合法。合法值：{'/'.join(legal)}",
        code="VALIDATION", reason="bad_contract_enum", route=route)


# object 是契约字段名（屏蔽内建名，职责单一不换名）
def execute_structured(behavior: str = "", object: str = "",  # noqa: A002
                       constraint: str = "", args=None, database: str = "") -> "ToolResult":
    """结构化指令契约入口：枚举校验 → 树路由 → args 适配 → execute_tool

    Args:
        behavior: 动作（查/增/改/删/导入/上传/导出）
        object: 操作对象（表/记录/字段/外键/索引/类型/精度/库/模板/文件/会话/暂存/统计/关联）
        constraint: 细分约束（可选：新建/自定义/一个/单条/非空）
        args: 目标工具参数（JSON 对象；MCP schema 声明为 object，字符串形式也受理）
        database: 目标数据源（可选）
    """
    behavior = str(behavior or "").strip()
    object = str(object or "").strip()  # noqa: A002
    constraint = str(constraint or "").strip()
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except (json.JSONDecodeError, ValueError) as e:
            return ToolResult.fail(f"args 不是合法 JSON 对象: {e}",
                                   code="VALIDATION", reason="bad_args_json")
    if not isinstance(args, dict):
        return ToolResult.fail("args 必须是 JSON 对象（键=目标工具参数名）",
                               code="VALIDATION", reason="bad_args_type")

    # ── 枚举校验（fail-closed，合法值清单随报错给出）──
    behaviors, objects, constraints = _contract_enums()
    if behavior not in behaviors:
        return _enum_fail("behavior", behavior, behaviors)
    if object not in objects:
        return _enum_fail("object", object, objects)
    if constraint and constraint not in constraints:
        return _enum_fail("constraint", constraint, constraints)

    # ── 树路由（强制路径：契约不含工具名，behavior+object 必须过树）──
    from agent.router import get_tree
    tool_name = get_tree().route(behavior, object, constraint)
    from core.tool_registry import get_tool, execute_tool
    tool_def = get_tool(tool_name)
    route_trace = f"{behavior}+{object}{'+' + constraint if constraint else ''} → {tool_name}"
    if tool_def is None or tool_name == "unsupported_op":
        # 结构化输入是精确意图，无"问法学习"语义——直接报不支持
        return ToolResult.fail(
            f"暂不支持的操作（意图：{behavior}+{object}"
            f"{'+' + constraint if constraint else ''}）",
            code="VALIDATION", reason="cannot_route", route=route_trace)

    # ── args 适配：只留工具签名内参数；dict/list 按 str 型参数序列化 ──
    #（execute_tool 亦有签名过滤与边界闸，此处适配让参数在闸前形状正确）
    adapted: dict = {}
    for p in tool_def.params:
        if p.internal or p.name not in args:
            continue
        val = args[p.name]
        if p.type == "str" and isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False)
        adapted[p.name] = val
    if database and not adapted.get("database"):
        adapted["database"] = database

    # 改/删+记录：结构化通道必须带 selection_id（显式影响面）——
    # query 自动建选择集并返回 selection_id，两步流程 fail-closed
    if tool_name in ("edit_data", "delete_data") and not adapted.get("selection_id"):
        return ToolResult.fail(
            "结构化通道的改/删记录必须分两步：先发 "
            "{behavior:'查', object:'记录', args:{table,...}} 查询拿 selection_id，"
            "再带 selection_id 改/删（影响面精确、人审有据）",
            code="VALIDATION", reason="missing_selection", route=route_trace)

    logger.info("结构化路由: %s", route_trace)
    r = execute_tool(tool_name, **adapted)
    from agent.tools.instruct import _with_trace
    return _with_trace(r, route_trace)
