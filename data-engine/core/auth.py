"""用户认证模块——多用户支持的核心

轻量级实现，不依赖第三方库（无 bcrypt / PyJWT）：
- 密码哈希：hashlib.pbkdf2_hmac + 随机 salt
- Token：HMAC-SHA256 签名的 base64 编码（兼容 JWT 思路，自实现）
- 用户存储：SQLite users 表

用户角色：
- admin：管理员，可访问所有功能和用户管理
- user：普通用户，可访问自己的数据
- readonly：只读用户，只能查询不能修改
"""
import re as _re
import base64

import os
import hmac
from core.crypto.connection import open_db, compat_row_factory
import json
import time
import hashlib
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from config.settings import settings


# Token 有效期（秒）：默认 24 小时
TOKEN_TTL = 86400

# 密码哈希迭代次数
PBKDF2_ITERATIONS = 600000  # OWASP 对 SHA-256 的现行建议量级
_LEGACY_ITERATIONS = 100000  # 旧哈希兼容档：验证回落 + 登录成功即透明重哈希升级

# 速率限制：每 IP 每分钟最大请求数
RATE_LIMIT_PER_MINUTE = 60


def _get_db_path() -> str:
    """获取 SQLite 数据库路径"""
    path = settings.SQLITE_DB_PATH
    if not os.path.isabs(path):
        path = os.path.join(str(Path(__file__).resolve().parent.parent), path)
    return path


# verify_token 高频路径：认证库连接每线程缓存——open_db 的密钥
# 派生每次 ~150ms，每请求重开是管理端延迟大头。只缓存连接，绝不缓存验证
# 结果：role/token_version 每调仍实时查库，降级/吊销即时生效的安全特性不动。
_conns: dict = {}
import threading as _threading
_conns_lock = _threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """获取 SQLite 连接（每线程缓存 + 死线程清扫 + 库路径感知）

    - 线程死了而连接未清，ident 复用后新线程会拿到死线程创建的连接
      （sqlite3 check_same_thread 触发 ProgrammingError）——取用前存活清扫。
    - 库路径切换（行业切换/测试隔离）时旧连接关闭重开——否则角色/
      token_version 读到旧库（正确性问题，非仅测试卫生）。
    """
    ident = _threading.get_ident()
    path = _get_db_path()
    with _conns_lock:
        for old_ident, old_entry in list(_conns.items()):
            if old_ident != ident and not any(
                    t.ident == old_ident for t in _threading.enumerate()):
                try:
                    old_entry[1].close()
                except Exception:
                    pass  # 死连接关闭失败无碍——OS 进程退出时回收
                _conns.pop(old_ident, None)
        entry = _conns.get(ident)
        if entry is not None and entry[0] != path:
            _conns.pop(ident, None)
            try:
                entry[1].close()
            except Exception:
                pass  # 旧库连接关闭失败无碍——路径已变，绝不复用
            entry = None
    if entry is not None:
        return entry[1]
    conn = open_db(path)
    conn.row_factory = compat_row_factory()
    with _conns_lock:
        _conns[ident] = (path, conn)
    return conn


def _close_all_conns() -> None:
    """关闭本模块全部缓存连接（测试隔离/库文件替换前显式释放——
    Windows 下持有连接即锁文件，临时库/还原替换场景必须先释放）"""
    with _conns_lock:
        entries = list(_conns.values())
        _conns.clear()
    for _, conn in entries:
        try:
            conn.close()
        except Exception:
            pass  # 单个关闭失败不阻断其余释放


def _release_conn(conn) -> None:
    """释放认证库连接——每线程缓存模式下是刻意 no-op：
    连接随线程存续复用（线程死亡时由 _get_conn 的存活清扫关闭）。
    调用方保持"获取-使用-释放"成对写法，语义不因缓存漂移。"""


