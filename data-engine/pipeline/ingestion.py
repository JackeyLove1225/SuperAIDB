"""入库层：提取结果 → 关系数据库（savepoint 单库事务 / saga 跨库补偿）

行级容错：独立数据的坏行进失败清单不拖垮好行；组级（FK 依赖）全或无。
"""
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

def _ingest_group_via_saga(cfg, cv, tables, main_table, code_field, overwrite):
    """跨数据源组的入库：无共享事务（SQLite 物理限制），走 saga 补偿

    仅当组内表分布在多个数据源时调用（单库场景走 savepoint 路径，行为不变）。
    步骤编排：按数据源分段、主表先于明细表；任一步失败 → saga 逆序补偿
    已提交步骤（库回到本组写入前状态），状态落盘 db/saga_journal/ 可续滚。

    Returns:
        {"ok": bool, "conflict": bool, "reason": str}
    """
    from core.datasource_manager import DataSourceManager
    from core.data_ops import _get_driver
    from core.federation.saga import Saga, SagaStep
    dsm = DataSourceManager()
    fed = _get_driver()

    main_rows = tables.get(main_table) or []

    # 冲突预检（与单库路径语义一致：编码已存在且非 overwrite → 待确认）
    if main_rows and code_field and not overwrite:
        cv_val = main_rows[0].get(code_field)
        if cv_val:
            safe_cv = str(cv_val).replace("'", "''")
            sel = fed.query(
                f"SELECT id FROM {safe_table_sql(main_table)} "
                f"WHERE {safe_column_sql(code_field)}='{safe_cv}' LIMIT 1")
            if sel:
                return {"ok": False, "conflict": True, "reason": "数据已存在，待确认"}

    saga = Saga(label=f"pipeline:{cv}")

    def _fill_detail_fk(s, idx):
        """主表步骤提交后：把捕获的主表 id 填入后续明细步骤的外键列（若该列物理存在）"""
        ids = s.steps[idx].compensation_data.get("inserted_ids", [])
        mid = ids[0] if ids else None
        if not mid:
            return
        for st in s.steps[idx + 1:]:
            fk_col = _find_fk_to_main(cfg, st.table, main_table)
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
    for tname, trows in tables.items():
        if tname == main_table or not trows:
            continue
        fk_col = _find_fk_to_main(cfg, tname, main_table)
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

    if not saga.steps:
        return {"ok": False, "conflict": False, "reason": "无可写入的步骤（全部跳过）"}
    result = saga.execute()
    if not result.get("ok"):
        return {"ok": False, "conflict": False,
                "reason": f"步骤{result.get('failed_step')}失败已补偿: {result.get('error', '')}"}
    return {"ok": True, "conflict": False, "reason": ""}




