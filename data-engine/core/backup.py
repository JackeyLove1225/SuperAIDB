"""数据库备份与恢复模块

功能：
- 启动时自动备份 SQLite 数据库
- 保留最近 N 份备份，自动清理旧备份
- 支持手动备份和从备份恢复
"""
import time as _t

from core.logger import get_logger
from core.crypto.connection import open_db

logger = get_logger(__name__)
from pathlib import Path
from datetime import datetime

from config.settings import settings


def _get_backup_dir() -> Path:
    """获取备份目录路径（锚定项目根，与 _get_db_path 同一锚点——
    相对路径配置在任何 cwd 下备份都与真库同目录）"""
    db_path = Path(settings.SQLITE_DB_PATH)
    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parent.parent / db_path
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def _get_db_path() -> Path:
    """获取数据库文件路径（处理相对路径）"""
    db_path = Path(settings.SQLITE_DB_PATH)
    if not db_path.is_absolute():
        # 相对于项目根目录
        project_root = Path(__file__).resolve().parent.parent
        db_path = project_root / db_path
    return db_path


def backup_database(max_backups: int = 7) -> dict:
    """备份数据库到 db/backups/ 目录

    Args:
        max_backups: 保留最近 N 份备份

    Returns:
        {"ok": bool, "path": str, "message": str}
    """
    db_path = _get_db_path()
    if not db_path.exists():
        return {"ok": False, "path": "", "message": f"数据库文件不存在: {db_path}"}

    backup_dir = _get_backup_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"data_engine_{timestamp}.db"

    try:
        # 使用 SQLite 的在线备份 API（避免锁冲突）
        src = open_db(str(db_path))
        dst = open_db(str(backup_path))
        src.backup(dst)
        dst.close()
        src.close()

        # 清理旧备份
        backups = sorted(backup_dir.glob("data_engine_*.db"))
        for old in backups[:-max_backups]:
            try:
                old.unlink()
            except OSError:
                pass  # 旧备份清理失败留到下次轮转

        return {
            "ok": True,
            "path": str(backup_path),
            "message": f"已备份到 {backup_path.name}",
        }
    except Exception as e:
        return {"ok": False, "path": "", "message": f"备份失败: {e}"}


def _close_active_connections() -> None:
    """恢复前释放活动库文件句柄/锁（含 daemon 侧）——顺序与身份纪律：
    1. 先关本进程连接（daemon 还活着，RPC close 正常清会话）——顺序颠倒会让
       close 经自愈链把 daemon 又拉起来
    2. 再让 daemon 退场：身份校验（命令行含 core.daemon.server，防 PID 复用误杀——
       与 launcher「宁不杀不错杀」同纪律）后杀进程+清运行文件
    """
    # 1. 本进程驱动单例的活动连接（daemon 活着时 RPC close 才有意义）
    try:
        from core import data_ops
        data_ops.close_driver_cache()  # 公开入口，不再直戳 _federated_driver
    except Exception as e:
        logger.warning("恢复前关闭驱动缓存异常（继续，OS 句柄随进程退出回收）: %s", e)
    # 2. daemon 退场（若启用）：身份校验后杀，运行文件清除，下次调用自动重拉
    try:
        from config.settings import settings
        if settings.DAEMON_MODE_EFFECTIVE == "true":
            from core.daemon import runtime as _rt
            rt = _rt.read_runtime()
            if rt and rt.get("pid"):
                pid = int(rt["pid"])
                try:
                    import psutil
                    proc = psutil.Process(pid)
                    cmdline = " ".join(proc.cmdline() or [])
                    if "core.daemon.server" in cmdline:
                        proc.kill()
                    else:
                        logger.warning("daemon 运行文件 PID %s 复用于无关进程，跳过", pid)
                except Exception:
                    pass  # 进程已死/无法探测
                _rt.clear_runtime()
                _t.sleep(0.5)  # 等 OS 释放句柄
    except Exception:
        pass  # daemon 探测/退场失败：进程已死或权限不足，恢复流程继续（后续连接会重建）


