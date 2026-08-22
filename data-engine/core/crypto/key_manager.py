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
        # utf-8-sig：剥 BOM——密钥文件被记事本碰过一次就静默锁库的坑（评审四轮 N6）
        return key_file.read_text(encoding="utf-8-sig").strip()
    key = secrets.token_urlsafe(32)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key, encoding="utf-8")
    logger.info("数据库主密钥已生成并写入隔离密钥库（%s）", key_file)
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
            return key_file.read_text(encoding="utf-8-sig").strip()
    else:
        try:
            import keyring
            existing = keyring.get_password(_SERVICE, _KEY_NAME)
            if existing:
                return existing
        except Exception:
            pass  # 后端不可用：进入加锁路径，由后端函数给出正式 fail-closed 报错
    backend = _get_key_file if _isolated_mode() else _get_key_keyring
    # 首次生成的跨进程互斥：get→generate→set 非原子，两进程并发首启会各生成各写、
    # 后写覆盖先写——先用旧 key 建的库即不可读（安全评审实测发现）。
    # 锁内写 PID 并校验身份：强杀/断电残留的死锁自动回收（与 launcher/daemon
    # 锁同纪律；此前死锁需手动删文件，评审三轮运维复核收口）
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
                    # psutil.pid_exists：os.kill(pid,0) 的 sig-0 语义在 Windows
                    # 3.13 以下不存在（探测=杀进程，评审五轮）——跨版本一致原语
                    import psutil
                    if not psutil.pid_exists(holder):
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
            return backend()
        except Exception as e:
            raise RuntimeError(
                f"数据库加密已启用但密钥后端不可用（{e}）。"
                "请安装/启动系统密钥服务，或在 config/.env 显式设置 DB_ENCRYPT=false "
                "（不推荐：库文件将以明文存储）")
    finally:
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass
