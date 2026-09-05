"""磁盘增长策略——保留策略集中定义，启动时执行

原则：
- 保留策略白盒集中（RETENTION），调整只改这里
- 只清理可再生/日志类产物（exports、批次原始文本、解析缓存、对话日志、
  预览缓存、过期 saga journal）；不碰数据库文件、chroma、backups、当前工作文件
- 清理动作逐项记日志，失败不阻塞启动
"""
from core.logger import get_logger
import time
from pathlib import Path

logger = get_logger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# 保留策略（白盒集中）：
#   keep_latest=N    只保留最新 N 个匹配项（按 mtime）
#   max_age_days=N   删除 mtime 早于 N 天的匹配项
#   dirs=True        匹配对象包含目录（递归删除）
RETENTION = [
    # 导出文件：保留最新 20 个
    {"path": "exports", "pattern": "*", "keep_latest": 20},
    # 入库批次原始文本（可重生成）：保留 7 天
    {"path": "uploads", "pattern": "batch_*", "max_age_days": 7, "dirs": True},
    # 预览缓存（可重生成）：保留 7 天
    {"path": "cache/preview", "pattern": "*", "max_age_days": 7, "dirs": True},
    # 解析缓存（可重生成）：保留 30 天
    {"path": "db/parser_cache", "pattern": "*.json", "max_age_days": 30},
    # 对话日志：保留 30 天
    {"path": "db/json", "pattern": "conversation_*.json", "max_age_days": 30},
    {"path": "db/md", "pattern": "conversation_*.md", "max_age_days": 30},
    # saga journal：成功份由 saga.py 自留 20 份；这里清 30 天前的陈旧 journal
    {"path": "db/saga_journal", "pattern": "saga_*.json", "max_age_days": 30},
    # 服务日志：轮转归档（.1）保留 14 天（活动日志由 launcher 启动时按 10MB 滚动）
    {"path": "logs", "pattern": "*.log.1", "max_age_days": 14},
    # 配置备份（write_text_atomic backup=True 产生，可重生成）：保留最新 50 个
    {"path": "config/backups", "pattern": "*", "keep_latest": 50},
]


def _apply_rule(rule: dict) -> int:
    """执行单条保留规则，返回清理的条目数"""
    base = _ROOT / rule["path"]
    if not base.is_dir():
        return 0
    removed = 0
    now = time.time()

    if "keep_latest" in rule:
        items = [p for p in base.glob(rule["pattern"])
                 if p.is_file() or (rule.get("dirs") and p.is_dir())]
        items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in items[rule["keep_latest"]:]:
            try:
                if p.is_dir():
                    import shutil
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
                removed += 1
            except OSError as e:
                logger.warning("磁盘清理失败 %s: %s", p, e)

    if "max_age_days" in rule:
        cutoff = now - rule["max_age_days"] * 86400
        for p in base.glob(rule["pattern"]):
            try:
                if rule.get("dirs") and p.is_dir():
                    if p.stat().st_mtime < cutoff:
                        import shutil
                        shutil.rmtree(p, ignore_errors=True)
                        removed += 1
                elif p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError as e:
                logger.warning("磁盘清理失败 %s: %s", p, e)
    return removed


def run_disk_maintenance() -> dict:
    """启动时执行全部保留规则，返回 {规则: 清理数} 汇总"""
    summary = {}
    for rule in RETENTION:
        try:
            n = _apply_rule(rule)
        except Exception as e:
            logger.warning("磁盘清理规则执行失败 %s: %s", rule["path"], e)
            n = 0
        if n:
            summary[rule["path"]] = n
            logger.info("磁盘清理: %s 清理 %d 项", rule["path"], n)
    if not summary:
        logger.info("磁盘清理: 无需清理")
    return summary
