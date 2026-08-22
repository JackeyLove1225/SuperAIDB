"""graph 子包·共用件：LLM 单例/角色化选模/消息补全/子任务规范化/文本铁证纠偏

由原 graph.py 拆分而来（facade 模式，纯搬家不改逻辑）。
patch 兼容约定：本模块中被测试遮蔽的名字（_LLM_ROLES_PATH 等）一律在
调用时经 facade（agent.open_layer.graph）取值——patch.object(graph, ...) 只改
facade 属性，读 facade 才能看到 mock。
"""

import json
from core.logger import get_logger
import re
from pathlib import Path
from typing import TYPE_CHECKING

logger = get_logger(__name__)

# langchain_openai 改为惰性导入：其导入链会连带 transformers→torch（~10s），
# 是图加载慢的唯一大头。改后模块导入 <1s，重依赖由 get_graph() 的后台预热线程加载。
if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI

from config.settings import settings

# 纯确认词表——整句归一后精确匹配（防"帮我看看对不对"式子串误判，20260805）
_PURE_CONFIRM_WORDS = frozenset({
    "确认", "执行", "好的", "可以", "对", "是",
    "yes", "ok", "确认执行", "确定",
})


def _is_pure_confirm(text: str) -> bool:
    """整句是否纯确认词（去首尾空白/大小写/常见语气标点后精确匹配）"""
    return text.strip().lower().rstrip("。！!~～ ") in _PURE_CONFIRM_WORDS

# 最大子任务数（防止无限循环）
MAX_SUB_TASKS = 10

# 全局 LLM 实例（单例，避免每次调用都创建新的 httpx 连接池）
_llm_cache: dict = {}  # model -> ChatOpenAI（按模型名单例，见 _get_llm）

# 3.2 角色化模型配置（config/llm_roles.yml，解析顺序见 _resolve_role_model）
_LLM_ROLES_PATH = Path(__file__).resolve().parent.parent.parent.parent / "config" / "llm_roles.yml"
_llm_roles_cache: dict | None = None
_LLM_TIERS = ("planning", "mechanical")


def _llm_roles() -> dict:
    """加载角色化模型配置（带缓存；缺失/损坏 fail-open 为空——回退 planning 参数语义）"""
    global _llm_roles_cache
    if _llm_roles_cache is not None:
        return _llm_roles_cache
    try:
        import yaml
        # 经 facade 取路径：tests/test_29 用 patch.object(graph, "_LLM_ROLES_PATH")
        # 换临时 yml——读 facade 当前值才能看到 mock（本模块全局同名常量只是默认值）
        from agent.open_layer import graph as _g
        data = yaml.safe_load(_g._LLM_ROLES_PATH.read_text(encoding="utf-8")) or {}
        _llm_roles_cache = {
            "tiers": {k: str(v or "") for k, v in (data.get("tiers") or {}).items()},
            "roles": {k: str(v or "") for k, v in (data.get("roles") or {}).items()},
        }
    except Exception as e:
        logger.warning("llm_roles.yml 加载失败（%s），回退 planning 参数语义", e)
        _llm_roles_cache = {"tiers": {}, "roles": {}}
    return _llm_roles_cache


def _reset_llm_roles_cache() -> None:
    """测试钩子：清空角色配置缓存（改 yml 后重载）"""
    global _llm_roles_cache
    _llm_roles_cache = None


def _resolve_role_model(role: str = "", planning: bool = False) -> str:
    """角色 → 模型名解析（3.2 配置化选模，与落账同一角色体系）

    解析顺序：
      1. roles[role] 是档位名（planning/mechanical）→ 查 tiers[档位]，
         空则回退 settings（planning→AI_MODEL_PLANNING→AI_MODEL；mechanical→AI_MODEL）
      2. roles[role] 是其他字符串 → 具体模型名直接用（角色级细调，绕过档位）
      3. role 未配置 → 按 planning 参数语义（与 3.2 之前行为一致）
    """
    cfg = _llm_roles()
    if role:
        target = cfg["roles"].get(role, "")
        if target and target not in _LLM_TIERS:
            return target  # 角色级具体模型名
        tier = target or ("planning" if planning else "mechanical")
        model = cfg["tiers"].get(tier, "")
        if model:
            return model
        if tier == "planning":
            return getattr(settings, "AI_MODEL_PLANNING", "") or settings.AI_MODEL
        return settings.AI_MODEL
    # 无角色：3.2 之前的 planning 参数语义（向后兼容）
    if planning and getattr(settings, "AI_MODEL_PLANNING", ""):
        return settings.AI_MODEL_PLANNING
    return settings.AI_MODEL


