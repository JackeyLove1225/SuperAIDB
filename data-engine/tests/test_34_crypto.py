"""层 34：数据库加密边界（core/crypto）——密文形态/无 key 拒读/迁移一致/开关行为

判据：加密边界是物理事实而非配置声明——文件必须是乱码，无 key 必须打不开。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_crypto_end_to_end():
    """加密全链路：明文库 → open_db 自动迁移 → 密文乱码 → 无 key 拒读 → 数据一致"""
    if os.environ.get("DB_ENCRYPT", "").lower() == "false":
        print("SKIP - 强制 OFF 模式（双模式回归的 OFF 半边由其他层覆盖）")
        return
    from core.crypto.connection import open_db
    from core.crypto.key_manager import encryption_enabled, get_db_key
    from core.crypto.migrate import is_plaintext_sqlite
    assert encryption_enabled(), "默认应开启加密（DB_ENCRYPT 默认 true）"
    key = get_db_key()
    assert key, "keyring 应能取到主密钥"

    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "t_enc.db")
        # 造明文库（原生 sqlite3，模拟历史库/外部接入库）
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, v TEXT)")
        c.executemany("INSERT INTO t1 VALUES (?,?)", [(1, "甲"), (2, "乙")])
        c.commit()
        c.close()
        assert is_plaintext_sqlite(p), "刚建的库应是明文"

        # open_db：接入即加密（自动迁移）
        conn = open_db(p)
        rows = conn.execute("SELECT * FROM t1 ORDER BY id").fetchall()
        conn.close()
        assert rows == [(1, "甲"), (2, "乙")], f"迁移后数据必须一致: {rows}"
        assert not is_plaintext_sqlite(p), "迁移后不应再是明文"
        head = open(p, "rb").read(16)
        assert not head.startswith(b"SQLite format 3"), "文件头必须是密文乱码"
        # 明文备份默认在迁移验证通过后删除（明文副本永久留存=静态加密形同虚设）；
        # 需要留存排障时 MIGRATE_KEEP_PLAIN_BACKUP=1
        assert not os.path.exists(p + ".plain.bak"), "明文备份默认应删除（防泄漏）"

        # 无 key 拒读（原生 sqlite3 直接打开密文库）
        c2 = sqlite3.connect(p)
        try:
            c2.execute("SELECT * FROM t1").fetchall()
            raise SystemExit("原生 sqlite3 竟能读密文库——加密失效！")
        except sqlite3.DatabaseError:
            pass  # "file is not a database" = 正确拒绝
        finally:
            c2.close()

        # 错 key 拒读
        from sqlcipher3 import dbapi2
        c3 = dbapi2.connect(p)
        c3.execute("PRAGMA key = 'wrong-key'")
        try:
            c3.execute("SELECT * FROM t1").fetchall()
            raise SystemExit("错 key 竟能读——加密失效！")
        except Exception:
            pass
        finally:
            c3.close()

        # 幂等：密文库再 open_db 不应重复迁移。
        # 探针直接盯住迁移函数本身——不能用 mtime 当代理（回滚日志模式下任何
        # INSERT+commit 都会写主文件刷新 mtime，与是否迁移无关，实测误报）
        from core.crypto import migrate as _mig
        _mig_calls = []
        _orig_migrate = _mig.migrate_to_encrypted
        def _spy(*a, **k):
            _mig_calls.append(a)
            return _orig_migrate(*a, **k)
        _mig.migrate_to_encrypted = _spy
        try:
            conn = open_db(p)
            conn.execute("INSERT INTO t1 VALUES (3, '丙')")
            conn.commit()
            conn.close()
        finally:
            _mig.migrate_to_encrypted = _orig_migrate
        assert not _mig_calls, "密文库重开不应重复迁移"
        conn = open_db(p)
        n = conn.execute("SELECT count(*) FROM t1").fetchone()[0]
        conn.close()
        assert n == 3, f"加密库持续读写正常: {n}"
    print("OK - 加密全链路：明文→自动迁移→乱码→无key/错key拒读→数据一致→幂等")


def test_encryption_switch_off():
    """DB_ENCRYPT=false：open_db 退回原生 sqlite3，行为与现状一致（明文）"""
    from config.settings import settings
    orig = settings._db_encrypt_override
    settings._db_encrypt_override = "false"
    try:
        from core.crypto import key_manager as km
        # 热键语义：override 立即生效
        assert not km.encryption_enabled(), "override=false 应关闭加密"
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "t_plain.db")
            from core.crypto.connection import open_db
            conn = open_db(p)
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES ('x')")
            conn.commit()
            conn.close()
            # 原生 sqlite3 可读 = 明文（关闭态行为与现状一致）
            c = sqlite3.connect(p)
            assert c.execute("SELECT * FROM t").fetchall() == [("x",)]
            c.close()
    finally:
        settings._db_encrypt_override = orig
    print("OK - 开关行为：DB_ENCRYPT=false 退回明文原生通道")


def test_isolated_keyfile_backend():
    """系统级隔离模式的密钥文件后端：isolated.flag 在 → 密钥走 db/.vault/master.key
    （凭据管理器是 per-user 的，服务账号读不到操作者 vault——隔离模式必须换后端）"""
    import importlib
    import tempfile
    from pathlib import Path
    flag = Path("config/runtime/isolated.flag")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SUPERAIDB_VAULT_DIR"] = tmp
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        try:
            from core.crypto import key_manager as km
            importlib.reload(km)  # 模块级路径常量重读（isolated.flag 状态是调用期判定，双保险）
            k1 = km.get_db_key()
            assert k1 and (Path(tmp) / "master.key").exists(), "隔离模式应生成密钥文件"
            assert km.get_db_key() == k1, "重读应得同一密钥（快路径无锁读文件）"
        finally:
            flag.unlink(missing_ok=True)
            os.environ.pop("SUPERAIDB_VAULT_DIR", None)
    print("OK - 隔离模式密钥文件后端：生成/重读一致")


if __name__ == "__main__":
    test_crypto_end_to_end()
    test_encryption_switch_off()
    test_isolated_keyfile_backend()
    print("\n=== ALL CRYPTO TESTS PASSED ===")
