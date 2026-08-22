"""体系B：Plan + OODA 深度研究

架构：Coordinator → Planner → Researcher(OODA) → Reporter
借鉴：Open Deep Research (LangChain 官方开源)

四类工具统一调度：
- Web 工具：web_search / web_fetch（网络搜索）
- DB 工具：_execute_single（合法路径 P1→树→P2）
- 文件工具：list_files / read_file / search_in_files（复用 parser）
- RAG 工具：search_documents（向量检索）

三种研究模式：
- web：纯网络搜索
- local：纯本地数据库+文件研究
- hybrid：网络+本地混合

人机协作：
- ask_user：信息不足时暂停，等用户回答后继续
- blocked：卡壳时告知处理方式，用户处理后继续
"""

import json
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config.settings import settings
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from agent.open_layer.state import AgentState


# ═══════════════════════════════════════════════════════════════
# 上下文采集
# ═══════════════════════════════════════════════════════════════

def _collect_context() -> dict:
    """采集数据库、文件、RAG 上下文（供 Coordinator 使用）

    元数据闭环关键：从 MetaDB 采集表/字段描述（business_name/description），
    让 AI 看到字段含义而非仅字段名。同时列出所有联邦数据源。

    数据源/表分布来自 MetaDB（含描述），表的真实存在性由实际 DB 校验。
    """
    ctx = {
        "db_tables": [],            # 兼容字段：表名列表（字符串）
        "db_columns": [],           # 兼容字段：字段名列表（字符串）
        "db_datasources": "",       # 新增：联邦数据源列表（字符串，给AI看）
        "db_schema_info": "",       # 新增：表+字段描述（结构化文本，给AI看）
        "rag_collections": [],
        "file_structure": "",
    }

    # ── 1. 联邦数据源列表 ──
    try:
        from core.datasource_manager import DataSourceManager
        dsm = DataSourceManager()
        dsm.load_config()
        ds_list = dsm.list_datasources()
        ds_lines = []
        for ds in ds_list:
            tag = " (默认)" if ds.get("is_default") else ""
            ds_lines.append(f"  - {ds['name']}{tag}: {ds.get('type', 'unknown')}")
        ctx["db_datasources"] = "\n".join(ds_lines) if ds_lines else "  (无)"
    except Exception:
        ctx["db_datasources"] = "  (未配置)"

    # ── 2. 表和字段元数据——优先从 MetaDB 读取（含描述），降级到 DB 直读 ──
    meta_tables = []
    meta_columns_by_table = {}
    try:
        from core.graph.meta_db import MetaDB
        meta = MetaDB.get_instance()
        meta_tables = meta.list_tables()  # [{name, business_name, description, datasource}, ...]
        # 批量获取所有字段（按表分组）
        all_cols = meta.get_all_columns()  # {table_name: [{name, type, description, ...}, ...]}
        for t_name, cols in all_cols.items():
            meta_columns_by_table[t_name] = cols
    except Exception:
        pass

    # ── 3. 校验表的真实存在性（联邦数据库：遍历所有数据源）──
    real_tables = set()
    try:
        from core.datasource_manager import DataSourceManager
        dsm = DataSourceManager()
        dsm.load_config()
        for ds_info in dsm.list_datasources():
            try:
                drv = dsm.get_driver(ds_info["name"])
                for t in drv.list_tables():
                    if not t.startswith("sqlite_") and not t.startswith("meta_"):
                        real_tables.add(t)
            except Exception:
                continue
    except Exception:
        # 降级：用默认驱动
        try:
            from core.data_ops import _get_driver
            drv = _get_driver()
            real_tables = {t for t in drv.list_tables()
                           if not t.startswith("sqlite_") and not t.startswith("meta_")}
        except Exception:
            pass

    # ── 4. 组装上下文 ──
    # 4a. 兼容字段：表名/字段名列表（保持向后兼容）
    ctx["db_tables"] = sorted(real_tables)[:30]

    # 4b. 新增：结构化 schema_info（表+字段描述，给AI看）
    # 优先用 MetaDB 的元数据（含描述），若无则用纯表名
    schema_lines = []
    tables_to_show = sorted(real_tables)[:15]  # 限制前15张表，防止上下文过长
    for t_name in tables_to_show:
        # 从 MetaDB 找元数据
        t_meta = next((m for m in meta_tables if m.get("name") == t_name), {})
        biz_name = t_meta.get("business_name", "")
        t_desc = t_meta.get("description", "")
        t_ds = t_meta.get("datasource", "primary")
        header = f"表: {t_name}"
        if biz_name:
            header += f" ({biz_name})"
        if t_desc:
            header += f" — {t_desc}"
        header += f" [数据源: {t_ds}]"
        schema_lines.append(header)

        # 字段列表
        cols = meta_columns_by_table.get(t_name, [])
        if not cols:
            # MetaDB 没有则降级到 DB 直读
            try:
                from core.datasource_manager import DataSourceManager
                dsm = DataSourceManager()
                dsm.load_config()
                drv = dsm.get_driver_for_table(t_name)
                cols = [{"name": c["name"], "type": c.get("type", "")}
                        for c in drv.get_columns(t_name)]
            except Exception:
                cols = []
        for c in cols[:20]:  # 每表限制前20字段
            c_name = c.get("name", "")
            c_type = c.get("column_type") or c.get("type", "")
            c_desc = c.get("description", "")
            c_flags = []
            if c.get("is_pk"):
                c_flags.append("PK")
            if c.get("not_null"):
                c_flags.append("NOT NULL")
            if c.get("is_unique"):
                c_flags.append("UNIQUE")
            if c.get("is_indexed"):
                c_flags.append("INDEXED")
            if c.get("check_constraint"):
                c_flags.append(f"CHECK({c['check_constraint']})")
            flag_str = f" [{', '.join(c_flags)}]" if c_flags else ""
            desc_str = f" — {c_desc}" if c_desc else ""
            schema_lines.append(f"    - {c_name} ({c_type}){flag_str}{desc_str}")

    ctx["db_schema_info"] = "\n".join(schema_lines) if schema_lines else "(无)"

    # 4c. 兼容字段：字段名集合（前40个，去重）
    all_col_names = set()
    for t_name in tables_to_show[:10]:
        cols = meta_columns_by_table.get(t_name, [])
        for c in cols:
            all_col_names.add(c.get("name", ""))
    ctx["db_columns"] = sorted(all_col_names)[:40]

    # ── 5. RAG 集合 ──
    try:
        from agent.open_layer.rag import list_document_collections
        ctx["rag_collections"] = list_document_collections()[:20]
    except Exception:
        pass

    # ── 6. 文件结构（顶层）──
    try:
        from agent.open_layer.file_tools import list_files
        ctx["file_structure"] = list_files(".", max_depth=2, max_items=50)
    except Exception:
        pass

    return ctx


# ═══════════════════════════════════════════════════════════════
# Coordinator（协调层）
# ═══════════════════════════════════════════════════════════════

_COORDINATOR_PROMPT = """你是深度研究协调员。分析用户的抽象指令，理解深层需求，判断研究模式，拆解为可执行目标。

用户指令：{user_input}

当前环境上下文：
- 联邦数据源：
{db_datasources}
- 数据库表（含描述和字段）：
{db_schema_info}
- 数据库表名列表：{db_tables}
- 数据库字段名（部分）：{db_columns}
- 已入库文档：{rag_collections}
- 文件结构：
{file_structure}

请分析并返回 JSON：
{{
    "understanding": "你对用户深层需求的理解（一句话概括用户真正想要什么）",
    "research_mode": "web | local | hybrid",
    "mode_reason": "为什么选择这个模式",
    "goals": [
        {{"id": 1, "goal": "具体可执行的目标1", "tool_type": "web|local|hybrid", "status": "pending"}},
        {{"id": 2, "goal": "具体可执行的目标2", "tool_type": "web|local|hybrid", "status": "pending"}}
    ]
}}

模式判断规则：
- web：指令涉及外部信息、最新动态、行业趋势、政策法规等（数据库中没有的信息）
- local：指令涉及本地数据分析、统计、趋势、异常检测等（数据库/文件中有信息）
- hybrid：指令需要对比本地数据与外部标准，或需要外部信息辅助本地分析

目标拆解原则：
- 每个目标必须是具体可执行的（"统计1月就诊数量" 而非 "分析就诊"）
- 目标之间可以有依赖关系（目标2依赖目标1的结果）
- 目标数量 2-{max_goals} 个
- tool_type 标注该目标主要用哪类工具

信息充足性判断：
- 如果指令信息不足无法拆解，返回 {{"understanding": "...", "research_mode": "...", "ask_user": "需要向用户确认的问题"}}
- 例如：用户说"对比A和B"但没说A和B是什么 → ask_user

只返回 JSON，不要其他文字。"""


