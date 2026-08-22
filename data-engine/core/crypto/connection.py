"""统一数据库连接工厂（open_db）——全仓唯一连接入口，加密边界在此收口

语义：
- DB_ENCRYPT=false：原生 sqlite3.connect（现状行为，向后兼容）
- DB_ENCRYPT=true（默认）：明文库 → 自动迁移为密文（接入即加密）；
  密文库/新建库 → SQLCipher + keyring 密钥打开
- 返回值与 sqlite3.Connection API 一致（sqlcipher3 dbapi2 全兼容），
  调用方（驱动/元库/认证/测试工具）零感知

参数转发：timeout/check_same_thread 等 sqlite3.connect 原样参数原样透传。
"""
from core.logger import get_logger
import os
import sqlite3

logger = get_logger(__name__)


def compat_row_factory():
    """与当前加密模式匹配的 Row 工厂（按名+按下标双访问）。

    sqlite3.Row 只认 sqlite3.Cursor，SQLCipher 连接必须配 dbapi2.Row——
    需要按列名访问结果的调用方一律用本工厂，不许直接 import sqlite3.Row。
    """
    from core.crypto.key_manager import encryption_enabled
    if encryption_enabled():
        from sqlcipher3 import dbapi2
        return dbapi2.Row
    return sqlite3.Row


def open_db(path: str, **kwargs):
    """打开数据库连接（加密边界唯一入口）。语义见模块 docstring。"""
    from core.crypto.key_manager import encryption_enabled, get_db_key
    from core.crypto.migrate import is_plaintext_sqlite
    if not encryption_enabled():
        if os.path.exists(path) and os.path.getsize(path) > 0 \
                and not is_plaintext_sqlite(path):
            # OFF 开关撞上密文库：fail-fast 给明确指引（不静默产出乱码错误）
            raise RuntimeError(
                f"数据库 {path} 是加密存储，但 DB_ENCRYPT=false（明文模式）无法打开。"
                "请设回 DB_ENCRYPT=true，或先用 python -c "
                "\"from core.crypto.migrate import migrate_to_plaintext; "
                "from core.crypto.key_manager import get_db_key; "
                f"migrate_to_plaintext(r'{path}', get_db_key())\" 回退为明文")
        return sqlite3.connect(path, **kwargs)

    key = get_db_key()  # keyring 不可用在此 fail-closed（带逃生门提示）
    from core.crypto.migrate import migrate_to_encrypted
    if is_plaintext_sqlite(path):
        migrate_to_encrypted(path, key)

    from sqlcipher3 import dbapi2
    conn = dbapi2.connect(path, **kwargs)
    conn.execute(f"PRAGMA key = '{key}'")  # key 出自 secrets.token_urlsafe，字符集安全
    return conn
