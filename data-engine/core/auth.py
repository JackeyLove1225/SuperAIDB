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
PBKDF2_ITERATIONS = 100000

# 速率限制：每 IP 每分钟最大请求数
RATE_LIMIT_PER_MINUTE = 60


def _get_db_path() -> str:
    """获取 SQLite 数据库路径"""
    path = settings.SQLITE_DB_PATH
    if not os.path.isabs(path):
        path = os.path.join(str(Path(__file__).resolve().parent.parent), path)
    return path


def _get_conn() -> sqlite3.Connection:
    """获取 SQLite 连接"""
    conn = open_db(_get_db_path())
    conn.row_factory = compat_row_factory()
    return conn


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
                pw_file.write_text(
                    f"初始管理员账号: admin\n初始密码: {admin_password}\n"
                    "请立即登录并修改密码，修改后手动删除本文件。\n", encoding="utf-8")
                print("[auth] 初始管理员已创建，密码见 config/runtime/initial_admin.txt")
            except OSError:
                # 回退取舍（诚实声明）：runtime/ 写不了多半是权限/盘问题——
                # 此时只能打到控制台（会被启动器收进 logs/，有泄漏面，
                # 但比"用户完全拿不到初始密码、进不了系统"可取）。尽快修复文件写入后改密。
                print(f"[auth] 初始管理员密码: admin / {admin_password}（请立即登录修改）")
        conn.commit()
    finally:
        conn.close()


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
    import re as _re
    if not _re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,31}", role):
        return False, "自定义角色名须以字母/下划线开头，含字母/数字/下划线，≤32 字符"
    return True, ""


def _hash_password(password: str, salt: str) -> str:
    """使用 PBKDF2-HMAC-SHA256 哈希密码"""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return dk.hex()


def _verify_password(password: str, salt: str, expected_hash: str) -> bool:
    """验证密码"""
    actual_hash = _hash_password(password, salt)
    return hmac.compare_digest(actual_hash, expected_hash)


# ── 媒体签名 URL（浏览器原生资源的认证通道）──
# iframe/img/下载锚点无法携带 Bearer——签名 URL（HMAC(path|exp)，一次性短期）
# 是它们的认证通道（评审五轮 S-3 资源面修复：认证开启后预览/下载全 401 的问题）

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
        # 首次启动自动生成并持久化到 .env（此前仅当 .env 已存在才写——
        # 全新部署没有 .env，每次重启换新密钥、全员掉线，评审实测发现）
        _TOKEN_SECRET = secrets.token_hex(32)
        env_path = Path(__file__).resolve().parent.parent / "config" / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        with open(env_path, "a", encoding="utf-8") as f:
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
            conn.close()
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
    # 边界口径（评审五轮定稿）：认证元数据面由本模块自持（裸 open_db 通道，
    # 与 users 表写的归属一致——数据面写已被内置只读闸封死）；业务数据面
    # 在 daemon 界内。文档叙事按此口径，不夸大为"所有密钥操作都在 daemon"。
    try:
        conn = _get_conn()
        try:
            row = conn.execute("SELECT role, token_version FROM users WHERE id = ?",
                               (payload.get("uid"),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    # token 版本戳校验：logout/改密/角色变更后旧 token 立即失效
    if row["token_version"] != payload.get("tv", 0):
        return None
    payload["role"] = row["role"]

    return payload


# ── Token 吊销 / 登录锁定 / API Key 校验（评审五轮 A9 认证件收口）──

def _bump_token_version(user_id: int) -> None:
    """tv+1：该用户全部在发 token 立即失效"""
    conn = _get_conn()
    try:
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?",
                     (user_id,))
        conn.commit()
    finally:
        conn.close()


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
        if not _verify_password(old_password, row["salt"], row["password_hash"]):
            return {"ok": False, "message": "原密码错误"}
        salt = secrets.token_hex(16)
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ?, "
            "token_version = token_version + 1 WHERE id = ?",
            (_hash_password(new_password, salt), salt, user_id))
        conn.commit()
        return {"ok": True, "message": "密码已修改，请重新登录"}
    finally:
        conn.close()


# 登录锁定：同账号连续失败 5 次锁 10 分钟（进程内存，重启清零——可接受口径）
_LOGIN_FAILS: dict = {}
_LOCK_THRESHOLD = 5
_LOCK_WINDOW = 600  # 秒


def _lock_remaining(username: str) -> int:
    """剩余锁定秒数（0=未锁定）"""
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
    fails, first_ts = _LOGIN_FAILS.get(username, (0, time.time()))
    _LOGIN_FAILS[username] = (fails + 1, first_ts)


def verify_api_key(key: str) -> bool:
    """API Key 校验（恒定时间比较——此前 == 直比有计时侧信道）"""
    from config.settings import settings
    expected = settings.API_KEY or ""
    if not key or not expected:
        return False
    return hmac.compare_digest(str(key), expected)


def _base64_url_encode(data: bytes) -> str:
    """URL 安全的 base64 编码"""
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64_url_decode(s: str) -> bytes:
    """URL 安全的 base64 解码"""
    import base64
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
        return {"ok": False, "message": f"注册失败: {e}"}
    finally:
        conn.close()


def login_user(username: str, password: str) -> dict:
    """用户登录

    Returns:
        {"ok": bool, "message": str, "token": str, "user": dict}
    """
    conn = _get_conn()
    try:
        # 登录锁定（评审五轮 A9）：同账号连续失败 5 次锁 10 分钟——
        # 此前只有 60 次/分/IP 的弱限速，账号级爆破无防
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

        if not _verify_password(password, row["salt"], row["password_hash"]):
            _record_login_fail(username)
            return {"ok": False, "message": "用户名或密码错误"}

        _LOGIN_FAILS.pop(username, None)  # 成功即清零
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
        conn.close()


def list_users() -> list[dict]:
    """列出所有用户（管理员功能）"""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "SELECT id, username, role, created_at, last_login FROM users ORDER BY id"
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


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
        conn.close()


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
        conn.close()


# ── 速率限制（内存计数器）──

class RateLimiter:
    """简单的内存速率限制器——按 IP 限制每分钟请求数"""

    def __init__(self, max_requests: int = RATE_LIMIT_PER_MINUTE, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests: dict[str, list[float]] = {}  # ip -> [timestamps]

    def check(self, ip: str) -> tuple[bool, str]:
        """检查 IP 是否超过速率限制

        Returns:
            (allowed, message)
        """
        now = time.time()
        if ip not in self._requests:
            self._requests[ip] = []

        # 清理过期记录
        self._requests[ip] = [t for t in self._requests[ip] if now - t < self.window]

        if len(self._requests[ip]) >= self.max_requests:
            remaining = int(self.window - (now - self._requests[ip][0]))
            return False, f"请求过于频繁，请 {remaining} 秒后重试"

        self._requests[ip].append(now)
        return True, ""


# 全局速率限制器实例
_rate_limiter = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    return _rate_limiter
