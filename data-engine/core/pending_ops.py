"""高危操作待批准挂起表（文件持久化版，20260822 重构）

背景：高危人审闸在 graph 通道走 LangGraph interrupt 人审卡；MCP server 进程
无 graph runtime，interrupt 安全拒绝会让 MCP 通道的高危操作永远不可用。
本模块提供 MCP 通道的回执链路：

  execute_tool 命中高危闸（channel=mcp）
    → register_pending 登记（name/kwargs/影响面）→ 抛 PendingApproval(token)
    → AI 收到"待批准"结果（**token 不回传 AI 通道**），向用户转述影响面
    → 用户在 Web 管理台审批中心批准（admin）→ 管理端进程取出原操作并执行

安全语义：
- fail-closed：未确认一律不执行，token 是结算的唯一凭据
- **token 不出管理通道**：AI 只见"待批准"，不见凭据——防 AI 自助结算
  （MCP 自助人审漏洞修复 20260822；参照 __escalate__ 的先例推广到全部高危操作）
- 一次性：pop 即焚，不可重放；批准/拒绝都销毁 token
- 有期：TTL 10 分钟，过期自动清除（防挂起堆积与迟来确认）
- **跨进程**：落盘持久化（config/pending_approvals.json，mtime 新鲜读取）——
  MCP 进程登记、管理端进程结算；进程重启后待批准项仍在（按 TTL 自然过期）
"""
import json
from core.logger import get_logger
import os
import time
import uuid
from pathlib import Path

logger = get_logger(__name__)

_TTL_SECONDS = 600  # 10 分钟——与一次人审卡片的合理等待时长同量级

_STORE = Path(__file__).resolve().parent.parent / "config" / "pending_approvals.json"
_mem: dict[str, dict] = {}          # 进程内缓存
_mem_mtime: float = -1.0            # 缓存对应的文件 mtime


def _load() -> dict:
    """读挂起表（文件为契约，mtime 新鲜通道——与 ConfigHub 同哲学）"""
    global _mem, _mem_mtime
    try:
        mtime = os.path.getmtime(_STORE)
    except OSError:
        _mem, _mem_mtime = {}, -1.0
        return _mem
    if mtime != _mem_mtime:
        try:
            data = json.loads(_STORE.read_text(encoding="utf-8"))
            _mem = data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning("挂起表读取失败（按空表处理，fail-closed 不影响）: %s", e)
            _mem = {}
        _mem_mtime = mtime
    return _mem


def _save(d: dict) -> None:
    """原子写挂起表（tmp + replace，防半写）"""
    global _mem, _mem_mtime
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _STORE)
    _mem = d
    try:
        _mem_mtime = os.path.getmtime(_STORE)
    except OSError:
        _mem_mtime = -1.0


def _sweep_expired(d: dict, now: float) -> dict:
    """惰性清理过期条目"""
    live = {t: op for t, op in d.items() if now - op["ts"] <= _TTL_SECONDS}
    for t in set(d) - set(live):
        logger.info("挂起表：token %s 已过期清除（%s）", t, d[t]["name"])
    return live


def register_pending(name: str, kwargs: dict, impact: str) -> str:
    """登记待批准操作，返回一次性 token（token 只用于管理端结算，不回传 AI）。

    登记也在同一互斥锁内（评审五轮）：否则管理端 pop 结算与 MCP 登记并发时，
    已结算 token 可被登记方的整存回写复活，TTL 内构成双执行面。"""
    from core.file_contract import FileLock
    with FileLock(_STORE.with_suffix(".lock")):
        token = "P-" + uuid.uuid4().hex[:8]
        d = _sweep_expired(_load(), time.time())
        d[token] = {"name": name, "kwargs": dict(kwargs), "impact": impact,
                    "ts": time.time()}
        _save(d)
    logger.info("挂起表：登记 %s → %s（影响面 %d 字）", name, token, len(impact))
    return token


def pop_pending(token: str) -> dict | None:
    """一次性取出并销毁（批准/拒绝都走这里；不存在或过期返回 None）。

    跨进程互斥（评审四轮 M + 五轮补齐）：读改写临界区进 FileLock
    （锁内写 PID，持有者死亡自动回收）——并发结算同一 token 不再双执行。"""
    from core.file_contract import FileLock
    with FileLock(_STORE.with_suffix(".lock")):
        d = _load()
        op = d.pop(token, None)
        _save(_sweep_expired(d, time.time()))
        return op


def list_pending() -> list[dict]:
    """列出当前待批准项（管理端审批中心用；含 token/操作/影响面/剩余秒数）"""
    now = time.time()
    d = _sweep_expired(_load(), now)
    return [{"token": t, "name": op["name"], "impact": op.get("impact", ""),
             "age_seconds": int(now - op["ts"]),
             "ttl_remaining": int(_TTL_SECONDS - (now - op["ts"]))}
            for t, op in sorted(d.items(), key=lambda kv: kv[1]["ts"])]


def pending_count() -> int:
    """当前挂起数（观测/测试用）"""
    return len(_sweep_expired(_load(), time.time()))
