"""工具定义——每个颗粒化功能暴露一个 Tool，Agent 自行编排调用"""
import json as _json
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from core.logger import get_logger
from core.tool_result import ToolResult
import threading

logger = get_logger(__name__)


@dataclass
class Param:
    """工具参数声明"""
    name: str
    type: str          # "str" | "int" | "file" | "bool"
    description: str
    required: bool = False
    default: Any = None
    schema: Optional[dict] = None  # JSON Schema，优先于 type
    internal: bool = False  # 内部保留参数：不进 FC schema（AI 不可见），
    # 且无批量预批准上下文时 execute_tool 入口剥离（AI 直传不生效）


@dataclass
class Tool:
    """颗粒化功能工具——Agent 看到后自行调用"""
    name: str
    description: str
    handler: Callable
    params: list[Param] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    # 元数据（20260805，差距 1/3.3 地基）：动态白名单/元数据路由的事实源。
    # 纯声明式，不改变任何运行时行为——护栏仍在 execute_tool 的人审闸/契约层。
    risk_level: str = ""      # readonly | record_write | ddl | file | admin
    intent_tags: list[str] = field(default_factory=list)  # 意图标签（如 ["查","记录"]）
    gate: str = ""            # nuke | selection | none（主安全闸）
    # 3.3（20260806）：执行是否需要确定的目标表（单表或多表；参数直给或选择集回退）。
    # 操作对象是"新表"的不算（batch_create_tables/create_standard_tables=False）。
    # 用途：统一循环按任务上下文动态筛工具白名单（无表上下文不注入 requires_table 工具）。
    requires_table: bool = False

    def _params_summary(self) -> str:
        parts = []
        for p in self.params:
            if p.required:
                parts.append(p.name)
            else:
                parts.append(f"{p.name}={p.default}")
        return ", ".join(parts)


# ============================================================
# 注册表
# ============================================================
_tools: dict[str, Tool] = {}
_tools_lock = threading.Lock()


def get_tools() -> dict:
    """工具注册表只读快照（公开门面）——外部不再直戳 _tools 私有字典；
    快照语义：遍历/计数不受并发注册影响"""
    with _tools_lock:
        return dict(_tools)


def get_tool(name: str):
    """按名取工具（公开门面）；未注册返回 None"""
    with _tools_lock:
        return _tools.get(name)


# ── 高危卡影响面能力注入口（单向依赖）──
# 本模块需要"取驱动/描述表变更影响面"两件能力，但不准反向 import data_ops
#（data_ops→tool_registry 的 execute_tool 依赖已存在，反向即互耦环）。
# 由 data_ops 模块加载时经 register_nuke_card_capabilities 注入实现（DI，
# 与 core.data_ops.register_tree_router 同款）。
_describe_mutation_impl = None
_driver_factory = None


def register_nuke_card_capabilities(describe_mutation, driver_factory) -> None:
    """注册高危卡影响面能力（data_ops 启动时调用）"""
    global _describe_mutation_impl, _driver_factory
    _describe_mutation_impl = describe_mutation
    _driver_factory = driver_factory

def register_tool(tool: Tool):
    """注册一个工具

    同名工具重复注册只告警不抛错（避免破坏启动），后注册者覆盖先注册者。
    告警用于暴露意外的双注册——同名工具原则上应只有一个实现。
    """
    with _tools_lock:
        if tool.name in _tools:
            logger.warning(
                "工具重复注册: '%s' 已被 %s.%s 注册，现被 %s.%s 覆盖",
                tool.name,
                getattr(_tools[tool.name].handler, "__module__", "?"),
                getattr(_tools[tool.name].handler, "__name__", "?"),
                getattr(tool.handler, "__module__", "?"),
                getattr(tool.handler, "__name__", "?"),
            )
        _tools[tool.name] = tool


def unregister_tool(name: str) -> None:
    """注销工具（公开入口）——测试/动态场景用；不存在静默放过"""
    with _tools_lock:
        _tools.pop(name, None)




# ── 元数据查询（20260805，差距 1/3.3 地基）──

def apply_metadata(meta: dict[str, dict]):
    """批量给已注册工具标注元数据（纯声明式，不改运行时行为）

    meta 格式：{tool_name: {"risk_level": "readonly", "intent_tags": [...],
                           "gate": "none", "requires_table": True}}
    未在 meta 中出现的工具保持默认空值。注册后调用一次即可。
    """
    with _tools_lock:
        for name, m in meta.items():
            t = _tools.get(name)
            if t is None:
                logger.warning("元数据标注跳过未注册工具: %s", name)
                continue
            t.risk_level = m.get("risk_level", "")
            t.intent_tags = list(m.get("intent_tags", []))
            t.gate = m.get("gate", "")
            t.requires_table = bool(m.get("requires_table", False))