def init_users_table():
    """初始化 users 表（如不存在则创建）；token_version 列增量迁移"""
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_login TEXT
            )
        """)
        # token_version 增量迁移（token 吊销的载体：logout/改密即全员旧 token 失效）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "token_version" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        # 创建默认管理员（如不存在）——随机生成初始密码，避免硬编码弱口令。
        # 密码只写 config/runtime/initial_admin.txt（用户私有目录，与 .env 同级保护），
        # 不打进 stdout/日志文件（启动器会把 stdout 收进 logs/——密码进日志=持久泄漏面）。
        cursor = conn.execute("SELECT COUNT(*) as c FROM users WHERE username = ?", ("admin",))
        if cursor.fetchone()["c"] == 0:
            admin_password = secrets.token_urlsafe(12)  # 16 位随机密码
            _create_user_internal(conn, "admin", admin_password, "admin")
            try:
                pw_file = Path(__file__).resolve().parent.parent / "config" / "runtime" / "initial_admin.txt"
                pw_file.parent.mkdir(parents=True, exist_ok=True)
                # 创建即终态权限（os.open 0600——写后收紧的微窗口归零）
                _fd = os.open(str(pw_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                    _f.write(
                        f"初始管理员账号: admin\n初始密码: {admin_password}\n"
                        "请立即登录并修改密码，修改后手动删除本文件。\n")
                # 走 logger 而非 print：MCP 进程（SUPERAIDB_ROLE=mcp）日志一律
                # stderr——stdout 是 JSON-RPC 协议通道，print 落 stdout 即毒死
                # MCP 会话（首启建管理员时必现，客户端 reader 解析非 JSON 行即崩）
                from core.logger import get_logger
                get_logger(__name__).info(
                    "初始管理员已创建，密码见 config/runtime/initial_admin.txt")
            except OSError as e:
                # 回退面不落密码：runtime/ 写不了多半是权限/盘问题——
                # 密码打控制台会被启动器收进 logs/，且 logs 不在文件收容闸
                # 黑名单内，可经 process_file 进向量库被 AI 检索——回退面恰好落在
                # 两道闸都不罩的位置）。宁可启动失败，也不让密码换载体出仓。
                raise RuntimeError(
                    f"初始管理员密码文件写入失败（{e}）——已中止创建管理员。"
                    "请修复 config/runtime/ 目录写权限后重启；系统不会在无法安全"
                    "交付密码的情况下以弱保护方式交付") from e
        conn.commit()
    finally:
        _release_conn(conn)


def _create_user_internal(conn: sqlite3.Connection, username: str, password: str, role: str = "user"):
    """内部方法：在给定连接上创建用户"""
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    conn.execute(
        "INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)",
        (username, password_hash, salt, role),
    )


# 内置角色（保留语义）：admin 全权限 / user 读写禁 ddl-drop / readonly 只读。
# 其余角色名为用户级自定义角色（如 user_zhangsan），由管理员在
# permissions.yml 的 roles.<角色名> 下配置专属的表/列 deny。
_BUILTIN_ROLES = ("admin", "user", "readonly")


def _validate_role(role: str) -> tuple[bool, str]:
    """校验角色名：内置三值 或 自定义角色（字母开头，含 _/字母/数字，≤32 字符）"""
    if not role:
        return False, "角色不能为空"
    if role in _BUILTIN_ROLES:
        return True, ""
    if not _re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,31}", role):
        return False, "自定义角色名须以字母/下划线开头，含字母/数字/下划线，≤32 字符"
    return True, ""


def _hash_password(password: str, salt: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """使用 PBKDF2-HMAC-SHA256 哈希密码"""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return dk.hex()


def _verify_password(password: str, salt: str, expected_hash: str) -> tuple:
    """验证密码。返回 (matched, used_legacy_iterations)——旧迭代档哈希命中时
    used_legacy=True，调用方应在登录成功路径透明重哈希升级（不强制改密）"""
    if hmac.compare_digest(_hash_password(password, salt), expected_hash):
        return True, False
    if hmac.compare_digest(
            _hash_password(password, salt, _LEGACY_ITERATIONS), expected_hash):
        return True, True
    return False, False


# ── 媒体签名 URL（浏览器原生资源的认证通道）──
# iframe/img/下载锚点无法携带 Bearer——签名 URL（HMAC(path|exp)，短期 TTL，
# TTL 内可重放——媒体预览场景的口径取舍，非一次性 token）是它们的认证通道

def sign_media_token(path: str, ttl: int = 300) -> str:
    """签名媒体令牌：HMAC(path|exp)，默认 5 分钟有效"""
    exp = int(time.time()) + ttl
    msg = f"{path}|{exp}"
    sig = hmac.new(_get_token_secret().encode("utf-8"), msg.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_media_token(path: str, token: str) -> bool:
    """校验媒体令牌（fail-closed：过期/篡改/畸形一律 False）"""
    try:
        exp_s, sig = token.split(".", 1)
        if int(time.time()) > int(exp_s):
            return False
        expect = hmac.new(_get_token_secret().encode("utf-8"),
                          f"{path}|{exp_s}".encode("utf-8"),
                          hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expect)
    except Exception:
        return False


# ── Token 生成与验证 ──


# Token 签名密钥（从环境变量读取，或自动生成）
_TOKEN_SECRET = os.getenv("AUTH_TOKEN_SECRET", "")


def _get_token_secret() -> str:
    """获取 Token 签名密钥"""
    global _TOKEN_SECRET
    if not _TOKEN_SECRET:
        # 首次启动自动生成并持久化到 .env（不存在也创建——
        # 全新部署没有 .env，仅当已存在才写会每次重启换新密钥、全员掉线）
        _TOKEN_SECRET = secrets.token_hex(32)
        env_path = Path(__file__).resolve().parent.parent / "config" / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        if not env_path.exists():
            # 创建即终态权限（os.open 0600——写后收紧的微窗口归零）
            _fd = os.open(str(env_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        else:
            # 存量 .env 先收紧再落秘密（顺序不可换——写后 chmod 会留
            # "0644 文件含密钥"的微窗口）
            try:
                os.chmod(env_path, 0o600)
            except OSError:
                pass  # Windows ACL 语义下尽力而为
            _fd = os.open(str(env_path), os.O_WRONLY | os.O_APPEND)
        with os.fdopen(_fd, "a", encoding="utf-8") as f:
            f.write(f"\nAUTH_TOKEN_SECRET={_TOKEN_SECRET}\n")
    return _TOKEN_SECRET


def generate_token(user_id: int, username: str, role: str) -> str:
    """生成签名 Token

    格式：base64(payload).base64(signature)
    payload = {"uid", "username", "role", "tv": token_version, "exp": timestamp}
    tv 参与吊销语义：logout/改密/角色变更使 tv+1，旧 token 立即失效
    """
    tv = 0
    try:
        conn = _get_conn()
        try:
            row = conn.execute("SELECT token_version FROM users WHERE id = ?",
                               (user_id,)).fetchone()
            tv = row["token_version"] if row else 0
        finally:
            _release_conn(conn)
    except Exception:
        tv = 0
    payload = {
        "uid": user_id,
        "username": username,
        "role": role,
        "tv": tv,
        "exp": int(time.time()) + TOKEN_TTL,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = _base64_url_encode(payload_json.encode("utf-8"))

    secret = _get_token_secret()
    signature = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _base64_url_encode(signature)

    return f"{payload_b64}.{sig_b64}"


def verify_token(token: str) -> Optional[dict]:
    """验证 Token 并返回 payload，无效则返回 None"""
    if not token or "." not in token:
        return None

    parts = token.split(".")
    if len(parts) != 2:
        return None

    payload_b64, sig_b64 = parts

    # 验证签名
    secret = _get_token_secret()
    expected_sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    expected_sig_b64 = _base64_url_encode(expected_sig)

    if not hmac.compare_digest(sig_b64, expected_sig_b64):
        return None

    # 解析 payload
    try:
        payload_json = _base64_url_decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_json)
    except Exception:
        return None

    # 检查过期时间
    if int(time.time()) > payload.get("exp", 0):
        return None

    # 角色以库内现值为准：角色降级/用户删除即时生效（不再是 24h 角色快照）。
    # 本地 SQLite 主键查询，单次开销可忽略；库不可读时 fail-closed 拒绝。
    # 边界口径：认证元数据面由本模块自持（裸 open_db 通道，
    # 与 users 表写的归属一致——数据面写已被内置只读闸封死）；业务数据面
    # 在 daemon 界内。文档叙事按此口径，不夸大为"所有密钥操作都在 daemon"。
    try:
        conn = _get_conn()
        try:
            row = conn.execute("SELECT role, token_version FROM users WHERE id = ?",
                               (payload.get("uid"),)).fetchone()
        finally:
            _release_conn(conn)
    except Exception:
        return None
    if not row:
        return None
    # token 版本戳校验：logout/改密/角色变更后旧 token 立即失效
    if row["token_version"] != payload.get("tv", 0):
        return None
    payload["role"] = row["role"]

    return payload


# ── Token 吊销 / 登录锁定 / API Key 校验（认证件收口）──

def _bump_token_version(user_id: int) -> None:
    """tv+1：该用户全部在发 token 立即失效"""
    conn = _get_conn()
    try:
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?",
                     (user_id,))
        conn.commit()
    finally:
        _release_conn(conn)


def logout_user(token: str) -> dict:
    """登出：吊销当前用户的全部在发 token（tv+1）"""
    payload = verify_token(token)
    if not payload:
        return {"ok": False, "message": "Token 无效或已过期"}
    _bump_token_version(int(payload["uid"]))
    return {"ok": True, "message": "已退出登录（本账号所有 token 已失效）"}


def change_password(user_id: int, old_password: str, new_password: str) -> dict:
    """修改密码（本人需旧密码验证）；成功后 tv+1 吊销全部旧 token"""
    if not new_password or len(new_password) < 6:
        return {"ok": False, "message": "新密码至少 6 个字符"}
    conn = _get_conn()
    try:
        row = conn.execute("SELECT password_hash, salt FROM users WHERE id = ?",
                           (user_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "用户不存在"}
        matched, _legacy = _verify_password(old_password, row["salt"], row["password_hash"])
        if not matched:
            return {"ok": False, "message": "原密码错误"}
        salt = secrets.token_hex(16)
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ?, "
            "token_version = token_version + 1 WHERE id = ?",
            (_hash_password(new_password, salt), salt, user_id))
        conn.commit()
        return {"ok": True, "message": "密码已修改，请重新登录"}
    finally:
        _release_conn(conn)


# 登录锁定：同账号连续失败 5 次锁 10 分钟（进程内存，重启清零——可接受口径）
_LOGIN_FAILS: dict = {}
_LOGIN_FAILS_LOCK = _threading.Lock()  # 计数读-改-写互斥（爆破并发下丢计数=阈值延迟到达）
_LOCK_THRESHOLD = 5
_LOCK_WINDOW = 600  # 秒


def _lock_remaining(username: str) -> int:
    """剩余锁定秒数（0=未锁定）"""
    with _LOGIN_FAILS_LOCK:
        rec = _LOGIN_FAILS.get(username)
        if not rec:
            return 0
        fails, first_ts = rec
        if fails < _LOCK_THRESHOLD:
            return 0
        remaining = int(_LOCK_WINDOW - (time.time() - first_ts))
        if remaining <= 0:
            _LOGIN_FAILS.pop(username, None)
            return 0
        return remaining


def _record_login_fail(username: str) -> None:
    with _LOGIN_FAILS_LOCK:
        fails, first_ts = _LOGIN_FAILS.get(username, (0, time.time()))
        _LOGIN_FAILS[username] = (fails + 1, first_ts)


def verify_api_key(key: str) -> bool:
    """API Key 校验（恒定时间比较——== 直比有计时侧信道）"""
    from config.settings import settings
    expected = settings.API_KEY or ""
    if not key or not expected:
        return False
    return hmac.compare_digest(str(key), expected)


# 操作密码闸的独立锁定桶（与登录锁定同策略：5 次锁 10 分钟；
# 独立桶防"拿操作密码闸当登录爆破 oracle"或反向干扰）
_OPERATOR_GATE_KEY = "@operator_gate"


def verify_operator_password(password: str, username: str = "") -> bool:
    """验证操作密码（高危操作的人因确认）。

    身份语义（20260901）：谁的会话谁确认——username 非空时验证该用户本人
    的密码（不限角色，普通用户也能确认自己的操作）；username 为空只发生在
    无身份通道（脚本/系统），回退为任一 admin 密码。密码只用于与 users 表
    PBKDF2 哈希比对，不留存不落日志。失败不区分"密码错/无此用户"（防枚举）；
    连续失败走独立锁定桶（按身份分桶，防互炸）。"""
    if not password:
        return False
    lock_key = f"{_OPERATOR_GATE_KEY}:{username or 'admin'}"
    if _lock_remaining(lock_key):
        from core.logger import get_logger
        get_logger(__name__).warning("操作密码验证：连续失败过多，已临时锁定")
        return False
    conn = _get_conn()  # 共享缓存连接（与 login_user 同款，不可 close）
    if username:
        rows = conn.execute(
            "SELECT password_hash, salt FROM users WHERE username = ?",
            (username,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT password_hash, salt FROM users WHERE role = 'admin'").fetchall()
    for row in rows:
        matched, _legacy = _verify_password(password, row["salt"], row["password_hash"])
        if matched:
            with _LOGIN_FAILS_LOCK:
                _LOGIN_FAILS.pop(lock_key, None)
            return True
    _record_login_fail(lock_key)
    return False


def _base64_url_encode(data: bytes) -> str:
    """URL 安全的 base64 编码"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64_url_decode(s: str) -> bytes:
    """URL 安全的 base64 解码"""
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


