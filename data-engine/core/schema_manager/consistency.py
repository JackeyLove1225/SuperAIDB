"""一致性校验域：_preflight_check 及其子校验 + require_consistency 守卫
（20260822 拆包：core/schema_manager.py 同名片段纯搬家，逻辑零变化）

patch 兼容（测试依赖，勿绕开）：_preflight_check / _all_industry_yaml_tables /
_unwrap_sqlite_conn / _last_extra_tables 会被 tests 在 facade（core.schema_manager）
上 patch/赋值（test_08/25/33），本模块一律在调用时经 _sm.X 访问，导入期不解引用。
"""
from functools import wraps

from config.settings import settings
from core.constants import (MSG_CONSISTENCY_BLOCK, MSG_CONSISTENCY_FIELD_DIFF,
    MSG_CONSISTENCY_INDEX_MISSING)
from core.contract.security_contract import safe_pragma_arg, is_valid_identifier

from core import schema_manager as _sm
from ._shared import _PROJECT_ROOT


# ── _preflight_check 按校验类型拆分的子函数（主函数只编排，首个错误即返回）──

def _all_industry_yaml_tables() -> set:
    """收集所有行业 schemas/*.yaml 中定义的表名（跨行业归属的唯一事实源）。

    生产场景：所有行业共享同一数据库，表归属行业不明确是致命的——
    若某表在任一行业 YAML 中定义过，则它是有主的，不能被当作"多余表"。
    """
    from core.schema_matcher import load_schemas as _load_schemas_all
    ind_dir = _PROJECT_ROOT / "industries"
    result = set()
    if not ind_dir.exists():
        return result
    for ind in ind_dir.iterdir():
        if not ind.is_dir() or ind.name in ("__pycache__", "templates", "custom"):
            continue
        sdir = ind / "schemas"
        if not sdir.exists():
            continue
        for t in _load_schemas_all(sdir):
            if t and t.get("name"):
                result.add(t["name"])
    return result


def _check_tables(schema_dir, db_tables) -> tuple[set, str]:
    """表级校验：YAML→DB 缺表、DB→YAML 多余表。
    返回 (yaml_tables, 错误消息)，一致时错误消息为 ""。"""
    # 统一从规范入口加载；坏 YAML 显式抛错（不再静默丢表）
    from core.schema_matcher import load_schemas as _load_schemas
    yaml_tables = set()
    missing = []
    for t in _load_schemas(schema_dir):
        if t and t.get("name"):
            yaml_tables.add(t["name"])
            if t["name"] not in db_tables:
                missing.append(t["name"])
    if missing: return yaml_tables, "配置与数据库不一致"
    # 检查1: DB→YAML 方向——数据库中有配置文件未定义的多余表
    # 联邦数据库：只检查默认数据源的表（其他数据源的表可能属于其他行业）
    from core.datasource_manager import DataSourceManager as _DSM_check
    _dsm_check = _DSM_check()
    try:
        _default_drv_tables = set(_dsm_check.get_driver(_dsm_check.get_default_name()).list_tables())
    except Exception:
        _default_drv_tables = db_tables  # 回退到全部表（单数据源模式）
    # 系统表/内部表不视为多余：认证/权限/会话等基础设施表、系统 meta 表（meta_ 前缀）、
    # SQLite 内部表（sqlite_ 前缀），均不属于行业配置
    _SYSTEM_TABLES = {"users", "roles", "permissions", "role_permissions", "sessions"}
    # 表归属（致命点4）：任一行业 YAML 定义过的表 = 有主，不算多余。
    # 注意：必须并集当前行业 yaml_tables —— 测试夹具可能用临时 schema_dir，
    # 其表不在真实 industries 目录里，但仍是"有主表"（当前行业定义即算）。
    all_industry_tables = yaml_tables | _sm._all_industry_yaml_tables()
    extra_tables = [t for t in _default_drv_tables
                    if t not in all_industry_tables and t not in _SYSTEM_TABLES
                    and not t.startswith(("sqlite_", "meta_"))]
    if extra_tables:
        # 记录本次多余表，供 allow_heal="drop" 识别「删除多余表」的修复操作
        _sm._last_extra_tables = set(extra_tables)
        # 单行业应用纪律：默认数据源中不应存在当前行业 YAML 未定义的表。
        # 若出现，多为历史测试/其他行业表的残留，提示清理而非放行。
        return yaml_tables, (f"数据库中有配置文件未定义的表: {', '.join(sorted(extra_tables))}，"
                             f"可能是其他行业表残留；单行业应用请清理这些表或为其补充配置")
    return yaml_tables, ""


