"""主密钥管理——生成一次，持久保管（用户零操作）

两种后端（按部署形态自动选择）：
- 默认：OS 凭据管理器（Windows WinVaultKeyring / macOS Keychain / Linux secret service）——
  密钥永不明文落盘、不进 .env、不进任何 LLM 可见的配置面；
  后端不可用（如无 secret service 的裸 Linux）时 fail-closed 报错并指明逃生门
  （DB_ENCRYPT=false），绝不静默降级为明文
- 系统级隔离模式（isolation_setup.ps1 enable 后）：daemon 跑在独立服务账号下，
  而凭据管理器是 per-user 的，服务账号读不到交互用户 vault——故改用
  ACL 保护的密钥文件 db/.vault/master.key（db/ 目录在隔离模式只放行
  服务账号+操作者+SYSTEM/Administrators，其余 OS 用户物理拒绝）。
  隔离标志：config/runtime/isolated.flag（enable 脚本写入，disable 删除）。
"""
from core.logger import get_logger
import os
import secrets
import time
from pathlib import Path

logger = get_logger(__name__)

_SERVICE = "SuperAIDB"
_KEY_NAME = "db_master_key"
_ROOT = Path(__file__).resolve().parent.parent.parent
_ISOLATED_FLAG = _ROOT / "config" / "runtime" / "isolated.flag"


def _isolated_mode() -> bool:
    """系统级隔离模式（密钥文件后端）是否生效"""
    return _ISOLATED_FLAG.exists()


def _chmod_600(p) -> None:
    """密钥/凭据文件权限收紧（0600 仅属主读写——与 daemon.json 同标准）"""
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass  # Windows ACL 语义下尽力而为


def _vault_dir() -> Path:
    """密钥文件目录（测试可用 SUPERAIDB_VAULT_DIR 重定向，避免污染真实 db/）"""
    override = os.environ.get("SUPERAIDB_VAULT_DIR")
    return Path(override) if override else _ROOT / "db" / ".vault"


def encryption_enabled() -> bool:
    """是否启用数据库加密（默认开；DB_ENCRYPT=false 显式关闭；进程内覆盖优先）"""
    from config.settings import settings
    return str(settings.DB_ENCRYPT_EFFECTIVE).lower() not in ("false", "0", "no")


def _get_key_keyring() -> str:
    """凭据管理器通道（默认部署形态）"""
    import keyring
    key = keyring.get_password(_SERVICE, _KEY_NAME)
    if not key:
        key = secrets.token_urlsafe(32)
        keyring.set_password(_SERVICE, _KEY_NAME, key)
        logger.info("数据库主密钥已生成并存入系统凭据管理器（一次性的首次启动动作）")
    return key


def _get_key_file() -> str:
    """密钥文件通道（系统级隔离模式；文件 ACL 由隔离脚本收紧到服务账号+操作者）"""
    key_file = _vault_dir() / "master.key"
    if key_file.exists():
        # utf-8-sig：剥 BOM——密钥文件被记事本碰过一次就静默锁库的坑
        return key_file.read_text(encoding="utf-8-sig").strip()
    key = secrets.token_urlsafe(32)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    _fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(_fd, "w", encoding="utf-8") as _fh:
        _fh.write(key)  # 创建即终态权限（写后收紧微窗口归零）
    logger.info("数据库主密钥已生成并写入隔离密钥库（%s）", key_file)
    return key


_SIGN_NAME = "escalation_signing_key"
_SIGN_CACHE: str = ""  # 进程内缓存：签名钥不轮换，避免每次验签都打 keyring 后端


def get_signing_key() -> str:
    """提权契约 HMAC 签名钥——与 DB 主密钥同后端但独立条目（不受 DB_ENCRYPT 开关影响）

    效力分层如实说：
    - 默认模式 + 系统凭据管理器可用：纵深防线——签名钥不入仓不落明文，
      篡改/伪造落盘契约须先取钥；但同用户进程在 Windows WinVault 下
      CredRead 可读 vault 条目，本防线是纵深不是硬边界（与 daemon
      威胁模型声明同口径：同用户本地进程在模型外）
    - 系统级隔离模式：签名钥落 ACL 收紧的 db/.vault/（仅服务账号+操作者可达），
      此时方为硬边界
    - 凭据管理器不可用的裸 Linux：降级 vault 文件并记 warning（同用户进程可读——
      该形态下"防同用户本地进程"本就不在承诺面内）
    """
    global _SIGN_CACHE
    if _SIGN_CACHE:
        return _SIGN_CACHE
    key = _mint_or_read_signing_key()
    _SIGN_CACHE = key
    return key