def _coordinator(state: AgentState) -> dict:
    """Coordinator：理解需求 + 判断模式 + 拆解目标"""
    from agent.open_layer.graph import _get_llm

    # 获取用户最新指令
    user_input = _get_latest_user_input(state)

    # 采集上下文
    ctx = _collect_context()

    prompt = _COORDINATOR_PROMPT.format(
        user_input=user_input,
        db_tables=", ".join(ctx["db_tables"]) or "（无）",
        db_columns=", ".join(ctx["db_columns"]) or "（无）",
        db_datasources=ctx.get("db_datasources") or "  （无）",
        db_schema_info=ctx.get("db_schema_info") or "（无）",
        rag_collections=", ".join(ctx["rag_collections"]) or "（无）",
        file_structure=ctx["file_structure"] or "（无）",
        max_goals=settings.OODA_MAX_GOALS,
    )

    llm = _get_llm(role="research")
    from core.llm_usage import set_role as _usage_role
    with _usage_role("research"):
        response = llm.invoke([
            SystemMessage(content="你是深度研究协调员，擅长理解用户的深层需求并制定研究计划。只返回 JSON。"),
            HumanMessage(content=prompt),
        ])

    content = _extract_json(response.content)

    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        plan = {
            "understanding": f"用户指令：{user_input}",
            "research_mode": "local",
            "mode_reason": "JSON 解析失败，降级为 local 模式",
            "goals": [{"id": 1, "goal": user_input, "tool_type": "local", "status": "pending"}],
        }

    # 补全字段
    plan.setdefault("understanding", user_input)
    plan.setdefault("research_mode", "local")
    plan.setdefault("goals", [{"id": 1, "goal": user_input, "tool_type": "local", "status": "pending"}])

    # 限制目标数量
    plan["goals"] = plan["goals"][:settings.OODA_MAX_GOALS]

    return plan


# ═══════════════════════════════════════════════════════════════
# Researcher（研究层·OODA循环）
# ═══════════════════════════════════════════════════════════════

