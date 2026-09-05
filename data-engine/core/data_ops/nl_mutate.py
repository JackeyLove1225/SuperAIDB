"""data_ops 子模块·自然语言改/删编排：结构化 op → 候选探测 → 单/多表分流
（单表走 execute_tool 人审闸；多表拓扑排序 + 合并确认卡 + MCP 通道挂起登记）
（20260830 拆包：core/data_ops.py 同名片段纯搬家，逻辑零变化）

patch 兼容（测试依赖，勿绕开）：get_driver / _load_table_schema /
_extract_mutation_ops / _route_tool 的引用一律在调用时经 facade 取值
（_ops.X(...)），使 patch.object(data_ops, "_extract_mutation_ops", ...)
之类的打桩保持有效。
"""
from collections import deque
import re as _re

from core.logger import get_logger
from core.tool_result import ToolResult
from core.contract.security_contract import safe_table_sql

# facade 回旋引用：仅用于调用时取值（_ops.get_driver()），导入期不解引用
from core import data_ops as _ops

logger = get_logger(__name__)


def _build_set_where(op: dict):
    """结构化 op → (set_clause, where)（确定性拼装，无 AI）"""
    def quote_val(v):
        if v is None: return "NULL"
        sv = str(v)
        # 严格数值判定：此前 strip 式判断会把 "1-2" 当数字裸拼（SQL 里变算术 1-2=-1，静默写错）
        if _re.match(r'^-?\d+(?:\.\d+)?$', sv):
            return sv
        if sv.startswith("'") and sv.endswith("'"):
            return sv  # AI 已带引号的输出原样兼容
        # 单引号 doubling 转义——否则 O'Brien 类值被驱动层正则重解析时静默截断
        return "'" + sv.replace("'", "''") + "'"
    set_parts = []
    for sf in op.get("set_fields", []):
        set_parts.append(f"{sf['field']}={quote_val(sf.get('value', ''))}")
    set_clause = ", ".join(set_parts)
    from core.condition_parser import build_where
    where = (build_where(op.get("where_conditions", [])) or "").strip()
    if where.startswith("WHERE "):
        where = where[6:].strip()
    return set_clause, where


def _reverse_referencing(drv, table: str) -> list[tuple[str, str]]:
    """反向 FK 扫描：哪些表的哪个列引用了 table（删除影响面评估的关键信息）

    任何一张表的 schema 读取失败都不影响其余表的扫描（评估是加分项不是前提）。
    """
    out: list[tuple[str, str]] = []
    try:
        others = drv.list_tables() or []
    except Exception:
        return out
    for other in others:
        if other.lower() == table.lower():
            continue
        try:
            schema = _ops._load_table_schema(other)
        except Exception:
            continue
        for fk in schema.get("foreign_keys", []):
            if fk.get("references", "").lower() == table.lower():
                cols = fk.get("columns", [])
                if cols:
                    out.append((other, cols[0]))
    return out