def restore_database(backup_filename: str) -> dict:
    """从备份恢复数据库

    Args:
        backup_filename: 备份文件名（如 data_engine_20260715_120000.db）

    Returns:
        {"ok": bool, "message": str}
    """
    db_path = _get_db_path()
    backup_dir = _get_backup_dir()

    # 安全校验：只允许文件名，不允许路径
    backup_name = Path(backup_filename).name
    backup_path = backup_dir / backup_name

    if not backup_path.exists():
        return {"ok": False, "message": f"备份文件不存在: {backup_name}"}

    # 维护窗旗标：恢复期间 daemon 业务调用一律被拒——
    # 否则 dashboard 轮询会在写库中途把 daemon 自愈拉回来抢库
    from core.daemon import runtime as _rt
    try:
        _rt.set_maintenance(True)
    except OSError as e:
        # fail-closed：隔离模式把 config/runtime 收紧为只读后
        # 旗标写不进去——宁可拒绝恢复，也不在没有维护窗保护的情况下动库
        return {"ok": False,
                "message": f"维护窗置位失败（config/runtime 不可写: {e}），已中止恢复。"
                           "请检查目录权限（系统级隔离模式下需以管理员流程操作）"}
    try:
        # 恢复前先备份当前数据库（以防恢复出错）。
        # 紧急备份同样走在线 backup API——copy2 裸拷活动库文件可能拷到
        # 半写状态（与 migrate.py 的 WAL busy 校验同一标准）
        if db_path.exists():
            # 紧急备份用独立前缀：不计入 data_engine_* 常规轮转 glob——
            # 恢复前的保命副本绝不能被"保留最近 N 份"策略误删
            emergency_backup = backup_dir / f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            _src = open_db(str(db_path))
            _dst = open_db(str(emergency_backup))
            try:
                _src.backup(_dst)
            finally:
                _dst.close()
                _src.close()

        # 关闭驱动单例的活动连接（best-effort，驱动会按需惰性重连）
        _close_active_connections()

        # 用 SQLite 在线备份 API 反向恢复：
        # source = 备份文件（只读打开），target = 活动库。
        # 不替换文件，由 SQLite 自身写入，规避 Windows 文件锁下
        # shutil.copy2 覆盖活动数据库导致的失败/毁库风险。
        src = open_db(f"file:{backup_path}?mode=ro", uri=True)
        dst = open_db(str(db_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        # 三层一致性修复：MetaDB 元数据与业务数据同库，恢复会把元数据一起回滚，
        # 但 YAML schema 文件（事实源）不会回滚 → 三层立即不一致。
        # 恢复后以 YAML 为准重建 SQLite 元数据 + Ladybug 图层（sync_from_yaml）。
        # 同步失败不阻断恢复结果，但必须在返回消息中明确提示用户手动同步。
        sync_msg = ""
        try:
            from core.graph.schema_graph_service import SchemaGraphService
            sync_result = SchemaGraphService.get_instance().sync_from_yaml()
            sync_msg = (f"；已按 YAML 同步元数据"
                        f"（synced={sync_result.get('synced', 0)}, errors={sync_result.get('errors', 0)}）")
        except Exception as e:
            sync_msg = ("；警告：元数据与 YAML 可能不一致，自动同步失败，"
                        f"请手动调用 POST /api/schema-graph/sync 执行同步（{str(e)[:60]}）")

        return {
            "ok": True,
            "message": f"已从 {backup_name} 恢复数据库{sync_msg}",
        }
    except Exception as e:
        return {"ok": False, "message": f"恢复失败: {e}"}
    finally:
        _rt.set_maintenance(False)


def list_backups() -> list[dict]:
    """列出所有可用的备份

    Returns:
        [{"filename": str, "size_mb": float, "time": str}, ...]
    """
    backup_dir = _get_backup_dir()
    backups = []
    # 常规备份（data_engine_*，参与轮转）+ 恢复前紧急备份（pre_restore_*，
    # 独立前缀不参与轮转但同样可恢复——命名分轨后列表必须两轨都收）
    files = sorted(backup_dir.glob("data_engine_*.db"), reverse=True) + \
        sorted(backup_dir.glob("pre_restore_*.db"), reverse=True)
    for f in files:
        stat = f.stat()
        backups.append({
            "filename": f.name,
            "size_mb": round(stat.st_size / 1024 / 1024, 2),
            "time": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return backups