_RESEARCHER_PROMPT = """你是深度研究执行员，正在对单个目标执行 OODA 循环。

用户原始需求：{user_input}
你对需求的理解：{understanding}
研究模式：{research_mode}

当前目标（ID:{goal_id}）：{goal}
该目标使用的工具类型：{tool_type}

已完成目标的发现：
{completed_findings}

OODA 历史观察（本目标）：
{ooda_history}

可用工具：
1. web_search(query) - 网络搜索，返回标题+摘要+链接
2. web_fetch(url) - 抓取网页内容
3. db_query(instruction, behavior_key, db_category_key, structured_args) - 数据库查询（合法路径）
4. file_read(path) - 读单个文件（支持 PDF/Excel/Word/txt/csv/json/md/py/yaml）
5. file_list(path) - 列目录
6. file_read_directory(path) - 批量读取整个目录下所有文件（自动选择parser，每段带文件名前缀）
7. search_in_files(keyword, path) - 跨文件检索
8. rag_search(query, collection) - 向量检索已入库文档

数据库可用表：{db_tables}
数据库可用字段（部分）：{db_columns}
数据库schema详情（含表/字段描述，用于精确理解字段含义）：
{db_schema_info}

请决定下一步行动，返回 JSON：

1. 需要网络搜索 → {{"action": "web_search", "thought": "为什么需要搜索", "query": "搜索关键词"}}
2. 需要抓取网页 → {{"action": "web_fetch", "thought": "为什么需要抓取", "url": "网页URL"}}
3. 需要查数据库 → {{"action": "db_query", "thought": "为什么需要查", "instruction": "自然语言指令", "behavior_key": "查|增|改|删", "db_category_key": "记录|关联|统计|结构", "structured_args": {{...}}}}
4. 需要读单个文件 → {{"action": "file_read", "thought": "为什么需要读", "path": "文件路径"}}
5. 需要列目录 → {{"action": "file_list", "thought": "为什么需要列", "path": "目录路径"}}
6. 需要批量读取目录下所有文件 → {{"action": "file_read_directory", "thought": "为什么需要批量读", "path": "目录路径"}}
7. 需要检索文件 → {{"action": "search_in_files", "thought": "为什么需要检索", "keyword": "关键词", "path": "搜索目录"}}
8. 需要检索文档 → {{"action": "rag_search", "thought": "为什么需要检索", "query": "检索内容", "collection": "文档集合名（空则搜全部）"}}
9. 信息不足需问用户 → {{"action": "ask_user", "thought": "为什么需要问", "question": "向用户提问的内容"}}
10. 遇到问题需用户处理 → {{"action": "blocked", "thought": "为什么卡壳", "reason": "卡壳原因", "suggestion": "处理方式建议"}}
11. 目标已完成 → {{"action": "complete", "thought": "为什么完成了", "findings": "发现总结（基于 OODA 历史得出结论）"}}

约束：
- 查数据库只能通过 db_query（structured_args），不能直接写 SQL
- structured_args 中的表名/字段名必须从可用列表中选择
- 每次只决定一个 action
- 优先用已有信息（OODA 历史、已完成发现），避免重复查询
- 如果已有信息足够回答目标，选择 complete

只返回 JSON，不要其他文字。"""