def describe_table_mutation(drv, table: str, action: str, ids: list,
                            sample: list | None = None, set_data: str = "") -> dict:
    """单表改/删影响面描述——单表人审卡（tool_registry）与多表合并卡
    （mutate_natural）共用同一套信息结构，保证两处展示一致：

    - 条数/总行数（判断是否整表清空）
    - 记录预览（前2条样本，确认没删错）
    - 表结构（全字段+主键标记）
    - 正向 FK（本表引用谁）
    - 反向引用计数（谁引用了将被删/改的记录、各多少行——删除决策最需要的信息）

    任何一步失败都降级，不阻断主流程。
    """
    try:
        rows = drv.query(f"SELECT COUNT(*) AS c FROM {safe_table_sql(table)}")
        total = rows[0]["c"] if rows else "?"
    except Exception:
        total = "?"
    preview = "；".join(
        ", ".join(f"{k}={str(v)[:15]}" for k, v in list(r.items())[:4])
        for r in (sample or [])[:2]) or "（无预览）"
    try:
        cols = drv.get_columns(table)
        structure = "\n".join(
            f"  {c['name']} {c['type']}" + ("（主键）" if c.get("pk") else "")
            for c in cols)
    except Exception:
        cols = []
        structure = "  （结构获取失败）"
    try:
        fks = _ops._load_table_schema(table).get("foreign_keys", [])
        fwd = [f"  {', '.join(fk.get('columns', []))} → {fk.get('references', '?')}.id"
               for fk in fks]
    except Exception:
        fwd = []
    # 反向引用计数：ids 经 ids_in_clause 类型感知拼装（文本主键的脏 id 不成注入文本；
    # 此前 str(int()) 过滤会静默丢弃文本主键——收口唯一实现）
    rev: list[str] = []
    if ids:
        from core.contract.security_contract import ids_in_clause
        for other, col in _reverse_referencing(drv, table):
            try:
                rows = drv.query(
                    f"SELECT COUNT(*) AS c FROM {safe_table_sql(other)} "
                    f"WHERE {ids_in_clause(ids, col)}")
                c = rows[0]["c"] if rows else 0
                rev.append(f"  ⚠ {other}.{col} → {c} 行引用了将被影响的记录"
                           if c else f"  {other}.{col} → 0 行（无影响）")
            except Exception:
                rev.append(f"  {other}.{col} → 引用统计失败")
    verb = "删除" if action == "DELETE" else "修改"
    extra = f"，新值：{set_data}（旧值被覆盖）" if action == "UPDATE" and set_data else ""
    summary = (
        f"【{table}】{verb} {len(ids)} 条（全表 {total} 行）{extra}，不可恢复\n"
        f"记录预览：{preview}\n"
        f"本表外键：{chr(10).join(fwd) if fwd else '无'}\n"
        f"被引用（连带影响）：\n{chr(10).join(rev) if rev else '  无表引用本表'}"
    )
    return {
        "summary": summary,
        "structure": f"  {table}（{len(cols)} 字段）：\n{structure}",
        "col_count": len(cols),
    }


