"""schemas 快照/恢复域（20260822 拆包：core/schema_manager.py 同名片段纯搬家，逻辑零变化）

单一事实源：备份由变更操作自动产生，取代旧的手工维护 schemas_bak/。
历史说明：旧的 industries/<行业>/schemas_bak/ 是手工维护的平行目录，与 schemas/ 已发散
（schemas=学生表，schemas_bak=工程造价表），restore 直接拷贝会污染当前配置。
现有 schemas_bak 已改名为 schemas_snapshot_legacy_工程造价/ 作为历史资产保留，不再参与恢复。
"""
import shutil
from core.logger import get_logger
from datetime import datetime
from pathlib import Path

from ._shared import _get_industry_dir, _get_schema_dir

logger = get_logger(__name__)

SNAPSHOT_KEEP = 5  # schemas 快照保留份数（超出后删除最旧）

def _get_snapshot_dir() -> Path:
    return _get_industry_dir() / "schemas_snapshots"

def _snapshot_schemas() -> Path | None:
    """schema 变更前自动快照当前 schemas/*.yaml（带时间戳，保留最近 N 份）。
    在 _save_config 写入前调用，保证「备份」与「当前」永远同源。返回快照目录，无内容可快照时返回 None。"""
    schema_dir = _get_schema_dir()
    if not schema_dir.exists():
        return None
    files = list(schema_dir.glob("*.yaml"))
    if not files:
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_dir = _get_snapshot_dir() / ts
    n = 1
    while snap_dir.exists():  # 同秒内多次变更：加序号避免互相覆盖
        n += 1
        snap_dir = _get_snapshot_dir() / f"{ts}_{n}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    for p in files:
        shutil.copy2(p, snap_dir / p.name)
    # 保留最近 N 份，删除最旧
    snaps = sorted(p for p in _get_snapshot_dir().iterdir() if p.is_dir())
    for old in snaps[:-SNAPSHOT_KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    logger.info("schemas 快照已保存: %s（%d 个文件）", snap_dir.name, len(files))
    return snap_dir

def restore_schema_templates(snapshot: str = "") -> str:
    """从 schemas_snapshots/ 恢复 schemas/ 到历史快照。

    快照制（单一事实源）：快照由 schema 变更操作在写入前自动产生（见 _snapshot_schemas），
    因此恢复源与当前配置永远同源，不存在手工维护的平行目录。
    旧的 schemas_bak/（手工维护的工程造价模板，已与学生表 schemas/ 发散）已改名为
    schemas_snapshot_legacy_工程造价/ 作为历史资产保留，本函数不再读取它。

    snapshot: 快照目录名（如 20260719_120000）；为空时恢复最新一份。
    恢复前会先快照当前状态（可再次回退）。恢复后需调用 repair_tables() 建表。
    """
    schema_dir = _get_schema_dir()
    snap_root = _get_snapshot_dir()
    if not snap_root.exists():
        return f"快照目录不存在: {snap_root}（schema 变更操作会自动创建快照）"
    snaps = sorted(p for p in snap_root.iterdir() if p.is_dir())
    if not snaps:
        return f"没有任何 schemas 快照: {snap_root}"
    if snapshot:
        chosen = snap_root / snapshot
        if not chosen.is_dir():
            return f"快照不存在: {snapshot}，可用快照: {', '.join(p.name for p in snaps)}"
    else:
        chosen = snaps[-1]
    # 恢复前快照当前状态，保证这次恢复本身也可回退
    _snapshot_schemas()
    schema_dir.mkdir(parents=True, exist_ok=True)
    for p in schema_dir.glob("*.yaml"):
        p.unlink()
    count = 0
    for p in chosen.glob("*.yaml"):
        shutil.copy2(p, schema_dir / p.name)
        count += 1
    logger.info("已从快照 %s 恢复 %d 个表结构到 %s", chosen.name, count, schema_dir)
    return f"已从快照 {chosen.name} 恢复 {count} 个表结构"
