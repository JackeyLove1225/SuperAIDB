"""修复/清库域：YAML→DB 同步修复、创建标准表、清空数据库
（20260822 拆包：core/schema_manager.py 同名片段纯搬家，逻辑零变化）

patch 兼容（测试依赖，勿绕开）：get_driver / _preflight_check / _unwrap_sqlite_conn /
_snapshot_schemas 会被 tests 在 facade（core.schema_manager）上 patch/赋值（test_25/33），
本模块一律在调用时经 _sm.X 访问。
"""
import shutil
import yaml
from core.logger import get_logger

logger = get_logger(__name__)
from config.settings import settings
from core.contract.security_contract import safe_table_sql, safe_column_sql, safe_index_sql

from core import schema_manager as _sm  # facade 回旋引用：仅调用时取值，见文件头说明
from ._shared import _PROJECT_ROOT, _get_schema_dir


def repair_tables(table: str = "", _skip_check: bool = True) -> str:
    """从配置文件修复数据库表结构（YAML→DB 同步）。
    - DB 中缺失的表：创建
    - DB 中已有但结构不一致的表：从 YAML 重建（迁移共有列数据）
    修复工具不应被一致性检查阻止，否则形成死循环。
    """
    drv = _sm.get_driver()
    schema_dir = _get_schema_dir()
    if not schema_dir.exists():
        return "schemas/ 目录不存在"
    results = []
    db_tables = set(drv.list_tables())
    for p in sorted(schema_dir.glob("*.yaml")):
        t = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not t or not t.get("name"):
            continue
        if table and t["name"].lower() != table.lower():
            continue
        tname = t["name"]
        if tname not in db_tables:
            # 创建缺失的表
            drv.create_table(t)
            # 创建索引
            for idx in t.get("indexes", []):
                idx_name = idx.get("name", "")
                idx_cols = idx.get("columns", [])
                idx_unique = "UNIQUE" if idx.get("unique") else ""
                if idx_name and idx_cols:
                    col_list = ", ".join(safe_column_sql(c) for c in idx_cols)
                    try:
                        # ContractDriver 包装层无 .conn，走公开 execute() 接口透传
                        drv.execute(f'CREATE {idx_unique} INDEX {safe_index_sql(idx_name)} ON {safe_table_sql(tname)} ({col_list})')
                    except Exception as e:
                        logger.warning("repair_tables: 索引 %s 创建失败（表 %s）: %s", idx_name, tname, e)
                        results.append(f"{tname}: 索引 {idx_name} 创建失败: {e}")
            results.append(f"已创建 {tname}")
        else:
            # 检查已有表是否需要修复：对比字段名和类型
            needs_repair = False
            try:
                db_cols = {c["name"].lower(): c for c in drv.get_columns(tname)}
            except Exception:
                needs_repair = True
                db_cols = {}
            if not needs_repair:
                yaml_cols = {c["name"].lower(): c for c in t.get("columns", [])}
                # 检查字段数量是否一致
                if len(yaml_cols) != len(db_cols):
                    needs_repair = True
                else:
                    # 检查每个字段的名称和类型是否一致
                    type_map = {"VARCHAR":"TEXT","CHAR":"TEXT","INT":"INTEGER","FLOAT":"REAL","DOUBLE":"REAL","DECIMAL":"REAL"}
                    for cn, col in yaml_cols.items():
                        if cn not in db_cols:
                            needs_repair = True
                            break
                        # 统一类型对比
                        yt = type_map.get(str(col.get("type","")).upper().split("(")[0], str(col.get("type","")).upper())
                        dt = type_map.get(str(db_cols[cn].get("type","")).upper().split("(")[0], str(db_cols[cn].get("type","")).upper())
                        if yt != dt:
                            needs_repair = True
                            break
            if needs_repair:
                drv.recreate_table(t)
                results.append(f"已修复 {tname}")
    drv.commit()
    if not results:
        return "没有需要创建或修复的表" if not table else f"表 {table} 结构一致或不存在于配置中"
    return "；".join(results)


def create_standard_tables_with_check(table: str = "") -> str:
    """对外安全的创建标准表接口（带一致性检查），内部调用 repair_tables"""
    # 如果 schemas/ 为空，先从模板恢复 YAML 文件
    schema_dir = _get_schema_dir()
    if not schema_dir.exists() or not list(schema_dir.glob("*.yaml")):
        tmpl_dir = _PROJECT_ROOT / "industries" / "templates" / settings.INDUSTRY / "schemas"
        if tmpl_dir.exists():
            schema_dir.mkdir(parents=True, exist_ok=True)
            for p in tmpl_dir.glob("*.yaml"):
                shutil.copy2(str(p), str(schema_dir / p.name))
    # 一致性检查（此时 YAML 应有文件了）
    return repair_tables(table)


