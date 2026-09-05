"""schema_manager 子包公共助手：系统列保护 / 驱动入口 / 安全检测 / 内部路径 / 配置读写
（20260822 拆包：core/schema_manager.py 同名片段纯搬家，逻辑零变化）

patch 兼容（测试依赖，勿绕开）：_save_config 内对 _snapshot_schemas 的引用在调用时
经 facade 取值（from core import schema_manager as _sm），使
patch("core.schema_manager._snapshot_schemas") 之类的打桩保持有效。
"""
import os, yaml
from core.logger import get_logger
from pathlib import Path

logger = get_logger(__name__)
from config.settings import settings
from core.constants import MSG_SYS_COL_PROTECTED

# 项目根目录：本文件位于 core/schema_manager/ 包内，比拆前的 core/schema_manager.py 深一层
_PROJECT_ROOT = Path(__file__).parent.parent.parent

# facade 回旋引用：仅用于调用时取值（_sm._snapshot_schemas()），导入期不解引用
from core import schema_manager as _sm

# ── 系统列保护 ──
_SYS_COLUMNS = frozenset(["id"])

def _guard_sys_column(column: str, action: str = "修改") -> dict | None:
    """系统列保护：id 不允许修改、删除、设置属性"""
    if column.lower() in _SYS_COLUMNS:
        return {"ok": False, "message": MSG_SYS_COL_PROTECTED.format(column=column, action=action)}
    return None
from core.steward import Steward


def get_driver(datasource: str = None):
    """调用期解析 Steward 驱动（重置安全：旧写法的模块级绑定
    `get_driver = Steward()._get_driver` 会把方法钉死在导入时的实例上，
    Steward.reset_instance 后本别名仍指向旧实例的旧缓存——白盒必须调用期取值）"""
    return Steward().get_driver(datasource)


# ── 安全检测 ──

def _unwrap_sqlite_conn(drv):
    """从驱动包装链中取裸 sqlite 连接（能力探测，不直接假设 .conn 存在）。

    Steward/DataSourceManager 返回的是 ContractDriver 包装层，本身没有 conn 属性，
    裸连接在最底层 SqliteDriver 上。沿 raw_driver/_driver/_inner 逐层解包，
    找到有 conn 的底层驱动即返回其连接；非 SQLite 数据源（解包到底仍无 conn）返回 None，
    调用方据此跳过 PRAGMA 深度校验（降级，而非误判为"无连接"）。
    """
    for _ in range(8):  # 深度上限，防异常包装链死循环
        if drv is None:
            return None
        conn = getattr(drv, "conn", None)
        if conn is not None:
            return conn
        drv = (getattr(drv, "raw_driver", None)
               or getattr(drv, "_driver", None)
               or getattr(drv, "_inner", None))
    return None


# ── 内部路径 ──

def _get_industry_dir() -> Path:
    return _PROJECT_ROOT / "industries" / settings.INDUSTRY

def _get_schema_dir() -> Path:
    return _get_industry_dir() / "schemas"

def _get_fields_path() -> Path:
    return _get_industry_dir() / "fields" / "fields.yml"

def _load_config() -> dict:
    schema_dir = _get_schema_dir()
    # 统一从规范入口加载；坏 YAML 显式抛错（不再 except: continue 静默丢表）
    from core.schema_matcher import load_schemas
    tables = load_schemas(schema_dir) if schema_dir.exists() else []
    field_dict = {}
    fp = _get_fields_path()
    if fp.exists(): field_dict = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
    # （旧 references+fk 格式转换已删：现行 YAML 全部 foreign_keys 格式，lint 保证不再回退）
    return {"tables": tables, "field_dict": field_dict}

def _atomic_write(path: Path, content: str):
    """原子写入：先写临时文件，再 os.replace 原子替换（防止写入中途崩溃导致文件损坏）"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _save_config(data: dict):
    _sm._snapshot_schemas()  # 变更前自动快照当前 schemas（带时间戳，保留最近 N 份）
    schema_dir = _get_schema_dir()
    schema_dir.mkdir(parents=True, exist_ok=True)
    existing = set()
    for t in data.get("tables", []):
        n = t.get("name", "")
        if not n: continue
        existing.add(n)
        _atomic_write(schema_dir / f"{n}.yaml",
            yaml.dump(t, allow_unicode=True, default_flow_style=False, sort_keys=False))
    for p in schema_dir.glob("*.yaml"):
        if p.stem not in existing: p.unlink()
    fd = data.get("field_dict", {})
    if fd:
        _atomic_write(_get_fields_path(),
            yaml.dump(fd, allow_unicode=True, default_flow_style=False, sort_keys=False))
    # 写完成事件（P-D）：订阅方（graph service）把 MetaDB/Ladybug 收敛到 YAML——
    # AI DDL 路径（tools/ddl → crud）曾只写 YAML+真实库，设计器图谱陈旧到下次重启
    from core.registry import notify_change
    notify_change("schemas_written")


def _save_with_rollback(yaml_op, rollback, rollback_desc: str = "配置回滚失败（DB 与 YAML 可能不一致）",
                        fail_message: str = "") -> dict | None:
    """「先 DB 后 YAML + 失败回滚」模式的收尾步骤：执行 YAML 写入，失败则执行回滚动作。

    - yaml_op: YAML 写入动作（无参可调用，通常是 lambda: _save_config(data)）
    - rollback: 回滚动作（无参可调用）。DB 操作可逆时回滚 DB（drop/rename 回去）；
      DB 操作不可逆时传 lambda: _save_config(backup) 恢复原配置
    - rollback_desc: 回滚动作描述，仅用于回滚失败时的 error 日志留痕
    - fail_message: 失败消息格式串，{e} 为原始异常
    返回 None 表示成功；返回 {"ok": False, "message": ...} 表示已回滚的失败（调用方 return 或记录）。
    """
    try:
        yaml_op()
    except Exception as e:
        try:
            rollback()
        except Exception as rb_err:  # 回滚失败不能再抛出打断主流程，但必须留痕（DB 与 YAML 可能已不一致）
            logger.error("%s: %s", rollback_desc, rb_err)
        return {"ok": False, "message": fail_message.format(e=e)}
    return None