def find_tools(risk_level: str = "", gate: str = "",
               intent_tags: list[str] | None = None,
               requires_table: bool | None = None) -> list[Tool]:
    """按元数据筛选工具——动态白名单/元数据路由的查询入口

    全部条件 AND 组合；空条件不过滤（返回全部）。
    requires_table: None=不过滤；True=只要需表工具；False=只要免表工具。
    """
    with _tools_lock:
        candidates = list(_tools.values())
    if risk_level:
        candidates = [t for t in candidates if t.risk_level == risk_level]
    if gate:
        candidates = [t for t in candidates if t.gate == gate]
    if intent_tags:
        tag_set = set(intent_tags)
        candidates = [t for t in candidates if tag_set & set(t.intent_tags)]
    if requires_table is not None:
        candidates = [t for t in candidates if t.requires_table == requires_table]
    return candidates

# ============================================================
# 高危人审闸（1a，20260804；1b 扩展记录级写 20260804）
# ============================================================
# 不可逆删除性操作执行前必须用户确认，与 AI 走哪条路径无关：
# - 拦截点在 execute_tool 统一入口——决策树 / agent_run 循环 / 体系B 复用
#   _execute_single 三条路径全经此口，一处加闸全路覆盖
# - interrupt 走 LangGraph 原生 HITL（payload 与 graph.py unrecognized_review
#   同款 action_requests/review_configs 格式），前端 agent-inbox 组件通用渲染
# - 非 graph 上下文（测试脚本/裸函数调用）无 checkpointer，interrupt 会抛异常：
#   降级为"拒绝执行 + 返回需确认文本"——宁可不执行，不可无确认执行
# 高危人审闸集合：单一事实源 = 注册元数据 Tool.gate=="nuke"
#（曾硬编码 frozenset 与 _TOOL_METADATA 双轨漂移——
# 改一处不改另一处即漏闸/误闸）。find_tools(gate="nuke") 可枚举全集合。


def _nuke_impact_head(name: str, kwargs: dict) -> str:
    """确认卡头：操作名 + 参数表（人话化：过滤内部字段与已在影响面展示的关键字段）"""
    _HEAD_KEYS = {"table", "column", "name", "drop_tables", "all"}
    def _param_rows():
        rows = []
        for k, v in kwargs.items():
            if k in _HEAD_KEYS or (k == "database" and not v):
                continue
            rows.append(f"| {k} | `{v}` |")
        return "\n".join(rows) if rows else "| （无额外参数） | - |"

    return (
        f"## ⚠️ 高危操作待批准\n\n"
        f"| 项目 | 值 |\n|---|---|\n"
        f"| 操作 | `{name}` |\n"
        f"| 类型 | 不可逆（删除/清空/结构变更） |\n"
        + _param_rows()
        + "\n"
    )


def _impact_db_level(name: str, kwargs: dict, head: str, drv, _count) -> str:
    """库级族影响面：clear_db / drop_table(all=True)——逐表列数据量"""
    tables = drv.list_tables() or []
    if not tables:
        return head + "| 影响面 | 当前库无表 |\n"
    keep_schema = name == "clear_db" and not kwargs.get("drop_tables")
    scope = "删除所有表的数据（保留表结构）" if keep_schema else "删除所有表及其数据（不可恢复）"
    rows = []
    for t in tables[:20]:
        try:
            rows.append(f"| `{t}` | {_count(t)} 行 |")
        except Exception:
            rows.append(f"| `{t}` | 行数统计失败 |")
    more = f"\n> …共 {len(tables)} 张表" if len(tables) > 20 else ""
    return (head
            + f"| 影响面 | {scope} |\n"
            + f"| 涉及表数 | {len(tables)} |\n\n"
            + "**将删除的表：**\n\n"
            + "| 表名 | 数据量 |\n|---|---|\n" + "\n".join(rows) + more + "\n")


