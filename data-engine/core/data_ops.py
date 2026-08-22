"""数据操作模块——原子化的 UPDATE 和 DELETE，AI 写条件，代码执行

还提供多表 JOIN 查询和聚合统计查询能力。

联邦数据库支持：_get_driver() 返回 FederatedDriver，
自动根据表名路由到对应数据源（单数据源时行为不变）。
"""

from core.logger import get_logger
import re as _re

# ── 编排回调注入（依赖倒置，评审四轮 P1：消灭 core→agent 反向 import 边）──
# mutate_natural 的工具路由（删→delete_data/改→edit_data）属于编排层职责；
# 引擎层只面向回调接口，由编排层（agent 包初始化）注册决策树路由。
_tree_router = None


def register_tree_router(fn):
    """编排层注册决策树路由：fn(behavior, category, constraint) -> tool_name"""
    global _tree_router
    _tree_router = fn


def _route_tool(behavior: str, category: str, constraint: str = "") -> str:
    if _tree_router is None:
        # 编排层未注册（mutate 只应经 agent/tools 流程到达，注册在 agent/__init__ 完成）——
        # 如实失败，不静默猜工具
        raise RuntimeError("决策树路由未注册（编排层未初始化）")
    return _tree_router(behavior, category, constraint)

from core.contract.security_contract import (
    safe_table_sql, safe_column_sql, SecurityContract,
    is_valid_identifier, _IDENTIFIER_RE,
)
from core.exceptions import SecurityError

logger = get_logger(__name__)


# 联邦驱动单例（懒加载）
_federated_driver = None


def _get_driver():
    """获取数据库驱动

    联邦数据库模式：返回 FederatedDriver，自动路由到表所属数据源
    单数据源模式：FederatedDriver 透明转发到默认 Driver，行为一致
    """
    global _federated_driver
    if _federated_driver is None:
        from core.drivers.federated_driver import FederatedDriver
        _federated_driver = FederatedDriver()
    return _federated_driver


# ── JOIN / 聚合查询辅助 ──

# 合法标识符正则统一从 core.contract.security_contract 导入（严格语义：
# 字母/下划线开头，只含字母数字下划线，长度 ≤ 64），本模块不再本地定义


def _validate_identifier(name: str) -> str:
    """校验标识符（表名/字段名）合法性，防止 SQL 注入"""
    if not name:
        raise ValueError("标识符不能为空")
    if not is_valid_identifier(name):
        raise ValueError(f"非法标识符: {name}")
    return name


def _load_table_schema(table: str) -> dict:
    """从 YAML 配置加载单张表的 schema（含外键关系）
    ——薄委托 schema_matcher.load_table_schema（P2-5 加载收敛）"""
    from core.schema_matcher import load_table_schema
    return load_table_schema(table) or {}


def _find_fk_relation(table_a: str, table_b: str) -> tuple[str, str, str, str] | None:
    """查找两张表之间的外键关系

    Returns:
        (from_table, from_col, to_table, to_col) 或 None
    """
    # table_a 有 FK 指向 table_b
    schema_a = _load_table_schema(table_a)
    for fk in schema_a.get("foreign_keys", []):
        if fk.get("references", "").lower() == table_b.lower():
            cols = fk.get("columns", [])
            ref_cols = fk.get("ref_columns", ["id"])
            if cols and ref_cols:
                return (table_a, cols[0], table_b, ref_cols[0])
    # table_b 有 FK 指向 table_a
    schema_b = _load_table_schema(table_b)
    for fk in schema_b.get("foreign_keys", []):
        if fk.get("references", "").lower() == table_a.lower():
            cols = fk.get("columns", [])
            ref_cols = fk.get("ref_columns", ["id"])
            if cols and ref_cols:
                return (table_b, cols[0], table_a, ref_cols[0])
    return None


def _resolve_field(expr: str, table: str = "") -> str:
    """将 SQL 表达式中的别名映射为真实字段名（基于 fields.yml 配置，不做模糊猜测）

    联邦数据库：当指定 table 时，仅替换目标表中实际存在的字段别名，
    避免跨数据源表的字段名冲突（如 price_history.price ≠ unit_price）

    已合并 db_chat._resolve_fields 的独有行为：
    别名同时注册"去掉空格"的变体（如 "人工 费" → labor_cost），
    用户/AI 输入省略空格时也能命中。
    """
    # 字段字典走行业加载器单源（industries.base 的 field_dict，ConfigHub 目录签名
    # 新鲜度）——此前本函数与 _extract_mutation_ops 各手搓一份 yaml 加载，
    # 同款两份必漂移（评审四轮 P2，与权限层的教训同型）
    from industries.base import get_current_industry
    fd = get_current_industry().field_dict or {}
    if not fd:
        return expr
    aliases = {}
    for fname, finfo in fd.items():
        aliases[fname] = fname
        for alias in finfo.get("alias", []):
            aliases[alias] = fname
            # 也支持去掉空格的匹配（合并自 db_chat._resolve_fields）
            aliases[alias.replace(" ", "")] = fname
    # 联邦数据库：指定 table 时，仅替换目标表中存在的字段
    table_cols = set()
    if table:
        try:
            drv = _get_driver()
            table_cols = {c["name"].lower() for c in drv.get_columns(table)}
        except Exception:
            pass
    # 精确别名替换（配置驱动，不猜测）
    for nick, real in sorted(aliases.items(), key=lambda x: -len(x[0])):
        if nick != real:
            # 指定了 table 时，仅当真实字段名存在于该表才替换
            if table_cols and real.lower() not in table_cols:
                continue
            expr = _re.sub(r'\b' + _re.escape(nick) + r'\b', real, expr)
    return expr