def _ooda_for_goal(goal: dict, state: AgentState, agent) -> dict:
    """对单个目标执行 OODA 循环"""
    from agent.open_layer.graph import _get_llm

    user_input = _get_latest_user_input(state)
    plan = state.get("research_plan", {})
    ooda_history = state.get("ooda_history", [])

    # 该目标的历史观察
    goal_history = [h for h in ooda_history if h.get("goal_id") == goal["id"]]
    history_text = _format_ooda_history(goal_history)

    # 已完成目标的发现
    completed_findings = _format_completed_findings(plan)

    # 采集上下文（从 state 缓存读取，避免每个 goal 都重新采集数据库信息）
    # 性能优化：8 个 goal × 原本每次 list_tables + get_columns + list_collections = 24 次查询
    # （键名 research_context_cache 已在 AgentState 声明，原 _cached_context 黑钥匙已正名，P2-4）
    ctx = state.get("research_context_cache")
    if ctx is None:
        ctx = _collect_context()
        state["research_context_cache"] = ctx

    max_rounds = settings.OODA_MAX_ROUNDS_PER_GOAL

    for rnd in range(max_rounds):
        prompt = _RESEARCHER_PROMPT.format(
            user_input=user_input,
            understanding=plan.get("understanding", ""),
            research_mode=plan.get("research_mode", "local"),
            goal_id=goal["id"],
            goal=goal["goal"],
            tool_type=goal.get("tool_type", "local"),
            completed_findings=completed_findings,
            ooda_history=history_text if history_text else "（尚无历史观察）",
            db_tables=", ".join(ctx["db_tables"]) or "（无）",
            db_columns=", ".join(ctx["db_columns"]) or "（无）",
            db_schema_info=ctx.get("db_schema_info") or "（无）",
        )

        llm = _get_llm(role="research")
        from core.llm_usage import set_role as _usage_role
        with _usage_role("research"):
            response = llm.invoke([
                SystemMessage(content="你是深度研究执行员，擅长 OODA 循环（观察→分析→决策→执行）。只返回 JSON。"),
                HumanMessage(content=prompt),
            ])

        decision = _extract_json(response.content)
        try:
            decision = json.loads(decision)
        except json.JSONDecodeError:
            decision = {"action": "complete", "thought": "JSON 解析失败，结束本目标", "findings": "解析失败"}

        action = decision.get("action", "complete")

        # 控制 action
        if action == "ask_user":
            return {"status": "ask_user", "question": decision.get("question", "需要更多信息"),
                    "thought": decision.get("thought", "")}
        if action == "blocked":
            return {"status": "blocked", "reason": decision.get("reason", "未知原因"),
                    "suggestion": decision.get("suggestion", "请联系管理员"),
                    "thought": decision.get("thought", "")}
        if action == "complete":
            return {"status": "complete", "findings": decision.get("findings", "目标完成")}

        # 执行工具
        observation = _execute_action(decision, agent)

        # 记录历史（精简 decision，移除大字段 structured_args 防止内存累积）
        slim_action = {k: v for k, v in decision.items()
                       if k in ("action", "thought", "query", "url", "path", "keyword",
                                "instruction", "behavior_key", "db_category_key", "question",
                                "reason", "suggestion", "findings")}
        # structured_args 只保留表名等关键信息
        if "structured_args" in decision:
            sa = decision["structured_args"] or {}
            slim_sa = {k: v for k, v in sa.items()
                       if k in ("table", "main_table", "join_tables", "agg_func", "agg_field")
                       and v}
            if slim_sa:
                slim_action["structured_args_summary"] = slim_sa

        obs_entry = {
            "goal_id": goal["id"],
            "round": rnd,
            "thought": decision.get("thought", "")[:300],  # thought 也截断
            "action": slim_action,
            "observation": observation[:1500],  # 截断防止 token 溢出
        }
        ooda_history.append(obs_entry)
        # OODA 历史总条数硬上限：超过时丢弃最旧的（FIFO）
        # 8 目标 × 5 轮 = 40 条上限，但用户可能调高 OODA_MAX_GOALS / OODA_MAX_ROUNDS_PER_GOAL
        _MAX_HISTORY_ENTRIES = settings.OODA_MAX_GOALS * settings.OODA_MAX_ROUNDS_PER_GOAL
        if len(ooda_history) > _MAX_HISTORY_ENTRIES:
            # 丢弃最旧的条目（保留最近的目标观察）
            ooda_history[:] = ooda_history[-_MAX_HISTORY_ENTRIES:]
        state["ooda_history"] = ooda_history
        goal_history.append(obs_entry)
        history_text = _format_ooda_history(goal_history)

    # 达到最大轮数
    return {"status": "complete", "findings": f"已达最大探索轮数（{max_rounds}），基于已有信息总结"}


