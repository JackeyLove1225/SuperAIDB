"""入库层：提取结果 → 关系数据库（savepoint 单库事务 / saga 跨库补偿）

行级容错：独立数据的坏行进失败清单不拖垮好行；组级（FK 依赖）全或无。
"""
import json as _json
from collections import Counter
from core.logger import get_logger

from core.contract.security_contract import SecurityContract, safe_table_sql, safe_column_sql
from core.data_ops import insert_rows
from pipeline.fc_schema import (_get_extraction_tables, _find_main_fks_to_base,
                                _find_fk_to_main)

logger = get_logger(__name__)

def _convert_main_virtual_codes(cfg, drv, main_table, main_rows):
    """主表入库前：把虚拟业务编码字段转换为外键 id，并删除虚拟字段

    AI 输出的主表数据包含虚拟字段（如 region_code），这里查基础表把编码转为 id，
    填到对应的外键列（如 region_id），然后删除虚拟字段。
    cfg 为 None（测试/旧调用）时无行业外键拓扑，直接跳过。
    """
    if cfg is None:
        return
    extraction_tables = _get_extraction_tables(cfg, drv)
    main_fks_to_base = _find_main_fks_to_base(cfg, main_table, extraction_tables)
    if not main_fks_to_base:
        return
    for row in main_rows:
        for fk_col, ref_table, ref_code in main_fks_to_base:
            code_val = row.pop(ref_code, None)  # 删除虚拟字段
            if code_val is None or code_val == "":
                continue
            safe_val = str(code_val).replace("'", "''")
            sel = drv.query(f"SELECT id FROM {ref_table} WHERE {ref_code}='{safe_val}' LIMIT 1")
            if sel:
                row[fk_col] = sel[0].get("id")
            else:
                logger.warning("    警告: %s.%s='%s' 未找到对应记录", ref_table, ref_code, code_val)


# ── 读取源数据 ──

def _saga_conflict_precheck(fed, main_table, code_field, main_rows, overwrite):
    """冲突预检（与单库路径语义一致：编码已存在且非 overwrite → 待确认）"""
    if not (main_rows and code_field and not overwrite):
        return False
    cv_val = main_rows[0].get(code_field)
    if not cv_val:
        return False
    safe_cv = str(cv_val).replace("'", "''")
    sel = fed.query(
        f"SELECT id FROM {safe_table_sql(main_table)} "
        f"WHERE {safe_column_sql(code_field)}='{safe_cv}' LIMIT 1")
    return bool(sel)


def _saga_add_detail_steps(saga, dsm, cfg, tables, main_table, code_field,
                           main_rows, overwrite):
    """明细表步骤装配（各自数据源）；有外键列但无主表数据的表按现有语义跳过"""
    from core.federation.saga import SagaStep
    for tname, trows in tables.items():
        if tname == main_table or not trows:
            continue
        fk_col = _find_fk_to_main(cfg, tname, main_table) if cfg else ""
        has_physical_fk = (fk_col and
                           dsm.get_driver_for_table(tname).column_exists(tname, fk_col))
        if has_physical_fk and not main_rows:
            logger.warning("    跳过 %s (%d条): 主表无对应记录，无法关联", tname, len(trows))
            continue
        for row in trows:
            row.pop(code_field, None)  # 删除虚拟关联字段
        saga.add_step(SagaStep(
            datasource=dsm.get_datasource_for_table(tname),
            action="insert", table=tname, rows=trows, overwrite=overwrite))


def _saga_failure_reason(result):
    """saga 失败口径装配：补偿完成与否分开说——补偿未完成绝不说"已补偿\""""
    if result.get("compensated"):
        return (f"步骤{result.get('failed_step')}失败，已逆序补偿（库已回本组写入前状态）"
                f": {result.get('error', '')}")
    # 补偿未完成必须如实大声说——残缺在哪、journal 在哪，绝不说"已补偿"
    return (f"步骤{result.get('failed_step')}失败且补偿未完成（数据可能有残留！）"
            f": {result.get('error', '')}。journal: {result.get('journal', '')}，"
            "重启应用后将自动续滚清理")