def _sanitize_dangling_tool_calls(messages):
    """补齐悬空 tool_calls（P1-11 后端真实补全）。

    AI 消息带 tool_calls 但其后没有对应 tool 响应消息时（会话中断/恢复所致），
    注入如实说明的 tool 响应"该工具调用未执行"，替代前端伪造的
    "Successfully handled tool call." 假消息——LLM 看到的是真实状态而非幻觉成功。
    无悬空时原样返回（identity），有悬空时返回新列表。
    """
    answered = set()
    for m in messages or []:
        mtype = getattr(m, "type", None) if not isinstance(m, dict) else m.get("type")
        if mtype == "tool":
            tcid = getattr(m, "tool_call_id", None) if not isinstance(m, dict) else m.get("tool_call_id")
            if tcid:
                answered.add(tcid)
    out = []
    changed = False
    for m in messages or []:
        out.append(m)
        mtype = getattr(m, "type", None) if not isinstance(m, dict) else m.get("type")
        tcs = getattr(m, "tool_calls", None) if not isinstance(m, dict) else m.get("tool_calls")
        if mtype == "ai" and tcs:
            for tc in tcs:
                tcid = getattr(tc, "id", None) if not isinstance(tc, dict) else tc.get("id")
                name = getattr(tc, "name", None) if not isinstance(tc, dict) else tc.get("name")
                if tcid and tcid not in answered:
                    changed = True
                    out.append({
                        "type": "tool",
                        "tool_call_id": tcid,
                        "name": name or "",
                        "content": "该工具调用未执行（会话中断或恢复），没有实际结果数据。",
                    })
    return out if changed else messages


def _get_llm(planning: bool = False, role: str = "") -> "ChatOpenAI":
    """获取 LLM 实例（按模型名单例缓存）

    性能优化：复用 ChatOpenAI 实例，避免每次调用都创建新的 httpx 连接池
    （原实现每次 _get_llm() 都 new 一个 ChatOpenAI，understand + synthesize +
    每轮 OODA 都重复创建，8目标×5轮=40 次连接池初始化）

    选模（3.2 角色化，config/llm_roles.yml）：role 传入时按角色查配置
    （规划档吃 AI_MODEL_PLANNING/pro，机械档吃 AI_MODEL/flash）；
    role 为空回退 planning 参数语义——planning=True 用 AI_MODEL_PLANNING
    （未配置回退 AI_MODEL）。选模角色与落账角色（core/llm_usage.set_role）同源。
    """
    from langchain_openai import ChatOpenAI  # 惰性导入（见模块头注释）

    model = _resolve_role_model(role, planning)
    if model not in _llm_cache:
        # DeepSeek v4 默认开思考：每次调用先长篇推理（分钟级），开放层
        # understand/decompose/synthesize 全是机械性短任务，不需要推理。
        # 与 AIClient 同口径禁用（max_tokens 也不再被推理吃光）
        extra = {}
        if str(model).startswith("deepseek-v4"):
            extra["model_kwargs"] = {"extra_body": {"thinking": {"type": "disabled"}}}
        # 3.0 统计：token 用量统一落账（角色经 contextvar 由调用方标注）
        from langchain_core.callbacks import BaseCallbackHandler
        from core.llm_usage import on_langchain_end

        class _UsageCallback(BaseCallbackHandler):
            def on_llm_end(self, response, **kwargs):
                on_langchain_end(response)

        _llm_cache[model] = ChatOpenAI(
            api_key=settings.AI_API_KEY,
            base_url=settings.AI_BASE_URL,
            model=model,
            temperature=0.1,
            max_tokens=4096,
            callbacks=[_UsageCallback()],
            **extra,
        )
    return _llm_cache[model]