def _impact_ddl(name: str, kwargs: dict, head: str, _count, _referenced_by) -> str:
    """ddl 族影响面：drop_table 单表 / 字段·精度·非空·外键·索引级结构变更"""
    if name == "drop_table":
        t = kwargs.get("table", "?")
        try:
            cnt = _count(t)
            return (head
                    + f"| 目标表 | `{t}` |\n"
                    + f"| 数据量 | {cnt} 行 |\n"
                    + f"| 可逆性 | ❌ 不可恢复 |\n"
                    + f"| 连带影响 | 被 {_referenced_by(t)} 引用 |\n")
        except Exception:
            return head + f"| 目标表 | `{t}` |\n| 可逆性 | ❌ 不可恢复 |\n"
    t, c = kwargs.get("table", "?"), kwargs.get("column", "?")
    op_label = {
        "drop_column": "删除字段", "modify_column": "修改字段类型",
        "alter_precision": "修改精度", "set_not_null": "设为非空",
        "drop_foreign_key": "删除外键", "drop_index": "删除索引",
    }.get(name, name)
    try:
        return (head
                + f"| 操作 | {op_label} |\n"
                + f"| 目标表 | `{t}`（{_count(t)} 行） |\n"
                + f"| 目标字段 | `{c}` |\n"
                + f"| 可逆性 | ⚠️ 视情况（数据可能受影响） |\n")
    except Exception:
        return (head
                + f"| 操作 | {op_label} |\n"
                + f"| 目标表 | `{t}` |\n"
                + f"| 目标字段 | `{c}` |\n")


def _impact_template(kwargs: dict, head: str) -> str:
    """模板族影响面：drop_template"""
    return (head
            + f"| 操作 | 删除模板 |\n"
            + f"| 模板名 | `{kwargs.get('name', '?')}` |\n"
            + "| 可逆性 | ⚠️ 不影响现有表，但模板不可恢复 |\n")


def _impact_record(name: str, kwargs: dict, head: str, drv) -> str:
    """记录族影响面：edit_data / delete_data（选择集回退与 handler 严格一致）"""
    from core.context import get_context
    ctx = get_context()
    sid = kwargs.get("selection_id") or ctx.get_last_selection_id()
    sel = ctx.get_selection(sid) if sid else None
    if not sel:
        # 冻结 id 集在场（结算重放/跨进程选择集已失效）时如实展示目标集——
        # 不得笼统说"执行会报错"误导审批人
        _fz = kwargs.get("frozen_ids")
        if _fz:
            try:
                import json as _json
                _ids = _json.loads(_fz) if isinstance(_fz, str) else _fz
                return (head + f"| 影响面 | 冻结 id 集 {len(_ids)} 条"
                        f"（登记时刻快照，结算直执）：{_ids[:10]}"
                        f"{'…' if len(_ids) > 10 else ''} |\n")
            except Exception:
                return head + "| 影响面 | 冻结 id 集解析失败（请拒绝） |\n"
        return head + "| 影响面 | 选择集不存在或已失效（执行会报错，请拒绝） |\n"
    t = kwargs.get("table") or sel.get("table", "?")
    info = _describe_mutation_impl(
        drv, t,
        "DELETE" if name == "delete_data" else "UPDATE",
        sel.get("ids") or [], sel.get("sample"),
        kwargs.get("set_data", ""))
    return (head
            + f"| 操作 | {'删除' if name == 'delete_data' else '修改'}数据 |\n"
            + f"| 目标表 | `{t}` |\n"
            + info["summary"].replace("\n", "\n")
            + "\n\n**表结构：**\n\n" + info["structure"] + "\n")