def _check_columns(tname, t, db_cols) -> str:
    """列级校验：YAML↔DB 字段名/类型/属性逐项对比 + DB 多余字段。一致返回 ""。
    db_cols: {小写列名: 列信息dict}（drv.get_columns 结果）"""
    for col in t.get("columns", []):
        cn = col["name"]
        cn_lower = cn.lower()
        if cn_lower not in db_cols:
            return f"表 {tname} 的字段 '{cn}' 在数据库中不存在，配置与数据库不一致"
        # 全量对比：统一化后直接比较 dict
        type_map = {"VARCHAR":"TEXT","CHAR":"TEXT","INT":"INTEGER","BOOL":"INTEGER","FLOAT":"REAL","DOUBLE":"REAL","DECIMAL":"REAL"}
        yc = {k: v for k, v in col.items()}
        dc = {k: v for k, v in db_cols[cn_lower].items()}
        for dd in [yc, dc]:
            if "type" in dd:
                raw = str(dd["type"]).upper()
                base = raw.split("(")[0].strip()  # 去掉精度后缀: REAL(4) → REAL
                dd["type"] = type_map.get(base, base)
            if "not_null" in dd: dd["not_null"] = bool(dd["not_null"])
        yc.setdefault("not_null", False)
        dc.setdefault("not_null", False)
        # SQLite 的 INTEGER PRIMARY KEY 隐含 NOT NULL，但 PRAGMA table_info 返回 not_null=0
        if dc.get("pk") and not dc.get("not_null"):
            dc["not_null"] = True
        # YAML 侧同理：is_pk/pk 的字段也隐含 NOT NULL
        if yc.get("is_pk") or yc.get("pk"):
            yc["not_null"] = True
        common = set(yc.keys()) & set(dc.keys())
        diff = {k: (yc.get(k), dc.get(k)) for k in common if yc.get(k) != dc.get(k)}
        if diff:
            return MSG_CONSISTENCY_FIELD_DIFF.format(table=tname, field=cn, diff=diff)
    # 检查1.5: DB→YAML 方向——DB 中有多余字段
    yaml_col_names = set(c["name"].lower() for c in t.get("columns", []))
    extra_db_cols = set(db_cols.keys()) - yaml_col_names
    if extra_db_cols:
        return f"表 {tname} 在数据库中有配置文件未定义的字段: {', '.join(extra_db_cols)}"
    return ""


def _check_foreign_keys(tname, t, db_tables, phys_conn, dsm) -> str:
    """外键深度对比：引用表存在性 + 列名 + ref_columns。一致返回 ""。
    phys_conn: 表所属数据源的裸 SQLite 连接（非 SQLite 传 None，跳过 PRAGMA 查询）
    dsm: DataSourceManager，用于判断跨数据源外键（跨库 FK 跳过深度校验）"""
    # 联邦数据库：使用物理 Driver 的 conn 执行 PRAGMA（按表路由）
    # 无裸连接（非 SQLite，或 daemon 模式 RPC 驱动）→ PRAGMA 深度校验降级跳过；
    # 但"引用表存在性"只用 db_tables 名单，与连接无关，任何模式都必须查
    #（降级只降列级对比，不降表存在性——否则 fk.ref_missing 误报为空）
    db_fks = {}
    if phys_conn is not None:
        try:
            for fk_row in phys_conn.execute(f"PRAGMA foreign_key_list({safe_pragma_arg(tname)})").fetchall():
                # fk_row: (id, seq, table, from, to, on_update, on_delete, match)
                db_fks.setdefault(fk_row[2], []).append((fk_row[3], fk_row[4]))
        except Exception:
            pass  # 非 SQLite 数据源跳过 PRAGMA 查询
    for fk in t.get("foreign_keys", []):
        ref = fk.get("references", "")
        if ref and ref not in db_tables:
            return f"外键引用表 '{ref}' 在数据库中不存在（定义在 {tname} 中）"
        # 无裸连接（daemon/非 SQLite）：列级深度对比降级跳过（不误报"不一致"）
        if phys_conn is None:
            continue
        # 联邦数据库：跨数据源外键无法在 SQLite 中实际创建（跨库 FK 限制），
        # 跳过跨数据源外键的深度校验，仅校验同库外键
        try:
            ref_ds = dsm.get_datasource_for_table(ref)
            if ref_ds != dsm.get_datasource_for_table(tname):
                continue  # 跨数据源外键，跳过深度校验
        except Exception:
            pass  # 无法判断数据源时，按同库处理
        fk_cols = fk.get("columns", [])
        ref_cols = fk.get("ref_columns", ["id"])
        db_fk_pairs = db_fks.get(ref, [])
        for fc, rc in zip(fk_cols, ref_cols):
            if (fc, rc) not in db_fk_pairs:
                return f"表 {tname} 的外键 '{fc}→{ref}.{rc}' 与数据库不一致，DB={db_fk_pairs}"
    return ""