def _mint_or_read_signing_key() -> str:
    if _isolated_mode():
        f = _vault_dir() / "signing.key"
        if f.exists():
            _chmod_600(f)  # 存量文件幂等收紧
            return f.read_text(encoding="utf-8-sig").strip()
        key = secrets.token_urlsafe(32)
        f.parent.mkdir(parents=True, exist_ok=True)
        _fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(_fd, "w", encoding="utf-8") as _fh:
            _fh.write(key)  # 创建即终态权限（写后收紧微窗口归零）
        logger.info("提权签名钥已生成并写入隔离密钥库（%s）", f)
        return key
    try:
        import keyring
        key = keyring.get_password(_SERVICE, _SIGN_NAME)
        if not key:
            key = secrets.token_urlsafe(32)
            keyring.set_password(_SERVICE, _SIGN_NAME, key)
            logger.info("提权签名钥已生成并存入系统凭据管理器")
        return key
    except Exception as e:
        f = _vault_dir() / "signing.key"
        if f.exists():
            _chmod_600(f)  # 存量文件幂等收紧
            return f.read_text(encoding="utf-8-sig").strip()
        key = secrets.token_urlsafe(32)
        f.parent.mkdir(parents=True, exist_ok=True)
        _fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(_fd, "w", encoding="utf-8") as _fh:
            _fh.write(key)  # 创建即终态权限（写后收紧微窗口归零）
        logger.warning("凭据管理器不可用（%s），提权签名钥降级落盘 %s——"
                       "同用户进程可读，防伪造面请启用系统级隔离模式", e, f)
        return key


def _validated_key(key: str) -> str:
    """密钥字符集硬校验（单一收口——一切消费方（open_db/migrate/未来方）
    天然免疫：文件通道 key 为落盘原文，含单引号/空白即拼出畸形 SQL 或
    静默锁库；keyring 通道出自 secrets.token_urlsafe 本就在集内，零代价）"""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_\-]+", key or ""):
        raise RuntimeError("数据库密钥含非法字符（仅允许 [A-Za-z0-9_-]）——"
                           "请检查密钥文件/凭据条目是否被污染")
    return key


def get_db_key() -> str:
    """取数据库主密钥（首次自动生成并持久化）。

    Returns:
        密钥字符串；encryption_enabled=False 时返回空串（调用方据此走明文通道）

    Raises:
        RuntimeError: 启用加密但密钥后端不可用（fail-closed + 逃生门提示）
    """
    if not encryption_enabled():
        return ""
    # 快路径：密钥已存在时无锁直读（隔离模式下 runtime/ 对操作者只读，
    # 读密钥不能依赖创建 keygen.lock；且常态读无锁省一次文件操作）
    if _isolated_mode():
        key_file = _vault_dir() / "master.key"
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8-sig").strip()
            _chmod_600(key_file)  # 存量文件幂等收紧（升级前铸成的旧权限迁移）
            return _validated_key(key)
    else:
        try:
            import keyring
            existing = keyring.get_password(_SERVICE, _KEY_NAME)
            if existing:
                return _validated_key(existing)
        except Exception:
            pass  # 后端不可用：进入加锁路径，由后端函数给出正式 fail-closed 报错
    backend = _get_key_file if _isolated_mode() else _get_key_keyring
    # 首次生成的跨进程互斥：get→generate→set 非原子，两进程并发首启会各生成各写、
    # 后写覆盖先写——先用旧 key 建的库即不可读。
    # 锁内写 PID 并校验身份：强杀/断电残留的死锁自动回收（与 launcher/daemon
    # 锁同纪律，残留死锁无需手动删文件）
    lock_file = _ROOT / "config" / "runtime" / "keygen.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + 30
    while True:
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                holder = int(lock_file.read_text(encoding="utf-8").strip() or "0")
                if holder:
                    # 持有者存活探测走 file_contract._pid_alive 唯一实现
                    #（三处拷贝收敛，自查清单第 7 项）
                    from core.file_contract import _pid_alive
                    if not _pid_alive(holder):
                        lock_file.unlink(missing_ok=True)  # 持有者已死：回收残留锁
                        continue
            except (ValueError, OSError):
                lock_file.unlink(missing_ok=True)  # 锁内容损坏：回收
                continue
            if time.time() > deadline:
                raise RuntimeError("密钥生成互斥超时——删 config/runtime/keygen.lock 重试")
            time.sleep(0.2)
    try:
        try:
            return _validated_key(backend())
        except Exception as e:
            raise RuntimeError(
                f"数据库加密已启用但密钥后端不可用（{e}）。"
                "请安装/启动系统密钥服务，或在 config/.env 显式设置 DB_ENCRYPT=false "
                "（不推荐：库文件将以明文存储）")
    finally:
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass  # 锁文件残留由下次启动按 PID 身份校验回收