def _execute_action(decision: dict, agent) -> str:
    """执行 AI 选择的工具——统一调度四类工具"""
    action = decision.get("action", "")

    try:
        if action == "web_search":
            from agent.open_layer.web_tools import web_search
            return web_search(decision.get("query", ""), max_results=5)

        if action == "web_fetch":
            from agent.open_layer.web_tools import web_fetch
            return web_fetch(decision.get("url", ""))

        if action == "db_query":
            # 合法路径：走 execute_single → P1→树→P2（返回 ToolResult，取 text 通道作为观察）
            return str(agent.execute_single(
                instruction=decision.get("instruction", ""),
                behavior_key=decision.get("behavior_key", "查"),
                db_category_key=decision.get("db_category_key", "记录"),
                constraint=decision.get("constraint", ""),
                structured_args=decision.get("structured_args", {}),
            ))

        if action == "file_read":
            from agent.open_layer.file_tools import read_file
            return read_file(decision.get("path", "."))

        if action == "file_read_directory":
            from agent.open_layer.file_tools import read_directory
            return read_directory(decision.get("path", "."))

        if action == "file_list":
            from agent.open_layer.file_tools import list_files
            return list_files(decision.get("path", "."))

        if action == "search_in_files":
            from agent.open_layer.file_tools import search_in_files
            return search_in_files(decision.get("keyword", ""), decision.get("path", "."))

        if action == "rag_search":
            from agent.open_layer.rag import search_documents
            return search_documents(decision.get("query", ""), decision.get("collection", ""))

        return f"未知 action: {action}"
    except Exception as e:
        return f"工具执行失败（{action}）: {e}"


# ═══════════════════════════════════════════════════════════════
# Reporter（报告层）
# ═══════════════════════════════════════════════════════════════

_REPORTER_PROMPT = """你是深度研究报告员。基于所有目标的研究发现，生成综合洞察报告。

用户原始需求：{user_input}
需求理解：{understanding}
研究模式：{research_mode}

各目标的发现：
{goals_with_findings}

OODA 历史摘要：
{ooda_summary}

请生成报告，格式：

## 深度研究报告：{title}

### 研究概述
（简要说明研究目的、方法、覆盖范围）

### 主要发现
（按目标分组，每个目标的发现，标注来源）

### 综合分析
（跨目标的综合洞察，发现模式/趋势/异常）

### 建议
（基于发现的行动建议，如有）

### 来源
- [本地DB] 使用的表和查询
- [Web] 引用的网页
- [文件] 引用的文件
- [RAG] 引用的文档

要求：
- 报告要有洞察力，不只是罗列数据
- 每个关键发现标注来源
- 如果有异常或值得关注的问题，重点提示
- 语言简洁有力，避免空话"""