def _describe_nuke_impact(name: str, kwargs: dict) -> str:
    """影响面评估：让用户在确认卡片里看到"将失去什么"

    输出为结构化 Markdown 表格（Reasonix 对话流渲染为卡片/表格，20260808）：
      - 参数表：操作名 + 完整参数（人话化）
      - 影响面表：目标 + 行数 + 可逆性
      - 连带影响：被哪些表引用（外键波及）

    评估是加分项不是前提——任何一步失败都降级为只显示操作本身，不挡闸。
    表名拼 SQL 前过 safe_table_sql（与契约层同一套标识符校验）。
    """
    head = _nuke_impact_head(name, kwargs)
    if _describe_mutation_impl is None or _driver_factory is None:
        # 能力未注册（data_ops 尚未加载）——显式降级，不静默进"评估失败"
        logger.warning("高危卡影响面能力未注册（data_ops 未加载），影响面降级为通用提示")
        return head + "| 影响面 | ⚠️ 影响面能力未注册（data_ops 未加载），请谨慎确认 |\n"
    try:
        from core.contract.security_contract import safe_table_sql
        drv = _driver_factory()

        def _count(t: str):
            rows = drv.query(f"SELECT COUNT(*) AS c FROM {safe_table_sql(t)}")
            return rows[0]["c"] if rows else "?"

        def _referenced_by(t: str) -> str:
            """找出引用 t 的其他表（外键波及）——仅影响面展示，不挡闸"""
            try:
                from core.graph.meta_db import MetaDB
                fks = MetaDB.get_instance().get_all_foreign_keys()
                refs = [f["table_name"] for f in fks if f.get("ref_table") == t]
                if not refs:
                    return "无其他表引用"
                shown = ", ".join(f"`{r}`" for r in refs[:8])
                more = f" 等共 {len(refs)} 张" if len(refs) > 8 else ""
                return shown + more
            except Exception:
                return "（查询失败）"

        if name == "clear_db" or (name == "drop_table" and kwargs.get("all")):
            return _impact_db_level(name, kwargs, head, drv, _count)
        if name == "drop_table" or name in (
                "drop_column", "modify_column", "alter_precision", "set_not_null",
                "drop_foreign_key", "drop_index"):
            return _impact_ddl(name, kwargs, head, _count, _referenced_by)
        if name == "drop_template":
            return _impact_template(kwargs, head)
        if name in ("edit_data", "delete_data"):
            return _impact_record(name, kwargs, head, drv)
    except Exception:
        pass  # 影响面装配失败——外层已有'评估失败请谨慎确认'卡兜底
    return head + "| 影响面 | ⚠️ 评估失败，请谨慎确认 |\n"


def _nuke_batch_preapproved(name: str, kwargs: dict) -> bool:
    """批量预批准命中判定（20260805）：mutate_natural 多表合并卡已获用户批准，
    逐表 execute_tool 时免检（避免同一批准重复弹卡）。命中返回 True"""
    try:
        from core.context import get_context
        _ctx = get_context()
        batch = _ctx.get_nuke_batch()
        if batch and name in batch.get("ops", set()):
            t = kwargs.get("table", "")
            if not t:
                # table 从选择集回退（与 handler 一致）
                sid = kwargs.get("selection_id") or _ctx.get_last_selection_id()
                sel = _ctx.get_selection(sid) if sid else None
                t = sel.get("table", "") if sel else ""
            # "*" 通配：库级无表操作（clear_db 等）的批准放行（管理端审批中心用）
            if (t and t in batch.get("tables", set())) or "*" in batch.get("tables", set()):
                logger.info("人审闸：%s(%s) 批量预批准命中，免检", name, t or "*")
                return True
    except Exception:
        pass  # 预批准检查失败不挡闸——继续走正常 interrupt 流程
    return False


def _register_pending_bridge(name: str, kwargs: dict, prepare: Callable) -> None:
    """MCP 通道桥公共段（人审闸/force 确认卡同构，20260807/20260824）：
    channel 判定 → 差异化备料 → register_pending 挂起登记 → PendingApproval 回执。

    MCP server 进程无 LangGraph runtime，interrupt 必然失败拒执——改走挂起表
    回执：登记 PendingApproval(token)，由管理端审批中心结算（复用批量预批准
    通道放行；confirm_action 已于 20260822 改只读——token 不回传 AI 通道，
    防 AI 自助结算）。安全语义不变：未确认不执行；token 一次性、10 分钟有期。

    prepare(kwargs) 为各闸差异化备料回调，返回
    (登记用 kwargs 副本, 影响面文本, 日志标签, 回执文案) 四元组；其内部主动
    抛出的 PendingApproval（如查重命中）经下方同型 except 原样向上放行。
    非 MCP 通道或任一环节异常：静默返回，各闸续走正常 interrupt 流程（不挡闸）。
    """
    try:
        from core.context import get_context as _gcb
        if _gcb().get_channel() != "mcp":
            return
        from core.exceptions import PendingApproval
        from core.pending_ops import register_pending_dedup
        kw, impact, log_label, confirm_text = prepare(kwargs)
        token, is_dup = register_pending_dedup(name, kw, impact)
        if is_dup:
            raise PendingApproval(
                "⏸️ 该操作已在审批队列中（见管理台审批中心），无需重复提交。\n"
                "操作尚未执行。请用户到 Web 管理台的「权限管理 → 待审批」中"
                "批准或拒绝；你不得也无法自行结算本操作。",
                token=token)
        logger.info("%s（MCP 通道）：%s → 待批准 %s…", log_label, name, token[:10])
        # token 不回传 AI 通道（防 AI 自助结算人审闸）——
        # AI 只转述影响面/风险，批准动作只能发生在 Web 管理台（admin）
        raise PendingApproval(confirm_text, token=token)
    except PendingApproval:
        raise
    except Exception:
        pass  # 通道检测失败不挡闸——继续走正常 interrupt 流程


