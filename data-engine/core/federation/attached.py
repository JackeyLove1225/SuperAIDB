"""跨 SQLite 数据源的单连接挂载写（真原子）——ATTACH 方案

全 SQLite 写组的真原子路径：主连接（主表所在数据源）上 ATTACH 其余库，
savepoint 覆盖全部挂载文件——ROLLBACK TO SAVEPOINT 即跨文件真回滚，
无需 saga 补偿（含 MySQL 等跨引擎组仍走 saga 补偿，物理上无共享事务）。

原子性边界（如实声明，SQLite 官方文档语义）：
- 异常路径（业务错误/约束失败）：ROLLBACK TO SAVEPOINT 跨文件真回滚，零残留
- WAL 模式下宿主进程崩溃于 COMMIT 间隙：各挂载库独立 WAL、无 super-journal，
  SQLite 逐库分别提交——崩溃可留下"一库已落、他库未落"的部分提交集合。
  该部分提交状态在单库事务中不存在，相对单库事务是真实放大的毫秒级窗口；
  需要更强保证时该组应改走 journal_mode=DELETE 的单机路径（当前未启用）
- saga 补偿路径对该窗口有 journal+启动续滚兜底；本路径不兜底——
  窗口期崩溃留下的部分提交以上述声明为准，不伪装不存在

密钥域：产品默认 DB_ENCRYPT=true，全部库经 open_db 统一迁移为同一主密钥
密文；ATTACH 用同一主密钥打开（per-attach KEY 通道）。
"""
from core.contract.security_contract import is_valid_identifier
from core.logger import get_logger

logger = get_logger(__name__)


def alias_for(ds_name: str) -> str:
    """数据源名 → ATTACH 别名（标识符校验后加前缀，绝不拼接未校验名）"""
    if not is_valid_identifier(ds_name):
        raise ValueError(f"非法数据源名（不可作 ATTACH 别名）: {ds_name}")
    return f"att_{ds_name}"


def open_attached(base_path: str, others: list):
    """打开主库连接并挂载其余 SQLite 库。

    Args:
        base_path: 主连接库文件（主表所在数据源）
        others: [(alias, db_path)]——已按 alias_for 生成的别名与库路径

    Returns:
        sqlite3.Connection（已挂载全部 others；调用方负责 close）
    """
    from core.crypto.connection import open_db
    from core.crypto.key_manager import encryption_enabled, get_db_key
    from core.crypto.migrate import is_plaintext_sqlite, migrate_to_encrypted

    conn = open_db(base_path)  # 加密边界唯一入口（明文库自动迁移为密文）
    conn.execute("PRAGMA foreign_keys=ON")  # 与驱动连接同口径（FK 强制）
    try:
        if encryption_enabled():
            key = get_db_key()
            safe_key = key.replace("'", "''")  # token_urlsafe 字符集，双保险转义
            for alias, path in others:
                if is_plaintext_sqlite(path):
                    migrate_to_encrypted(path, key)  # 统一密钥域
                # alias 已过标识符校验（is_valid_identifier），KEY 为密钥域内密钥
                conn.execute(f"ATTACH DATABASE '{path.replace(chr(39), chr(39)*2)}' "
                             f"AS {alias} KEY '{safe_key}'")
        else:
            for alias, path in others:
                conn.execute(f"ATTACH DATABASE '{path.replace(chr(39), chr(39)*2)}' AS {alias}")
    except Exception:
        # 挂载中途失败：主连接必须立刻关闭（本项目纪律：Windows 文件锁不靠 GC 兜底；
        # migrate 的 WAL-busy 拒绝是现实触发路径，泄漏会长期占用库文件句柄）
        try:
            conn.close()
        except Exception:
            pass
        raise
    logger.info("挂载写连接就绪: 主库 + %d 个 ATTACH（全 SQLite 组真原子路径）", len(others))
    return conn
