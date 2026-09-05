"""高危操作待批准挂起表（文件持久化版，20260822 重构）

背景：图编排通道已下线，高危人审闸在 MCP 通道走本模块的挂起表回执链路：

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
import hashlib as _hashlib
import hmac as _hmac
import json
from core.logger import get_logger
import os
import time
import uuid
from pathlib import Path

from core.crypto.key_manager import get_signing_key
from core.file_contract import FileLock

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
    """原子写挂起表（tmp + replace，防半写）；
    落盘 0600 收紧（含操作载荷的挂起表与 daemon.json/JsonContract 同标准）"""
    global _mem, _mem_mtime
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix(".tmp")
    # tmp 创建即 0600（os.open 终态权限——tmp 本体的默认权限窗口也归零）
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
    """惰性清理过期条目"""
    live = {t: op for t, op in d.items() if now - op["ts"] <= _TTL_SECONDS}
    for t in set(d) - set(live):
        logger.info("挂起表：token %s 已过期清除（%s）", t, d[t]["name"])
    return live


def register_pending_dedup(name: str, kwargs: dict, impact: str) -> tuple:
    """原子查重登记（find+register 同一 FileLock 临界区——并发下同风险
    不得双登记双卡双批双执行；TOCTOU 窗口为零）。
    返回 (token, is_dup)：is_dup=True 时返回已在队列中的 token（不新建）。"""
    with FileLock(_STORE.with_suffix(".lock")):
        d = _sweep_expired(_load(), time.time())
        fp = json.dumps(kwargs, ensure_ascii=False, sort_keys=True, default=str)
        for t, op in d.items():
            if op["name"] != name:
                continue
            if json.dumps(op.get("kwargs", {}), ensure_ascii=False,
                          sort_keys=True, default=str) == fp:
                return t, True
        token = "P-" + uuid.uuid4().hex
        op = {"name": name, "kwargs": dict(kwargs), "impact": impact,
              "ts": time.time()}
        op["sig"] = _sign_op(op)
        d[token] = op
        _save(d)
        logger.info("挂起表：登记 %s → %s…（影响面 %d 字）", name, token[:10], len(impact))
        return token, False


def _op_payload(op: dict) -> bytes:
    """挂起条目签名载荷（canonical JSON：键序/分隔符固定，跨进程可复算；
    "pending:" 域分离前缀——与提权契约签名（"esc:"）消息空间硬隔离）"""
    return b"pending:" + json.dumps(
        {"name": op.get("name", ""), "kwargs": op.get("kwargs", {}),
         "impact": op.get("impact", ""), "ts": op.get("ts", 0)},
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sign_op(op: dict) -> str:
    """挂起条目 HMAC-SHA256 签名（签名钥与提权契约同通道——
    同用户进程篡改在途挂起的 kwargs/impact 会在结算验签时拒认）"""
    return _hmac.new(get_signing_key().encode("utf-8"), _op_payload(op),
                     _hashlib.sha256).hexdigest()


def _verify_op(op: dict) -> bool:
    """验签（常量时间比较；无签/签错=伪造或被篡改，结算一律拒认 fail-closed）"""
    sig = op.get("sig", "")
    if not sig:
        return False
    try:
        return _hmac.compare_digest(sig, _sign_op(op))
    except Exception:
        return False


def register_pending(name: str, kwargs: dict, impact: str) -> str:
    """登记待批准操作，返回一次性 token（token 只用于管理端结算，不回传 AI）。

    登记也在同一互斥锁内：否则管理端 pop 结算与 MCP 登记并发时，
    已结算 token 可被登记方的整存回写复活，TTL 内构成双执行面。"""
    with FileLock(_STORE.with_suffix(".lock")):
        token = "P-" + uuid.uuid4().hex  # 全 128bit 熵（[:8] 仅 32bit）
        d = _sweep_expired(_load(), time.time())
        op = {"name": name, "kwargs": dict(kwargs), "impact": impact,
              "ts": time.time()}
        op["sig"] = _sign_op(op)  # 落盘即签：结算端验签拒认篡改/伪造条目
        d[token] = op
        _save(d)
    logger.info("挂起表：登记 %s → %s…（影响面 %d 字）", name, token[:10], len(impact))  # token 只记前 10 位指纹——明文不落日志
    return token


def peek_pending(token: str) -> dict | None:
    """非破坏性读取（不销毁 token）——用于结算前的类型分流判定
    （如 __escalate__ 须先认出再走专属端点，误走 settle 不得烧毁 token）"""
    with FileLock(_STORE.with_suffix(".lock")):
        d = _sweep_expired(_load(), time.time())
        _save(d)
        return d.get(token)


def pop_pending(token: str) -> dict | None:
    """一次性取出并销毁（批准/拒绝都走这里；不存在或过期返回 None）。

    跨进程互斥：读改写临界区进 FileLock
    （锁内写 PID，持有者死亡自动回收）——并发结算同一 token 不再双执行。"""
    with FileLock(_STORE.with_suffix(".lock")):
        now = time.time()
        d = _sweep_expired(_load(), now)  # 先清扫（过期 token 不得结算——
        # 旧顺序先 pop 后 sweep，"防迟来确认"承诺形同虚设，实跑复现）
        op = d.pop(token, None)
        _save(d)
        if op is not None and not _verify_op(op):
            # 验签失败（落盘后遭篡改/伪造）：结算拒认——批准对象≠执行对象的
            # 注入面在落盘层封死（与提权契约同签名通道）。token 已焚不可重试。
            logger.warning("挂起表：token %s 验签失败（疑似篡改/伪造），结算拒认", token[:10])
            return None
        return op


def list_pending() -> list[dict]:
    """列出当前待批准项（管理端审批中心用；含 token/操作/影响面/剩余秒数）。
    验签失败（篡改/伪造）的条目不出示——结算端本就拒认，展示层同步过滤
    防诱批噪声"""
    now = time.time()
    d = _sweep_expired(_load(), now)
    return [{"token": t, "name": op["name"], "impact": op.get("impact", ""),
             "age_seconds": int(now - op["ts"]),
             "ttl_remaining": int(_TTL_SECONDS - (now - op["ts"]))}
            for t, op in sorted(d.items(), key=lambda kv: kv[1]["ts"])
            if _verify_op(op)]


def pending_count() -> int:
    """当前挂起数（观测/测试用）"""
    return len(_sweep_expired(_load(), time.time()))