def _nuke_confirmed(name: str, kwargs: dict) -> bool:
    """高危确认闸：interrupt 挂起等用户决策

    返回 True=用户批准；False=拒绝/无法确认（安全默认）。
    decision 解析与 graph.py unrecognized_review 同格式，但默认拒绝
    （高危场景宁可误拒，不可误放）。

    批量预批准（20260805）：mutate_natural 多表合并卡已获用户批准，
    逐表 execute_tool 时免检（避免同一批准重复弹卡）。
    """
    if _nuke_batch_preapproved(name, kwargs):
        return True

    # MCP 通道桥差异化备料：影响面评估 → 表名解析进 kwargs（挂起表跨进程
    # 落盘后由管理端结算执行，届时选择集已不可用）→ 冻结 ids 注入
    def _prepare(orig: dict):
        impact = _describe_nuke_impact(name, orig)
        kw = dict(orig)
        from core.context import get_context as _gc2
        sid = kw.get("selection_id") or _gc2().get_last_selection_id()
        sel = _gc2().get_selection(sid) if sid else None
        if not kw.get("table") and sel and sel.get("table"):
            kw["table"] = sel["table"]
        # 审批冻结 ids（TOCTOU 防护）：单表改/删的结算在管理端进程重放——
        # 选择集是登记进程的主体载荷，跨进程/跨主体读不到（owner 隔离）；
        # 冻结 id 集后结算直执，不受选择集生命周期与主体隔离影响，
        # 且批准对象=执行对象精确同一（行级 TOCTOU 无面）
        if sel and sel.get("ids") is not None and name in ("edit_data", "delete_data"):
            kw["frozen_ids"] = _json.dumps(sel["ids"], ensure_ascii=False)
        return kw, impact, "高危人审闸", (
            f"⏸️ 高危操作待批准（审批编号见管理台审批中心）\n{impact}\n"
            "操作尚未执行。请向用户完整展示上述影响面，并请用户到"
            " Web 管理台的「权限管理 → 待审批」中批准或拒绝；"
            "你不得也无法自行结算本操作。")
    _register_pending_bridge(name, kwargs, _prepare)

    try:
        from langgraph.types import interrupt
    except ImportError:
        logger.warning("人审闸：langgraph 不可用，拒绝执行 %s", name)
        return False
    try:
        decision = interrupt({
            "action_requests": [{
                "name": name,
                "args": kwargs,
                "description": _describe_nuke_impact(name, kwargs),
            }],
            "review_configs": [{
                "action_name": name,
                "allowed_decisions": ["approve", "reject"],
            }],
        })
    except Exception as e:
        # GraphInterrupt 是正常挂起信号（人审卡片就靠它弹给前端），不是故障——
        # 必须放行给 LangGraph runtime，吞掉它闸就永远拦不住也确认不了（1a 修复 20260804）
        from langgraph.errors import GraphInterrupt
        if isinstance(e, GraphInterrupt):
            raise
        import traceback as _tb
        logger.warning("人审闸：interrupt 上下文缺失（%s），拒绝执行 %s\n堆栈:\n%s",
                       e, name, _tb.format_exc())
        return False
    d = {}
    if isinstance(decision, dict):
        decisions = decision.get("decisions") or []
        d = decisions[0] if decisions else {}
    approved = d.get("type") == "approve"
    logger.info("人审闸：%s(%s) → 用户决策=%s", name,
                str(kwargs)[:120], "批准" if approved else "拒绝")
    return approved