# ── 用户管理 API ──

def register_user(username: str, password: str, role: str = "user") -> dict:
    """注册新用户

    Returns:
        {"ok": bool, "message": str, "user_id": int}
    """
    if not username or len(username) < 2:
        return {"ok": False, "message": "用户名至少 2 个字符"}
    if not password or len(password) < 6:
        return {"ok": False, "message": "密码至少 6 个字符"}
    ok, msg = _validate_role(role)
    if not ok:
        return {"ok": False, "message": msg}

    conn = _get_conn()
    try:
        # 检查用户名是否已存在
        cursor = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
        if cursor.fetchone():
            return {"ok": False, "message": "用户名已存在"}

        _create_user_internal(conn, username, password, role)
        conn.commit()

        cursor = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
        user_id = cursor.fetchone()["id"]

        return {"ok": True, "message": "注册成功", "user_id": user_id}
    except Exception as e:
        # 原始异常不外泄（f"注册失败: {e}" 曾把库错误细节
        # 回给客户端——信息泄露面；细节进服务端日志）
        from core.logger import get_logger
        get_logger(__name__).warning("注册失败（细节不回客户端）: %s", e)
        return {"ok": False, "message": "注册失败，请稍后重试或更换用户名"}
    finally:
        _release_conn(conn)


def login_user(username: str, password: str) -> dict:
    """用户登录

    Returns:
        {"ok": bool, "message": str, "token": str, "user": dict}
    """
    conn = _get_conn()
    try:
        # 登录锁定：同账号连续失败 5 次锁 10 分钟——
        # 仅 60 次/分/IP 的弱限速防不住账号级爆破
        remaining = _lock_remaining(username)
        if remaining:
            return {"ok": False,
                    "message": f"账号已临时锁定（连续失败过多），请 {remaining // 60 + 1} 分钟后重试"}
        cursor = conn.execute(
            "SELECT id, username, password_hash, salt, role FROM users WHERE username = ?",
            (username,),
        )
        row = cursor.fetchone()
        if not row:
            _record_login_fail(username)
            return {"ok": False, "message": "用户名或密码错误"}

        matched, legacy = _verify_password(password, row["salt"], row["password_hash"])
        if not matched:
            _record_login_fail(username)
            return {"ok": False, "message": "用户名或密码错误"}

        with _LOGIN_FAILS_LOCK:
            _LOGIN_FAILS.pop(username, None)  # 成功即清零
        if legacy:
            # 旧迭代档哈希透明升级（600k 现行档重哈希，用户零感知）
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (_hash_password(password, row["salt"]), row["id"]))
        # 更新最后登录时间
        conn.execute(
            "UPDATE users SET last_login = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        conn.commit()

        token = generate_token(row["id"], row["username"], row["role"])
        return {
            "ok": True,
            "message": "登录成功",
            "token": token,
            "user": {
                "id": row["id"],
                "username": row["username"],
                "role": row["role"],
            },
        }
    finally:
        _release_conn(conn)


def list_users() -> list[dict]:
    """列出所有用户（管理员功能）"""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "SELECT id, username, role, created_at, last_login FROM users ORDER BY id"
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        _release_conn(conn)


def get_user_role(username: str) -> str | None:
    """按用户名查角色（MCP 通道用户绑定的消费点）；用户不存在返回 None"""
    if not username:
        return None
    conn = _get_conn()
    try:
        row = conn.execute("SELECT role FROM users WHERE username = ?",
                           (username,)).fetchone()
        return row["role"] if row else None
    finally:
        _release_conn(conn)


def delete_user(user_id: int) -> dict:
    """删除用户（不允许删除 admin）"""
    conn = _get_conn()
    try:
        cursor = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "message": "用户不存在"}
        if row["username"] == "admin":
            return {"ok": False, "message": "不允许删除默认管理员"}

        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return {"ok": True, "message": "用户已删除"}
    finally:
        _release_conn(conn)


