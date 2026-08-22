"""data-engine MCP server——能力面 MCP 化（形态①：原生 Reasonix 作为上层 AI，20260807）

定位：Reasonix 原生二进制作为上层 AI（循环/规划/审批 UI，随上游升级），
本 server 经 MCP stdio 把 data-engine 的全部数据能力暴露为工具。
每个工具调用 = execute_tool 直达（P1→树→P2 + 高危人审闸/选择集闸/P0 校验
全在位）——上层 AI 没有任何裸通道，能力边界即安全边界。

启动（项目根 reasonix.toml 示例）：
  [[plugins]]
  name = "data-engine"
  command = "python"
  args = ["C:/path/to/SuperAIDB/data-engine/mcp_server.py"]   # 改为本机实际路径
  # INDUSTRY 不需经 env 传——settings 对 config/.env 做新鲜读取（mtime 缓存），
  # 主应用切换行业写 .env 后，本进程下次访问自动同口径

高危人审双层纵深（缺一不推荐）：
  1. Reasonix 侧（配置层）：高危工具 + confirm_action 配 approval = "ask"
     → Reasonix 原生审批卡（只有用户点头，AI 才能发起调用）
  2. 本进程侧（代码层）：高危闸 channel=mcp → 挂起表 token →
     confirm_action 结算（一次性、10 分钟有期、fail-closed）

注意：本进程内复用主应用的 db/上下文基础设施（同目录、同 INDUSTRY），
与主应用并发同库时按既有驱动层行为处理（SQLite 单写者）。
"""
import asyncio
import os
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)
# 工作目录锚定到本文件所在目录：Reasonix 以自身 cwd 拉起子进程，
# 而 SQLITE_DB_PATH 等配置默认值为相对路径（./db/...）——不锚定就会
# 在错误位置新建空库。锚定后从任何 cwd 启动行为一致。
os.chdir(_script_dir)

# 触发全量工具注册（含元数据标注；agent.tools 是内置工具唯一实现方）
import agent.tools  # noqa: F401
from core.tool_registry import _tools, execute_tool
from core.context import get_context

from mcp.server.lowlevel import Server
from mcp import types
from mcp.types import TextContent, Tool as MCPTool, ToolAnnotations
import mcp.server.stdio
import anyio

# 不进 MCP 面的工具（图内占位/无独立调用意义）
_EXCLUDE = frozenset({"unsupported_op"})

_TYPE_MAP = {"str": "string", "int": "integer", "bool": "boolean", "file": "string"}

# risk_level → MCP 工具注解（Reasonix 按 SPEC 消费两个 hint）：
# - readOnlyHint=True → 只读工具进 Plan 模式/规划器工具面 + 权限层 reader 默认
#   放行（不声明则一律按 writer 处理，只读查询也会被审批卡拦住）
# - destructiveHint=True → ddl/admin 级（删表/清库等）标注破坏性，
#   Reasonix 审批卡显著警示；记录级写不标（有自身人审闸/契约，非"破坏性"语义）
_READONLY = frozenset({"readonly"})
_DESTRUCTIVE = frozenset({"ddl", "admin"})


def _annotations(tool) -> ToolAnnotations | None:
    ro = tool.risk_level in _READONLY
    de = tool.risk_level in _DESTRUCTIVE
    if not (ro or de):
        return None
    return ToolAnnotations(readOnlyHint=ro, destructiveHint=de)


def _input_schema(tool) -> dict:
    """ToolDef.params → MCP inputSchema（Param.schema 优先，type 映射兜底）"""
    props: dict = {}
    required: list[str] = []
    for p in tool.params:
        if p.schema:
            props[p.name] = {**p.schema, "description": p.description or ""}
        else:
            item = {"type": _TYPE_MAP.get(p.type, "string"),
                    "description": p.description or ""}
            if p.default is not None:
                item["default"] = p.default
            props[p.name] = item
        if p.required:
            required.append(p.name)
    return {"type": "object", "properties": props, "required": required}


def _tool_brief(tool) -> str:
    """MCP 工具描述：原描述 + 风险级标注（让上层 AI 对高危工具有预期）"""
    risk = {"record_write": "记录级写", "ddl": "结构变更", "admin": "管理",
            "file": "文件"}.get(tool.risk_level, "")
    prefix = f"[{risk}] " if risk else ""
    return prefix + (tool.description or tool.name)


server = Server("data-engine")


@server.list_tools()
async def _list_tools() -> list[MCPTool]:
    return [
        MCPTool(name=name, description=_tool_brief(t), inputSchema=_input_schema(t),
                annotations=_annotations(t))
        for name, t in _tools.items() if name not in _EXCLUDE
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict | None = None) -> types.CallToolResult:
    arguments = arguments or {}
    ctx = get_context()
    ctx.set_channel("mcp")       # 高危闸按通道走挂起表回执（非 interrupt）
    ctx.set_trace_id()           # 审计可追踪（与图入口同口径）
    # 角色注入（security_review HIGH 修复）：MCP 是无用户上下文的通道，
    # 默认降级为 readonly（fail-closed 只读）；外部 AI 接入需显式配置
    # MCP_ROLE 环境变量（如 admin/user）才获得对应写权限。
    # sudo 模式（20260809）：MCP_ROLE 默认 user，AI 需管理员时先
    # escalate_permission 提权（经人审闸批准）→ get_effective_role 优先取提权；
    # 未提权时始终按 MCP_ROLE 受限身份，绝不默认 admin。
    from core.permission import set_current_role, get_effective_role, set_mcp_channel
    # 提权通道域化（评审四轮）：sudo 提权契约只在本进程（MCP 通道）生效——
    # 管理端/web 请求不吃提权窗口
    set_mcp_channel(True)
    # MCP_ROLE 空值/未设置 → 强制 readonly（fail-closed），绝不落到 system
    base_role = os.environ.get("MCP_ROLE") or "readonly"
    if base_role not in ("admin", "user", "readonly"):
        base_role = "readonly"
    set_current_role(get_effective_role(base_role))
    try:
        result = await anyio.to_thread.run_sync(
            lambda: execute_tool(name, **arguments))
        return types.CallToolResult(
            content=[TextContent(type="text", text=str(result))], isError=False)
    except Exception as e:
        # GraphInterrupt（图通道人审挂起信号）等运行时异常兜底：
        # MCP 通道本不该出现 interrupt；出现即说明有未经桥接的确认点，
        # 如实返回文本（fail-closed，绝不静默放行）
        from langgraph.errors import GraphInterrupt
        if isinstance(e, GraphInterrupt):
            msg = ("该操作需要交互式确认，但 MCP 通道未桥接此确认点——"
                   "操作未执行。请改用主应用界面完成本操作。")
        else:
            msg = f"工具执行异常（未生效）: {e}"
        return types.CallToolResult(
            content=[TextContent(type="text", text=msg)], isError=True)
    finally:
        ctx.set_channel("graph")  # 复位（同进程后续若有图调用防误判）


async def _main() -> None:
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