def _force_confirmed(name: str, kwargs: dict, detail: str, report: dict) -> bool:
    """契约层 force 确认卡：handler/契约自报 need_force 时，
    把风险明细列在卡片上给用户看，批准才允许带 force=True 内部重试。

    与人审闸同原语同协议（interrupt + action_requests/review_configs），职责不同：
    人审闸管"不可逆操作做不做"，本闸管"契约检出的具体风险认不认"。
    MCP 通道桥（20260824）：无 LangGraph runtime 的进程 interrupt 恒拒——
    need_force 类操作曾因此成永久死路；改走挂起表登记
    （kwargs 带 force=True + 风险明细），管理端审批中心批准后按批量预批准
    通道放行。非 graph/MCP 上下文安全默认拒绝（宁可不执行，不可无确认执行）。
    """
    # MCP 通道桥差异化备料：风险明细影响面 + force=True 注入——
    # 查重不在此做（锁外查重是 TOCTOU 面）：桥内 register_pending_dedup
    # 原子完成（find+register 同一 FileLock 临界区，重复提交回"已在审批队列"）
    def _prepare(orig: dict):
        impact = (f"⚠️ 契约校验发现风险（操作：{name}）：\n{detail}"
                  + (f"\n\n风险报告：{_json.dumps(report, ensure_ascii=False, default=str)[:800]}"
                     if report else ""))
        kw = dict(orig)
        kw["force"] = True
        return kw, impact, "force 风险确认", (
            f"⏸️ 契约风险确认待批准（审批编号见管理台审批中心）\n{impact}\n"
            "操作尚未执行。请向用户完整展示上述风险，并请用户到 Web 管理台的"
            "「权限管理 → 待审批」中批准或拒绝；你不得也无法自行结算本操作。")
    _register_pending_bridge(name, kwargs, _prepare)
    try:
        from langgraph.types import interrupt
    except ImportError:
        logger.warning("force确认卡：langgraph 不可用，拒绝放行 %s", name)
        return False
    args = {"操作": name, "参数": _json.dumps(kwargs, ensure_ascii=False),
            "风险详情": detail}
    if report:
        # 长报告折叠展示（前端 __fold__ 协议），卡片主体保持简洁
        args["风险报告"] = {"__fold__": "契约风险报告明细（点击展开）",
                          "content": _json.dumps(report, ensure_ascii=False,
                                               indent=1, default=str)}
    try:
        decision = interrupt({
            "action_requests": [{
                "name": f"风险确认：{name}",
                "args": args,
                "description": (
                    f"⚠️ 契约校验发现风险，操作暂未执行：\n{detail}\n\n"
                    "确认执行 = 认可上述风险并强制执行（force=True）；\n"
                    "拒绝执行 = 操作取消，数据保持不变。"),
            }],
            "review_configs": [{
                "action_name": f"风险确认：{name}",
                "allowed_decisions": ["approve", "reject"],
            }],
        })
    except Exception as e:
        # GraphInterrupt 是正常挂起信号，必须放行给 runtime（同人审闸口径）
        from langgraph.errors import GraphInterrupt
        if isinstance(e, GraphInterrupt):
            raise
        logger.warning("force确认卡：interrupt 上下文缺失（%s），拒绝放行 %s", e, name)
        return False
    d = {}
    if isinstance(decision, dict):
        decisions = decision.get("decisions") or []
        d = decisions[0] if decisions else {}
    approved = d.get("type") == "approve"
    logger.info("force确认卡：%s → 用户决策=%s", name, "批准" if approved else "拒绝")
    return approved


def _strip_internal_params(tool: Tool, tool_name: str, kwargs: dict) -> None:
    """内部保留参数闸：internal=True 的参数（frozen_ids 等）只有两个合法携带者——
    本模块人审闸登记时的自注入（在下方分支内完成，不经入口）与管理端结算重放
    （结算前必置批量预批准上下文）。其余来源（AI 提参/直连自供）一律剥离：
    否则"批准对象=执行对象同一"不变量可被注入载荷架空"""
    _internal = [p.name for p in tool.params if p.internal]
    if _internal and any(kwargs.get(n) for n in _internal):
        from core.context import get_context as _gc0
        if not _gc0().get_nuke_batch():
            for n in _internal:
                if kwargs.get(n):
                    kwargs.pop(n)
                    logger.warning("内部参数 %s 无批量预批准上下文，已剥离（工具 %s）",
                                   n, tool_name)


def _coerce_param_types(tool: Tool, kwargs: dict) -> "ToolResult | None":
    """参数类型转换（裸抛收口：int/bool 转换失败归一 VALIDATION——
    此前 int(val) 裸抛 ValueError，管理端结算链撞上即 500 且 token 已焚）。
    返回 None=通过；否则为转换失败的 ToolResult"""
    for p in tool.params:
        if p.name in kwargs:
            val = kwargs[p.name]
            try:
                if p.type == "int":
                    kwargs[p.name] = int(val)
                elif p.type == "bool":
                    kwargs[p.name] = val if isinstance(val, bool) else val.lower() in ("true", "1", "yes")
            except (TypeError, ValueError, AttributeError) as e:
                return ToolResult.fail(f"参数 {p.name} 类型转换失败: {e}",
                                       code="VALIDATION", reason="type_coerce")
        elif p.default is not None:
            kwargs[p.name] = p.default
    return None