def update_rows(table: str, set_clause: str, where: str = "") -> "ToolResult":
    """安全执行 UPDATE（自动纠正字段名）。双轨：text 文案不变，data 带 affected/table"""
    from core.tool_result import ToolResult
    drv = _get_driver()
    if not drv.table_exists(table):
        return ToolResult.fail(f"表 {table} 不存在", code="NOT_FOUND",
                               reason="table_not_found", table=table)
    set_clause = _resolve_field(set_clause, table)
    where = _resolve_field(where, table) if where else where
    r = drv.update(table, set_clause, where)
    if not r["ok"]:
        return ToolResult.fail(r["message"], table=table)
    drv.commit()
    return ToolResult.ok(f"已更新 {table} 中 {r['count']} 条记录",
                         table=table, action="UPDATE", affected=r["count"])


def delete_rows(table: str, where: str = "") -> "ToolResult":
    """安全执行 DELETE（必须带 WHERE）。双轨：text 文案不变，data 带 affected/table"""
    from core.tool_result import ToolResult
    drv = _get_driver()
    if not drv.table_exists(table):
        return ToolResult.fail(f"表 {table} 不存在", code="NOT_FOUND",
                               reason="table_not_found", table=table)
    r = drv.delete(table, where)
    if not r["ok"]:
        return ToolResult.fail(r["message"], table=table)
    drv.commit()
    return ToolResult.ok(f"已从 {table} 删除 {r['count']} 条记录",
                         table=table, action="DELETE", affected=r["count"])