def _ingest_group_via_saga(cfg, cv, tables, main_table, code_field, overwrite):
    """跨数据源组的入库：无共享事务（SQLite 物理限制），走 saga 补偿

    仅当组内表分布在多个数据源时调用（单库场景走 savepoint 路径，行为不变）。
    步骤编排：按数据源分段、主表先于明细表；任一步失败 → saga 逆序补偿
    已提交步骤（库回到本组写入前状态），状态落盘 db/saga_journal/ 可续滚。

    Returns:
        {"ok": bool, "conflict": bool, "reason": str}
    """
    from core.datasource_manager import DataSourceManager
    from core.data_ops import get_driver
    from core.federation.saga import Saga, SagaStep
    dsm = DataSourceManager()
    fed = get_driver()

    main_rows = tables.get(main_table) or []

    if _saga_conflict_precheck(fed, main_table, code_field, main_rows, overwrite):
        return {"ok": False, "conflict": True, "reason": "数据已存在，待确认"}

    saga = Saga(label=f"pipeline:{cv}")

    def _fill_detail_fk(s, idx):
        """主表步骤提交后：把捕获的主表 id 填入后续明细步骤的外键列（若该列物理存在）"""
        ids = s.steps[idx].compensation_data.get("inserted_ids", [])
        mid = ids[0] if ids else None
        if not mid:
            return
        for st in s.steps[idx + 1:]:
            fk_col = _find_fk_to_main(cfg, st.table, main_table) if cfg else ""
            if not fk_col:
                continue
            if not dsm.get_driver_for_table(st.table).column_exists(st.table, fk_col):
                continue  # 跨数据源外键物理上被过滤（见 FederatedDriver.create_table）
            for row in st.rows:
                row[fk_col] = mid

    # 步骤1：主表（先于明细表）
    if main_rows:
        _convert_main_virtual_codes(cfg, fed, main_table, main_rows)
        saga.add_step(SagaStep(
            datasource=dsm.get_datasource_for_table(main_table),
            action="insert", table=main_table, rows=main_rows,
            overwrite=overwrite, on_committed=_fill_detail_fk))

    # 步骤2..：明细表（各自数据源）；有外键列但无主表数据的表按现有语义跳过
    _saga_add_detail_steps(saga, dsm, cfg, tables, main_table, code_field,
                           main_rows, overwrite)

    if not saga.steps:
        return {"ok": False, "conflict": False, "reason": "无可写入的步骤（全部跳过）"}
    # overwrite 的内部 DELETE 权限：由 ContractDriver.insert 单点收口
    #（overwrite=True 时 INSERT+DELETE 同查）——saga 步骤经 ContractDriver
    # 执行，权限栈自动继承；挂载写路径走裸连接，在其自身处显式判定
    result = saga.execute()
    if not result.get("ok"):
        return {"ok": False, "conflict": False, "reason": _saga_failure_reason(result)}
    return {"ok": True, "conflict": False, "reason": ""}


def _open_group_conn(dsm, table_ds, main_table):
    """建立挂载写连接（批次复用与单次调用共用唯一实现）

    主连接 = 主表所在数据源（组的锚）；其余 SQLite 库 ATTACH 上来。
    路径锚定与 DataSourceManager._create_driver 同一实现（resolve_sqlite_path）
    ——不靠 CWD 隐式约定。
    Returns: (conn, main_ds)
    """
    from core.federation.attached import open_attached, alias_for
    from core.datasource_manager import resolve_sqlite_path
    ds_meta = {d["name"]: d for d in dsm.list_datasources()}
    main_ds = table_ds.get(main_table) or dsm.get_default_name()
    others = []
    for ds in sorted(set(table_ds.values())):
        if ds != main_ds:
            others.append((alias_for(ds), resolve_sqlite_path(ds_meta[ds]["database"])))
    conn = open_attached(resolve_sqlite_path(ds_meta[main_ds]["database"]), others)
    return conn, main_ds