def _check_indexes(tname, t, phys_conn) -> str:
    """索引深度对比：名称 + unique + 列 + 多余索引。一致返回 ""。
    phys_conn: 表所属数据源的裸 SQLite 连接（非 SQLite 传 None，跳过 PRAGMA 查询）"""
    # 无裸连接（非 SQLite，或 daemon 模式 RPC 驱动）→ 索引深度校验整体降级跳过：
    # 不得拿空集对比 YAML——会把"未校验"误报成"索引缺失"
    if phys_conn is None and t.get("indexes"):
        from core.logger import get_logger
        get_logger(__name__).debug(
            "表 %s 索引深度校验降级跳过（无裸连接/daemon 模式）", tname)
        return ""
    # 联邦数据库：使用物理 Driver 的 conn 执行 PRAGMA（按表路由）
    yaml_idxs = {i.get("name",""): i for i in t.get("indexes", [])}
    db_idx_rows = []
    db_idxs = {}
    if phys_conn is not None:
        try:
            db_idx_rows = [r for r in phys_conn.execute(f"PRAGMA index_list({safe_pragma_arg(tname)})").fetchall() if not r[1].startswith("sqlite_")]
            db_idxs = {r[1]: r for r in db_idx_rows}
        except Exception:
            pass  # 非 SQLite 数据源跳过 PRAGMA 查询
    for idx_name, idx_info in yaml_idxs.items():
        if idx_name not in db_idxs:
            return MSG_CONSISTENCY_INDEX_MISSING.format(table=tname, name=idx_name)
        if idx_info.get("unique") != bool(db_idxs[idx_name][2]):
            return f"表 {tname} 的索引 '{idx_name}' unique不一致：YAML={idx_info.get('unique')}，DB={bool(db_idxs[idx_name][2])}"
        # 对比索引列
        db_idx_cols = []
        if phys_conn is not None:
            try:
                db_idx_cols = [r[2] for r in phys_conn.execute(f"PRAGMA index_info({safe_pragma_arg(idx_name)})").fetchall()]
            except Exception:
                pass  # 非 SQLite 数据源跳过 PRAGMA 查询
        yaml_idx_cols = idx_info.get("columns", [])
        if [c.lower() for c in db_idx_cols] != [c.lower() for c in yaml_idx_cols]:
            return f"表 {tname} 的索引 '{idx_name}' 列不一致：YAML={yaml_idx_cols}，DB={db_idx_cols}"
    # 检查 DB 中多余的索引
    for idx_name in db_idxs:
        if idx_name not in yaml_idxs:
            return f"表 {tname} 有数据库中多余的索引 '{idx_name}'，配置文件中未定义"
    return ""