def _apply_text_evidence(sub_tasks: list, user_input: str) -> list:
    """文本铁证纠偏（拆解层，白盒）：LLM 拆解会改写子任务 query——改写一旦丢掉
    原句关键词（如"有哪些表"被改成"查询数据库"），下游执行层的文本纠偏就失去
    铁证。恰好 1 个 db 子任务时，原句铁证无歧义地归属于它，在此预先纠正其
    行为/对象标签；多子任务时不动（各子任务的文本纠偏在执行层逐条进行，防串台）。
    """
    from agent.router import text_behavior_override as _tbo, text_db_override as _tdbo
    db_idx = [i for i, t in enumerate(sub_tasks) if t.get("type", "db") == "db"]
    if len(db_idx) != 1 or not user_input:
        return sub_tasks
    t = sub_tasks[db_idx[0]]
    tbk, tdk = _tbo(user_input), _tdbo(user_input)
    if tbk and tbk != t.get("behavior_key", ""):
        logger.info("文本铁证纠偏（拆解层）: 行为 %s→%s（原句）", t.get("behavior_key", ""), tbk)
        t["behavior_key"] = tbk
    if tdk and tdk != t.get("db_category_key", ""):
        logger.info("文本铁证纠偏（拆解层）: 对象 %s→%s（原句）", t.get("db_category_key", ""), tdk)
        t["db_category_key"] = tdk
    # 行为条件对象铁证（方案A，20260806）："删除表格X"（X=已知表名）是删表不是删记录。
    # "表格"一词两义——查/改语境=记录容器（"表格X的数据"），删语境+具体表名=表结构本身；
    # LLM 误拆 {删,记录} 会路由进记录级循环，AI 在沙盒里如实报"没有删表工具"（真事故）。
    # 记录级尾词（"表格X的记录/数据"）不纠——那确为记录级。
    if t.get("behavior_key") == "删" and t.get("db_category_key") == "记录" \
            and "表格" in user_input \
            and not re.search(r"表格.{0,20}?(的|中|里)?(记录|数据|行)", user_input):
        from core.schema_matcher import _load_schemas as _ls
        if any(s["name"] in user_input for s in _ls()):
            logger.info("文本铁证纠偏（拆解层）: 删×表格+已知表名 → 对象 记录→表")
            t["db_category_key"] = "表"
    return sub_tasks


def _normalize_sub_tasks(raw_tasks, user_input: str) -> list[dict]:
    """将子任务规范化为带类型的字典列表

    兼容 LLM 返回的字符串列表或字典列表。
    保留 behavior_key/db_category_key/constraint/structured_args 结构化标签（type=db 时）。
    支持 type=file_query（从 runtime.context.file_contents 按需读取文件内容）。
    """
    normalized = []
    for task in raw_tasks[:MAX_SUB_TASKS]:
        if isinstance(task, dict):
            t_type = task.get("type", "db")
            t_query = task.get("query", task.get("instruction", ""))
            if t_query:
                item = {"type": t_type, "query": t_query}
                # type=db 时保留结构化标签（供 P1 跳过 AI 解析）
                if t_type == "db":
                    item["behavior_key"] = task.get("behavior_key", "")
                    item["db_category_key"] = task.get("db_category_key", "")
                    item["constraint"] = task.get("constraint", "")
                    # structured_args：LangGraph 输出的工具参数 JSON（供 FC 跳过 AI 调用）
                    sargs = task.get("structured_args", {})
                    if isinstance(sargs, str):
                        try:
                            sargs = json.loads(sargs)
                        except Exception:
                            sargs = {}
                    if not isinstance(sargs, dict):
                        sargs = {}
                    item["structured_args"] = sargs
                elif t_type == "file_query":
                    # file_query：path 必须与文件清单中的 path 完全一致
                    item["path"] = task.get("path", "")
                normalized.append(item)
        elif isinstance(task, str):
            normalized.append({"type": "db", "query": task})
    if not normalized:
        normalized = [{"type": "db", "query": user_input}]
    return normalized