def _topo_sort_deletes(pending: list[dict]) -> list[dict]:
    """多表删除的外键拓扑排序：子表（引用方）先删，主表（被引用方）后删，
    避免先删主表导致子表行悬空（或 FK 约束报错）。

    只对 DELETE 排序（UPDATE 不删行，无顺序安全问题），UPDATE 保持原相对顺序附后。
    循环引用（A↔B）无法拓扑时退化为原顺序，执行报错由底层如实抛出（不静默）。
    """
    del_idx = [i for i, p in enumerate(pending) if p["action"] == "DELETE"]
    if len(del_idx) < 2:
        return pending
    tables = list(dict.fromkeys(pending[i]["table"] for i in del_idx))
    # 边 t→ref 表示"t 引用 ref，t 必须先删"；indeg[t]=引用 t 的表数
    edges: dict[str, set] = {t: set() for t in tables}
    indeg: dict[str, int] = {t: 0 for t in tables}
    for t in tables:
        for fk in _ops._load_table_schema(t).get("foreign_keys", []):
            ref = fk.get("references", "")
            if ref in edges and ref != t and ref not in edges[t]:
                edges[t].add(ref)
                indeg[ref] += 1
    q = deque([t for t in tables if indeg[t] == 0])
    order: list[str] = []
    while q:
        n = q.popleft()
        order.append(n)
        for m in edges[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    if len(order) < len(tables):  # 有环：未排出的按原顺序追加
        order += [t for t in tables if t not in order]
    rank = {t: i for i, t in enumerate(order)}
    sorted_del = sorted(del_idx,
                        key=lambda i: rank.get(pending[i]["table"], len(rank)))
    del_set = set(del_idx)
    return [pending[i] for i in sorted_del] + \
           [p for i, p in enumerate(pending) if i not in del_set]


def _multi_ops_preapproved(pending: list[dict]) -> bool:
    """批量预批准命中免检（管理端结算通道，与 _nuke_confirmed 同型）"""
    from core.context import get_context as _gcm
    _batch = _gcm().get_nuke_batch()
    if _batch:
        _ptables = {p["table"] for p in pending}
        _pops = {"delete_data", "edit_data"}
        if _ptables <= set(_batch.get("tables", set())) and _pops <= set(_batch.get("ops", set())):
            logger.info("多表确认闸：批量预批准命中，免检（%d 张表）", len(pending))
            return True
    return False


def _assemble_multi_impact(drv, pending: list[dict]) -> tuple[str, str, list]:
    """逐 op 装配影响面：各表 describe_table_mutation → 明细行 + 合并描述 +
    结构列表（确认卡与挂起登记共用同一份文案）

    返回 (desc, detail, structures)。
    """
    sections, structures = [], []
    for p in pending:
        info = describe_table_mutation(drv, p["table"], p["action"], p["ids"],
                                       p.get("sample"), p.get("set_data", ""))
        sections.append(info["summary"])
        structures.append(info["structure"])
    detail_lines = [f"  {i+1}. {p['action']} {p['table']}：{len(p['ids'])} 条"
                    for i, p in enumerate(pending)]
    detail = "\n".join(detail_lines)
    desc = (f"⚠️ 不可逆批量操作：共涉及 {len(pending)} 张表，"
            f"执行顺序已按外键依赖排序（先删子表后删主表）\n{detail}\n\n"
            + "\n\n".join(sections))
    return desc, detail, structures


def _register_multi_pending(pending: list[dict], instruction: str, desc: str) -> None:
    """MCP 通道桥：登记单项挂起并抛 PendingApproval——token 不回传
    AI 通道，批准动作只能发生在 Web 管理台（admin）"""
    from core.exceptions import PendingApproval
    from core.pending_ops import register_pending_dedup
    token, is_dup = register_pending_dedup(
        "__multi_mutate__",
        {"instruction": instruction,
         "_tables": sorted({p["table"] for p in pending}),
         "_ops": ["delete_data", "edit_data"],
         # 审批冻结（TOCTOU）：登记时刻的结构化 ops +
         # 各表候选数——结算重放冻结 ops 并重算候选比对，漂移即拒绝
         "_ops_frozen": [p["_raw_op"] for p in pending],
         "_expect_ids": [{"table": p["table"], "ids": sorted(p["ids"])}
                         for p in pending]},
        desc)
    if is_dup:
        raise PendingApproval(
            f"⏸️ 该批量写操作已在审批队列中（见管理台审批中心），无需重复提交。\n"
            "操作尚未执行。请用户到 Web 管理台的「权限管理 → 待审批」中"
            "批准或拒绝；你不得也无法自行结算本操作。",
            token=token)
    logger.info("多表合并闸（MCP 通道）：%d 张表 → 待批准 %s…", len(pending), token[:10])
    raise PendingApproval(
        f"⏸️ 批量写操作待批准（审批编号见管理台审批中心）\n{desc}\n"
        "操作尚未执行。请向用户完整展示上述影响面，并请用户到 Web 管理台的"
        "「权限管理 → 待审批」中批准或拒绝；你不得也无法自行结算本操作。",
        token=token)


def _interrupt_multi_decision(pending: list[dict], instruction: str,
                              detail: str, desc: str,
                              structures: list) -> bool:
    """人审卡 interrupt 通道：无 runtime 的裸上下文安全默认拒绝；
    GraphInterrupt 原样放行"""
    try:
        from langgraph.types import interrupt
    except ImportError:
        logger.warning("多表确认闸：langgraph 不可用，拒绝执行")
        return False
    try:
        decision = interrupt({
            "action_requests": [{
                "name": f"批量写操作（{len(pending)}张表）",
                "args": {
                    "指令": instruction,
                    "操作明细": detail,
                    "执行顺序": "已按外键依赖拓扑排序：先删子表（引用方），后删主表（被引用方），避免悬空引用",
                    "各表结构": {"__fold__": f"各表字段结构（{len(pending)} 张表，点击展开）",
                                 "content": "\n".join(structures)},
                },
                "description": desc,
            }],
            "review_configs": [{
                "action_name": "批量写操作",
                "allowed_decisions": ["approve", "reject"],
            }],
        })
    except Exception as e:
        from langgraph.errors import GraphInterrupt
        if isinstance(e, GraphInterrupt):
            raise
        import traceback as _tb
        logger.warning("多表确认闸：interrupt 上下文缺失（%s），拒绝执行\n堆栈:\n%s",
                       e, _tb.format_exc())
        return False
    d = {}
    if isinstance(decision, dict):
        decisions = decision.get("decisions") or []
        d = decisions[0] if decisions else {}
    approved = d.get("type") == "approve"
    logger.info("多表确认闸：%d 张表 → 用户决策=%s",
                len(pending), "批准" if approved else "拒绝")
    return approved


def _multi_ops_confirmed(pending: list[dict], instruction: str) -> bool:
    """多表改/删合并确认闸：一张卡片展示全部表的影响面，一次确认全执行/全拒绝

    三通道语义（与人审闸同型）：
    - 批量预批准：管理端审批中心结算时已批量放行（nuke batch 命中免检）
    - MCP 通道：无 LangGraph runtime，interrupt 恒拒会把多表改删变成永久
      死路——登记为单项挂起，管理端批准后重放原指令结算
    - 其余（无 runtime 的裸上下文）：安全默认拒绝；GraphInterrupt 放行
    """
    if _multi_ops_preapproved(pending):
        return True
    desc, detail, structures = _assemble_multi_impact(_ops.get_driver(), pending)
    from core.context import get_context as _gcm
    if _gcm().get_channel() == "mcp":
        _register_multi_pending(pending, instruction, desc)
    return _interrupt_multi_decision(pending, instruction, detail, desc, structures)


# 候选安全帽：单个 op 命中的候选记录数超过该值即整组拒绝，要求用户缩小条件；
# 探测查询多取一行（帽+1）以区分"恰好帽值"与"超帽"
_CANDIDATE_CAP = 100


def _execute_pending_op(p: dict, instruction: str) -> "ToolResult":
    """执行单个已收拢的改/删 op（单表/多表路径共用）：留选择集痕 →
    树路由（删/改+记录→delete_data/edit_data）→ execute_tool 人审闸；
    成功时装配 data.effects（UPDATE 另带 expected_values）"""
    from core.tool_registry import execute_tool
    from core.context import get_context
    sid = get_context().save_selection(p["table"], p["rows"], query=instruction)
    behavior = "删" if p["action"] == "DELETE" else "改"
    tool_name = _ops._route_tool(behavior, "记录", "")
    kwargs = {"selection_id": sid, "table": p["table"]}
    if p["action"] == "UPDATE":
        kwargs["set_data"] = p["set_data"]
    r = execute_tool(tool_name, **kwargs)
    if r.data.get("ok"):
        r.data["effects"] = {
            "table": p["table"], "action": p["action"],
            "affected": r.data.get("affected", 0),
            "affected_ids": p["ids"],
            "changed_fields": p["changed_fields"],
        }
        if p["expected_values"]:
            r.data["effects"]["expected_values"] = p["expected_values"]
    return r


def _probe_single_op(op: dict, action: str, expect_ids_map: dict | None,
                     track) -> dict | None:
    """单 op 校验 + 候选探测：表名/WHERE 安全校验 → 确定性查候选 →
    冻结漂移比对 → 0 条/超帽/不支持操作如实失败（经 track 记入失败轨）。

    通过校验且候选数未超安全帽时返回收拢的 pending 条目，否则返回 None。
    """
    t = op.get("table", "")
    a = op.get("action", "").upper()
    if action and a != action:
        return None
    set_clause, where = _build_set_where(op)
    changed_fields = [sf["field"] for sf in op.get("set_fields", []) if sf.get("field")]
    if not t:
        track(ToolResult.fail(
            "未能确定要操作的表，请明确表名", code="VALIDATION",
            reason="table_unclear"))
        return None
    # 确定性查候选（WHERE 与表名都先过安全校验——表名是 AI 结构化参数，
    # 未校验直接拼 SQL 会让注入文本以"函数参数"形态穿透：实测路径）
    try:
        from core.contract.security_contract import SecurityContract
        SecurityContract.validate_identifier(t, "表名")
        if where:
            SecurityContract.validate_where(where)
        drv = _ops.get_driver()
        cands = drv.query(
            f"SELECT id FROM {t} {('WHERE ' + where) if where else ''} LIMIT {_CANDIDATE_CAP + 1}")
    except Exception as e:
        track(ToolResult.fail(
            f"候选查询失败: {str(e)[:120]}", table=t, action=a,
            error_kind=type(e).__name__))
        return None
    if expect_ids_map is not None:
        frozen = expect_ids_map.get(t)
        got_ids = sorted(c["id"] for c in cands if "id" in c)
        if frozen is None or got_ids != frozen:
            track(ToolResult.fail(
                f"候选与批准时漂移（{t}: 批准 {len(frozen or [])} 条 → "
                f"现 {len(got_ids)} 条）——批准语义可能落空，未执行；请重新发起操作",
                code="VALIDATION", reason="candidate_drift",
                table=t, action=a))
            return None
    if not cands:
        track(ToolResult.fail(
            f"未在 {t} 找到符合条件的记录（0 条），未执行任何操作",
            code="NOT_FOUND", reason="no_candidates", table=t, action=a))
        return None
    if len(cands) > _CANDIDATE_CAP:
        track(ToolResult.fail(
            f"候选记录超过 {_CANDIDATE_CAP} 条，范围过大，请缩小条件后重试",
            code="VALIDATION", reason="too_many_candidates",
            table=t, action=a, candidates=len(cands)))
        return None
    if a not in ("DELETE", "UPDATE"):
        track(ToolResult.fail(f"不支持的操作: {a}", code="VALIDATION",
                              reason="unsupported_action", table=t, action=a))
        return None
    # 收 pending：全量候选行（候选数 ≤ 安全帽，下方 LIMIT 覆盖全集），
    # 单表/多表分流在循环外统一决策
    rows = drv.query(
        f"SELECT * FROM {t} {('WHERE ' + where) if where else ''} LIMIT {_CANDIDATE_CAP}")
    return {
        "_raw_op": op,  # 冻结原料（审批登记用：结算重放不再跑 LLM）
        "table": t, "action": a,
        "set_clause": set_clause,
        "set_data": ",".join(
            f"{sf['field']}={sf.get('value','')}" for sf in op.get("set_fields", [])),
        "changed_fields": changed_fields,
        "expected_values": ({sf["field"]: sf.get("value")
                             for sf in op.get("set_fields", []) if sf.get("field")}
                            if a == "UPDATE" else None),
        "ids": [c["id"] for c in cands],
        "rows": rows,
        "sample": rows[:2],
    }


def _collect_pending_ops(ops: list, action: str, expect_ids: list | None,
                         track) -> list[dict]:
    """逐 op 校验并收拢候选：校验失败的 op 经 track 记入失败轨并继续
    下一 op；返回通过校验、候选数未超安全帽的 pending 列表
    （单表/多表分流在调用方统一决策）"""
    # 冻结 id 集索引（结算重放用）：[{table, ids}] → {table: sorted ids}
    # 同表多 op 合并收拢为并集比对（dict 按键覆盖曾把多 op 压成一条——
    # 重放误报漂移的可用性 wart）；expect_ids 缺省时为 None，跳过漂移比对
    expect_ids_map: dict | None = None
    if expect_ids is not None:
        expect_ids_map = {}
        for _e in expect_ids:
            _t, _ids = _e.get("table", ""), sorted(_e.get("ids") or [])
            expect_ids_map[_t] = sorted((expect_ids_map.get(_t) or []) + _ids)
    pending: list[dict] = []
    for op in ops:
        p = _probe_single_op(op, action, expect_ids_map, track)
        if p is not None:
            pending.append(p)
    return pending


def _run_multi_ops(pending: list[dict], instruction: str, track) -> None:
    """多表分流：拓扑排序（子表先删）→ _multi_ops_confirmed 合并一张确认卡 →
    批量预批准 → 逐表走树路由+execute_tool（人审闸免检因批量预批准，
    契约层/权限层正常生效）。与单表路径统一：不再直调 delete_rows/update_rows。

    MCP 通道桥：挂起表回执（待批准）——token 不回传 AI 通道。
    """
    ordered = _topo_sort_deletes(pending)
    try:
        _confirmed = _multi_ops_confirmed(ordered, instruction)
    except Exception as _pa:
        from core.exceptions import PendingApproval
        if isinstance(_pa, PendingApproval):
            # token 进 data（机器通道，__str__ 只暴露 text——AI 可见通道
            # 永远无 token）；data 仅由 mcp_server 同步等待桥消费
            track(ToolResult.fail(_pa.message, code="CONTRACT",
                                  reason="pending_approval",
                                  approval_token=_pa.token))
            _confirmed = None
        else:
            raise
    if _confirmed is False:
        track(ToolResult.fail(
            "⛔ 批量操作未执行：需用户在前端确认卡片中批准（或已被拒绝/当前环境不支持确认）",
            code="VALIDATION", reason="nuke_rejected"))
    elif _confirmed:
        from core.context import get_context
        ctx = get_context()
        ctx.set_nuke_batch(
            tables={p["table"] for p in ordered},
            ops={"delete_data", "edit_data"},
        )
        try:
            for p in ordered:
                track(_execute_pending_op(p, instruction))
        finally:
            ctx.clear_nuke_batch()


def _summarize_mutation(parts: list[str], fails: list) -> "ToolResult":
    """汇总多 op 结果：全部失败按首败归并（单 op 场景=原样透传）；
    任一成功即整体 ok=True，已执行 op 的明细入 effects_list"""
    if not parts:
        return ToolResult.fail(
            "未能从指令中解析出可执行的修改/删除操作，请明确表名、条件和要改的内容",
            code="VALIDATION", reason="parse_failed")
    text = "\n".join(parts)
    if fails and all(not f_.data.get("ok") for f_ in fails):
        # 全部失败：沿用首个失败的 code/reason（单 op 场景=原样透传）
        first = fails[0]
        if len(fails) == 1:
            return ToolResult(text, dict(first.data))
        return ToolResult(text, {"ok": False, "code": first.data.get("code"),
                                 "reason": first.data.get("reason", "")})
    # 混合（部分成功/部分失败）：整体 ok=True，已执行 op 的明细在 effects_list
    data = {"ok": True, "code": "OK"}
    done = [f_.data.get("effects") for f_ in fails if f_.data.get("effects")]
    if done:
        data["effects_list"] = done
    return ToolResult(text, data)


def mutate_natural(instruction: str, action: str = "",
                   ops_override: list | None = None,
                   expect_ids: list | None = None) -> "ToolResult":
    """统一改/删执行语义：条件 → 候选集 → 分流

    审批冻结重放（TOCTOU）：ops_override 给出登记时刻冻结的
    结构化 ops（结算不再重跑 LLM 提取——同一句两次提取可以不一致）；
    expect_ids 给出批准时各表候选 id 集（[{table, ids}]），重放重算 id 集
    比对——计数/等数替换/0 条/超帽/同表多 op 全部归并为漂移即整组拒。

    所有自然语言改/删共用一套语义，按通过校验的 op 数分流到人审闸：
    - 候选 0 条：如实报未找到
    - 候选超过安全帽（_CANDIDATE_CAP 条）：要求缩小范围
    - 单表（1 个 op）：走 execute_tool 人审闸（树路由删/改→delete_data/
      edit_data），批准才执行
    - 多表（≥2 个 op）：拓扑排序（子表先删）→ _multi_ops_confirmed
      合并确认卡，批准后逐表走 execute_tool；MCP 通道登记单项挂起
      （PendingApproval），管理端批准后冻结重放原指令结算

    双轨：写操作成功时 data.effects 携带
    {table, action, affected, affected_ids, changed_fields}
    （UPDATE 另带 expected_values），供目标达成检测复查。
    """
    action = (action or "").upper()
    parts: list[str] = []       # 用户文本（多 op 按序拼接，与历史文案一致）
    fails: list[ToolResult] = []  # 各 op 的结构化判定（多 op 取最严）

    def _track(tr: ToolResult):
        fails.append(tr)
        parts.append(str(tr))

    _ops_list = ops_override if ops_override is not None else _ops._extract_mutation_ops(instruction)
    pending = _collect_pending_ops(_ops_list, action, expect_ids, _track)
    # 重放冻结语义的硬边界（TOCTOU）：expect_ids 在场时，
    # 任一 op 漂移即整组拒绝——不得出现"漂移表拦住、其余表照删"的半执行
    #（批准的是整组影响面，漂移一行=批准语义落空）
    if expect_ids is not None:
        drifted = [f for f in fails if f.data.get("reason") == "candidate_drift"]
        if drifted:
            first = drifted[0]
            return ToolResult(str(first), {"ok": False, "code": "VALIDATION",
                                           "reason": "candidate_drift",
                                           "table": first.data.get("table", "")})
    if len(pending) == 1:
        # 单表：走 execute_tool 人审闸——建选择集 → 树路由（删/改+记录→
        # delete_data/edit_data）→ execute_tool → _nuke_confirmed 弹卡片
        #（影响面+记录数+外键+反向引用）→ 批准才执行
        _track(_execute_pending_op(pending[0], instruction))
    elif len(pending) >= 2:
        _run_multi_ops(pending, instruction, _track)
    return _summarize_mutation(parts, fails)
