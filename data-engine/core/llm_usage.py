"""LLM token 用量统一落账（3.0 前置统计②地基）

两个 LLM 网关共用的事实源：
  - LangChain 侧：graph._get_llm 给每个 ChatOpenAI 挂 _UsageCallback，
    on_llm_end 时从 usage_metadata / response_metadata 提取用量
  - raw 侧：core/ai_runtime/ai_client.py 在 _call / call_function 拿到 response.usage 直录

角色（role）通过 contextvar 传递：调用方在逻辑入口 set_role("decompose")，
网关落账时读取。角色清单（聚合脚本按此分组，档档对应 3.2 角色档）：
  decompose      拆解（understand_and_decompose）
  synthesize     综合（synthesize_result）
  agent_loop     统一循环步（run_agent 每步规划）
  extract_param  工具参数/条件提取（condition_parser / data_ops / db_chat / router FC）
  extract_file   文件管道提取（pipeline/extraction）
  research       深度研究 OODA（research.py）
  review         未识别问法审查映射（unrecognized_review / propose_examples）
  schema_design  建库 schema 设计（build_db_design）
  other          未标注（兜底）

落账文件：logs/llm_usage.jsonl，每行
  {ts, role, model, in, out, cache_hit, trace}
全程 fail-open：任何异常都不允许影响 LLM 主路径。
"""
import json
from core.logger import get_logger
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

logger = get_logger(__name__)

_usage_role: ContextVar[str] = ContextVar("llm_usage_role", default="")
_lock = threading.Lock()

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "llm_usage.jsonl"


@contextmanager
def set_role(role: str):
    """标注当前逻辑块的 LLM 角色（嵌套时内层覆盖、退出恢复）"""
    tok = _usage_role.set(role)
    try:
        yield
    finally:
        _usage_role.reset(tok)


def current_role() -> str:
    return _usage_role.get() or "other"


def record_usage(role: str, model: str, input_tokens, output_tokens,
                 cache_hit=None, trace: str = "") -> None:
    """追加一条用量记录（fail-open，永不抛异常）"""
    try:
        if input_tokens is None and output_tokens is None:
            return
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "role": role or "other",
            "model": model or "",
            "in": int(input_tokens or 0),
            "out": int(output_tokens or 0),
            "cache_hit": int(cache_hit or 0),
            "trace": trace or "",
        }
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def extract_langchain_usage(ai) -> tuple:
    """从 LangChain AIMessage 提取 (输入, 输出, 缓存命中)——尽力而为，缺字段给 None。
    与 agent_loop._log_usage 同一套字段探针（DeepSeek usage_metadata /
    OpenAI 兼容 response_metadata.token_usage 两态）。"""
    try:
        um = getattr(ai, "usage_metadata", None) or {}
        inp = um.get("input_tokens")
        out = um.get("output_tokens")
        hit = (um.get("input_token_details") or {}).get("cache_read")
        if inp is None:
            tu = (getattr(ai, "response_metadata", None) or {}).get("token_usage") or {}
            inp = tu.get("prompt_tokens")
            out = tu.get("completion_tokens")
            hit = tu.get("prompt_cache_hit_tokens")
        return inp, out, hit
    except Exception:
        return None, None, None


def _trace_id() -> str:
    try:
        from core.context import get_context
        return getattr(get_context(), "_trace_id", "") or ""
    except Exception:
        return ""


def on_langchain_end(response) -> None:
    """LangChain on_llm_end 落账逻辑（回调类在 graph._get_llm 内定义——
    BaseCallbackHandler 需随 langchain 惰性导入，此处保持零重依赖）"""
    try:
        msg = response.generations[0][0].message
    except Exception:
        return
    inp, out, hit = extract_langchain_usage(msg)
    if inp is None and out is None:
        return
    model = ""
    try:
        model = (response.llm_output or {}).get("model_name", "")
    except Exception:
        pass
    record_usage(current_role(), model, inp, out, hit, _trace_id())
