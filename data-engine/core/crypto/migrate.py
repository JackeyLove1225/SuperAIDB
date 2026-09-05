"""明文检测与自动迁移（接入即加密的执行点）

检测：明文 SQLite 以 `SQLite format 3\0` 文件头开头，SQLCipher 密文为随机字节。
迁移：WAL 先 checkpoint 合并（busy 重试，持续占用即拒绝迁移不硬来）→
明文备份（默认迁移验证通过后删除——明文副本永久留存会让"静态加密"形同虚设；
需要留存排障时设环境变量 MIGRATE_KEEP_PLAIN_BACKUP=1）→ sqlcipher_export 到
临时密文库 → 新库可读验证 → 原子替换 → 清理 -wal/-shm 残片。
全程失败即回滚，不留半迁移状态。
"""
import logging
import os
import shutil
import time

logger = logging.getLogger(__name__)

_PLAIN_HEADER = b"SQLite format 3\x00"


def is_plaintext_sqlite(path: str) -> bool:
    """是否明文 SQLite 文件（不存在/空文件视为非明文——将由工厂直接建密文库）"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return len(head) > 0 and head.startswith(_PLAIN_HEADER)


def _checkpoint_wal(path: str) -> None:
    """WAL checkpoint 合并并校验 busy 状态（持续被占用则拒绝迁移）

    wal_checkpoint(TRUNCATE) 返回 (busy, log_frames, checkpointed_frames)——
    必须读返回值：别的连接持库时 busy=1 静默跳过，export 只导主文件，
    WAL 尾部数据永久丢失。
    """
    wal = str(path) + "-wal"
    if not os.path.exists(wal):
        return
    import sqlite3
    for attempt in range(5):
        c = sqlite3.connect(path)
        try:
            busy = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        finally:
            c.close()
        if busy == 0:
            return
        time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(
        f"数据库 {os.path.basename(path)} 的 WAL 被其他连接持续占用，无法安全迁移。"
        "请关闭所有占用该库的程序后重试（数据未做任何改动）")


def migrate_to_encrypted(path: str, key: str) -> None:
    """明文库原地迁移为密文库（sqlcipher_export 配方）。

    步骤：WAL checkpoint（busy 校验）→ .plain.bak 备份 → export 到临时库 →
    新库可读验证 → 原子替换 → 清理 WAL 残片 → 默认删除明文备份
    （MIGRATE_KEEP_PLAIN_BACKUP=1 可保留）。任何一步失败都回滚。
    """
    from sqlcipher3 import dbapi2

    _checkpoint_wal(path)

    bak = str(path) + ".plain.bak"
    tmp = str(path) + ".enc_tmp"
    try:
        shutil.copy2(path, bak)
        src = dbapi2.connect(path)  # 不 PRAGMA key = 明文模式
        _tmp_q = tmp.replace("'", "''")  # 路径引号倍增（与 attached.py 同型）
        src.execute(f"ATTACH DATABASE '{_tmp_q}' AS encrypted KEY '{key}'")
        src.execute("SELECT sqlcipher_export('encrypted')")
        src.execute("DETACH DATABASE encrypted")
        src.close()

        # 新库可读验证通过才替换（防半迁移库上线）
        verify = dbapi2.connect(tmp)
        verify.execute(f"PRAGMA key = '{key}'")
        verify.execute("SELECT count(*) FROM sqlite_master").fetchone()
        verify.close()

        os.replace(tmp, path)
        # WAL 残片清理（明文 -wal/-shm 留在密文库旁=泄漏面）
        for suffix in ("-wal", "-shm"):
            leftover = str(path) + suffix
            if os.path.exists(leftover):
                os.remove(leftover)
        logger.info("数据库已迁移为加密存储: %s（明文备份留存 %s）", os.path.basename(path),
                    os.path.basename(bak))
        if os.environ.get("MIGRATE_KEEP_PLAIN_BACKUP") != "1":
            os.remove(bak)
            logger.info("明文备份已按默认策略删除（MIGRATE_KEEP_PLAIN_BACKUP=1 可保留）")
    except Exception:
        # 失败回滚：临时库清掉，原文件不动（备份仍在就还在）
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def migrate_to_plaintext(path: str, key: str) -> None:
    """密文库回退为明文库（DB_ENCRYPT 逃生门的回程票）。

    与正向迁移同配方反方向：WAL 合并（busy 校验）→ 密文读 → export 明文临时库
    → 验证 → 原子替换 → 密文备份留存（.enc.bak）。失败即回滚。
    """
    from sqlcipher3 import dbapi2

    _checkpoint_wal(path)

    bak = str(path) + ".enc.bak"
    tmp = str(path) + ".plain_tmp"
    try:
        shutil.copy2(path, bak)
        src = dbapi2.connect(path)
        src.execute(f"PRAGMA key = '{key}'")
        # 不 KEY 的 ATTACH = 明文目标库
        _tmp_q = tmp.replace("'", "''")  # 路径引号倍增（与正向迁移同型）
        src.execute(f"ATTACH DATABASE '{_tmp_q}' AS plaintext KEY ''")
        src.execute("SELECT sqlcipher_export('plaintext')")
        src.execute("DETACH DATABASE plaintext")
        src.close()

        import sqlite3
        verify = sqlite3.connect(tmp)
        verify.execute("SELECT count(*) FROM sqlite_master").fetchone()
        verify.close()

        os.replace(tmp, path)
        for suffix in ("-wal", "-shm"):
            leftover = str(path) + suffix
            if os.path.exists(leftover):
                os.remove(leftover)
        logger.info("数据库已回退为明文存储: %s（密文备份留存 %s）", os.path.basename(path),
                    os.path.basename(bak))
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
