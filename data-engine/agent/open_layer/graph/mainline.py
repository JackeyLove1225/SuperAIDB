"""graph 子包·主流程节点组：理解拆解 → 执行 → 综合，及 agent_run/replan/deep_research 路由

由原 graph.py 拆分而来（facade 模式，纯搬家不改逻辑）。
"""

import json
from core.logger import get_logger
import re
from typing import Literal

logger = get_logger(__name__)

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langgraph.runtime import Runtime

from agent.open_layer.state import AgentState, FileContext
from agent.open_layer.executor import execute_sub_task
from agent.open_layer.rag import search_documents
from agent.open_layer.prompts import (
    MAX_RESULT_CHARS,
    build_system_prompt,
    build_decompose_prompt,
    build_truncated_results,
    build_error_summary,
    build_simple_file_synthesis_prompt,
    build_complex_synthesis_prompt,
    truncate_result,
    extract_latest_user_input,
    format_conversation_history,
    build_chat_history,
    format_file_manifest,
)
from ._shared import (
    MAX_SUB_TASKS,
    _is_pure_confirm,
    _sanitize_dangling_tool_calls,
    _normalize_sub_tasks,
    _apply_text_evidence,
)
from .build_db import _BUILD_DB_SUMMARY_MARKER

# 经 facade 调用时取值的名字：_get_llm / list_document_collections 均被测试
# patch.object(graph, ...) 遮蔽，节点内必须读 facade 上的当前值
# （循环导入安全：本模块只经 facade 导入，被导入时 facade 已在 sys.modules
# 中，属性到调用时才解析——patch 因此在图装配后依然生效）
from agent.open_layer import graph as _g