class _AttachedConnPool:
    """批次级挂载写连接池：同 (main_ds, others) 组复用单连接

    每组各开连接 = 每组各付一次 open_db 密钥派生 + ATTACH，
    大文件数百组时是纯浪费。组级 savepoint 语义不变（每组仍 SAVEPOINT→
    commit/ROLLBACK+RELEASE）；组失败丢弃该缓存连接，下一组重建。
    """

    def __init__(self):
        self._conns = {}

    @staticmethod
    def _key(dsm, table_ds, main_table):
        main_ds = table_ds.get(main_table) or dsm.get_default_name()
        return (main_ds, tuple(sorted(
            ds for ds in set(table_ds.values()) if ds != main_ds)))

    def get(self, dsm, table_ds, main_table):
        key = self._key(dsm, table_ds, main_table)
        if key not in self._conns:
            self._conns[key] = _open_group_conn(dsm, table_ds, main_table)[0]
        return self._conns[key]

    def discard(self, dsm, table_ds, main_table):
        conn = self._conns.pop(self._key(dsm, table_ds, main_table), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass  # 丢弃失败的连接关闭异常无碍——OS 进程退出时回收

    def close_all(self):
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass  # 批次收尾关闭失败无碍——OS 进程退出时回收
        self._conns.clear()


def _attached_qualifier(table_ds, main_ds):
    """表名 → 挂载命名空间限定（主库无前缀，挂载库 att_ds."t"）"""
    from core.federation.attached import alias_for

    def qt(table):
        ds = table_ds[table]
        if ds == main_ds:
            return safe_table_sql(table)
        return f"{alias_for(ds)}.{safe_table_sql(table)}"
    return qt


def _attached_conflict_precheck(conn, qt, main_table, code_field, main_rows, overwrite):
    """冲突预检（与 saga 路径一致：编码已存在且非 overwrite → 待确认）"""
    if not (main_rows and code_field and not overwrite):
        return False
    cv_val = main_rows[0].get(code_field)
    if not cv_val:
        return False
    sel = conn.execute(
        f"SELECT id FROM {qt(main_table)} "
        f"WHERE {safe_column_sql(code_field)}=? LIMIT 1", (str(cv_val),)).fetchall()
    return bool(sel)


def _attached_check_permissions(policy, table_ds, tables, overwrite):
    """组级权限判定（与 saga/驱动路径同源）

    overwrite 的内部 DELETE 同查（只查 INSERT 会让
    "只许写不许删"的角色借 overwrite 删掉既有记录）
    """
    from core.permission import Operation
    for tname, trows in tables.items():
        if not trows:
            continue
        policy.check(table_ds[tname], Operation.INSERT, "pipeline:ingest", table=tname)
        if overwrite:
            policy.check(table_ds[tname], Operation.DELETE, "pipeline:ingest", table=tname)


def _attached_write_main(conn, qt, policy, table_ds, main_table, main_rows,
                         code_field, overwrite):
    """主表写入段：逐行 INSERT（overwrite 先按编码 DELETE），返回捕获的主表 id 列表"""
    from core.permission import Operation
    inserted_main_ids = []
    if not main_rows:
        return inserted_main_ids
    before = conn.execute(
        f"SELECT MAX(id) AS m FROM {qt(main_table)}").fetchone()
    max_before = (before[0] or 0) if before else 0
    for row in main_rows:
        cols = [c for c in row.keys() if c.lower() != "id"]
        for c in cols:
            SecurityContract.validate_identifier(c, "字段名")
            policy.check_column(table_ds[main_table], main_table, c, Operation.INSERT)
        if overwrite and code_field and row.get(code_field) is not None:
            conn.execute(
                f"DELETE FROM {qt(main_table)} WHERE {safe_column_sql(code_field)}=?",
                (row[code_field],))
        col_sql = ", ".join(safe_column_sql(c) for c in cols)
        conn.execute(
            f"INSERT INTO {qt(main_table)} ({col_sql}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [row[c] for c in cols])
    after = conn.execute(
        f"SELECT id FROM {qt(main_table)} WHERE id > ?", (int(max_before),)).fetchall()
    return [r[0] for r in after]


def _attached_write_details(conn, qt, policy, dsm, cfg, table_ds, tables,
                            main_table, code_field, overwrite, inserted_main_ids):
    """明细表写入段：FK 回填（捕获的主表 id）+ 无主跳过（与 saga 路径同语义）

    返回 wrote_any——主表或任一明细表有实际写入。
    """
    from core.permission import Operation
    mid = inserted_main_ids[0] if inserted_main_ids else None
    wrote_any = bool(inserted_main_ids)
    for tname, trows in tables.items():
        if tname == main_table or not trows:
            continue
        fk_col = _find_fk_to_main(cfg, tname, main_table) if cfg else ""
        has_physical_fk = (fk_col and
                           dsm.get_driver_for_table(tname).column_exists(tname, fk_col))
        if has_physical_fk and not inserted_main_ids:
            logger.warning("    跳过 %s (%d条): 主表无对应记录，无法关联", tname, len(trows))
            continue
        wrote_any = True
        for row in trows:
            # overwrite 判定必须先取码值再删虚拟列（旧序先 pop 后判，
            # 明细 overwrite 的 DELETE 永不触发，重跑入库产生重复明细行）
            code_val = row.get(code_field) if code_field else None
            row.pop(code_field, None)  # 删除虚拟关联字段
            if fk_col and mid and has_physical_fk:
                row[fk_col] = mid
            cols = [c for c in row.keys() if c.lower() != "id"]
            for c in cols:
                SecurityContract.validate_identifier(c, "字段名")
                policy.check_column(table_ds[tname], tname, c, Operation.INSERT)
            if overwrite and code_field and code_val is not None:
                conn.execute(
                    f"DELETE FROM {qt(tname)} WHERE {safe_column_sql(code_field)}=?",
                    (code_val,))
            col_sql = ", ".join(safe_column_sql(c) for c in cols)
            conn.execute(
                f"INSERT INTO {qt(tname)} ({col_sql}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [row[c] for c in cols])
    return wrote_any


def _ingest_group_attached(cfg, cv, tables, main_table, code_field, overwrite, conn=None):
    """全 SQLite 跨数据源组的挂载写（真原子：savepoint 覆盖全部挂载库）

    与 saga 路径语义逐项对齐（冲突预检/虚拟编码转换/FK 回填/无主明细跳过），
    本质区别：任何一步失败时 ROLLBACK TO SAVEPOINT——跨文件真回滚，
    主库连写前快照都不留（saga 是提交后逆序补偿，这是原子撤销）。
    仅当组内全部数据源为 SQLite 时由 write_batch_groups 分派调用。

    conn=None 时自开自关（单次调用）；传入批次共享连接时跳过开/关，
    连接生命周期归调用方（_AttachedConnPool）管理。
    """
    from core.datasource_manager import DataSourceManager
    from core.data_ops import get_driver
    dsm = DataSourceManager()
    fed = get_driver()

    table_ds = {t: dsm.get_datasource_for_table(t) for t in tables}
    main_rows = tables.get(main_table) or []

    own_conn = conn is None
    if own_conn:
        conn, main_ds = _open_group_conn(dsm, table_ds, main_table)
    else:
        main_ds = table_ds.get(main_table) or dsm.get_default_name()
    try:
        qt = _attached_qualifier(table_ds, main_ds)

        if _attached_conflict_precheck(conn, qt, main_table, code_field,
                                       main_rows, overwrite):
            return {"ok": False, "conflict": True, "reason": "数据已存在，待确认"}

        _convert_main_virtual_codes(cfg, fed, main_table, main_rows)

        # 权限判定与 saga/驱动路径同源（挂载写曾裸连直写、
        # 权限栈整体缺席——同一组数据走 saga 被拦、走挂载写就放行）
        from core.permission import PermissionPolicy
        policy = PermissionPolicy.get_instance()
        _attached_check_permissions(policy, table_ds, tables, overwrite)

        conn.execute("SAVEPOINT att_group")
        try:
            inserted_main_ids = _attached_write_main(
                conn, qt, policy, table_ds, main_table, main_rows,
                code_field, overwrite)
            wrote_any = _attached_write_details(
                conn, qt, policy, dsm, cfg, table_ds, tables, main_table,
                code_field, overwrite, inserted_main_ids)
            if not wrote_any:
                # 与 saga 路径同语义：全部跳过=无可写入，
                # 如实报失败，不静默提交一个零写入的空组
                conn.execute("ROLLBACK TO SAVEPOINT att_group")
                conn.execute("RELEASE att_group")
                conn.commit()
                return {"ok": False, "conflict": False,
                        "reason": "无可写入的步骤（全部跳过）"}
            conn.commit()
            logger.info("  组 %s 挂载写提交（真原子，%d 个挂载库）", cv,
                        len({ds for ds in table_ds.values() if ds != main_ds}))
            return {"ok": True, "conflict": False, "reason": ""}
        except Exception as e:
            conn.execute("ROLLBACK TO SAVEPOINT att_group")
            conn.execute("RELEASE att_group")
            conn.commit()
            # 403 语义不吞：权限拒绝如实上抛——
            # 吞成 ok=False 会把"无权操作"伪装成"数据失败"，审计链断掉
            from core.permission import PermissionDenied
            if isinstance(e, PermissionDenied):
                raise
            return {"ok": False, "conflict": False,
                    "reason": f"挂载写已跨库真回滚（无任何残留）: {str(e)[:180]}"}
    finally:
        if own_conn:
            conn.close()




def _group_rows_by_code(tables_data, cfg, bt_main_table, bt_code_field):
    """按主表业务编码分组 → {code_value: {table_name: [rows]}}

    主表用自身 code_field，明细表用虚拟关联字段；明细行经外键列携带主表
    编码值（如 quota_item_id='A1-25'）时，同样按码分组——写入阶段再由
    _fill_detail_fk 把码替换为解析后的主表数字 id
    """
    from pipeline.common import norm_code_value as _ncv
    fk_cols_of = {}  # {表名: [引用主表的外键列]}
    for _t in ((cfg.tables if cfg is not None else []) or []):
        _fks = []
        for _fk in (_t.get("foreign_keys") or []):
            if _fk.get("references") == bt_main_table:
                _fks.extend(_fk.get("columns") or [])
        if _fks:
            fk_cols_of[_t.get("name", "")] = _fks

    groups = {}  # {code_value: {table_name: [rows]}}
    for td in tables_data:
        name = td.get("name", "")
        for row in td.get("rows", []):
            cv = row.get(bt_code_field)
            if not cv and name in fk_cols_of:
                for _fc in fk_cols_of[name]:
                    _v = row.get(_fc)
                    if _v and not str(_v).isdigit():
                        cv = _ncv(_v)
                        break
            cv = cv or "__none__"
            if cv not in groups:
                groups[cv] = {}
            if name not in groups[cv]:
                groups[cv][name] = []
            groups[cv][name].append(row)
    return groups


def _dedup_group_main_rows(cv, tables, bt_main_table, failures):
    """组内同码主行去重（白盒：一个业务编码 = 一条主记录）——上提到分派前
    三路径共用（此前只在单库段，saga/挂载写路径会被 AI 过度
    展开的同码主行顶爆唯一键，切多数据源演示联邦特性时整组失败）
    """
    _mrows0 = tables.get(bt_main_table)
    if not (_mrows0 and len(_mrows0) > 1):
        return
    seen = {}
    for r in _mrows0:
        seen.setdefault(_json.dumps(r, sort_keys=True, ensure_ascii=False), r)
    deduped = list(seen.values())
    dups = []
    if len(deduped) < len(_mrows0):
        logger.info("    组 %s: 主表重复行去重 %d→%d", cv, len(_mrows0), len(deduped))
    if len(deduped) > 1:
        # 同码不同内容：保留非空字段最多的行（真记录字段全，
        # 混入的碎片行只有码+一两个字段），其余如实上报
        best = max(deduped,
                   key=lambda r: sum(1 for v in r.values() if str(v).strip()))
        dups = [r for r in deduped if r is not best]
        logger.warning("    组 %s: 同码不同内容主行 %d 条，保留字段最全行其余上报",
                       cv, len(dups))
        deduped = [best]
    tables[bt_main_table] = deduped
    for r in dups:
        failures.append({"table": bt_main_table, "row": r,
                         "reason": f"同一业务编码 {cv} 的重复主记录"
                                   f"（内容不同），已跳过待确认"})


def _ingest_group_cross_datasource(cfg, cv, tables, bt_main_table, bt_code_field,
                                   overwrite, ds_names, pool, failures, conflict_ids):
    """跨数据源组的分派：全 SQLite 组 → 挂载写（真原子）；含跨引擎组 → saga 补偿"""
    from core.datasource_manager import DataSourceManager
    # 组级容错：跨库分派段此前无 try——pool.get/
    # open_attached（WAL-busy 拒绝是现实路径）等逃逸异常会穿透
    # 炸掉整批剩余组，与单库路径"坏组不拖垮好组"语义不一致；
    # PermissionDenied 仍然上抛（403 语义不吞）
    try:
        _types = {d["name"]: d.get("type", "sqlite")
                  for d in DataSourceManager().list_datasources()}
        if all(_types.get(n, "sqlite") == "sqlite" for n in ds_names):
            logger.info("  组 %s 跨数据源 %s（全 SQLite），走挂载写（真原子）",
                        cv, sorted(ds_names))
            # 批次级连接复用：同 (main_ds, others) 的组共享
            # 单连接，省掉每组 open_db 密钥派生 + ATTACH；组失败丢弃重建
            _tds = {t: DataSourceManager().get_datasource_for_table(t) for t in tables}
            _conn = pool.get(DataSourceManager(), _tds, bt_main_table)
            r = _ingest_group_attached(cfg, cv, tables, bt_main_table,
                                       bt_code_field, overwrite, conn=_conn)
            if not r.get("ok") and not r.get("conflict"):
                pool.discard(DataSourceManager(), _tds, bt_main_table)
        else:
            logger.info("  组 %s 跨数据源 %s，走 saga 补偿入库", cv, sorted(ds_names))
            r = _ingest_group_via_saga(cfg, cv, tables, bt_main_table,
                                       bt_code_field, overwrite)
    except Exception as _ge:
        from core.permission import PermissionDenied
        if isinstance(_ge, PermissionDenied):
            raise
        logger.error("  ! %s: 跨库组异常 - %s", cv, str(_ge)[:80])
        failures.append({"table": bt_main_table,
                         "row": (tables.get(bt_main_table) or [{}])[0],
                         "reason": str(_ge)[:80], "group_fail": True})
        return
    if r.get("conflict"):
        conflict_ids.append(cv)
        logger.warning("  ? %s: 数据已存在，待确认", cv)
    elif r.get("ok"):
        for tname, trows in tables.items():
            logger.info("  写入: %s -> %s (%d条)", cv, tname, len(trows))
    else:
        # 失败口径如实（挂载写是回滚非补偿，saga 补偿可能未完成——
        # 一律以 r.reason 为准，不再统一说"已补偿"）
        logger.error("  ! %s: 跨库写入失败 - %s", cv, r.get("reason", ""))
        failures.append({"table": bt_main_table,
                         "row": (tables.get(bt_main_table) or [{}])[0],
                         "reason": r.get("reason", "unknown"),
                         "group_fail": True})  # 组级失败标记（缺它则
                                 # systemic_error 护栏对跨库组失效）


def _lookup_main_id(drv, bt_main_table, bt_code_field, code_val):
    """按业务编码回查主表 id（单库写入段两处共用：写后取新 id / 跨批按组键取已入库 id）"""
    safe_cv = str(code_val).replace("'", "''")
    SecurityContract.validate_identifier(bt_main_table, "主表名")
    SecurityContract.validate_identifier(bt_code_field, "编码字段")
    sel = drv.query(f"SELECT id FROM {safe_table_sql(bt_main_table)} WHERE {safe_column_sql(bt_code_field)}='{safe_cv}' LIMIT 1")
    if sel:
        return sel[0].get("id")
    return None


def _write_details_single_db(cfg, tables, bt_main_table, bt_code_field,
                             overwrite, main_id, failures):
    """明细表写入段（单库 savepoint 事务内）

    Returns: (all_ok, has_conflict)
    """
    all_ok = True
    has_conflict = False
    for tname, trows in tables.items():
        if tname == bt_main_table:
            continue
        fk_col = _find_fk_to_main(cfg, tname, bt_main_table)
        # main_id=None 时跳过有外键的明细表（避免 not_null 约束失败）
        if fk_col and not main_id:
            logger.warning("    跳过 %s (%d条): 主表无对应记录，无法关联", tname, len(trows))
            continue
        for row in trows:
            row.pop(bt_code_field, None)  # 始终删除虚拟关联字段
            if fk_col and main_id:
                row[fk_col] = main_id
        if not fk_col:
            # 行级（互相独立的数据——各自为政）：逐行入库，
            # 坏行进失败清单不影响好行，不触发整组回滚
            for row_no, row in enumerate(trows, start=1):
                r = insert_rows(tname, [row], overwrite=overwrite, auto_commit=False)
                if not r.get("ok"):
                    reason = r.get('message', 'unknown')
                    failures.append({"table": tname, "row": row, "reason": reason})
                    logger.warning("    行级跳过 %s 第 %d 行: %s",
                                   tname, row_no, reason)
            continue
        # 组级（有 FK 依赖，如定额+材料——全或无）：任一行失败整组回滚
        result = insert_rows(tname, trows, overwrite=overwrite, auto_commit=False)
        if result.get("conflict"):
            has_conflict = True
        elif not result.get("ok"):
            logger.error("    insert fail: %s", result.get('message', 'unknown'))
            all_ok = False
            break
    return all_ok, has_conflict


def _ingest_group_single_db(cfg, drv, cv, tables, bt_main_table, bt_code_field,
                            overwrite, failures, conflict_ids):
    """单库组：savepoint 事务写入（全或无）；冲突/失败显式回滚收尾"""
    try:
        drv.begin("sp_ingest")
        all_ok = True
        has_conflict = False

        # 先写主表，拿 id
        main_id = None
        if bt_main_table in tables:
            main_rows = tables[bt_main_table]
            # （同码主行去重已上提到分派前，三路径共用）
            # 主表入库前：虚拟业务编码字段 → 外键 id（如 region_code → region_id）
            _convert_main_virtual_codes(cfg, drv, bt_main_table, main_rows)
            result = insert_rows(bt_main_table, main_rows, overwrite=overwrite, auto_commit=False)
            if result.get("conflict"):
                has_conflict = True
            elif not result.get("ok"):
                logger.error("    insert fail: %s", result.get('message', 'unknown'))
                all_ok = False
                failures.append({"table": bt_main_table,
                                 "row": main_rows[0] if main_rows else {},
                                 "reason": result.get('message', 'unknown'),
                                 "group_fail": True})

            # 查询主表 id（用业务编码查）
            if all_ok and not has_conflict and main_rows:
                cv_val = main_rows[0].get(bt_code_field)
                if cv_val:
                    main_id = _lookup_main_id(drv, bt_main_table, bt_code_field, cv_val)

        # 如果组里没有主表数据（main_id=None），按组键编码从已入库主表查找 id——
        # 跨批拆分是常态（上批写了主表行，本批只有明细行）；
        # 明细行的编码在外键列里（quota_item_id='A1-25'），旧逻辑按虚拟码字段找不到
        if main_id is None and cv and cv != "__none__":
            main_id = _lookup_main_id(drv, bt_main_table, bt_code_field, cv)

        # 写明细表
        if all_ok and not has_conflict:
            all_ok, has_conflict = _write_details_single_db(
                cfg, tables, bt_main_table, bt_code_field, overwrite, main_id, failures)

        if has_conflict and not overwrite:
            # 冲突语义="本组放弃写入"：savepoint 必须显式回滚收尾——
            # 否则主表行/前段明细悬置在 savepoint 里，被批次末尾的全局
            # commit 静默落盘成"幽灵半组"（用户以为整组
            # 未入，库里已有主表行）
            drv.rollback("sp_ingest")
            conflict_ids.append(cv)
            logger.warning("  ? %s: 数据已存在，待确认（本组未写入）", cv)
        elif all_ok:
            drv.commit()
            for tname, trows in tables.items():
                logger.info("  写入: %s -> %s (%d条)", cv, tname, len(trows))
        else:
            drv.rollback("sp_ingest")
            logger.error("  ! %s: 写入失败，已回滚", cv)
    except Exception as e:
        drv.rollback("sp_ingest")
        logger.error("  ! %s: 异常回滚 - %s", cv, str(e)[:80])
        failed_row = (tables.get(bt_main_table) or [{}])[0] if tables else {}
        failures.append({"table": bt_main_table, "row": failed_row,
                         "reason": str(e)[:80], "group_fail": True})


def _check_systemic_error(groups, failures, all_results):
    """系统性错误护栏（别拿行级容错给代码 bug 兜底）：
    全部组因同一原因失败 = 系统性错误（代码/配置问题），不是"坏数据行"——
    升级为 all_results["systemic_error"]，由 process_file 显著上报而不是静默列清单。
    只统计真正的组级失败（group_fail）：同码去重跳过等行级备注不算组失败
    """
    if groups and failures:
        sigs = Counter(str(f.get("reason", ""))[:60] for f in failures
                       if f.get("group_fail"))
        if sigs:
            top_reason, top_count = sigs.most_common(1)[0]
            if top_count >= len(groups) and len(groups) > 0:
                all_results["systemic_error"] = {
                    "reason": top_reason,
                    "affected": top_count,
                    "groups": len(groups),
                }
                logger.error("系统性错误：全部 %d 组因同一原因失败（%s）——这不是数据问题",
                             len(groups), top_reason)


def write_batch_groups(data, cfg, drv, main_table, code_field, overwrite, all_results):
    """把一批提取数据按主表业务编码分组写入（自 runner.run() 拆出）。

    单库：savepoint 事务（全或无）；跨库：saga 补偿。
    冲突编码写 all_results["conflicts"]，行级失败写 all_results["failures"]。
    """
    # 写入数据库（兼容 AI 输出数组或对象）
    if isinstance(data, list):
        tables_data = data
    else:
        tables_data = data.get("tables", [])
    if not tables_data:
        return

    # 批次级路由场景：使用批次 meta 的主表/编码字段（与提取 schema 对齐）
    _bmeta = data.get("_batch_meta", {}) if isinstance(data, dict) else {}
    bt_main_table = _bmeta.get("main_table") or main_table
    bt_code_field = _bmeta.get("code_field") or code_field

    groups = _group_rows_by_code(tables_data, cfg, bt_main_table, bt_code_field)

    conflict_ids = []
    failures = all_results["failures"]  # 行级失败清单：独立数据的坏行不影响好行

    pool = _AttachedConnPool()
    try:
        for cv, tables in groups.items():
            _dedup_group_main_rows(cv, tables, bt_main_table, failures)
            # 跨数据源检测：组内表分布在多个数据源 → 分治：
            #   全 SQLite 组 → 挂载写（ATTACH 单连接 savepoint，跨文件真原子）；
            #   含 MySQL 等跨引擎组 → saga 补偿（物理上无共享事务，逆序补偿+journal 续滚）；
            # 单库场景保持原有 savepoint 路径不变
            from core.datasource_manager import DataSourceManager
            _ds_names = {DataSourceManager().get_datasource_for_table(t) for t in tables}
            if len(_ds_names) > 1:
                _ingest_group_cross_datasource(cfg, cv, tables, bt_main_table,
                                               bt_code_field, overwrite, _ds_names,
                                               pool, failures, conflict_ids)
                continue
            _ingest_group_single_db(cfg, drv, cv, tables, bt_main_table,
                                    bt_code_field, overwrite, failures, conflict_ids)
    finally:
        pool.close_all()

    _check_systemic_error(groups, failures, all_results)
    drv.commit()
    all_results["conflicts"] = conflict_ids
