"""ConfigHub——配置类状态的唯一读写通道（真解耦地基）

判据：任意进程重启或新增，无需其他进程配合，下一次操作必然拿到正确状态。

读（load_yaml）：
- mtime 键缓存——文件没变走内存（零读放大），变了原子重读
- 解析失败按域定策略（fail_policy）：
  - "closed"（权限域默认）：抛 AppError，宁可全禁不读错配置
  - "last_good"（其他域默认）：用最近一次好值 + 大字告警，保可用

写（write_yaml_atomic）：
- tmp 文件 + os.replace 原子替换（杜绝写一半）
- 写前可传 validate 回调试载，失败拒写并保留原文件
- 默认写前备份到 <dir>/backups/<name>_<时间戳>.yml

设计：进程无配置副本，文件即契约；写落盘即全域生效，不需要任何广播。
"""
from core.logger import get_logger
import os
import shutil
import time
from pathlib import Path

import yaml

from core.exceptions import AppError

logger = get_logger(__name__)

# {path_str: {"mtime": float, "data": object}}
_CACHE: dict = {}


def load_yaml(path, default=None, fail_policy: str = "last_good"):
    """读取 YAML（mtime 新鲜度缓存）

    Args:
        path: 文件路径
        default: 文件不存在时返回的值（默认 {}）
        fail_policy: 解析失败策略——"closed" 抛 AppError；"last_good" 用最近好值
    """
    p = Path(path)
    key = str(p)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _CACHE.pop(key, None)
        return default if default is not None else {}

    hit = _CACHE.get(key)
    if hit and hit["mtime"] == mtime:
        return hit["data"]

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        if data is None:
            data = default if default is not None else {}
    except Exception as e:
        if fail_policy == "closed":
            logger.error("配置文件解析失败（fail-closed）: %s: %s", p, e)
            raise AppError(f"配置文件损坏: {p.name}: {e}")
        if hit:
            logger.warning("配置文件解析失败，沿用 last_good: %s: %s", p, e)
            return hit["data"]
        logger.warning("配置文件解析失败且无历史好值，用默认: %s: %s", p, e)
        return default if default is not None else {}

    _CACHE[key] = {"mtime": mtime, "data": data}
    return data


_write_lock = __import__("threading").Lock()


def write_yaml_atomic(path, data, validate=None, backup: bool = True):
    """原子写 YAML（唯一写入通道）

    进程内写串行化 + tmp 名带唯一后缀（固定 tmp 名在两写者
    交错时结果反转——A 的变更静默丢失且返回成功，B 报错但其内容已落盘）。
    跨进程互斥由 FileLock 承担（与 JsonContract 同型）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if validate:
        validate(data)  # 抛异常即拒写
    if backup and p.exists():
        bdir = p.parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, bdir / f"{p.stem}_{time.strftime('%Y%m%d_%H%M%S')}{p.suffix}")
    from core.file_contract import FileLock
    with _write_lock, FileLock(p.with_suffix(p.suffix + ".lock")):
        tmp = p.with_suffix(p.suffix + f".{os.getpid()}.{id(data):x}.tmp")
        tmp.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, p)
        # 写入后让本进程缓存立即反映新值（其他进程靠 mtime 自然发现）
        try:
            _CACHE[str(p)] = {"mtime": p.stat().st_mtime, "data": data}
        except OSError:
            _CACHE.pop(str(p), None)


def write_text_atomic(path, content: str, backup: bool = False):
    """原子写纯文本（.env 等非 YAML 文件的统一写入通道）。

    权限不降级：目标已存在时沿用其权限位（0600 的秘密文件不会被
    tmp+replace 静默放宽成 0644）；新建默认 0600。
    进程内写串行化 + 跨进程 FileLock + tmp 名带唯一后缀（与 write_yaml_atomic 同型）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if backup and p.exists():
        bdir = p.parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, bdir / f"{p.stem}_{time.strftime('%Y%m%d_%H%M%S')}{p.suffix}")
    from core.file_contract import FileLock
    with _write_lock, FileLock(p.with_suffix(p.suffix + ".lock")):
        tmp = p.with_suffix(p.suffix + f".{os.getpid()}.{id(content):x}.tmp")
        try:
            mode = (p.stat().st_mode & 0o777) if p.exists() else 0o600
        except OSError:
            mode = 0o600
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, p)


def invalidate(path=None):
    """主动失效缓存（测试/特殊场景用；正常流程靠 mtime 自发现，不需要它）"""
    if path is None:
        _CACHE.clear()
    else:
        _CACHE.pop(str(Path(path)), None)