def _reporter(state: AgentState) -> str:
    """Reporter：综合所有发现，生成洞察报告"""
    from agent.open_layer.graph import _get_llm

    user_input = _get_latest_user_input(state)
    plan = state.get("research_plan", {})

    # 各目标的发现
    goals_with_findings = _format_all_findings(plan)

    # OODA 历史摘要
    ooda_history = state.get("ooda_history", [])
    ooda_summary = _format_ooda_summary(ooda_history)

    prompt = _REPORTER_PROMPT.format(
        user_input=user_input,
        understanding=plan.get("understanding", ""),
        research_mode=plan.get("research_mode", "local"),
        goals_with_findings=goals_with_findings,
        ooda_summary=ooda_summary,
        title=user_input[:30],
    )

    llm = _get_llm(role="research")
    from core.llm_usage import set_role as _usage_role
    with _usage_role("research"):
        response = llm.invoke([
            SystemMessage(content="你是深度研究报告员，擅长综合多源信息生成有洞察力的报告。"),
            HumanMessage(content=prompt),
        ])

    return response.content


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def run_research(state: AgentState, agent) -> AgentState:
    """体系B 主入口：Coordinator → Researcher(OODA) → Reporter

    流程：
    1. 如果有 waiting_for_user 且用户已回答，恢复
    2. 如果没有 research_plan，先 Coordinator 制定计划
    3. OODA 循环逐目标执行
    4. 如遇 ask_user/blocked 暂停，返回等用户输入
    5. 所有目标完成后，Reporter 生成报告
    """
    messages = list(state.get("messages", []))

    # 1. 恢复暂停状态
    if state.get("waiting_for_user") and state.get("user_clarification"):
        _resume_from_wait(state, messages)

    # 2. Coordinator 阶段（如果没有计划）
    if not state.get("research_plan"):
        plan = _coordinator(state)

        # 检查是否需要 ask_user
        if plan.get("ask_user"):
            wait_info = {
                "type": "ask_user",
                "question": plan["ask_user"],
                "thought": plan.get("understanding", ""),
            }
            state["waiting_for_user"] = wait_info
            state["research_plan"] = plan
            messages.append(AIMessage(content=f"📋 我需要更多信息：\n\n{plan['ask_user']}"))
            return {**state, "messages": messages, "results": [plan["ask_user"]]}

        state["research_plan"] = plan
        state["research_mode"] = plan.get("research_mode", "local")

        # 输出任务清单（可视化）
        plan_text = _format_plan(plan)
        messages.append(AIMessage(content=plan_text))

    # 3. Researcher 阶段（OODA 循环逐目标执行）
    plan = state["research_plan"]
    total_rounds = len(state.get("ooda_history", []))

    goal = _next_pending_goal(plan)
    while goal:
        # 检查总轮数限制
        if total_rounds >= settings.OODA_MAX_TOTAL_ROUNDS:
            break

        # 标记目标为进行中
        goal["status"] = "in_progress"

        result = _ooda_for_goal(goal, state, agent)

        if result["status"] == "ask_user":
            wait_info = {
                "type": "ask_user",
                "question": result["question"],
                "goal_id": goal["id"],
                "thought": result.get("thought", ""),
            }
            state["waiting_for_user"] = wait_info
            messages.append(AIMessage(content=f"❓ 关于目标{goal['id']}，我需要确认：\n\n{result['question']}"))
            return {**state, "messages": messages, "results": [result["question"]]}

        if result["status"] == "blocked":
            wait_info = {
                "type": "blocked",
                "reason": result["reason"],
                "suggestion": result["suggestion"],
                "goal_id": goal["id"],
                "thought": result.get("thought", ""),
            }
            state["waiting_for_user"] = wait_info
            blocked_text = (
                f"⚠ 目标{goal['id']}卡壳：{result['reason']}\n\n"
                f"📋 处理方式：{result['suggestion']}\n\n"
                f"处理完成后说「继续」即可恢复研究。"
            )
            messages.append(AIMessage(content=blocked_text))
            return {**state, "messages": messages, "results": [blocked_text]}

        # 目标完成
        goal["status"] = "completed"
        goal["findings"] = result.get("findings", "")

        # 输出进度（可视化）
        progress_text = f"✅ 目标{goal['id']}完成：{goal['goal']}\n   发现：{goal['findings'][:200]}"
        messages.append(AIMessage(content=progress_text))

        total_rounds += 1
        goal = _next_pending_goal(plan)

    # 4. Reporter 阶段
    report = _reporter(state)
    messages.append(AIMessage(content=report))

    return {**state, "messages": messages, "results": [report]}


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _get_latest_user_input(state: AgentState) -> str:
    """获取最新的用户指令"""
    # 问题6：多模态消息 content 可能是列表（含上传文件），需提取纯文本
    from agent.open_layer.graph import _extract_text_from_content
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return _extract_text_from_content(msg.content)
        elif isinstance(msg, dict) and msg.get("role") == "user":
            return _extract_text_from_content(msg.get("content", ""))
    return ""


def _resume_from_wait(state: AgentState, messages: list):
    """从暂停状态恢复"""
    wait = state.get("waiting_for_user", {})
    clarification = state.get("user_clarification", "")

    if wait.get("type") == "ask_user":
        # 将用户回答加入消息历史
        messages.append(HumanMessage(content=clarification))
    elif wait.get("type") == "blocked":
        # 用户处理完毕，继续
        messages.append(HumanMessage(content=f"已处理：{clarification}"))

    # 清除暂停状态
    state["waiting_for_user"] = {}
    state["user_clarification"] = ""


