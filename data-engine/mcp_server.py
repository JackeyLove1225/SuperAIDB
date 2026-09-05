"""data-engine MCP server——能力面 MCP 化（形态①：原生 Reasonix 作为上层 AI，20260807）

定位：Reasonix 原生二进制作为上层 AI（循环/规划/审批 UI，随上游升级），
本 server 经 MCP stdio 把 data-engine 的全部数据能力暴露为工具。
数据操作唯一入口 = execute_structured（结构化指令契约，20260905）：
上层 AI 把用户指令翻译成结构化契约（behavior+object+args，枚举真源=决策树），
仓内 枚举校验→树路由→args 适配→execute_tool（零仓内 LLM 翻译；高危人审闸/
选择集闸/P0 校验全在位）——上层 AI 没有任何裸通道，也不转述自然语言，
能力边界即安全边界。

启动（项目根 reasonix.toml 示例）：
  [[plugins]]
  name = "data-engine"
  command = "python"
  args = ["<本仓库绝对路径>/data-engine/mcp_server.py"]   # 替换为本机实际路径
  # INDUSTRY 不需经 env 传——settings 对 config/.env 做新鲜读取（mtime 缓存），
  # 主应用切换行业写 .env 后，本进程下次访问自动同口径

高危人审收口单点在管理端审批中心（本进程侧，必需）；客户端 ask 卡可选叠加：
  1. 本进程侧（代码层，必需）：高危闸 channel=mcp → 挂起表 token →
     管理端审批中心结算（一次性、10 分钟有期、fail-closed；token 不回传 AI 通道）
  2. 客户端侧（配置层，可选）：面上的机制工具（confirm_action/提权两件套）
     配 approval = "ask" → 客户端原生审批卡（只有用户点头，AI 才能发起调用）

同步人审桥（20260903，MCP_APPROVAL_SYNC 默认开）：高危挂起后本进程不再
"立即返回待批准文案"，而是确保管理端/前端在线（必要时 launcher 自启）、
打开审批页，同步等待用户在 Web 管理台批准/拒绝（前端任意页面全局弹卡），
把真实结算结果带回对话。等待全程不执行任何操作（执行只在管理端 settle
端点，admin + 操作密码）；MCP_APPROVAL_SYNC=0 可回退旧行为。

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

# 进程角色标记（必须在任何 core.* import 之前）：
# 1) 日志输出面切 stderr——stdout 是 JSON-RPC 协议通道，日志落 stdout
#    即毒死 MCP 会话（daemon 拉起两行 INFO 即会触发此场景）；
# 2) 文件日志按 mcp_<pid>.log 分档（多 MCP 进程共享单文件在 Windows
#    上轮转必撞锁）。
os.environ["SUPERAIDB_ROLE"] = "mcp"

# 触发全量工具注册（含元数据标注；agent.tools 是内置工具唯一实现方）
import agent.tools  # noqa: F401
from core.tool_registry import get_tools, execute_tool
from core.context import get_context

from mcp.server.lowlevel import Server
from mcp import types
from mcp.types import TextContent, Tool as MCPTool, ToolAnnotations
import mcp.server.stdio
import anyio

# MCP 面白名单（20260905 结构化契约）：只有这 5 个工具对上层 AI 可见——
# 新工具默认不上 MCP 面（白名单默认方向，安全边界），要上面必须显式加进
# 本集合（并有 test_32 面断言锁定）。
# 数据通道只走 execute_structured（结构化契约：枚举校验→树路由→args 适配
# →execute_tool，零仓内 LLM 翻译）——上游 AI 把用户指令翻译成结构化意图，
# 不再转述自然语言。execute_instruction（自然语言）已从 MCP 面摘除，保留在
# 注册表服务仓内/Web 测试；33 个具体工具仓内保留（树与管理端可用），上层
# AI 没有任何绕过树的路径。
_INCLUDE = frozenset({
    "execute_structured",    # 唯一数据通道（结构化指令契约）
    "confirm_action",        # 人审闸状态查询（只读说明，不结算）
    "escalate_permission",   # sudo 提权（人审闸内置）
    "deescalate_permission", # 撤销提权
    "list_vector_collections",  # 只读辅助（向量库集合枚举）
})

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
        for name, t in get_tools().items() if name in _INCLUDE
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict | None = None) -> types.CallToolResult:
    arguments = arguments or {}
    # 面即边界（20260905 结构化契约，白名单）：新工具默认不上 MCP 面——
    # 按名直调未暴露工具的绕过通道必须在调用点封死（面即安全边界的默认
    # 方向）——33 个具体工具只能经 execute_structured 的树路由到达
    if name not in _INCLUDE:
        from core.logger import get_logger as _gl
        _gl(__name__).info("面边界拦截：%s 按名直调被拒（硬路由白名单）", name)
        return types.CallToolResult(
            content=[TextContent(
                type="text",
                text=f"工具 {name} 不在 MCP 能力面（硬路由）。"
                     "一切数据操作请走 execute_structured——"
                     "把用户指令翻译成结构化契约（behavior+object+args），"
                     "仓内决策树会路由到正确的工具。")],
            isError=True)
    ctx = get_context()
    ctx.set_channel("mcp")       # 高危闸按通道走挂起表回执（非 interrupt）
    ctx.set_trace_id()           # 审计可追踪（与图入口同口径）
    # 身份注入：MCP 与控制台走同一条用户权限通道——
    # MCP_USER 环境变量把本通道绑定到具体用户（角色从 users 表取，
    # 用户级规则/自助收紧全生效）。**未绑定 = 拒绝服务**（连只读都不给：
    # 只读也是信息泄露面——MCP 通道必须先绑定且绑定必须有效）。
    # 提权不变：escalate_permission 经人审批准 → 临时 admin（TTL 自动降回）。
    from core.permission import (set_current_role, get_effective_role,
                                 set_mcp_channel, set_current_user)
    # 提权通道域化：sudo 提权契约只在本进程（MCP 通道）生效——
    # 管理端/web 请求不吃提权窗口
    set_mcp_channel(True)
    _mcp_user, _mcp_role = _resolve_mcp_identity(os.environ.get("MCP_USER"))
    if not _mcp_user:
        return types.CallToolResult(
            content=[TextContent(
                type="text",
                text="MCP 通道未绑定用户：拒绝服务。请在客户端 env 配置 "
                     "MCP_USER（users 表中存在的用户名）——未绑定用户时"
                     "本通道任何调用（含只读）一律拒绝，防信息泄露。")],
            isError=True)
    set_current_user(_mcp_user)
    set_current_role(get_effective_role(_mcp_role))
    try:
        result = await anyio.to_thread.run_sync(
            lambda: execute_tool(name, **arguments))
        text = str(result)
        # ── MCP 同步人审桥（20260903）──
        # 高危挂起不再是"一去不回"的待批准回执：同步等待用户在 Web 管理台
        # 批准/拒绝，把真实结算结果带回对话（对话不断流，体验对齐前端）。
        # 安全不变量：等待全程不执行任何操作（fail-closed）；执行仍只发生
        # 在管理端 settle 端点（admin + 操作密码）；token 只在 data 机器
        # 通道流转（__str__ 只暴露 text），AI 文本可见面是组合后的结果文本。
        # MCP_APPROVAL_SYNC=0/off/false → 精确旧行为（回滚通道）。
        _rd = getattr(result, "data", None) or {}
        if (_rd.get("reason") == "pending_approval"
                and _rd.get("approval_token")
                and _approval_sync_mode() != "off"):
            try:
                text = await _await_approval_sync(_rd["approval_token"]) or text
            except Exception as e:
                from core.logger import get_logger as _gl
                _gl(__name__).warning(
                    "同步人审桥故障（回退原待批准文案，安全不受影响）: %s", e)
        return types.CallToolResult(
            content=[TextContent(type="text", text=text)], isError=False)
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


# ── MCP 同步人审桥（20260903）──
# 环境开关：MCP_APPROVAL_SYNC（默认 interactive：保活自启 + 开浏览器 + 等待；
# passive：仅等待，测试/无头环境用；0/off/false：精确旧行为回滚通道）、
# MCP_APPROVAL_SYNC_MAX_WAIT（等待上限秒数，默认 615——挂起表 TTL 600 + 余量）
_SYNC_OFF = ("0", "off", "false", "")


def _approval_sync_mode() -> str:
    v = (os.environ.get("MCP_APPROVAL_SYNC") or "interactive").strip().lower()
    if v in _SYNC_OFF:
        return "off"
    if v == "passive":
        return "passive"
    return "interactive"


def _sync_max_wait() -> float:
    try:
        return float(os.environ.get("MCP_APPROVAL_SYNC_MAX_WAIT", "615"))
    except ValueError:
        return 615.0


async def _await_approval_sync(token: str) -> str | None:
    """同步人审桥 async 侧：进度通知（best-effort）+ 阻塞等待体（线程）。

    返回组合回执文本；桥自身故障抛出/返回 None 均由调用方回退原
    "待批准"文案（安全不受影响——等待失败 ≠ 操作执行）。
    """
    # 捕获请求上下文（contextvar 只在 async 侧可见，线程里拿不到）：
    # session + progressToken 用于向客户端发进度通知（"⏳ 等待人工审批"，
    # 部分客户端还按活动重置 tool-call 超时）；任何失败都降级为无通知
    session = progress_token = None
    try:
        rc = server.request_context
        session = rc.session
        if rc.meta is not None:
            progress_token = getattr(rc.meta, "progressToken", None)
    except Exception:
        session = None

    async def _notify_loop(elapsed: int = 0):
        while True:
            await anyio.sleep(5)
            elapsed += 5
            if progress_token is None or session is None:
                continue
            try:
                await session.send_progress_notification(
                    progress_token, elapsed,
                    message="⏳ 等待人工审批——可在 Web 管理台「权限管理→待审批」处理")
            except Exception:
                pass  # 通知是体验增强，失败不影响等待

    mode = _approval_sync_mode()
    async with anyio.create_task_group() as tg:
        tg.start_soon(_notify_loop)
        try:
            settlement = await anyio.to_thread.run_sync(
                lambda: _await_settlement_blocking(token, mode))
        finally:
            # 等待体结束/异常都必须先停掉通知循环（无限 sleep，不 cancel
            # 则 task group 退出被挂死）
            tg.cancel_scope.cancel()
    return _compose_sync_reply(settlement)


def _await_settlement_blocking(token: str, mode: str) -> dict | None:
    """同步人审桥线程体。安全边界：本函数**不执行任何数据操作**——只做
    服务保活、打开审批页、轮询结算回执；执行只发生在管理端 settle 端点
    （admin + 操作密码）。返回结算回执 dict 或 None（超时 fail-closed）。"""
    import time as _t
    from core.settlement_hub import wait_settlement
    from core.pending_ops import peek_pending, _TTL_SECONDS
    # ① 快查（用户可能已批——dedup 复用 token 时回执可能已在）
    rec = wait_settlement(token, 1.0)
    if rec is not None:
        return rec
    # ② 挂起表剩余 TTL——等待上限跟随 token 生命周期，不超期空等
    op = peek_pending(token)
    if op is None:
        # 登记项已消失（刚被结算或过期）：兜底再取一次回执，仍无即超时
        return wait_settlement(token, 2.0)
    ttl_remaining = _TTL_SECONDS - (_t.time() - op["ts"])
    # ③ interactive：确保管理端/前端在线（结算端点在管理端、确认 UI 在
    #    前端）——双检后才自启，防 _cleanup_residual 误杀瞬时抖动的活服务
    if mode == "interactive":
        _ensure_services()
        _open_approval_page()
    # ④ 等待结算回执（上限 = min(配置上限, token 剩余 TTL + 5s)）
    return wait_settlement(token, min(_sync_max_wait(), ttl_remaining + 5.0))


def _ensure_services() -> None:
    """管理端/前端保活：健康检查失败（间隔 2s 双检）才复用 launcher.start()
    自启（单源，不复制启动逻辑）。launcher.start() 含 _cleanup_residual
    （会杀占端口的本项目进程），瞬时抖动不得触发；自启失败只记日志，
    等待继续——回执链路不依赖本函数成功（用户可手动启动）。"""
    import time as _t
    try:
        from agent.management import launcher

        def _both_up() -> bool:
            return (launcher._http_identity_ok(launcher.PORT_MGMT)
                    and launcher._http_identity_ok(launcher.PORT_FRONTEND,
                                                   marker=None))
        if _both_up():
            return
        _t.sleep(2)  # 双检：给瞬时抖动一个恢复窗口
        if _both_up():
            return
        launcher._log("MCP 同步人审桥：管理端/前端不在线，自动拉起服务")
        launcher.start()
    except Exception as e:
        from core.logger import get_logger as _gl
        _gl(__name__).warning(
            "MCP 同步人审桥：服务保活失败（等待继续，用户可手动启动）: %s", e)


def _open_approval_page() -> None:
    """打开前端审批页（浏览器直达确认面）。无头环境无浏览器——吞掉，
    等待继续（前端全局 ApprovalWatcher 弹卡，用户也可手动打开）。"""
    try:
        import webbrowser
        from agent.management import launcher
        webbrowser.open(
            f"http://localhost:{launcher.PORT_FRONTEND}/dashboard/permissions")
    except Exception:
        pass


def _compose_sync_reply(settlement: dict | None) -> str:
    """结算回执 → AI 可见文本（如实转述，不伪装成功）"""
    if settlement is None:
        return ("⏱️ 未在有效期内批准，操作未执行（token 已过期）。"
                "如仍需执行请重新发起。")
    status = settlement.get("status", "")
    result = settlement.get("result", "")
    if status == "approved":
        return f"✅ 已批准并执行：\n{result}" if result else "✅ 已批准并执行"
    if status == "rejected":
        # 回执文本本身已是完整拒绝语义（"已拒绝：xxx——操作未执行…"）
        return f"⛔ {result}" if result else "⛔ 已拒绝：操作未执行"
    # approved_failed / error：如实转述执行未生效
    return f"⚠️ {result}" if result else "⚠️ 结算异常（操作未生效）"


def _resolve_mcp_identity(env_val: str | None) -> tuple:
    """MCP 通道身份解析：MCP_USER 绑定具体用户 → (用户名, users 表角色)；
    未设置/用户不存在 → ("", "")（未绑定=拒绝服务：handler 在调用点
    拒绝并提示配置 MCP_USER；启动期 _main 也会直接拒起）"""
    mcp_user = (env_val or "").strip()
    if not mcp_user:
        return "", ""
    from core.auth import get_user_role
    role = get_user_role(mcp_user)
    if role is None:
        return "", ""
    return mcp_user, role


async def _main() -> None:
    # 启动即绑定校验：未配置 MCP_USER 或用户不存在 → 拒起（fail-closed，
    # 未绑定通道连只读都不给——只读也是信息泄露面）
    from core.auth import init_users_table
    init_users_table()  # 入口自举：首启/全新环境无 users 表时先建表
    #（与 mgmt 首启同义；已存在则 no-op）
    _u, _r = _resolve_mcp_identity(os.environ.get("MCP_USER"))
    if not _u:
        import sys as _sys
        print("拒绝启动：MCP_USER 未配置或用户不存在。请在客户端 env 配置 "
              "MCP_USER（users 表中存在的用户名）。", file=_sys.stderr)
        _sys.exit(1)
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