def _extract_mutation_ops(instruction: str):
    """自然语言 → 结构化改/删操作（AI 只提取结构，不碰 SQL）

    返回 (tables_desc, operations)，供 parse_instruction / mutate_natural 共用。
    """
    from core.ai_runtime.ai_client import AIClient

    drv = _get_driver()
    table_lines = []
    for tname in drv.list_tables():
        cols = [c["name"] for c in drv.get_columns(tname)]
        table_lines.append(f"{tname}: {', '.join(cols)}")
    tables_desc = "\n".join(table_lines)

    ai = AIClient.get_instance()

    # 构建可用字段名提示（字段字典走行业加载器单源，与 _resolve_field 同一货源）
    from industries.base import get_current_industry
    fd = get_current_industry().field_dict or {}
    field_aliases = {}
    for fname, finfo in fd.items():
        field_aliases[fname] = fname
        for alias in (finfo or {}).get("alias", []):
            field_aliases[alias] = fname
    fields_hint = ", ".join(sorted(field_aliases.keys())[:20])

    functions = [{
        "type": "function",
        "function": {
            "name": "edit_data",
            "description": "修改或删除数据",
            "parameters": {
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "table": {"type": "string", "description": "表名"},
                                "action": {"type": "string", "enum": ["UPDATE", "DELETE"]},
                                "set_fields": {
                                    "type": "array",
                                    "description": f"UPDATE 时设置的值。支持字段: {fields_hint}",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "field": {"type": "string", "description": "字段名"},
                                            "value": {"type": "string", "description": "新值"},
                                        },
                                        "required": ["field", "value"],
                                    },
                                },
                                "where_conditions": {
                                    "type": "array",
                                    "description": f"WHERE 条件（多个条件用 link 连接）。多个值用 OR 连接，不要用 IN。支持字段: {fields_hint}",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "field": {"type": "string", "description": "字段名"},
                                            "op": {"type": "string", "enum": ["=", "!=", "<>", ">", "<", ">=", "<=", "LIKE", "NOT LIKE", "IN", "NOT IN", "BETWEEN", "NOT BETWEEN", "IS NULL", "IS NOT NULL"], "description": "运算符"},
                                            "value": {"type": "string", "description": "值"},
                                            "link": {"type": "string", "enum": ["AND", "OR"], "description": "与上个条件的连接符"},
                                        },
                                        "required": ["field", "op", "value"],
                                    },
                                },
                            },
                        },
                    }
                },
                "required": ["operations"],
            },
        },
    }]
    from core.llm_usage import set_role as _usage_role
    with _usage_role("extract_param"):
        fn_name, fn_args = ai.call_function(functions, instruction, system_prompt=f"你是一个数据操作助手。当前表结构：\n{tables_desc}")
    return fn_args.get("operations", [])


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
            schema = _load_table_schema(other)
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
    """单表改/删影响面描述——单表核武卡（tool_registry）与多表合并卡
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
        fks = _load_table_schema(table).get("foreign_keys", [])
        fwd = [f"  {', '.join(fk.get('columns', []))} → {fk.get('references', '?')}.id"
               for fk in fks]
    except Exception:
        fwd = []
    # 反向引用计数：ids 经 ids_in_clause 类型感知拼装（文本主键的脏 id 不成注入文本；
    # 此前 str(int()) 过滤会静默丢弃文本主键——评审四轮收口唯一实现）
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
        for fk in _load_table_schema(t).get("foreign_keys", []):
            ref = fk.get("references", "")
            if ref in edges and ref != t and ref not in edges[t]:
                edges[t].add(ref)
                indeg[ref] += 1
    from collections import deque
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


def _multi_ops_confirmed(pending: list[dict], instruction: str) -> bool:
    """多表改/删合并确认闸：一张卡片展示全部表的影响面，一次确认全执行/全拒绝

    前端 agent-inbox 当前只渲染 action_requests[0]，故合并为单个 action_request：
    - args 放摘要（明细+执行顺序）与 __fold__ 折叠的各表结构（前端折叠渲染）
    - description 放每表完整影响面（含反向引用计数）

    与 _nuke_confirmed 同语义：interrupt 不可用（非 graph 上下文）→ 拒绝执行
    （安全默认）；GraphInterrupt 是正常挂起信号，必须放行给 LangGraph runtime。
    """
    try:
        from langgraph.types import interrupt
    except ImportError:
        logger.warning("多表确认闸：langgraph 不可用，拒绝执行")
        return False
    drv = _get_driver()
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


def mutate_natural(instruction: str, action: str = "") -> "ToolResult":
    """统一改/删执行语义（方案C）：条件 → 候选集 → 分流

    所有自然语言改/删共用一套语义，不再有"选择集缺失"的失败态：
    - 候选 0 条：如实报未找到
    - 候选 1 条（唯一条件场景）：直接执行
    - 候选 >1 条：形成选择集挂起，请用户确认后执行（批量安全的人审闸）
    - 候选 >100 条：要求缩小范围（安全帽）

    双轨（迭代 4.4）：写操作 data.effects 携带
    {table, action, affected, affected_ids, changed_fields}；
    挂起态 effects.pending=True + candidate_ids，供确认执行后目标达成检测复查。
    """
    from core.tool_result import ToolResult
    action = (action or "").upper()
    parts: list[str] = []       # 用户文本（多 op 按序拼接，与历史文案一致）
    effects_list: list[dict] = []
    fails: list[ToolResult] = []  # 各 op 的结构化判定（多 op 取最严）

    def _track(tr: ToolResult):
        fails.append(tr)
        parts.append(str(tr))

    pending: list[dict] = []  # 通过校验、候选 1..100 条的 op（统一收拢后分流）
    for op in _extract_mutation_ops(instruction):
        t = op.get("table", "")
        a = op.get("action", "").upper()
        if action and a != action:
            continue
        set_clause, where = _build_set_where(op)
        changed_fields = [sf["field"] for sf in op.get("set_fields", []) if sf.get("field")]
        if not t:
            _track(ToolResult.fail(
                "未能确定要操作的表，请明确表名", code="VALIDATION",
                reason="table_unclear"))
            continue
        # 确定性查候选（WHERE 与表名都先过安全校验——表名是 AI 结构化参数，
        # 未校验直接拼 SQL 会让注入文本以"函数参数"形态穿透：评审实测路径）
        try:
            from core.contract.security_contract import SecurityContract
            SecurityContract.validate_identifier(t, "表名")
            if where:
                SecurityContract.validate_where(where)
            drv = _get_driver()
            cands = drv.query(
                f"SELECT id FROM {t} {('WHERE ' + where) if where else ''} LIMIT 101")
        except Exception as e:
            _track(ToolResult.fail(
                f"候选查询失败: {str(e)[:120]}", table=t, action=a,
                error_kind=type(e).__name__))
            continue
        if not cands:
            _track(ToolResult.fail(
                f"未在 {t} 找到符合条件的记录（0 条），未执行任何操作",
                code="NOT_FOUND", reason="no_candidates", table=t, action=a))
            continue
        if len(cands) > 100:
            _track(ToolResult.fail(
                "候选记录超过 100 条，范围过大，请缩小条件后重试",
                code="VALIDATION", reason="too_many_candidates",
                table=t, action=a, candidates=len(cands)))
            continue
        if a not in ("DELETE", "UPDATE"):
            _track(ToolResult.fail(f"不支持的操作: {a}", code="VALIDATION",
                                   reason="unsupported_action", table=t, action=a))
            continue
        # 收 pending：全量候选行（≤100 安全帽保证 LIMIT 100 覆盖全集），
        # 单表/多表分流在循环外统一决策
        rows = drv.query(
            f"SELECT * FROM {t} {('WHERE ' + where) if where else ''} LIMIT 100")
        pending.append({
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
        })

    from core.context import get_context
    if len(pending) == 1:
        # 单表：走 execute_tool 核武闸（方案D 20260804；树路由统一 20260805）——
        # 建选择集 → 树路由（删/改+记录→delete_data/edit_data）→ execute_tool →
        # _nuke_confirmed 弹卡片（影响面+记录数+外键+反向引用）→ 批准才执行
        p = pending[0]
        sid = get_context().save_selection(p["table"], p["rows"], query=instruction)
        from core.tool_registry import execute_tool
        behavior = "删" if p["action"] == "DELETE" else "改"
        tool_name = _route_tool(behavior, "记录", "")
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
        _track(r)
    elif len(pending) >= 2:
        # 多表（方案E 20260805；树路由统一 20260805）：拓扑排序（子表先删）→
        # 合并一张确认卡 → 批量预批准 → 逐表走树路由+execute_tool
        # （核武闸免检因批量预批准，契约层/权限层正常生效）。
        # 与单表路径统一：不再直调 delete_rows/update_rows。
        ordered = _topo_sort_deletes(pending)
        if not _multi_ops_confirmed(ordered, instruction):
            _track(ToolResult.fail(
                "⛔ 批量操作未执行：需用户在前端确认卡片中批准（或已被拒绝/当前环境不支持确认）",
                code="VALIDATION", reason="nuke_rejected"))
        else:
            from core.tool_registry import execute_tool
            ctx = get_context()
            ctx.set_nuke_batch(
                tables={p["table"] for p in ordered},
                ops={"delete_data", "edit_data"},
            )
            try:
                for p in ordered:
                    # 留选择集痕（与单表路径一致，供审计/复用）
                    sid = ctx.save_selection(p["table"], p["rows"], query=instruction)
                    behavior = "删" if p["action"] == "DELETE" else "改"
                    tool_name = _route_tool(behavior, "记录", "")
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
                    _track(r)
            finally:
                ctx.clear_nuke_batch()
    # 汇总：任一 op 挂起 → 整体 pending；否则按各 op ok 归并
    if not parts:
        return ToolResult.fail(
            "未能从指令中解析出可执行的修改/删除操作，请明确表名、条件和要改的内容",
            code="VALIDATION", reason="parse_failed")
    text = "\n".join(parts)
    if effects_list and not fails:
        # 纯挂起：操作未执行但非失败——ok=True + pending 子码
        data = {"ok": True, "code": "OK", "reason": "pending_confirm"}
        if len(effects_list) == 1:
            data["effects"] = effects_list[0]
        else:
            data["effects_list"] = effects_list
        return ToolResult(text, data)
    if fails and all(not f_.data.get("ok") for f_ in fails) and not effects_list:
        # 全部失败：沿用首个失败的 code/reason（单 op 场景=原样透传）
        first = fails[0]
        if len(fails) == 1:
            return ToolResult(text, dict(first.data))
        return ToolResult(text, {"ok": False, "code": first.data.get("code"),
                                 "reason": first.data.get("reason", "")})
    # 混合（部分成功/部分挂起）：整体 ok=True，明细在 effects_list
    data = {"ok": True, "code": "OK"}
    if effects_list:
        data["effects_list"] = effects_list
    done = [f_.data.get("effects") for f_ in fails if f_.data.get("effects")]
    if done:
        data.setdefault("effects_list", [])
        data["effects_list"] = done + data["effects_list"]
    return ToolResult(text, data)


def parse_instruction(instruction: str, restrict_action: str = "") -> str:
    """自然语言 → UPDATE / DELETE（兼容旧路径：同步返回字符串）

    内部统一委托 mutate_natural（走树路由+核武闸），仅包装为同步字符串返回。
    新代码请直接用 mutate_natural（统一候选集语义+双轨契约）。
    """
    r = mutate_natural(instruction, action=restrict_action)
    return str(r)


def insert_row(table: str, data_json: str) -> "ToolResult":
    """插入一行数据到指定表。data_json 是 JSON 格式的字段值字典。

    双轨：text 文案不变；data 带 ok/table/effects（values=插入行，供目标达成检测复查）。
    """
    import json
    from core.tool_result import ToolResult
    try:
        row = json.loads(data_json)
    except Exception:
        return ToolResult.fail("data 格式错误，请使用 JSON 格式",
                               code="VALIDATION", reason="data_format")
    if not isinstance(row, dict):
        return ToolResult.fail("data 须为 JSON 对象", code="VALIDATION",
                               reason="data_format")
    from core.schema_manager import _guard_sys_column as _gsc
    if any(_gsc(k, "手动插入值") for k in row.keys()):
        return ToolResult.fail(
            "id 是系统主键，由系统自动生成，不允许手动指定。请去掉 id 字段后重新插入",
            code="VALIDATION", reason="primary_key", table=table)
    # 数据校验（CHECK 约束）
    from core.schema_manager import _load_config
    cfg = _load_config()
    tbl_cfg = next((t for t in cfg.get("tables", []) if t["name"].lower() == table.lower()), None)
    if tbl_cfg:
        from core.validator import validate_row
        err = validate_row(tbl_cfg, row)
        if err:
            return ToolResult.fail(err, code="VALIDATION", reason="check_constraint",
                                   table=table)
    drv = _get_driver()
    r = drv.insert(table, [row])
    drv.commit()
    if r.get("ok"):
        cols = "; ".join(f"{k}={v}" for k, v in row.items())
        return ToolResult.ok(
            f"已插入{table}数据：{cols}", table=table, action="INSERT", affected=1,
            effects={"table": table, "action": "INSERT", "affected": 1,
                     "values": [row]})
    return ToolResult.fail(f"插入失败: {r.get('message','')}", table=table,
                           action="INSERT")


def insert_rows(table: str, rows: list, overwrite: bool = False, auto_commit: bool = True) -> dict:
    """批量插入数据到指定表（带校验，供 pipeline 调用）

    与 insert_row 的区别：
    - 接收 list[dict] 而非 JSON 字符串
    - 返回结构化 dict 而非字符串（pipeline 需要 ok/conflict 判断）
    - 复用同样的校验逻辑：系统字段保护 + CHECK 约束
    - auto_commit=False 时不在内部 commit，供 pipeline 事务控制

    Returns:
        {"ok": bool, "conflict": bool, "message": str, "count": int}
    """
    if not rows:
        return {"ok": True, "conflict": False, "message": "无数据", "count": 0}

    from core.schema_manager import _guard_sys_column as _gsc, _load_config

    # 系统字段保护（id 由系统自动生成）
    for row in rows:
        if any(_gsc(k, "批量插入") for k in row.keys()):
            return {"ok": False, "conflict": False,
                    "message": "id 是系统主键，不允许手动指定", "count": 0}

    # 数据校验（CHECK 约束）
    cfg = _load_config()
    tbl_cfg = next((t for t in cfg.get("tables", [])
                    if t["name"].lower() == table.lower()), None)
    if tbl_cfg:
        from core.validator import validate_row
        for i, row in enumerate(rows):
            err = validate_row(tbl_cfg, row)
            if err:
                return {"ok": False, "conflict": False,
                        "message": f"第{i+1}行校验失败: {err}", "count": 0}

    drv = _get_driver()
    r = drv.insert(table, rows, overwrite=overwrite)
    if auto_commit:
        drv.commit()
    return {
        "ok": r.get("ok", False),
        "conflict": r.get("conflict", False),
        "message": r.get("message", ""),
        "count": r.get("count", len(rows)),
    }


# ═══════════════════════════════════════════════════════════════
# 多表 JOIN 查询 & 聚合统计查询
# ═══════════════════════════════════════════════════════════════

# 手动 ON 条件安全校验：table.column OP table.column（多个条件用 AND 连接）
_ON_CONDITION_RE = _re.compile(
    r'^[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?'
    r'\s*(?:=|!=|<>|>=|<=|>|<)\s*'
    r'[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?'
    r'(?:\s+AND\s+[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?'
    r'\s*(?:=|!=|<>|>=|<=|>|<)\s*'
    r'[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?)*$',
    _re.IGNORECASE,
)


def join_query(main_table: str, join_tables: str = "", select_fields: str = "*",
               where: str = "", join_type: str = "LEFT", on_condition: str = "") -> "ToolResult":
    """多表联合查询——通过 schema 外键配置自动推断 ON 条件，或手动指定 ON 条件

    Args:
        main_table: 主表名
        join_tables: 要关联的表名，逗号分隔（如 "region,specialty"）
        select_fields: 查询字段（如 "t1.code, t2.name"），默认 "*"
        where: WHERE 条件（支持 table.column 格式，如 "region.id=1"）
        join_type: JOIN 类型，INNER / LEFT / RIGHT，默认 LEFT
            （明细联查以主表为准——明细表为空的行保留主表、明细列补 NULL；
            INNER 会把空明细表的主表行整体吞掉，S6"主表对应明细"返空事故）
        on_condition: 手动 ON 条件（如 "a.id = b.a_id"，多个条件用 AND 连接）。
                      显式给出时优先使用，跳过外键自动推断；
                      多表关联时该条件对所有 JOIN 子句复用

    Returns:
        ToolResult：text 为格式化结果；data 带
        {rows, row_count, total_count, truncated, tables}，失败带 code/reason

    联邦数据库：跨数据源的 JOIN 自动走应用层编排（federated_join）
    """
    from core.tool_result import ToolResult
    # 防御性校验：主表名合法性（防 SQL 注入）
    SecurityContract.validate_identifier(main_table, "表名")

    # 联邦数据库：检测跨库 → 委托给跨库 JOIN 编排器
    join_list = [t.strip() for t in join_tables.split(",") if t.strip()] if join_tables else []
    if join_list:
        try:
            from core.federation.join_executor import federated_join
            fed_result = federated_join(main_table, join_tables, select_fields, where, join_type)
            if fed_result is not None:
                # 跨库 JOIN 已完成（编排器返回纯文本，结构未知——legacy 过渡态）
                return ToolResult.legacy(fed_result)
        except Exception as e:
            # 跨库编排失败：涉及多个数据源时，原生单连接 JOIN 注定失败或产出
            # 错误结果（同名表静默错连）——显式报错（P1-5）；单数据源才可回退
            from core.datasource_manager import DataSourceManager
            dsm = DataSourceManager()
            sources = {dsm.get_datasource_for_table(t) for t in [main_table] + join_list}
            if len(sources) > 1:
                return ToolResult.fail(
                    f"跨库 JOIN 编排失败（涉及 {len(sources)} 个数据源: "
                    f"{', '.join(sorted(sources))}），已中止: {e}",
                    code="UNKNOWN", reason="federated_join_failed",
                    table=main_table, tables=[main_table] + join_list)
            logger.warning(f"JOIN 编排失败，回退原生路径: {e}", exc_info=True)

    drv = _get_driver()

    # 校验主表
    if not drv.table_exists(main_table):
        return ToolResult.fail(f"表 {main_table} 不存在", code="NOT_FOUND",
                               reason="table_not_found", table=main_table)

    # 解析关联表
    join_list = [t.strip() for t in join_tables.split(",") if t.strip()] if join_tables else []
    for jt in join_list:
        if not drv.table_exists(jt):
            return ToolResult.fail(f"表 {jt} 不存在", code="NOT_FOUND",
                                   reason="table_not_found", table=jt)

    if not join_list:
        return ToolResult.fail("请指定至少一个要关联的表", code="VALIDATION",
                               reason="missing_params", table=main_table)

    # 校验 JOIN 类型（RIGHT JOIN 使用原生语法，SQLite 3.39+ / MySQL 均支持）
    join_type = join_type.upper().strip()
    if join_type not in ("INNER", "LEFT", "RIGHT"):
        return ToolResult.fail(f"不支持的 JOIN 类型: {join_type}，只支持 INNER、LEFT 和 RIGHT",
                               code="VALIDATION", reason="unsupported_join_type",
                               table=main_table)

    # 手动 ON 条件安全校验（显式给出时优先使用，跳过外键推断）
    on_condition = on_condition.strip() if on_condition else ""
    if on_condition and not _ON_CONDITION_RE.match(on_condition):
        return ToolResult.fail(
            "ON 条件格式不安全，请使用 table.column = table.column 格式"
            "（多个条件用 AND 连接，如 a.id = b.a_id）",
            code="CONTRACT", reason="unsafe_on_condition", table=main_table)

    # 构建 SQL FROM + JOIN 子句
    sql_parts = [f'FROM {safe_table_sql(main_table)}']

    for jt in join_list:
        if on_condition:
            on_clause = on_condition
        else:
            relation = _find_fk_relation(main_table, jt)
            if not relation:
                return ToolResult.fail(
                    f"未找到 {main_table} 和 {jt} 之间的外键关系，"
                    f"无法自动推断 JOIN 条件，可通过 on_condition 参数手动指定",
                    code="NOT_FOUND", reason="no_fk_relation",
                    table=main_table, tables=[main_table, jt])
            from_table, from_col, to_table, to_col = relation
            on_clause = f'{from_table}.{from_col} = {to_table}.{to_col}'
        sql_parts.append(f'{join_type} JOIN {safe_table_sql(jt)} ON {on_clause}')

    # WHERE 去重：LLM 常把 FK 等值条件（id = fk）同时写进 WHERE——
    # 与 ON 重复的子条件必须剔除：LEFT JOIN 下它会强制明细表必须有行，
    # 把 LEFT 退化成 INNER（"主表对应明细"返空事故）
    if where and not on_condition:
        import re as _re2
        fk_pairs = set()
        for jt in join_list:
            rel = _find_fk_relation(main_table, jt)
            if rel:
                ft, fc, tt, tc = rel
                fk_pairs.add(f"{ft}.{fc} = {tt}.{tc}")
                fk_pairs.add(f"{tt}.{tc} = {ft}.{fc}")
        segs = [s.strip() for s in _re2.split(r'\s+AND\s+', where, flags=_re2.I) if s.strip()]
        kept = [s for s in segs if s not in fk_pairs]
        where = " AND ".join(kept)

    # SELECT 字段安全校验：允许 table.column, table.*, column, *, 聚合(field), field AS alias
    if select_fields.strip() != "*":
        # table.* 原生支持（SQLite/MySQL 均可），但表必须在本查询的 FROM/JOIN 中——
        # 不再静默改写为 *（会把 a.*, b.id 变成全表 *，列错位，P1-5）
        tables_in_query = {main_table, *join_list}
        for part in [p.strip() for p in select_fields.split(",") if p.strip()]:
            if part.endswith(".*"):
                tname = part[:-2]
                if not _re.match(r'^[a-zA-Z_]\w*$', tname) or tname not in tables_in_query:
                    return ToolResult.fail(
                        f"SELECT 字段含未参与本查询的表通配: {select_fields}，"
                        f"table.* 中的表必须在 FROM/JOIN 中（本查询: {main_table}, {join_tables}）",
                        code="VALIDATION", reason="unsafe_select_fields", table=main_table)
                continue
            # 整段过全串锚定校验（标识符/聚合/AS 别名）——此前只验每段首词，
            # "id, username FROM users UNION SELECT ...--" 各段首词全合法即穿透（评审实测），
            # 改用与单表查询同款的 validate_select_fields（test_14 覆盖）
            if part != "*" and not validate_select_fields(part):
                return ToolResult.fail(
                    "查询字段格式不安全，请使用 table.column 格式（如 t1.code, t2.name）",
                    code="CONTRACT", reason="unsafe_select_fields", table=main_table)

    sql = f'SELECT {select_fields} ' + " ".join(sql_parts)

    # WHERE 安全校验（SecurityContract.validate_where 支持 table.column 格式）
    if where:
        try:
            SecurityContract.validate_where(where)
        except SecurityError:
            return ToolResult.fail("WHERE 条件不安全，请检查后重试",
                                   code="CONTRACT", reason="unsafe_where", table=main_table)
        sql += f" WHERE {where}"

    # 上下文窗口管理：限制返回行数，防止大结果集导致 LLM token 溢出
    JOIN_ROW_LIMIT = 100
    count_sql = f'SELECT COUNT(*) as c ' + " ".join(sql_parts)
    if where:
        count_sql += f" WHERE {where}"
    try:
        total_count = drv.query(count_sql)[0]["c"]
    except Exception as e:
        # P1-5：不再用 -1 哨兵伪装成功，COUNT 失败显式报错
        return ToolResult.fail(f"JOIN 查询总数统计失败: {e}", code="UNKNOWN",
                               reason="count_failed", table=main_table)
    sql += f" LIMIT {JOIN_ROW_LIMIT}"

    # 执行查询
    try:
        rows = drv.query(sql)
    except Exception as e:
        return ToolResult.fail(f"查询失败: {_translate_query_error(e)}",
                               code="UNKNOWN", reason="query_failed", table=main_table)

    tables_meta = [main_table] + join_list
    if not rows:
        return ToolResult.ok("查询结果为空", table=main_table, tables=tables_meta,
                             rows=[], row_count=0, total_count=total_count)

    # 格式化结果
    from core.db_chat import DBChat
    chat = DBChat()
    result = chat._format_multi_table({"JOIN结果": rows})
    truncated = total_count > JOIN_ROW_LIMIT
    if truncated:
        result += (f"\n\n（共 {total_count} 条，已显示前 {JOIN_ROW_LIMIT} 条。"
                  "如需查看完整数据，请缩小查询范围或使用导出功能。）")
    return ToolResult.ok(result, table=main_table, tables=tables_meta, rows=rows,
                         row_count=len(rows), total_count=total_count,
                         truncated=truncated)


def aggregate_query(table: str, agg_func: str, agg_field: str = "*",
                    group_by: str = "", having: str = "", where: str = "") -> "ToolResult":
    """聚合统计查询——支持 COUNT/SUM/AVG/MIN/MAX + GROUP BY + HAVING

    也可执行 DISTINCT 去重查询：当 agg_func=DISTINCT 时，返回去重字段值列表
    （SELECT DISTINCT field FROM table），适用于"查询所有不同的XX"场景。

    Args:
        table: 表名
        agg_func: 聚合函数（COUNT/SUM/AVG/MIN/MAX/DISTINCT）
                  DISTINCT 时返回去重字段值列表，而非单一聚合值
        agg_field: 聚合字段（COUNT 时用 *），默认 "*"
        group_by: 分组字段，逗号分隔（如 "region_id,specialty_id"）
        having: HAVING 条件（如 "COUNT(*) > 5"）
        where: WHERE 条件

    Returns:
        ToolResult：text 为格式化结果；data 带
        {rows, row_count, agg_func, agg_field, group_by}，失败带 code/reason
    """
    from core.tool_result import ToolResult
    # 防御性校验：表名合法性（防 SQL 注入）
    SecurityContract.validate_identifier(table, "表名")

    drv = _get_driver()
    if not drv.table_exists(table):
        return ToolResult.fail(f"表 {table} 不存在", code="NOT_FOUND",
                               reason="table_not_found", table=table)

    # 校验聚合函数
    agg_func = agg_func.upper().strip()
    valid_funcs = {"COUNT", "SUM", "AVG", "MIN", "MAX", "DISTINCT"}
    if agg_func not in valid_funcs:
        return ToolResult.fail(
            f"不支持的聚合函数: {agg_func}，只支持 {', '.join(sorted(valid_funcs))}",
            code="VALIDATION", reason="unsupported_agg_func", table=table)

    # DISTINCT 模式：返回去重字段值列表（SELECT DISTINCT field FROM table）
    # 适用于"查询所有不同的药品名称"等去重列表查询场景
    if agg_func == "DISTINCT":
        agg_field_clean = agg_field.strip()
        # 兼容 AI 传入 "DISTINCT drug_name" 或 "drug_name" 两种格式
        if agg_field_clean.upper().startswith("DISTINCT "):
            agg_field_clean = agg_field_clean[9:].strip()
        if agg_field_clean == "*" or not agg_field_clean:
            return ToolResult.fail("DISTINCT 模式必须指定具体字段名（不能用 *）",
                                   code="VALIDATION", reason="missing_params", table=table)
        if not _IDENTIFIER_RE.match(agg_field_clean):
            return ToolResult.fail(f"字段名不合法: {agg_field_clean}",
                                   code="CONTRACT", reason="unsafe_identifier", table=table)
        if not drv.column_exists(table, agg_field_clean):
            return ToolResult.fail(f"字段 {agg_field_clean} 在表 {table} 中不存在",
                                   code="NOT_FOUND", reason="column_not_found", table=table)
        sql = f'SELECT DISTINCT {safe_column_sql(agg_field_clean)} FROM {safe_table_sql(table)}'
        if where:
            try:
                SecurityContract.validate_where(where)
            except SecurityError:
                return ToolResult.fail("WHERE 条件不安全，请检查后重试",
                                       code="CONTRACT", reason="unsafe_where", table=table)
            sql += f" WHERE {where}"
        sql += f' ORDER BY {safe_column_sql(agg_field_clean)}'
        try:
            rows = drv.query(sql)
        except Exception as e:
            return ToolResult.fail(f"查询失败: {_translate_query_error(e)}",
                                   code="UNKNOWN", reason="query_failed", table=table)
        if not rows:
            return ToolResult.ok("查询结果为空", table=table, rows=[], row_count=0,
                                 agg_func=agg_func, agg_field=agg_field_clean)
        from core.db_chat import DBChat
        chat = DBChat()
        return ToolResult.ok(
            chat._format_multi_table({f"DISTINCT {agg_field_clean}": rows}),
            table=table, rows=rows, row_count=len(rows),
            agg_func=agg_func, agg_field=agg_field_clean)

    # 校验聚合字段（支持 DISTINCT 前缀，如 "DISTINCT drug_name"）
    distinct_prefix = ""
    agg_field_clean = agg_field.strip()
    if agg_field_clean.upper().startswith("DISTINCT "):
        distinct_prefix = "DISTINCT "
        agg_field_clean = agg_field_clean[9:].strip()

    if agg_field_clean != "*":
        if not _IDENTIFIER_RE.match(agg_field_clean):
            return ToolResult.fail(f"聚合字段名不合法: {agg_field}",
                                   code="CONTRACT", reason="unsafe_identifier", table=table)
        if not drv.column_exists(table, agg_field_clean):
            return ToolResult.fail(f"字段 {agg_field_clean} 在表 {table} 中不存在",
                                   code="NOT_FOUND", reason="column_not_found", table=table)

    # 构建 SELECT
    if agg_field_clean == "*":
        agg_expr = f"{agg_func}(*)"
    else:
        agg_expr = f'{agg_func}({distinct_prefix}{safe_column_sql(agg_field_clean)})'

    select_parts = []
    group_fields: list[str] = []
    if group_by:
        group_fields = [f.strip() for f in group_by.split(",") if f.strip()]
        for gf in group_fields:
            if not _IDENTIFIER_RE.match(gf):
                return ToolResult.fail(f"分组字段名不合法: {gf}",
                                       code="CONTRACT", reason="unsafe_identifier", table=table)
            if not drv.column_exists(table, gf):
                return ToolResult.fail(f"字段 {gf} 在表 {table} 中不存在",
                                       code="NOT_FOUND", reason="column_not_found", table=table)
        select_parts.append(", ".join(safe_column_sql(gf) for gf in group_fields))

    select_parts.append(f'{agg_expr} AS agg_result')
    sql = f'SELECT {", ".join(select_parts)} FROM {safe_table_sql(table)}'

    # WHERE
    if where:
        try:
            SecurityContract.validate_where(where)
        except SecurityError:
            return ToolResult.fail("WHERE 条件不安全，请检查后重试",
                                   code="CONTRACT", reason="unsafe_where", table=table)
        sql += f" WHERE {where}"

    # GROUP BY
    if group_fields:
        sql += f' GROUP BY {", ".join(safe_column_sql(gf) for gf in group_fields)}'

    # HAVING 安全校验：允许 聚合函数(字段) + 比较运算符 + 值
    if having:
        having_pattern = _re.compile(
            r'^(?:COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(?:\*|[a-zA-Z_]\w*)\s*\)'
            r'\s*(?:[=!<>]+|BETWEEN|IN|LIKE|NOT\s+IN|NOT\s+LIKE|IS\s+NOT\s+NULL|IS\s+NULL)'
            r'\s*[\w\s,.()\'"-]+$',
            _re.IGNORECASE
        )
        if not having_pattern.match(having.strip()):
            return ToolResult.fail(
                "HAVING 条件不安全，请使用聚合函数格式"
                "（如 COUNT(*) > 5 或 SUM(total_cost) > 1000）",
                code="CONTRACT", reason="unsafe_having", table=table)
        sql += f" HAVING {having}"

    # 执行查询
    try:
        rows = drv.query(sql)
    except Exception as e:
        return ToolResult.fail(f"查询失败: {_translate_query_error(e)}",
                               code="UNKNOWN", reason="query_failed", table=table)

    meta = {"table": table, "agg_func": agg_func, "agg_field": agg_field_clean,
            "group_by": group_fields}
    if not rows:
        return ToolResult.ok("查询结果为空", rows=[], row_count=0, **meta)

    # 格式化结果
    from core.db_chat import DBChat
    chat = DBChat()
    return ToolResult.ok(chat._format_multi_table({f"{agg_func}统计": rows}),
                         rows=rows, row_count=len(rows), **meta)


def _translate_query_error(e: Exception) -> str:
    """翻译常见 SQL 查询错误为中文提示（薄委托 core.contract.ErrorTranslator，P2-5：
    ambiguous 规则已并入 ErrorTranslator，本地无规则副本）"""
    from core.contract.error_translator import ErrorTranslator
    drv = _get_driver()
    # _get_driver() 恒为 FederatedDriver（模块级单例）：解包到默认驱动以推断
    # driver_type（FederatedDriver 本身无翻译规则；解包失败则沿用联邦驱动按类名推断）
    try:
        drv = drv._get_default_driver()
    except Exception:
        pass
    # 契约包装驱动（ContractDriver）自带 driver_type；否则按类名推断
    driver_type = getattr(drv, "driver_type", "") or ErrorTranslator.get_driver_type(drv)
    result = ErrorTranslator.translate(driver_type, e)
    # 兜底文案与旧实现保持一致（直接返回截断的原始错误，不加前缀）
    if result.message.startswith("数据库操作失败:"):
        return str(e)[:200]
    return result.message

# ── SELECT 子句拼装与校验（查询路径唯一拼装点，P2-5 自 db_chat 迁入）──
# 字段：标识符、table.column、聚合函数包裹（COUNT(*)/SUM(x) 等）、可选 AS 别名
_FIELD_ITEM_RE = _re.compile(
    r'^\s*(?:(?:COUNT|SUM|AVG|MIN|MAX|DISTINCT)\s*\(\s*(?:[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?|\*)\s*\)'
    r'|[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?)'
    r'(?:\s+AS\s+[a-zA-Z_]\w*)?\s*$',
    _re.IGNORECASE)
# 排序项：标识符 + 可选 ASC/DESC
_ORDER_ITEM_RE = _re.compile(
    r'^\s*[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?(?:\s+(?:ASC|DESC))?\s*$',
    _re.IGNORECASE)
# 分组项：纯标识符
_GROUP_ITEM_RE = _re.compile(r'^\s*[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?\s*$')


def validate_select_fields(fields: str) -> bool:
    """校验 SELECT 字段列表：*, 标识符, table.column, 聚合(field), field AS alias"""
    if not fields or not fields.strip():
        return False
    if fields.strip() == "*":
        return True
    return all(_FIELD_ITEM_RE.match(p) for p in fields.split(","))


def validate_order_by(order_by: str) -> bool:
    """校验 ORDER BY 子句：标识符列表，每项可选 ASC/DESC"""
    if not order_by or not order_by.strip():
        return False
    return all(_ORDER_ITEM_RE.match(p) for p in order_by.split(","))


def validate_group_by(group_by: str) -> bool:
    """校验 GROUP BY 子句：纯标识符列表"""
    if not group_by or not group_by.strip():
        return False
    return all(_GROUP_ITEM_RE.match(p) for p in group_by.split(","))


def build_select_sql(table: str, fields: str, where: str = "",
                     order_by: str = "", group_by: str = "",
                     limit: int = 0) -> str:
    """构建 SELECT SQL——查询路径唯一拼装点（P2-5 自 db_chat 迁入）。

    所有子句先做安全校验，任一子句不安全时抛 ValueError。
    """
    if not validate_select_fields(fields):
        raise ValueError(f"SELECT 字段不安全，已拒绝执行: '{fields[:80]}'")
    if where:
        # 归一化：剥离 AI 可能预加的 "WHERE" 前缀与前导空格
        # （db_chat.py L320 仅对 where_conditions 分支剥离，where 字符串
        # 直传路径会带 " WHERE ..." 进入校验导致误拒——query 归一化补丁 20260803）
        where = where.strip()
        if where.upper().startswith("WHERE "):
            where = where[6:]
        try:
            SecurityContract.validate_where(where)
        except Exception as e:
            raise ValueError(f"WHERE 条件不安全，已拒绝执行: {e}")
    sql = f'SELECT {fields} FROM {safe_table_sql(table)}'
    if where:
        sql += f" WHERE {where}"
    if order_by:
        if not validate_order_by(order_by):
            raise ValueError(f"ORDER BY 子句不安全，已拒绝执行: '{order_by[:80]}'")
        sql += f" ORDER BY {order_by}"
    if group_by:
        if not validate_group_by(group_by):
            raise ValueError(f"GROUP BY 子句不安全，已拒绝执行: '{group_by[:80]}'")
        sql += f" GROUP BY {group_by}"
    if limit and isinstance(limit, int) and limit > 0:
        sql += f" LIMIT {limit}"
    return sql