def understand_and_decompose(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """节点 1：理解意图 + 拆解子任务

    LLM 分析用户指令，判断简单/复杂，并生成带类型的子任务列表。
    利用对话历史理解上下文引用（如"再查一下"、"对比它们的"等）。

    问题4+5改进：文件清单通过 runtime.context.file_manifest 注入 prompt（不含文件内容），
    AI 看到清单后判断是否需要 file_query 子任务按需读取内容，不再被海量文件内容淹没。
    """
    # 请求边界：LangGraph 入口（旧 Agent.run 路径在 agent/__init__.py 设置）——
    # 每次请求生成新 trace ID，保证审计可追踪且不再出现 [trace:??]
    from core.context import get_context
    get_context().set_trace_id()

    llm = _g._get_llm(role="decompose")  # 拆解是意图理解核心步——答非所问多源于此，吃 pro

    # 获取所有消息（含对话历史）；悬空 tool_calls 先补真实说明（P1-1 文案协议化/
    # P1-11 后端真实补全：替代前端伪造 "Successfully handled" 假消息）
    messages = _sanitize_dangling_tool_calls(state.get("messages", []))
    if messages is not state.get("messages", []):
        state = {**state, "messages": messages}

    # 提取最新用户消息
    user_input = extract_latest_user_input(messages)

    # replan_note（方案C）消费即清：只影响本轮重拆，不残留污染后续正常轮次
    replan_note = state.get("replan_note", "")
    if replan_note:
        state = {**state, "replan_note": ""}

    if not user_input:
        return {**state, "sub_tasks": [], "is_complex": False, "current_step": 0,
                "results": [], "failed_tasks": []}

    # 无挂起态的纯确认词 → 会话重置显式提示（2.2：挂起态进程内，重启即失效；
    # 用户此时发「确认」不能落入 LLM 自由发挥编造"已执行"）。
    # 注：mutation_pending 无写入方，确认一律走前端卡片点击而非聊天文本；
    # 此处仅保留整句精确匹配的纯确认词提示，自由表述不再被子串误判拦截。
    if _is_pure_confirm(user_input):
        logger.info("路由决策: 无挂起态的纯确认词 → 会话重置显式提示")
        _res = ("当前没有待确认的操作。若此前有批量修改/删除正在等待确认，"
                "服务可能已重启（会话已重置，待确认状态不跨进程保留），"
                "请重新发起操作。")
        return {**state, "sub_tasks": [], "is_complex": False, "task_type": "basic",
                "results": [_res], "failed_tasks": [],
                "current_step": 0, "iteration": 0}

    # 提取最近 N 轮对话历史（用于理解上下文引用；窗口规则统一在 prompts 历史组装器）
    conversation_history = format_conversation_history(messages)

    # 获取可用文档集合
    try:
        collections = _g.list_document_collections()
    except Exception:
        collections = []
    collections_str = ", ".join(collections) if collections else "暂无已入库文档"

    # 问题4+5：从 runtime.context 获取文件清单，注入 prompt（不含文件内容）
    file_manifest_section = ""
    build_manifest_present = False
    ctx_manifest: list = []
    if runtime and runtime.context:
        ctx = runtime.context
        manifest = ctx.get("file_manifest") if isinstance(ctx, dict) else None
        if manifest:
            ctx_manifest = manifest
            file_manifest_section = format_file_manifest(manifest) + "\n\n"
        build_manifest_present = bool(
            isinstance(ctx, dict) and ctx.get("build_manifest")
        )

    # 新行业意图（行业注册已并入建库流程，替代原独立向导）：
    # 含"行业"且含创建类动词 → build_db + 行业注册标记
    if "行业" in user_input and any(
        k in user_input for k in ("创建", "新建", "定制", "配置", "建一个", "做一个")
    ):
        logger.info("路由决策: build_db（新行业意图，行业注册并入建库流程）")
        return {**state, "sub_tasks": [], "is_complex": True, "task_type": "build_db",
                "industry_intent": True, "current_step": 0, "results": [],
                "iteration": 0, "failed_tasks": []}

    # 路由决策（闸门三）：能确定的不靠 AI——建库集合非空 + 建库意图词 → 确定性走建库流程
    # "入库"不在其中：录数据是独立步骤，走 basic 的 导入→文件 通道
    _BUILD_DB_KEYWORDS = ("建库", "建成数据库", "自动建", "把文件建", "把文件夹建")
    if any(kw in user_input for kw in _BUILD_DB_KEYWORDS):
        if build_manifest_present:
            logger.info("路由决策: build_db（确定性规则：建库集合非空 + 建库意图词命中）")
            return {
                **state, "sub_tasks": [], "is_complex": True, "task_type": "build_db",
                "current_step": 0, "results": [], "iteration": 0, "failed_tasks": [],
            }
        # 建库集合为空：明确引导（用户可能只是加载了文件夹看看，并不想建库）
        if file_manifest_section:
            logger.info("路由决策: 建库意图但建库集合为空 → 引导用户指定建库文件")
            guidance = AIMessage(content=(
                "要建库的文件还没有指定。请二选一：\n"
                "1. 在左侧「浏览中的文件夹」里，点文件行的 ＋ 把要建库的文件加入建库"
                "（文件夹行可整目录加入）；\n"
                "2. 或在聊天输入区点「上传文件」直接选择要建库的文件。\n"
                "然后再对我说「建成数据库」。"))
            return {**state,
                    "messages": list(state.get("messages", [])) + [guidance],
                    "sub_tasks": [], "is_complex": False, "task_type": "basic"}

    # 未识别问法审核入口：看看没识别的问题 → AI 提议映射样例 → 人工确认纳入
    _REVIEW_KWS = ("看看没识别的问题", "未识别问题", "未识别问法", "没识别的问法", "未识别清单")
    if any(kw in user_input for kw in _REVIEW_KWS):
        logger.info("路由决策: unrecognized_review（未识别问法自学习审核）")
        return {**state, "sub_tasks": [], "is_complex": False,
                "task_type": "unrecognized_review", "current_step": 0,
                "results": [], "iteration": 0, "failed_tasks": []}

    # 映射确认指令（有待确认未映射项时）：对/忽略/是X.Y → 存档规则 + 免 AI 补录
    from core.context import get_context as _get_ctx
    _pending_um = _get_ctx().get("pending_unmapped")
    if _pending_um and _pending_um.get("items"):
        from pipeline.unified import handle_mapping_confirmation
        _hit, _reply = handle_mapping_confirmation(user_input, _pending_um)
        if _hit:
            logger.info("路由决策: 映射确认（存档规则并补录）")
            return {**state,
                    "messages": list(state.get("messages", [])) + [AIMessage(content=_reply)],
                    "results": [_reply],  # synthesize 取 results[0] 作最终回复，防止陈旧结果覆盖
                    "sub_tasks": [], "is_complex": False, "task_type": "basic"}

    # 定向提取（目标驱动入库）：把[这份]文件/图片[里]的 X（和Y）录/提进来——
    # 目标表由白盒术语解析（不靠 LLM 猜表名），命中即确定性走 process_file 限定提取
    _TARGET_INGEST_RE = re.compile(r"(?:文件|图片|照片|扫描件)[里中内]?的?(.+?)(?:录|提取|导入|存|放)(?:进|入|到)")
    _m_te = _TARGET_INGEST_RE.search(user_input)
    if _m_te and ctx_manifest:
        terms = [t for t in re.split(r"[和、，,与及跟\s]+", _m_te.group(1).strip("的了 "))
                 if len(t.strip()) >= 2]
        if terms:
            from core.target_resolve import resolve_tables_by_terms
            targets, unmatched = resolve_tables_by_terms(terms)
            if targets:
                # 取清单中最近一个已落盘的文件作为提取对象
                latest_path = next(
                    (e.get("server_path", "") for e in reversed(ctx_manifest)
                     if e.get("server_path")), "")
                logger.info("路由决策: 定向提取（目标表 %s，未命中词 %s）", targets, unmatched)
                notice = ""
                if unmatched:
                    from core.schema_matcher import _load_schemas as _ls2
                    avail = "、".join(s.get("business_name") or s.get("name", "") for s in _ls2())
                    notice = (f"提示：'{'、'.join(unmatched)}' 在当前行业没有匹配的表，已跳过"
                              f"（现有表：{avail}）。\n")
                messages = list(state.get("messages", []))
                if notice:
                    messages.append(AIMessage(content=notice.rstrip()))
                return {
                    **state, "messages": messages,
                    "sub_tasks": [{
                        "type": "db", "query": user_input,
                        "behavior_key": "导入", "db_category_key": "文件",
                        "structured_args": {
                            "filepath": latest_path,
                            "tables": ",".join(targets),
                        },
                    }],
                    "is_complex": False, "task_type": "basic",
                    "current_step": 0, "results": [], "iteration": 0, "failed_tasks": [],
                }
            logger.info("定向提取：目标词 %s 全部未命中，走通用拆解", terms)

    # 3.1 规划层确定性短路：关键词+schema 命中直接产意图标签，跳过 LLM 拆解。
    # 产出与 LLM 拆解同构（sub_tasks/is_complex/task_type），下游分流与护栏不变；
    # 未命中（None）fail-open 落回下方完整 LLM 拆解，行为与 3.1 之前一致。
    # replan 重拆时跳过短路：短路看不见失败证据，命中只会重蹈覆辙同一路径。
    if not replan_note:
        from agent.open_layer.shortcircuit import try_shortcircuit
        if _sc_plan := try_shortcircuit(user_input):
            logger.info("路由决策: %s（确定性短路，跳过 LLM 拆解）", _sc_plan["task_type"])
            return {**state, **_sc_plan,
                    "current_step": 0, "results": [], "iteration": 0, "failed_tasks": []}

    # 调用 LLM 拆解子任务（含对话历史上下文 + 文件清单）
    # 收集数据库当前真实表名（防止 LLM 用示例里的假想表名——"引用的表不存在"的根因）
    try:
        from core.schema_matcher import _load_schemas
        _schemas = _load_schemas()
        tables_str = "、".join(
            f"{s['name']}（{s.get('business_name', '')}）" if s.get("business_name") else s["name"]
            for s in _schemas
        )
    except Exception:
        tables_str = ""
    # B1 缓存友好：历史/文件清单/用户指令统一由模板尾部排布（稳定段前置），
    # 不再在调用方头部 prepend（原布局每条新消息让模板全文 miss 缓存）
    # replan_note（方案C）：上一轮失败的证据摘要随指令进拆解 prompt，
    # LLM 据此换路径重拆（不拼进 user_input 本体——防污染文本铁证匹配）
    prompt = build_decompose_prompt(
        user_input + (f"\n{replan_note}" if replan_note else ""),
        collections_str, MAX_SUB_TASKS, tables_str,
        history_text=conversation_history,
        manifest_text=file_manifest_section.rstrip("\n"),
    )

    from core.llm_usage import set_role as _usage_role
    with _usage_role("decompose"):
        response = llm.invoke([
            SystemMessage(content=build_system_prompt()),
            # 缓存友好（B1）：稳定段（JSON 格式/规则/表清单/术语）在前，
            # 历史与文件清单等每轮变化内容随用户指令沉底（模板内排布）
            HumanMessage(content=prompt),
        ])

    # 解析 JSON——LLM 可能在 JSON 外包裹说明文字：
    # 优先提取 ``` 代码块中的 JSON，否则截取首个 { 到末个 } 的子串
    try:
        content = response.content.strip()
        if "```" in content:
            for seg in content.split("```"):
                seg = seg.strip()
                if seg.startswith("json"):
                    seg = seg[4:].strip()
                if seg.startswith("{"):
                    content = seg
                    break
        if not content.startswith("{"):
            _i, _j = content.find("{"), content.rfind("}")
            if _i >= 0 and _j > _i:
                content = content[_i:_j + 1]
        plan = json.loads(content)
        raw_tasks = plan.get("sub_tasks", [{"type": "db", "query": user_input}])
        is_complex = plan.get("is_complex", False)
        task_type = plan.get("task_type", "basic")
    except (json.JSONDecodeError, KeyError):
        # 解析失败，当作简单 DB 任务处理
        raw_tasks = [{"type": "db", "query": user_input}]
        is_complex = False
        task_type = "basic"

    sub_tasks = _normalize_sub_tasks(raw_tasks, user_input)
    # 文本铁证纠偏（拆解层）：LLM 改写子任务 query 可能丢掉原句关键词
    # （"有哪些表"被改成"查询数据库"——执行层的文本纠偏就失去了铁证），
    # 单 db 子任务时原句铁证无歧义，在此预先纠正标签
    sub_tasks = _apply_text_evidence(sub_tasks, user_input)

    # 确定性安全闸：agent_query 是查询通道，含写操作关键词压回 basic 再分流——
    # 防 LLM 把"增/删/改/导入/建表"误判为纯查询（basic 的记录级 DML 会再分流到 agent_run）
    if task_type == "agent_query":
        _WRITE_HINTS = ("删除", "删掉", "清空", "移除", "修改", "改成", "改为", "设置", "设为",
                        "重命名", "插入", "录入", "添加", "新增", "增加", "新建", "创建",
                        "导入", "上传", "导出", "入库", "建表", "删表", "加字段", "删字段")
        if any(k in user_input for k in _WRITE_HINTS):
            logger.info("路由纠偏：含写操作语义，agent_query → basic")
            task_type = "basic"
            if not sub_tasks:  # 查询通道指示留空子任务，压回 basic 时补一条兜底任务
                sub_tasks = [{"type": "db", "query": user_input, "behavior_key": "",
                              "db_category_key": "", "constraint": "", "structured_args": {}}]
    # 建库流程的结果是多段（建表+逐文件入库+事实汇总），标记为复杂任务；
    # 最终汇报由 synthesize_result 直接采用 build_db_create 的事实汇总（不经 LLM）
    if task_type == "build_db":
        is_complex = True
    logger.info("路由决策: %s（AI 判断）", task_type)

    return {
        **state,
        "sub_tasks": sub_tasks,
        "is_complex": is_complex,
        "task_type": task_type,
        "current_step": 0,
        "results": [],
        "iteration": 0,
        "failed_tasks": [],
    }


def execute_next_sub_task(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """节点 2：执行下一个子任务

    根据子任务类型路由：
    - type=db → execute_sub_task() → P1→树→P2 → 27 工具（带重试）
    - type=rag → search_documents() → ChromaDB 向量检索
    - type=file_query → 从 runtime.context.file_contents 按需读取文件内容（问题4+5）

    执行失败的任务会记录到 failed_tasks，但不会中断后续子任务。
    """
    step = state.get("current_step", 0)
    sub_tasks = state.get("sub_tasks", [])
    results = list(state.get("results", []))
    failed_tasks = list(state.get("failed_tasks", []))

    if step >= len(sub_tasks):
        return state

    task = sub_tasks[step]
    task_type = task.get("type", "db") if isinstance(task, dict) else "db"
    task_query = task.get("query", str(task)) if isinstance(task, dict) else str(task)

    try:
        if task_type == "file_query":
            # 问题4+5：从 runtime.context.file_contents 按需读取文件内容
            ctx = runtime.context if (runtime and runtime.context) else {}
            file_contents = ctx.get("file_contents", {}) if isinstance(ctx, dict) else {}
            path = task.get("path", "") if isinstance(task, dict) else ""
            if path and path in file_contents:
                content = file_contents[path]
                result = f"📄 文件内容：{path}\n```\n{truncate_result(content, MAX_RESULT_CHARS)}\n```"
            else:
                available = list(file_contents.keys()) if file_contents else []
                hint = f"可用文件：{available[:10]}" if available else "工作区无文件内容（可能为二进制文件）"
                result = f"未找到文件 '{path}'。{hint}"
        elif task_type == "rag":
            # 文档检索：走向量数据库
            result = search_documents(task_query)
        else:
            # 数据库操作：走完整的 P1→树→P2（executor 内部带重试）
            # 传递结构化标签（behavior_key/db_category_key/constraint）
            # 传递 structured_args（工具参数 JSON，供 FC 跳过 AI 调用）
            result = execute_sub_task(
                task_query,
                behavior_key=task.get("behavior_key", ""),
                db_category_key=task.get("db_category_key", ""),
                constraint=task.get("constraint", ""),
                structured_args=task.get("structured_args", {}),
            )

        # 检查结果是否为错误：db 路径读 SubTaskResult.code；
        # rag 路径读 ToolResult.data.ok；file_query 路径（graph 自产文本）沿用旧判定
        from agent.open_layer.executor import SubTaskResult as _STR
        from core.result_codes import ResultCode as _RC
        from core.tool_result import ToolResult as _TR
        if isinstance(result, _STR):
            is_fail = result.code != _RC.OK
        elif isinstance(result, _TR):
            is_fail = result.data.get("ok") is False
        else:
            is_fail = not result or result.startswith("操作失败")
        if is_fail:
            failed_tasks.append({
                "step": step,
                "type": task_type,
                "query": task_query,
                "error": str(result) or "空结果",
            })
            # 未识别采集：路由失败特征（无可匹配/无法确定/不存在等）入池攒样例
            # 树结构不动，攒够后由 AI 提议映射样例、人确认纳入（映射层自学习）
            try:
                from core.unrecognized import record as _ur_record, should_collect as _ur_sc
                if task_type == "db" and _ur_sc(str(result)):
                    from config.settings import settings as _st
                    _pool_n = _ur_record(_st.INDUSTRY, task_query, str(result))
                    if _pool_n >= 3:
                        logger.info("未识别问法池已达 %d 条，可说「看看没识别的问题」审核样例", _pool_n)
            except Exception as _e:
                logger.warning("未识别采集失败（不影响主流程）: %s", _e)
    except Exception as e:
        # GraphInterrupt（核武人审闸等 HITL 挂起信号）不是子任务故障——
        # 必须放行给 LangGraph runtime，吞掉它确认卡片永远弹不出来（1a 修复 20260804）
        from langgraph.errors import GraphInterrupt
        if isinstance(e, GraphInterrupt):
            raise
        result = f"操作失败：子任务执行异常 - {str(e)[:200]}"
        failed_tasks.append({
            "step": step,
            "type": task_type,
            "query": task_query,
            "error": str(e)[:500],
        })

    results.append(str(result))

    return {
        **state,
        "results": results,
        "current_step": step + 1,
        "failed_tasks": failed_tasks,
    }


def should_continue(state: AgentState) -> Literal["execute", "synthesize"]:
    """条件判断：还有子任务？→ 继续执行 / 完成 → 综合结果"""
    step = state.get("current_step", 0)
    sub_tasks = state.get("sub_tasks", [])
    if step < len(sub_tasks):
        return "execute"
    return "synthesize"


def synthesize_result(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """节点 3：综合所有子任务结果，生成最终回复

    问题4+5改进：从 runtime.context 获取文件清单（file_manifest），在综合阶段注入。
    - 简单任务路径：当结果为 file_query 失败（"未找到文件"）且存在文件清单时，
      改用 LLM 基于清单综合回答（修复"有哪些文件"类问题回答牛头不对马嘴）
    - 复杂任务路径：将文件清单注入综合 prompt，让 LLM 看到完整上下文
    """
    results = state.get("results", [])
    is_complex = state.get("is_complex", False)
    messages = state.get("messages", [])
    failed_tasks = state.get("failed_tasks", [])

    # 获取用户原始指令
    user_input = extract_latest_user_input(messages)

    # 问题4+5：从 runtime.context 获取文件清单
    file_manifest = None
    if runtime and runtime.context:
        ctx = runtime.context
        if isinstance(ctx, dict):
            file_manifest = ctx.get("file_manifest")

    # 构建失败任务提示
    error_summary = build_error_summary(failed_tasks)

    # 判断简单任务路径是否需要改用 LLM 综合：
    # 当存在文件清单且结果是 file_query 失败（"未找到文件"）时，
    # 说明 AI 误对"有哪些文件"类问题生成了 file_query，改用 LLM 基于清单回答
    single_result = results[0] if results else ""
    needs_llm_synthesis_for_files = (
        file_manifest
        and (not is_complex or len(results) <= 1)
        and isinstance(single_result, str)
        and "未找到文件" in single_result
    )

    if state.get("task_type") == "build_db":
        # 建库流程：最终汇报直接采用 build_db_create 末尾生成的确定性事实汇总，
        # 不调 LLM 综合——防止 LLM 自由发挥出现"声称导入3条实际0条"的不实汇报
        final_result = next(
            (r for r in reversed(results)
             if isinstance(r, str) and r.startswith(_BUILD_DB_SUMMARY_MARKER)),
            "",
        )
        if not final_result:
            # 兜底：无事实汇总（如确认环节超限直接进 synthesize）时拼接原始结果，仍不经 LLM
            final_result = "\n\n".join(results) if results else "无结果"
        if error_summary:
            final_result += error_summary
    elif needs_llm_synthesis_for_files:
        # 简单任务但 file_query 失败：基于文件清单用 LLM 重新综合回答
        llm = _g._get_llm(role="synthesize")
        prompt = build_simple_file_synthesis_prompt(
            user_input, single_result, format_file_manifest(file_manifest))
        from core.llm_usage import set_role as _usage_role
        with _usage_role("synthesize"):
            response = llm.invoke([
                SystemMessage(content=build_system_prompt()),
                HumanMessage(content=prompt),
            ])
        final_result = response.content
        if error_summary:
            final_result += error_summary
    elif not is_complex or len(results) <= 1:
        # 简单任务：直接返回结果（截断超长结果）
        final_result = truncate_result(single_result, MAX_RESULT_CHARS * 2) if results else "无结果"
        if error_summary:
            final_result += error_summary
    else:
        # 复杂任务：LLM 综合结果（先截断每条结果，再汇总）
        llm = _g._get_llm(role="synthesize")
        results_text, has_truncated = build_truncated_results(results)

        # 问题4+5：复杂任务也注入文件清单，让 LLM 看到完整上下文
        manifest_section = ""
        if file_manifest:
            manifest_section = format_file_manifest(file_manifest) + "\n\n"

        prompt = build_complex_synthesis_prompt(
            user_input, results_text, bool(failed_tasks), has_truncated, manifest_section)
        from core.llm_usage import set_role as _usage_role
        with _usage_role("synthesize"):
            response = llm.invoke([
                SystemMessage(content=build_system_prompt()),
                HumanMessage(content=prompt),
            ])
        final_result = response.content

    # 添加 AI 回复到消息历史
    return {
        **state,
        "messages": messages + [AIMessage(content=final_result)],
    }


def deep_research(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """节点：深度研究（体系B——Plan + OODA）

    架构：Coordinator → Researcher(OODA循环) → Reporter
    - Coordinator：理解深层需求 + 判断研究模式(web/local/hybrid) + 拆解目标
    - Researcher：OODA循环逐目标执行，四类工具统一调度（Web/DB/文件/RAG）
    - Reporter：综合所有发现，生成洞察报告（标注来源）

    人机协作：
    - ask_user：信息不足时暂停，等用户回答后继续
    - blocked：卡壳时告知处理方式，用户处理后继续

    性能优化：复用 executor 的 Agent 单例，避免每次 deep_research 都新建
    Agent（连带新建 AIClient + load_history 磁盘 I/O）
    """
    from agent.open_layer.research import run_research
    from agent.open_layer.executor import get_agent

    # P2-4：不改写共享 Agent 单例——保存/恢复 open_ai_mode，消除跨调用污染
    agent = get_agent()
    prev_mode = agent.open_ai_mode
    agent.open_ai_mode = True
    try:
        return run_research(state, agent)
    finally:
        agent.open_ai_mode = prev_mode


def agent_run(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """统一智能体循环：观察-调整循环（ReAct）——
    AI 先判断（指令合理性/需要什么信息/工具面能否达成），再摸底
    （describe_schema/条件查询确认目标真实存在），计划执行，
    观察结果、调整策略直到完成。动作空间=注册表全量（20260806 方案B），
    核武人审闸/选择集闸全在底层 execute_tool（与树同套）。

    20260805 上下文修复：循环注入最近 N 轮历史（元凶1：原只传当前句，
    "再查一下/把它删掉"类指代在主路径必然丢失）+ 会话状态便签（选择集）。
    """
    from agent.open_layer.agent_loop import run_agent, build_session_note
    from core.llm_usage import set_role as _usage_role

    messages = state.get("messages", [])
    user_input = extract_latest_user_input(messages)
    llm = _g._get_llm(role="agent_loop")
    with _usage_role("agent_loop"):
        answer, trace, status = run_agent(
            user_input, llm,
            history=build_chat_history(messages),
            state_note=build_session_note(),
        )
    if trace:
        steps = " → ".join(t["tool"] for t in trace)
        answer += f"\n\n---\n执行轨迹（{len(trace)} 步）：{steps}"
    # is_complex=False + results=[answer]：synthesize 走简单任务通道直接返回（不经 LLM 二次加工）
    return {**state, "sub_tasks": [], "is_complex": False,
            "results": [answer], "failed_tasks": [],
            "current_step": 0, "task_type": "agent_run",
            # replan 回路证据：结局状态 + 轨迹摘要（exhausted 时 route_after_agent 消费）
            "agent_status": status,
            "agent_trace_note": " → ".join(t["tool"] for t in trace) or "（未调用工具）"}


def route_after_agent(state: AgentState) -> str:
    """agent_run 后路由（方案C）：循环步数耗尽（路径走不通）且有重规划额度
    → replan 携失败证据重拆；否则 synthesize 如实汇报。"""
    if state.get("agent_status") == "exhausted" and state.get("replan_count", 0) < 1:
        logger.info("路由决策: replan（循环步数耗尽，携失败证据重规划）")
        return "replan"
    return "synthesize"


def replan(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """失败重规划（方案C）：把上轮失败证据写进 replan_note，回 understand 重拆。

    与首拆的区别：拆解 prompt 附带失败摘要（步数耗尽事实 + 已尝试工具序列），
    LLM 据此换路径（拆成多个明确子任务走 execute / 升级 deep_research），
    而不是在同一条走不通的路上重复。replan_count+1（route_after_agent 上限 1 次）。
    replan_note 在 understand 消费后即清——防残留污染后续正常轮次。
    """
    return {**state,
            "replan_note": (
                "（系统提示：上一轮执行失败——智能体循环步数耗尽仍未完成。"
                f"已尝试路径：{state.get('agent_trace_note', '')}。"
                "请换一条路径拆解：如拆成多个明确子任务、或判断为 deep_research 深研任务。）"),
            "replan_count": state.get("replan_count", 0) + 1,
            "task_type": "basic", "sub_tasks": [], "results": [],
            "current_step": 0, "failed_tasks": [], "agent_status": ""}


def route_by_task_type(state: AgentState) -> str:
    """路由函数：根据 task_type 分流

    - basic → execute（DDL/文件/模板等走决策树确定性路径）
    - basic 且全部子任务为记录级 DML → agent_run（统一智能体循环）
    - agent_query → agent_run（纯查询走统一智能体循环）
    - deep_research → deep_research（深度研究走体系B）
    - build_db → build_db_explore（全自动建库流程）
    """
    task_type = state.get("task_type", "basic")
    if task_type == "deep_research":
        return "deep_research"
    if task_type == "build_db":
        return "build_db_explore"
    if task_type == "unrecognized_review":
        return "unrecognized_review"
    if task_type == "agent_query":
        return "agent_run"
    if task_type == "basic":
        # 确定性分流（不靠 LLM 自觉）：全部 db 子任务都是记录级操作
        # （查/增/改/删 + 对象=记录）→ 统一循环。查+改/删混合是安全拆解的常态
        # （先查后改/删），循环内由 mutate_data（方案C闸）或选择集承接；
        # DDL/文件/模板/导出等留决策树确定性路径
        subs = [t for t in state.get("sub_tasks", []) if t.get("type", "db") == "db"]
        if subs and all(t.get("behavior_key") in ("查", "增", "改", "删")
                        and t.get("db_category_key") == "记录" for t in subs):
            # 通道-工具匹配预检（方案A，20260806）：空跑决策树验证每个子任务
            # 路由出的工具确在循环工具面内——标签组合与通道能力不符时落 execute
            # （防线：树/词表未来扩展出记录级新工具而未入循环面时，在此兜住）
            from agent.router import get_tree as _gt
            from agent.open_layer.agent_loop import _agent_tool_names as _atn
            _face = set(_atn())
            if all(_gt().route(t["behavior_key"], t["db_category_key"],
                               t.get("constraint", "")) in _face for t in subs):
                logger.info("路由决策: agent_run（记录级操作，统一智能体循环）")
                return "agent_run"
            logger.info("路由纠偏：树路由工具超出循环工具面，落 execute 决策树路径")
    return "execute"