def _next_pending_goal(plan: dict) -> dict | None:
    """获取下一个 pending 状态的目标"""
    for goal in plan.get("goals", []):
        if goal.get("status") == "pending":
            return goal
    return None


def _format_plan(plan: dict) -> str:
    """格式化研究计划为可视化文本"""
    lines = []
    lines.append(f"📋 研究计划")
    lines.append(f"")
    lines.append(f"🎯 需求理解：{plan.get('understanding', '')}")
    lines.append(f"🔍 研究模式：{plan.get('research_mode', 'local')}（{plan.get('mode_reason', '')}）")
    lines.append(f"")
    lines.append(f"📝 任务清单：")
    for goal in plan.get("goals", []):
        status_icon = {"pending": "☐", "in_progress": "🔄", "completed": "✅", "blocked": "⚠"}.get(
            goal.get("status", "pending"), "☐")
        lines.append(f"  {status_icon} {goal['id']}. {goal['goal']}（工具：{goal.get('tool_type', 'local')}）")
    lines.append(f"")
    lines.append(f"开始研究...")
    return "\n".join(lines)


def _format_ooda_history(history: list) -> str:
    """格式化 OODA 历史为文本"""
    if not history:
        return ""
    lines = []
    for h in history:
        lines.append(f"轮次{h['round'] + 1}:")
        lines.append(f"  思考: {h.get('thought', '')}")
        action = h.get("action", {})
        lines.append(f"  行动: {action.get('action', '')} - {_describe_action(action)}")
        obs = h.get("observation", "")
        lines.append(f"  观察: {obs[:300]}")
        lines.append("")
    return "\n".join(lines)


def _describe_action(action: dict) -> str:
    """描述 action 的关键参数"""
    a = action.get("action", "")
    if a == "web_search":
        return f"搜索'{action.get('query', '')}'"
    if a == "web_fetch":
        return f"抓取{action.get('url', '')}"
    if a == "db_query":
        return f"查询{action.get('instruction', '')}"
    if a == "file_read":
        return f"读取{action.get('path', '')}"
    if a == "file_read_directory":
        return f"批量读取目录{action.get('path', '')}"
    if a == "file_list":
        return f"列出{action.get('path', '')}"
    if a == "search_in_files":
        return f"检索'{action.get('keyword', '')}'"
    if a == "rag_search":
        return f"RAG检索'{action.get('query', '')}'"
    return a


def _format_completed_findings(plan: dict) -> str:
    """格式化已完成目标的发现"""
    findings = []
    for goal in plan.get("goals", []):
        if goal.get("status") == "completed":
            findings.append(f"- 目标{goal['id']}（{goal['goal']}）: {goal.get('findings', '')}")
    return "\n".join(findings) if findings else "（尚无已完成目标）"


def _format_all_findings(plan: dict) -> str:
    """格式化所有目标的发现（供 Reporter 用）"""
    lines = []
    for goal in plan.get("goals", []):
        status = goal.get("status", "pending")
        findings = goal.get("findings", "")
        lines.append(f"目标{goal['id']}（{goal['goal']}）[{status}]:")
        lines.append(f"  {findings}")
        lines.append("")
    return "\n".join(lines)


def _format_ooda_summary(history: list) -> str:
    """格式化 OODA 历史摘要（供 Reporter 用）"""
    if not history:
        return "（无 OODA 历史）"
    lines = []
    for h in history[-10:]:  # 最近10条
        action = h.get("action", {})
        obs = h.get("observation", "")[:150]
        lines.append(f"- 目标{h['goal_id']}轮{h['round']+1}: {_describe_action(action)} → {obs}")
    return "\n".join(lines)


def _extract_json(text: str) -> str:
    """从文本中提取 JSON（处理 ```json 代码块）"""
    text = text.strip()
    if text.startswith("```"):
        # 去掉第一行（```json 或 ```）
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        # 去掉结尾的 ```
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    return text