def _preflight_check():
    _sm._last_extra_tables = set()  # 每次检查重置
    schema_dir = _PROJECT_ROOT / "industries" / settings.INDUSTRY / "schemas"
    fields_path = _PROJECT_ROOT / "industries" / settings.INDUSTRY / "fields" / "fields.yml"
    schema_empty = not schema_dir.exists() or not list(schema_dir.glob("*.yaml"))
    if schema_empty:
        try:
            from core.data_ops import get_driver as _get_fed_driver; drv = _get_fed_driver()
            real_tables = [t for t in drv.list_tables() if not t.startswith("sqlite_")]
            if real_tables:
                return "表结构配置文件不存在或为空，但数据库中有表。请先创建标准表或导入模板修复一致性问题。"
        except Exception:
            pass  # DB 连不上时报 DB 异常，不在这里报
    if not fields_path.exists():
        return "字段别名文件不存在"
    try:
        from core.data_ops import get_driver as _get_fed_driver; drv = _get_fed_driver()
        if not drv.ping(): return "数据库连接失败"
    except Exception as e: return f"数据库异常: {e}"
    # 联邦数据库：使用 FederatedDriver 聚合所有数据源的表
    db_tables = set(drv.list_tables())
    # 表级校验（YAML↔DB 双向）
    _yaml_tables, err = _check_tables(schema_dir, db_tables)
    if err: return err
    # 深度校验：YAML 与 DB 的表结构逐项对比（使用 FederatedDriver 路由到正确数据源）
    from core.data_ops import get_driver as _get_fed_driver; drv = _get_fed_driver()
    # 联邦数据库：用于 PRAGMA 查询的物理 Driver（按表路由）
    from core.datasource_manager import DataSourceManager
    _dsm = DataSourceManager()
    from core.schema_matcher import load_schemas as _load_schemas_deep
    for t in _load_schemas_deep(schema_dir):
        if not t or not t.get("name"): continue
        tname = t["name"]
        if tname not in db_tables: continue  # 表级已在 missing 中处理
        # 联邦数据库：获取表所属数据源的物理 Driver（用于 PRAGMA 深度校验）
        # 根因修复：get_driver_for_table 返回 ContractDriver 包装层，无 .conn 属性，
        # 旧的 hasattr(phys_drv, "conn") 恒为 False → 深度校验被静默跳过且误报索引/外键缺失。
        # 改为解包到底层 SqliteDriver 取裸连接；取不到（非 SQLite）才降级跳过。
        try:
            phys_conn = _sm._unwrap_sqlite_conn(_dsm.get_driver_for_table(tname))
        except Exception:
            phys_conn = None
        # 对比字段名和类型
        try:
            db_cols = {c["name"].lower(): c for c in drv.get_columns(tname)}
        except Exception:
            continue
        # 列 → 外键 → 索引，顺序与原实现一致（影响报错先后），首个错误即返回
        err = _check_columns(tname, t, db_cols)
        if err: return err
        err = _check_foreign_keys(tname, t, db_tables, phys_conn, _dsm)
        if err: return err
        err = _check_indexes(tname, t, phys_conn)
        if err: return err
    return ""

def require_consistency(func=None, *, allow_heal: str = ""):
    """一致性守卫：DDL 前先过 _preflight_check，不一致即阻止。

    allow_heal="add"：当唯一不一致恰为"被加字段在 DB 缺失"时放行——
    加这个字段本身就是恢复一致（否则不一致状态会死锁：修它的操作也被拦）。
    allow_heal="drop"：当唯一不一致恰为"待删表是 DB 多余表（DB 有、YAML 无）"时放行——
    删这张多余表本身就是恢复一致（生产死锁场景：反向不一致时连删除操作都被拦）。
    """
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            e = _sm._preflight_check()
            if e and allow_heal == "add":
                col = kwargs.get("column") or (args[1] if len(args) > 1 else "")
                tbl = kwargs.get("table") or (args[0] if args else "")
                only = f"表 {tbl} 的字段 '{col}' 在数据库中不存在，配置与数据库不一致"
                if e == only:
                    e = ""
            if e and allow_heal == "drop":
                # 仅当「唯一不一致」恰为待删表属于 DB 多余表时放行。
                # 双重校验：tbl 在 _last_extra_tables 中 + e 确实是"多余表"错误且点名 tbl
                # （防止其他类型不一致被误放行；与 allow_heal="add" 的精确消息匹配对称）
                tbl = kwargs.get("table") or (args[0] if args else "")
                _EXTRA_PREFIX = "数据库中有配置文件未定义的表:"
                if (tbl and is_valid_identifier(tbl)
                        and tbl in _sm._last_extra_tables
                        and e.startswith(_EXTRA_PREFIX)
                        and tbl in e):
                    e = ""
            if e: return {"ok": False, "message": MSG_CONSISTENCY_BLOCK.format(reason=e)}
            return f(*args, **kwargs)
        return wrapper
    return deco(func) if func is not None else deco
