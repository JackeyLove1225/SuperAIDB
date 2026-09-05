"""结算回执中继站（MCP 同步人审桥的回执通道，20260903）

背景：MCP 通道高危操作命中人审闸后，mcp_server 侧改为同步等待用户在
Web 管理台批准/拒绝（MCP_APPROVAL_SYNC，默认开）——本模块是"结算结果
如何送回等待中的 MCP 进程"的跨进程文件契约：

  mgmt settle 端点（admin + operator_password + pop_pending 执行）
    → record_settlement(token, status, result)   ← 结算回执落盘
  MCP server 等待线程
    → wait_settlement(token, timeout)            ← 轮询取走回执，组合成
                                                     AI 可见的执行结果文本

安全定位（重要）：
- 本模块只是**信息性回执邮筒**——真正的执行与全部安全（admin 鉴权 +
  operator_password 慢哈希 + token 一次性 + HMAC 验签）都在 mgmt settle
  端点，这里零安全决策；无需签名（写入方是已鉴权的 mgmt 进程）。
- record 全兜底：回执链路任何故障绝不阻断真实结算（吞异常只打日志）。
- 等待方超时即 fail-closed 返回"未批准"——回执丢了 ≠ 操作执行了。
- 文件含 token 明文（作为 key），与 pending_approvals.json 同标准：
  敏感文件黑名单（agent/tools/_shared.py）+ config/ 禁区 + 0600 落盘。
- wait 用 peek 语义读到不删（靠 TTL 清扫）——dedup 场景多个等待者
  共用同一 token 时都能收到回执。
"""
import json
import os
import time
from pathlib import Path

from core.logger import get_logger
from core.file_contract import FileLock

logger = get_logger(__name__)

_TTL_SECONDS = 900  # 15 分钟——> 挂起表 600s，满 TTL 等待的回执不先消失

_STORE = Path(__file__).resolve().parent.parent / "config" / "settlement_results.json"
_mem: dict[str, dict] = {}       # 进程内缓存
_mem_mtime: float = -1.0         # 缓存对应的文件 mtime


def _load() -> dict:
    """读回执表（文件为契约，mtime 新鲜通道——与 pending_ops 同哲学）"""
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
            logger.warning("结算回执表读取失败（按空表处理）: %s", e)
            _mem = {}
        _mem_mtime = mtime
    return _mem


def _save(d: dict) -> None:
    """原子写回执表（tmp + replace，防半写）；0600 落盘（token 明文文件面）"""
    global _mem, _mem_mtime
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix(".tmp")
    _fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(_fd, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps(d, ensure_ascii=False))
    os.replace(tmp, _STORE)
    _mem = d
    try:
        _mem_mtime = os.path.getmtime(_STORE)
    except OSError:
        _mem_mtime = -1.0


def _sweep_expired(d: dict, now: float) -> dict:
    """惰性清理过期条目（与 pending_ops 同手法）"""
    live = {t: r for t, r in d.items() if now - r["ts"] <= _TTL_SECONDS}
    for t in set(d) - set(live):
        logger.info("结算回执：%s 已过期清除（%s）", t[:10], d[t].get("status"))
    return live


def record_settlement(token: str, status: str, result: str = "") -> None:
    """结算端回执写入（status: approved / approved_failed / rejected / error）。

    全兜底：任何异常吞掉只打日志——回执链路故障绝不阻断真实结算
    （结算已在调用方完成，本函数只是"顺路告知"等待中的 MCP 进程）。
    """
    try:
        with FileLock(_STORE.with_suffix(".lock")):
            d = _sweep_expired(_load(), time.time())
            d[token] = {"status": status, "result": str(result)[:2000],
                        "ts": time.time()}
            _save(d)
        logger.info("结算回执：%s → %s", token[:10], status)  # token 只记指纹
    except Exception as e:
        logger.warning("结算回执写入失败（结算本身不受影响，MCP 侧将超时回退）: %s", e)


def wait_settlement(token: str, timeout: float, poll: float = 0.5) -> dict | None:
    """阻塞轮询等待回执（MCP server 工作线程用）。

    peek 语义：读到不删（靠 TTL 清扫）——dedup 场景多个等待者共用同一
    token 时都能收到。返回 {"status": str, "result": str} 或 None（超时/
    回执已过期——过期回执视同无回执，fail-closed：等待方迟到不改变
    "未执行"语义）。
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        rec = _load().get(token)
        if rec is not None:
            if time.time() - rec.get("ts", 0) <= _TTL_SECONDS:
                return {"status": rec.get("status", ""),
                        "result": rec.get("result", "")}
            return None  # 回执过期：视同无回执
        if time.time() >= deadline:
            return None
        time.sleep(poll)