def write_batch_groups(data, cfg, drv, main_table, code_field, overwrite, all_results):
    """把一批提取数据按主表业务编码分组写入（自 runner.run() 拆出，P2-1）。

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

    # 按主表业务编码分组（主表用自身 code_field，明细表用虚拟关联字段）
    # 明细行经外键列携带主表编码值（如 quota_item_id='A1-25'）时，同样按码分组——
    # 写入阶段再由 _fill_detail_fk 把码替换为解析后的主表数字 id
    from pipeline.unified import _norm_code_value as _ncv
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

    conflict_ids = []
    failures = all_results["failures"]  # 行级失败清单：独立数据的坏行不影响好行

    for cv, tables in groups.items():
        # 跨数据源检测：组内表分布在多个数据源 → 无共享事务（savepoint 只在
        # 默认数据源生效），走 saga 补偿；单库场景保持原有 savepoint 路径不变
        from core.datasource_manager import DataSourceManager
        _ds_names = {DataSourceManager().get_datasource_for_table(t) for t in tables}
        if len(_ds_names) > 1:
            logger.info("  组 %s 跨数据源 %s，走 saga 补偿入库", cv, sorted(_ds_names))
            r = _ingest_group_via_saga(cfg, cv, tables, bt_main_table,
                                       bt_code_field, overwrite)
            if r.get("conflict"):
                conflict_ids.append(cv)
                logger.warning("  ? %s: 数据已存在，待确认", cv)
            elif r.get("ok"):
                for tname, trows in tables.items():
                    logger.info("  写入: %s -> %s (%d条)", cv, tname, len(trows))
            else:
                logger.error("  ! %s: 跨库写入失败已补偿 - %s", cv, r.get("reason", ""))
                failures.append({"table": bt_main_table,
                                 "row": (tables.get(bt_main_table) or [{}])[0],
                                 "reason": r.get("reason", "unknown")})
            continue
        try:
            drv.begin("sp_ingest")
            all_ok = True
            has_conflict = False

            # 先写主表，拿 id
            main_id = None
            if bt_main_table in tables:
                main_rows = tables[bt_main_table]
                # 组内同码主行去重（白盒：一个业务编码 = 一条主记录）。
                # AI 过度展开（如同一编码出多行）不再顶爆唯一键导致全组回滚：
                # 内容完全相同的去重；同码不同内容的留首行、其余进行级失败清单如实上报
                if len(main_rows) > 1:
                    import json as _json
                    seen, dups = {}, []
                    for r in main_rows:
                        seen.setdefault(_json.dumps(r, sort_keys=True, ensure_ascii=False), r)
                    deduped = list(seen.values())
                    if len(deduped) < len(main_rows):
                        logger.info("    组 %s: 主表重复行去重 %d→%d", cv, len(main_rows), len(deduped))
                    if len(deduped) > 1:
                        # 同码不同内容：保留非空字段最多的行（真记录字段全，
                        # 混入的碎片行只有码+一两个字段），其余如实上报
                        best = max(deduped,
                                   key=lambda r: sum(1 for v in r.values() if str(v).strip()))
                        dups = [r for r in deduped if r is not best]
                        logger.warning("    组 %s: 同码不同内容主行 %d 条，保留字段最全行其余上报",
                                       cv, len(dups))
                        deduped = [best]
                    main_rows = tables[bt_main_table] = deduped
                    for r in dups:
                        failures.append({"table": bt_main_table, "row": r,
                                         "reason": f"同一业务编码 {cv} 的重复主记录"
                                                   f"（内容不同），已跳过待确认"})
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
                        safe_cv = str(cv_val).replace("'", "''")
                        SecurityContract.validate_identifier(bt_main_table, "主表名")
                        SecurityContract.validate_identifier(bt_code_field, "编码字段")
                        sel = drv.query(f"SELECT id FROM {safe_table_sql(bt_main_table)} WHERE {safe_column_sql(bt_code_field)}='{safe_cv}' LIMIT 1")
                        if sel:
                            main_id = sel[0].get("id")

            # 如果组里没有主表数据（main_id=None），按组键编码从已入库主表查找 id——
            # 跨批拆分是常态（上批写了主表行，本批只有明细行）；
            # 明细行的编码在外键列里（quota_item_id='A1-25'），旧逻辑按虚拟码字段找不到
            if main_id is None and cv and cv != "__none__":
                safe_cv = str(cv).replace("'", "''")
                SecurityContract.validate_identifier(bt_main_table, "主表名")
                SecurityContract.validate_identifier(bt_code_field, "编码字段")
                sel = drv.query(f"SELECT id FROM {safe_table_sql(bt_main_table)} WHERE {safe_column_sql(bt_code_field)}='{safe_cv}' LIMIT 1")
                if sel:
                    main_id = sel[0].get("id")

            # 写明细表
            if all_ok and not has_conflict:
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
                        for row in trows:
                            r = insert_rows(tname, [row], overwrite=overwrite, auto_commit=False)
                            if not r.get("ok"):
                                reason = r.get('message', 'unknown')
                                failures.append({"table": tname, "row": row, "reason": reason})
                                logger.warning("    行级跳过 %s 第 %d 行: %s",
                                               tname, trows.index(row) + 1, reason)
                        continue
                    # 组级（有 FK 依赖，如定额+材料——全或无）：任一行失败整组回滚
                    result = insert_rows(tname, trows, overwrite=overwrite, auto_commit=False)
                    if result.get("conflict"):
                        has_conflict = True
                    elif not result.get("ok"):
                        logger.error("    insert fail: %s", result.get('message', 'unknown'))
                        all_ok = False
                        break

            if has_conflict and not overwrite:
                conflict_ids.append(cv)
                logger.warning("  ? %s: 数据已存在，待确认", cv)
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
    # 系统性错误护栏（别拿行级容错给代码 bug 兜底）：
    # 全部组因同一原因失败 = 系统性错误（代码/配置问题），不是"坏数据行"——
    # 升级为 all_results["systemic_error"]，由 process_file 显著上报而不是静默列清单。
    # 只统计真正的组级失败（group_fail）：同码去重跳过等行级备注不算组失败
    if groups and failures:
        from collections import Counter
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
    drv.commit()
    all_results["conflicts"] = conflict_ids
