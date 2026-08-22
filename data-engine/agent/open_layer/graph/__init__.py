"""LangGraph 图定义——开放式 AI 编排器（facade）

流程：理解意图 → 判断简单/复杂 → 拆解子任务（DB/RAG 类型）→ 逐个执行 → 综合结果

核心原则：
- 数据库操作（type=db）通过 execute_sub_task() 走完整的 P1→树→P2，不直接操作数据库
- 文档检索（type=rag）通过 search_documents() 走向量数据库，回答文档内容问题

本模块只保留图逻辑（节点/路由/构建）；所有 prompt 模板与拼装集中在
agent.open_layer.prompts，通过 builder 函数调用。

拆分布局（20260821，facade 模式——import 面零变化）：
- 实现按节点组拆到 graph/ 子包：_shared.py（LLM 单例/选模/规范化等共用件）、
  mainline.py（understand/execute/synthesize/agent_run/replan/路由）、
  build_db.py（建库流程节点）、review.py（unrecognized_review）
- 本 facade 保留：全部公开名 re-export + _with_role 包装器 + build_graph 图装配
  + get_graph/run_open_agent 入口。外部 import 路径全部不变。

patch 兼容约定（测试依赖，勿绕开）：节点内对 _get_llm / list_document_collections /
_LLM_ROLES_PATH 的引用一律在调用时经本 facade 取值（子模块 `from agent.open_layer
import graph as _g`），因此 patch.object(graph, ...) 在图装配后依然生效；
_with_role 留在本模块也是为了 patch("agent.open_layer.graph.settings") 命中。
"""

import json
from core.logger import get_logger
import re
import sys
from pathlib import Path
from typing import Literal, TYPE_CHECKING

logger = get_logger(__name__)

# langchain_openai 改为惰性导入：其导入链会连带 transformers→torch（~10s），
# 是图加载慢的唯一大头。改后模块导入 <1s，重依赖由 get_graph() 的后台预热线程加载。
if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime

# 确保项目根目录在 sys.path 中
_project_root = str(Path(__file__).resolve().parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config.settings import settings
from agent.open_layer.state import AgentState, FileContext
from agent.open_layer.executor import execute_sub_task
from agent.open_layer.rag import search_documents, list_document_collections
from agent.open_layer.prompts import (
    MAX_RESULT_CHARS,
    build_system_prompt,
    build_decompose_prompt,
    build_schema_design_prompt,
    build_truncated_results,
    build_error_summary,
    build_simple_file_synthesis_prompt,
    build_complex_synthesis_prompt,
    truncate_result,
    extract_text_from_content,
    extract_latest_user_input,
    format_conversation_history,
    build_chat_history,
    format_file_manifest,
)

# 兼容旧引用：tests/test_12 从本模块导入 _extract_text_from_content
_extract_text_from_content = extract_text_from_content

# ═══ 实现 re-export（import 面与拆前完全一致，含私有名——测试直接引用）═══
from ._shared import (
    MAX_SUB_TASKS,
    _PURE_CONFIRM_WORDS,
    _is_pure_confirm,
    _llm_cache,
    _LLM_ROLES_PATH,
    _llm_roles_cache,
    _LLM_TIERS,
    _llm_roles,
    _reset_llm_roles_cache,
    _resolve_role_model,
    _sanitize_dangling_tool_calls,
    _get_llm,
    _apply_text_evidence,
    _normalize_sub_tasks,
)
from .mainline import (
    understand_and_decompose,
    execute_next_sub_task,
    should_continue,
    synthesize_result,
    deep_research,
    agent_run,
    route_after_agent,
    replan,
    route_by_task_type,
)
from .build_db import (
    _BUILD_SUPPORTED_PIPELINE_EXTS,
    _BUILD_TEXT_EXTS,
    _BUILD_DB_SUMMARY_MARKER,
    _is_create_failure,
    _build_db_fact_summary,
    _get_file_ctx,
    build_db_explore,
    build_db_design,
    build_db_confirm,
    route_after_confirm,
    _switch_industry_local,
    build_db_create,
)
from .review import unrecognized_review


def _with_role(fn):
    """节点包装器：节点开头从 runtime.context 验签 user_token 并注入权限角色（1.4）

    同步节点跑线程池，contextvar 不从主线程穿透，必须在节点内显式注入。
    角色口径（与 mgmt 中间件对齐）：
    - 有效 token → token 内角色
    - 无/无效 token：
      - API_KEY_ENABLED=false（开发模式）→ system（全放行，兼容现状）
      - API_KEY_ENABLED=true（认证模式）→ readonly（安全降级，可读不可写；
        不硬拒绝——LangGraph 层无法向前端回 401，硬拒绝会变成图执行报错，
        readonly 让权限矩阵在执行层自然拦截写操作）
    """
    from functools import wraps

    @wraps(fn)
    def wrapper(state: AgentState, runtime: "Runtime[FileContext]" = None, *args, **kwargs):
        from core.permission import set_current_role
        token = ""
        try:
            ctx = runtime.context if runtime else None
            if isinstance(ctx, dict):
                token = ctx.get("user_token") or ""
        except Exception:
            pass
        role = ""
        if token:
            try:
                from core.auth import verify_token
                payload = verify_token(token)
                if payload:
                    role = payload.get("role") or "user"
                else:
                    logger.warning("聊天路径 user_token 验签失败（无效/过期），按降级角色处理")
            except Exception as e:
                logger.warning("user_token 验签异常（%s），按降级角色处理", e)
        if not role:
            auth_on = str(getattr(settings, "API_KEY_ENABLED", "")).lower() in ("true", "1", "yes")
            role = "readonly" if auth_on else "system"
        set_current_role(role)
        return fn(state, runtime, *args, **kwargs)

    return wrapper


def build_graph():
    """构建 LangGraph 图

    体系A（基础操作）：understand → execute → synthesize
    体系B（深度研究）：understand → deep_research → synthesize
    路由由 task_type 决定（route_by_task_type）

    问题4+5改进：注册 context_schema=FileContext，前端通过 runtime.context 传递
    文件清单（file_manifest）和文件内容（file_contents），实现清单与内容分离。
    context 不进 checkpoint，每次 run 独立，不污染 messages 历史。
    """
    workflow = StateGraph(AgentState, context_schema=FileContext)

    # 添加节点（全部经 _with_role 包装：节点开头验签注入用户角色）
    workflow.add_node("understand", _with_role(understand_and_decompose))
    workflow.add_node("execute", _with_role(execute_next_sub_task))
    workflow.add_node("synthesize", _with_role(synthesize_result))
    workflow.add_node("deep_research", _with_role(deep_research))
    # 全自动建库流程节点
    workflow.add_node("build_db_explore", _with_role(build_db_explore))
    workflow.add_node("build_db_design", _with_role(build_db_design))
    workflow.add_node("build_db_confirm", _with_role(build_db_confirm))
    workflow.add_node("build_db_create", _with_role(build_db_create))
    # 未识别问法审核节点（自学习：提议→确认→纳入映射层）
    workflow.add_node("unrecognized_review", _with_role(unrecognized_review))
    # 统一智能体循环节点（全量工具面：观察-调整，护栏在底层 execute_tool）
    workflow.add_node("agent_run", _with_role(agent_run))
    # 失败重规划节点（方案C）：携失败证据回 understand 重拆，上限 1 次
    workflow.add_node("replan", _with_role(replan))

    # 添加边
    workflow.add_edge(START, "understand")
    # 根据task_type路由：basic→execute, deep_research→deep_research, build_db→建库流程
    workflow.add_conditional_edges(
        "understand",
        route_by_task_type,
        {"execute": "execute", "deep_research": "deep_research",
         "build_db_explore": "build_db_explore",
         "unrecognized_review": "unrecognized_review",
         "agent_run": "agent_run"},
    )
    workflow.add_conditional_edges(
        "execute",
        should_continue,
        {"execute": "execute", "synthesize": "synthesize"},
    )
    workflow.add_edge("deep_research", "synthesize")
    # 建库流程：探索→设计→确认→（批准→入库 / 拒绝→重新设计，限2轮）
    workflow.add_edge("build_db_explore", "build_db_design")
    workflow.add_edge("build_db_design", "build_db_confirm")
    workflow.add_conditional_edges(
        "build_db_confirm",
        route_after_confirm,
        {"build_db_design": "build_db_design", "build_db_create": "build_db_create",
         "synthesize": "synthesize"},
    )
    workflow.add_edge("build_db_create", "synthesize")
    workflow.add_edge("unrecognized_review", "synthesize")
    # 方案C：循环步数耗尽 → replan 携失败证据重拆（上限1次）；正常收口 → synthesize
    workflow.add_conditional_edges(
        "agent_run",
        route_after_agent,
        {"replan": "replan", "synthesize": "synthesize"},
    )
    workflow.add_edge("replan", "understand")
    workflow.add_edge("synthesize", END)

    return workflow.compile()


# 全局图实例
_graph_instance = None
_warmup_started = False


def _warmup_heavy_imports():
    """后台预热重依赖（langchain_openai → transformers → torch，~10s）

    与服务器启动/用户打开页面的时间重叠：用户看到"就绪"后还要读界面、
    输第一条消息，预热通常在此之前完成，首条消息不再承担导入成本。
    若消息先到达，_get_llm() 的导入会等在同一个模块锁上——
    成本相同，只是从"启动等待"挪到"与预热重叠"，不会重复支付。
    """
    try:
        import langchain_openai  # noqa: F401
    except Exception as e:
        logger.warning(f"重依赖预热失败（不影响功能，首次使用时会重新导入）: {e}")


def get_graph():
    """获取全局图实例（单例）"""
    global _graph_instance, _warmup_started
    if _graph_instance is None:
        _graph_instance = build_graph()
    if not _warmup_started:
        _warmup_started = True
        import threading

        threading.Thread(target=_warmup_heavy_imports, daemon=True).start()
    return _graph_instance


def run_open_agent(user_input: str) -> str:
    """开放式 AI Agent 入口

    Args:
        user_input: 用户自然语言指令

    Returns:
        最终回复字符串
    """
    graph = get_graph()
    initial_state = {
        "messages": [HumanMessage(content=user_input)],
        "sub_tasks": [],
        "current_step": 0,
        "results": [],
        "is_complex": False,
        "iteration": 0,
        "failed_tasks": [],
        "task_type": "basic",
        # 体系B新增
        "research_plan": {},
        "ooda_history": [],
        "waiting_for_user": {},
        "user_clarification": "",
        "research_mode": "",
        # 建库流程新增
        "build_samples": [],
        "build_manifest": [],
        "proposed_schema": {},
        "schema_feedback": "",
        "schema_confirm_rounds": 0,
        # 失败重规划回路（方案C）
        "agent_status": "",
        "agent_trace_note": "",
        "replan_count": 0,
        "replan_note": "",
    }
    final_state = graph.invoke(initial_state)

    # 提取 AI 回复
    messages = final_state.get("messages", [])
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg.content
        elif isinstance(msg, dict) and msg.get("role") == "assistant":
            return msg.get("content", "")
    return "无结果"