def _run_nuke_gate(tool_name: str, kwargs: dict) -> "ToolResult | None":
    """高危人审闸段：确认通过返回 None；拒绝/待批准返回对应 ToolResult"""
    from core.exceptions import PendingApproval
    try:
        _confirmed = _nuke_confirmed(tool_name, kwargs)
    except PendingApproval as p:
        # MCP 通道：挂起表回执——返回"待批准"结果（操作未执行），
        # AI 转述影响面，用户到管理端审批中心批准（token 不出 AI 通道）。
        # token 进 data（机器通道）而非 text：__str__ 只暴露 text，AI/LLM
        # 可见通道永远看不到 token（防 AI 自助结算的不变量保持）；
        # data 仅由 mcp_server 同步等待桥（MCP_APPROVAL_SYNC）消费
        return ToolResult.fail(p.message, code="CONTRACT",
                               reason="pending_approval",
                               approval_token=p.token)
    if not _confirmed:
        return ToolResult.fail(
            f"⛔ 操作未执行：{tool_name} 属不可逆写操作，"
            "需用户在前端确认卡片中批准（或已被拒绝/当前环境不支持确认）",
            code="VALIDATION", reason="nuke_rejected")
    return None


def _run_force_predeclared_gate(tool_name: str, filtered_kw: dict) -> "ToolResult | None":
    """force 参数前置闸的人审确认段：批准/免检返回 None；拒绝/待批准返回 ToolResult"""
    # 本轮已获人审批准（人审闸批量预批准/管理端审批中心结算）→ 免检，不重复弹卡
    from core.context import get_context as _gc3
    _batch = _gc3().get_nuke_batch()
    _preapproved = bool(_batch) and tool_name in _batch.get("ops", set())
    if not _preapproved:
        from core.exceptions import PendingApproval as _PA
        try:
            _ok = _force_confirmed(tool_name, filtered_kw,
                                   "调用方预先声明 force=true（要求跳过安全检查）——"
                                   "契约风险明细未出示即要求强制执行。", {})
        except _PA as p:
            # MCP 通道桥：挂起表回执（待批准），token 不回传 AI 通道。
            # token 进 data（机器通道，__str__ 只暴露 text）——仅由
            # mcp_server 同步等待桥消费
            return ToolResult.fail(p.message, code="CONTRACT",
                                   reason="pending_approval",
                                   approval_token=p.token)
        if not _ok:
            from core.tool_result import ToolResult as _TR
            return _TR.fail(f"⛔ 操作未执行：{tool_name} 的 force=true 未经人审批准",
                            code="VALIDATION", reason="force_rejected")
    return None


def _invoke_handler(tool: Tool, kw: dict) -> "ToolResult":
    """单次 handler 调用：返回通道与异常通道统一映射为 ToolResult"""
    try:
        result = tool.handler(**kw)
        if isinstance(result, ToolResult):
            return result
        return ToolResult.legacy(str(result) if result is not None else "执行完成")
    except Exception as e:
        # GraphInterrupt（HITL 挂起信号）不是错误，必须放行给 LangGraph runtime
        from langgraph.errors import GraphInterrupt
        if isinstance(e, GraphInterrupt):
            raise
        # 结构化异常映射：code 由异常类型决定，不经文本反推（双轨契约核心收益）
        from core.exceptions import (AppError, PrimaryKeyError, RiskError,
                                     SecurityError)
        from core.permission import PermissionDenied
        text = e.message if isinstance(e, AppError) else f"执行失败: {e}"
        if isinstance(e, PermissionDenied):
            return ToolResult.fail(text, code="CONTRACT",
                                   reason="permission_denied",
                                   error_kind=type(e).__name__)
        if isinstance(e, RiskError):
            return ToolResult.fail(text, code="VALIDATION", reason="need_force",
                                   error_kind="RiskError",
                                   forceable=getattr(e, "forceable", True),
                                   report=getattr(e, "report", {}) or {})
        if isinstance(e, PrimaryKeyError):
            return ToolResult.fail(text, code="VALIDATION", reason="primary_key",
                                   error_kind="PrimaryKeyError")
        if isinstance(e, SecurityError):
            return ToolResult.fail(text, code="CONTRACT", reason="security",
                                   error_kind="SecurityError")
        if isinstance(e, AppError):
            return ToolResult.fail(text, code="UNKNOWN", reason="app_error",
                                   error_kind=type(e).__name__)
        return ToolResult.fail(f"执行失败: {e}", code="UNKNOWN", reason="exception",
                               error_kind=type(e).__name__)