def clear_database(drop_tables: bool = False) -> str:
    """清空数据或删除所有表

    drop_tables=True: 删除所有 DB 表 + 清空 schemas/*.yaml（重置到初始状态）
        保证清库后 YAML = DB = 空（一致状态），后续操作不会被 require_consistency 阻断
        清空前 schemas 会自动快照到 schemas_snapshots/，需要时通过 restore_schema_templates() 恢复
    drop_tables=False: 仅清空各表数据（保留表结构）
    """
    # 清库模式跳过一致性检查（本身就是用来修复不一致的）
    if not drop_tables:
        e = _sm._preflight_check()
        if e: return {"ok": False, "message": f"操作被阻止: {e}"}
    drv = _sm.get_driver()
    if drop_tables:
        # 删除所有 DB 表
        # PRAGMA 为 SQLite 专属：解包取裸连接，非 SQLite 数据源（None）跳过
        conn = _sm._unwrap_sqlite_conn(drv)
        try:
            if conn is not None:
                conn.execute("PRAGMA foreign_keys=OFF")
            tables_to_drop = [t for t in drv.list_tables() if not t.startswith("sqlite_")]
            # 按 FK 依赖拓扑逆序删除（引用者先删、被引用者后删）：
            # ContractDriver.drop_table 内置 assert_can_drop_table 契约（被引用表禁删），
            # PRAGMA foreign_keys=OFF 只关 SQLite 引擎层检查、管不到 Python 契约层——
            # 按 list_tables() 顺序删会让父表（被引用方）先删、必被契约拦截，
            # "删除数据库所有表格"在有 FK 的库上必失败（20260804）
            dependents = {}  # t -> 引用 t 的表集合（t 的删除阻碍者）
            for t in tables_to_drop:
                try:
                    refs = drv.get_referencing_tables(t) or []
                except Exception:
                    refs = []
                dependents[t] = {r.get("table") for r in refs
                                 if r.get("table") in tables_to_drop}
            remaining = set(tables_to_drop)
            ordered = []
            while remaining:
                # 每轮删"不被任何剩余表引用"的表（其引用者已在前面轮次删除）
                leaf = next((t for t in sorted(remaining)
                             if not (dependents.get(t, set()) & remaining)), None)
                if leaf is None:
                    # 循环引用（A↔B）：契约无法逐步放行，按名序追加，
                    # 让契约对第一张表如实报错（极端情况，正常 schema 不会遇到）
                    ordered.extend(sorted(remaining))
                    break
                ordered.append(leaf)
                remaining.remove(leaf)
            for t in ordered:
                try:
                    drv.drop_table(t)
                except Exception as e:
                    # 系统表豁免（ContractDriver 抛 SecurityError）：清库跳过系统表，
                    # 不中断清库流程（security_review MEDIUM）
                    from core.exceptions import SecurityError as _SE
                    if isinstance(e, _SE):
                        logger.info("清库跳过系统表: %s", t)
                        continue
                    raise
        finally:
            # 任何异常下都恢复 FK（防连接残留 foreign_keys=OFF 的数据完整性风险）
            if conn is not None:
                try:
                    conn.execute("PRAGMA foreign_keys=ON")
                except Exception:
                    pass  # 非 SQLite 驱动无 PRAGMA——跳过外键强制开关
        drv.commit()
        # 同时清空 schemas/*.yaml（保证 YAML = DB = 空的一致状态）
        # 清空前先快照，保证可恢复（快照制，见 _snapshot_schemas）
        schema_dir = _get_schema_dir()
        if schema_dir.exists():
            _sm._snapshot_schemas()
            for p in schema_dir.glob("*.yaml"):
                p.unlink()
        return f"已删除 {len(tables_to_drop)} 个表（同时清空 schemas 配置）"
    else:
        # 全表清空：管理员的明确清库意图，走裸连接（契约层"禁全表删除"是面向
        # 用户/AI 的安全闸，不该拦管理面维护操作）。drv.delete(t,"1=1")
        # 会被契约拒绝，except 吞掉即成静默清 0 表
        from core.contract.security_contract import safe_table_sql
        count = 0
        skipped = []
        conn = _sm._unwrap_sqlite_conn(drv)
        # 系统表不清（与契约层同口径，唯一真源 core.contract.base._is_system_table）：
        # users 等认证表清了等于把自己锁门外；未来新增的 sessions/roles 等授权面
        # 表同样必须豁免——不随 clear_db 裸连接误清（懒导入防循环依赖，
        # 与本函数上方 safe_table_sql 懒引同款）
        from core.contract.base import _is_system_table
        for t in [t for t in drv.list_tables() if not _is_system_table(t)]:
            try:
                if conn is not None:
                    conn.execute(f"DELETE FROM {safe_table_sql(t)}")
                else:
                    # 非 SQLite 驱动：按唯一键逐行删（此类库暂无清数据需求，保守直行）
                    pk = drv._get_unique_key_column(t)
                    for r in drv.query(f"SELECT * FROM {safe_table_sql(t)}"):
                        drv.delete_by_pk(t, pk, r[pk])
                count += 1
            except Exception as e:
                skipped.append(f"{t}（{str(e)[:40]}）")
        drv.commit()
        msg = f"已清空 {count} 个表的数据（表结构保留）"
        if skipped:
            msg += f"；跳过 {len(skipped)} 个: {', '.join(skipped)}"
        return msg
