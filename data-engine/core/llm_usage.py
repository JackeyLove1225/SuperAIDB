"""LLM token 用量统一落账（3.0 前置统计②地基）

两个 LLM 网关共用的事实源：
  - LangChain 侧：graph._get_llm 给每个 ChatOpenAI 挂 _UsageCallback，
    on_llm_end 时从 usage_metadata / response_metadata 提取用量
  - raw 侧：core/ai_runtime/ai_client.py 在 _call / call_function 拿到 response.usage 直录

角色（role）通过 contextvar 传递：调用方在逻辑入口 set_role("extract_param")，
网关落账时读取。现役角色清单（聚合脚本按此分组）：
  extract_param  工具参数/条件提取（condition_parser / data_ops / db_chat / router FC）
  extract_file   文件管道提取（pipeline/extraction）
  other          未标注（兜底）
（历史角色 decompose/synthesize/agent_loop/research/review/schema_design
 随进程内图编排于 20260824 下线；旧 jsonl 数据中的这些标签仍可被聚合脚本读取）

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
            # 容量帽（纯 append 曾无界——长跑必胀）。
            # 超 10MB 先归档一档再重开（与 logger 轮转同标准，历史不丢）
            try:
                if LOG_PATH.exists() and LOG_PATH.stat().st_size > 10 * 1024 * 1024:
                    old = LOG_PATH.with_suffix(".jsonl.1")
                    old.unlink(missing_ok=True)
                    LOG_PATH.replace(old)
            except OSError:
                pass  # 归档失败则继续追加（计量通道降级方向）
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 用量日志写失败不影响主调用（计量通道降级）


def _trace_id() -> str:
    try:
        from core.context import get_context
        return getattr(get_context(), "_trace_id", "") or ""
    except Exception:
        return ""