def _run_need_force_card(tool: Tool, tool_name: str, filtered_kw: dict,
                         tr: "ToolResult") -> "ToolResult":
    """need_force 确认卡段：批准→带 force=True 内部重试；拒绝→原结果照常返回；
    MCP 通道 PendingApproval→待批准结果"""
    from core.exceptions import PendingApproval as _PA
    try:
        _ok = _force_confirmed(tool_name, filtered_kw, tr.text,
                               tr.data.get("report") or {})
    except _PA as p:
        # MCP 通道桥：挂起表回执（待批准），token 不回传 AI 通道。
        # token 进 data（机器通道，__str__ 只暴露 text）——仅由
        # mcp_server 同步等待桥消费
        return ToolResult.fail(p.message, code="CONTRACT",
                               reason="pending_approval",
                               approval_token=p.token)
    if _ok:
        tr = _invoke_handler(tool, {**filtered_kw, "force": True})
    return tr


def execute_tool(tool_name: str, /, **kwargs) -> "ToolResult":
    """执行指定工具，返回双轨契约 ToolResult（str(result) 与历史文本完全一致）

    data.code 结构化映射（替代文本分类）：
    - handler 返回 ToolResult → 直通（工具自报 ok/code/reason/effects）
    - handler 返回 str → legacy 包装（ok=None，迁移期过渡态）
    - 契约/权限异常 → 按异常类型映射 code（不再靠文案关键词反推）
    """
    with _tools_lock:
        tool = _tools.get(tool_name)
    if not tool:
        return ToolResult.fail(f"未知工具: {tool_name}", code="VALIDATION",
                               reason="unknown_tool")

    _strip_internal_params(tool, tool_name, kwargs)

    _fail = _coerce_param_types(tool, kwargs)
    if _fail is not None:
        return _fail

    # 生成后校验边界闸（20260824）：requires_table 工具的表/字段参数在执行前
    # 过确定性存在性校验（唯一实现 core.tool_arg_guard）——
    # MCP 直连无此闸时，AI 拿带噪/假想表名直达驱动层，报错丢名难自查。
    # fail-open：仅作健壮性增强，
    # 表存在性的安全兜底仍在各工具/契约层，不替代。
    if getattr(tool, "requires_table", False):
        from core.tool_arg_guard import guard_for_execute
        _g = guard_for_execute(tool_name, kwargs)
        if _g is not None:
            return _g

    # 高危人审闸：类型转换之后、handler 调用之前（卡片显示转换后的准确参数）
    # 闸集合 = 元数据 gate=="nuke"（单一事实源）
    if tool.gate == "nuke":
        _fail = _run_nuke_gate(tool_name, kwargs)
        if _fail is not None:
            return _fail

    sig = inspect.signature(tool.handler)
    allowed = set(sig.parameters.keys())
    # 透传 query / original_input 等额外上下文
    if "query" in sig.parameters: allowed.add("query")
    filtered_kw = {k: v for k, v in kwargs.items() if k in allowed}

    # force 参数前置闸（20260822 安全修复）：调用方直接传 force=true =
    # 声明"跳过安全检查"——本身即高危声明，必须过人审卡；此前直放导致
    # AI 可用 force=true 静默绕过全部契约风险确认卡（security_review 发现）。
    # 批准 → 保留 force 执行；拒绝/环境不支持 → 不执行（宁可不动）。
    if filtered_kw.get("force") and "force" in allowed:
        _fail = _run_force_predeclared_gate(tool_name, filtered_kw)
        if _fail is not None:
            return _fail

    tr = _invoke_handler(tool, filtered_kw)
    # 契约层 force 确认卡（对话模式与人工闸同款卡片，替代"请回复 force=true"文字交互）：
    # handler/契约自报 need_force 且可放行（forceable）→ 弹卡列风险明细 →
    # 批准则带 force=True 内部重试（直调 handler，不再过人审闸——用户已在本轮确认）；
    # 拒绝/环境不支持确认 → 原 need_force 结果照常返回（上层文字路径兜底）。
    if (tr.data.get("reason") == "need_force"
            and tr.data.get("forceable", True)
            and not filtered_kw.get("force")
            and "force" in allowed):
        tr = _run_need_force_card(tool, tool_name, filtered_kw, tr)
    return tr


# 注意：内置工具统一在 agent/tools.py 注册（唯一实现方）。
# 本模块只提供注册表基础设施，不再自带工具注册，
# 避免与 agent/tools.py 形成同名工具双注册（dict 覆盖取决于 import 顺序，危险）。