def update_user_role(user_id: int, role: str) -> dict:
    """修改用户角色（管理员功能）

    防御：
    - 目标用户必须存在
    - role 必须是 admin/user/readonly
    - 不允许降级/变更默认管理员 admin（防止唯一管理员被注销）
    - 不允许把最后一名 admin 降级（防止系统失去管理员）

    Returns:
        {"ok": bool, "message": str}
    """
    ok, msg = _validate_role(role)
    if not ok:
        return {"ok": False, "message": msg}

    conn = _get_conn()
    try:
        cursor = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "message": "用户不存在"}
        if row["username"] == "admin":
            return {"ok": False, "message": "不允许修改默认管理员的角色"}
        # 降级最后一名 admin 的保护：若目标当前是 admin 且是唯一 admin，拒绝
        if row["role"] == "admin" and role != "admin":
            admins = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role = 'admin'"
            ).fetchone()["c"]
            if admins <= 1:
                return {"ok": False, "message": "不允许降级最后一名管理员"}

        conn.execute("UPDATE users SET role = ?, "
                     "token_version = token_version + 1 WHERE id = ?", (role, user_id))
        conn.commit()
        return {"ok": True, "message": f"用户角色已更新为 {role}"}
    finally:
        _release_conn(conn)


# ── 速率限制（内存计数器）──

class RateLimiter:
    """简单的内存速率限制器——按 IP 限制每分钟请求数"""

    def __init__(self, max_requests: int = RATE_LIMIT_PER_MINUTE, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests: dict[str, list[float]] = {}  # ip -> [timestamps]

    def check(self, ip: str, limit: int = 0) -> tuple[bool, str]:
        """检查 IP 是否超过速率限制（limit=0 时用默认档）

        Returns:
            (allowed, message)
        """
        max_requests = limit or self.max_requests
        now = time.time()
        if ip not in self._requests:
            self._requests[ip] = []

        # 清理过期记录；空键顺手淘汰（唯一 IP 洪泛下键无界增长的收口）
        self._requests[ip] = [t for t in self._requests[ip] if now - t < self.window]
        if not self._requests[ip]:
            del self._requests[ip]

        if len(self._requests.get(ip, [])) >= max_requests:
            remaining = int(self.window - (now - self._requests[ip][0]))
            return False, f"请求过于频繁，请 {remaining} 秒后重试"

        self._requests.setdefault(ip, []).append(now)
        return True, ""


# 全局速率限制器实例
_rate_limiter = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    return _rate_limiter
