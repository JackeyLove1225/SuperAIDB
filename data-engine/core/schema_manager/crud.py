"""表结构 CRUD 域：建表/删表/改列/索引（20260822 拆包：core/schema_manager.py 同名片段纯搬家，逻辑零变化）

patch 兼容（测试依赖，勿绕开）：_load_config / _save_config / _save_with_rollback /
get_driver / _check_fk_references / _commit_table_delete 会被 tests 在 facade
（core.schema_manager）上 patch/赋值（test_25/33），本模块一律在调用时经 _sm.X 访问。
"""
import copy
from core.logger import get_logger
from pathlib import Path

logger = get_logger(__name__)
from core.constants import MSG_TABLE_NOT_FOUND, MSG_FIELD_NOT_FOUND, MSG_FIELD_NN_SET
from core.contract.security_contract import (
    safe_table_sql, safe_column_sql, safe_index_sql, is_valid_identifier
)

from core import schema_manager as _sm  # facade 回旋引用：仅调用时取值，见文件头说明
from ._shared import _PROJECT_ROOT, _guard_sys_column
from .types import _normalize_type
from .consistency import require_consistency
from .repair import clear_database  # drop_table(all=True) 用；repair 不回依赖本模块，无环


def list_tables() -> list[dict]:
    data = _sm._load_config()
    return [{"name": t["name"], "columns": [{"name": c["name"], "type": c["type"]} for c in t.get("columns", [])]}
            for t in data.get("tables", [])]


# ── 表结构变更操作 ──

def create_table(table: str, columns: str) -> dict:
    data = _sm._load_config()
    if any(t["name"].lower() == table.lower() for t in data.get("tables", [])):
        return {"ok": False, "message": f"表 {table} 已存在"}
    col_defs = []; fk_info = None
    for pair in columns.split(","):
        pair = pair.strip()
        if not pair: continue
        parts = pair.split(":")
        col = parts[0].strip()
        typ, prec = _normalize_type(parts[1] if len(parts) > 1 else "TEXT")
        if not typ:
            return {"ok": False, "message": f"不支持的类型: {parts[1].strip()}"}
        entry = {"name": col, "type": typ}
        if prec: entry["precision"] = prec
        for extra in parts[2:]:
            ex = extra.strip()
            ex_l = ex.lower()
            if ex_l == "pk": entry["pk"] = True
            elif ex_l == "nn": entry["not_null"] = True
            elif ex_l == "uq": entry["unique"] = True
            elif ex_l.startswith("default="): entry["default"] = ex.split("=", 1)[1]
            elif ex_l.startswith("fk(") and ex_l.endswith(")"): fk_info = (col, ex[3:-1])
        col_defs.append(entry)
        data.setdefault("field_dict", {})[col] = {"alias": [col], "type": typ}
    table_config = {"name": table, "columns": col_defs, "business_name": table, "description": table}
    if fk_info:
        table_config.setdefault("foreign_keys", []).append({"columns": [fk_info[0]], "references": fk_info[1], "ref_columns": ["id"]})
    # 先 DB 后 YAML + 回滚：DB 成功后 YAML 失败则回滚 DB
    _sm.get_driver().create_table(table_config)
    data.setdefault("tables", []).append(table_config)
    fail = _sm._save_with_rollback(lambda: _sm._save_config(data), lambda: _sm.get_driver().drop_table(table),
        rollback_desc=f"DB回滚失败 drop_table({table})",
        fail_message="配置写入失败，已回滚DB: {e}")
    if fail: return fail
    return {"ok": True, "message": f"已创建表 {table}（{len(col_defs)}个字段）"}


# ── batch_create_tables 拆分的子函数（主流程只留编排）──

def _topo_sort_tables(definitions: list) -> list:
    """按 FK 依赖拓扑排序表定义，返回排序后的表名列表（被引用表在前）。
    纯函数：不修改入参。有环时剩余表按名称排序追加。要求每个定义已有 name。"""
    names = [d["name"] for d in definitions]
    # 从 foreign_keys 列表提取被引用表名，用于拓扑排序
    refs = {}
    for d in definitions:
        fk_refs = set()
        for fk_entry in d.get("foreign_keys", []):
            ref = fk_entry.get("references", "")
            if ref:
                fk_refs.add(ref)
        if d.get("fk"):
            fk_refs.add(d["fk"])
        refs[d["name"]] = fk_refs
    ordered = []
    remaining = set(names)
    while remaining:
        found = False
        for n in sorted(remaining):
            r = refs.get(n, set())
            if not r or not (r & remaining):
                ordered.append(n)
                remaining.remove(n)
                found = True
                break
        if not found:
            ordered.extend(sorted(remaining))
            break
    return ordered


def _prepare_columns(d: dict, name: str, results: list) -> list:
    """建表前的列定义整理（就地修改 d）：用户自建主键转唯一约束、补 id 主键、
    拼装 col_def 列表。不支持的类型记入 results 并跳过该列（表仍创建，与原逻辑一致）。"""
    cols_parts = []
    # 始终在列最前面补主键 id（用户有自建主键时转为唯一约束）
    user_pk_cols = [c for c in d.get("columns", []) if c.get("pk") or c.get("is_pk")]
    for c in user_pk_cols:
        if c["name"] != "id":
            c["unique"] = True
            c["not_null"] = True
            c["pk"] = False
            c["is_pk"] = False
    if not any(c.get("name", "").lower() == "id" for c in d.get("columns", [])):
        d.setdefault("columns", []).insert(0, {"name": "id", "type": "INTEGER", "pk": True, "not_null": True})
    for c in d.get("columns", []):
        cn = c["name"]
        ct, prec = _normalize_type(c.get("type", "TEXT"))
        if not ct:
            results.append(f"{name}: 不支持的类型 {c.get('type','')}")
            continue
        col_def = f"{cn}:{ct}"
        if c.get("pk"):
            col_def += ":pk"
        # FK 支持：从 foreign_keys 列表查找匹配当前列的 FK
        # 兼容旧格式：fk + fk_column → 转为 foreign_keys
        fk_list = d.get("foreign_keys", [])
        if not fk_list and d.get("fk"):
            fk_list = [{"columns": [d.get("fk_column", c["name"])], "references": d["fk"], "ref_columns": [d.get("fk_column", c["name"])]}]
        for fk_entry in fk_list:
            if cn.lower() in [x.lower() for x in fk_entry.get("columns", [])] and fk_entry.get("references"):
                col_def += ":fk(" + fk_entry["references"] + ")"
        cols_parts.append(col_def)
    return cols_parts


def _filter_cross_ds_fks(d: dict, ds_name: str, name: str) -> dict:
    """跨数据源外键过滤——避免物理驱动建表时因引用表不存在而失败。
    返回传给 DB 驱动的定义（有跨库 FK 时为过滤后的深拷贝，否则为原对象）。
    注意：仅过滤传给 DB 驱动的副本，YAML 配置必须保留完整 foreign_keys
    （跨库 JOIN 的 _find_fk_relation 依赖 YAML 中的完整外键关系）。"""
    if ds_name and d.get("foreign_keys"):
        import copy as _copy
        d = _copy.deepcopy(d)  # d 用于 DB 操作（过滤后）
        kept_fks = []
        skipped_fks = []
        from core.datasource_manager import DataSourceManager as _DSM
        _dsm = _DSM()
        for fk in d["foreign_keys"]:
            ref_table = fk.get("references", "")
            ref_ds = _dsm.get_datasource_for_table(ref_table)
            if ref_ds == ds_name:
                kept_fks.append(fk)
            else:
                skipped_fks.append(fk)
        d["foreign_keys"] = kept_fks
        if skipped_fks:
            logger.info("  [联邦] 跳过跨数据源外键: %s → %s（仅在schema层面记录）",
                        name, [(fk.get('references'), fk.get('columns')) for fk in skipped_fks])
    return d


def _create_table_indexes(drv, d: dict, name: str, results: list):
    """建表后创建索引（_build_create_sql 不处理索引，需单独创建）。
    单个索引失败记入 results，不影响其他索引和主流程。"""
    for idx in d.get("indexes", []):
        idx_name = idx.get("name", "")
        idx_cols = idx.get("columns", [])
        idx_unique = idx.get("unique", True)
        if idx_name and idx_cols:
            col_str = ",".join(safe_column_sql(c) for c in idx_cols)
            try:
                # ContractDriver 包装层无 .conn，走公开 execute() 接口透传
                drv.execute(f'CREATE {"UNIQUE " if idx_unique else ""}INDEX IF NOT EXISTS {safe_index_sql(idx_name)} ON {safe_table_sql(name)} ({col_str})')
            except Exception as e:
                results.append(f"{name}: 索引 {idx_name} 创建失败: {e}")


def batch_create_tables(definitions: list, overwrite: bool = False) -> str:
    """批量建表：按 FK 依赖拓扑排序，逐个创建。AI 只填数据，代码控制顺序。"""
    import re
    # 校验每个定义必须有 name
    for i, d in enumerate(definitions):
        if not isinstance(d, dict) or not d.get("name"):
            return f"第{i+1}个表定义缺少 name 字段"
    ordered = _topo_sort_tables(definitions)
    results = []
    for name in ordered:
        d = next(x for x in definitions if x["name"].lower() == name.lower())
        _prepare_columns(d, name, results)
        # 检查数据库中表是否已存在
        # 联邦数据库：根据 datasource 字段路由到对应数据源检查
        ds_name = d.get("datasource", "")
        if ds_name:
            from core.datasource_manager import DataSourceManager
            check_drv = DataSourceManager().get_driver(ds_name)
        else:
            check_drv = _sm.get_driver()
        exists = name.lower() in [t.lower() for t in check_drv.list_tables()]
        if exists and not overwrite:
            from core.context import get_context
            get_context().save("overwrite_table", {"_tool": "batch_create_tables", "definitions": definitions, "name": name})
            return f"数据库中已有同名表「{name}」，需要覆盖吗？这可能导致原表数据丢失！"
        # 先 DB 后 YAML：先操作数据库，成功后再写配置
        drv = check_drv  # 复用上面已解析的 drv（路由到正确数据源）
        d_for_yaml = d  # 保存原始定义引用（含完整 foreign_keys），用于写 YAML
        d = _filter_cross_ds_fks(d, ds_name, name)
        if overwrite:
            sync_result = drv.recreate_table(d)
        else:
            drv.create_table(d)
            sync_result = f"已创建 {name}"
        # 联邦数据库：注册表到数据源映射（FederatedDriver 路由依赖此映射）
        if ds_name:
            try:
                from core.datasource_manager import DataSourceManager
                DataSourceManager().register_table(name, ds_name)
            except Exception:
                pass
        _create_table_indexes(drv, d, name, results)
        drv.commit()
        # DB 成功后写 YAML 配置 + 回滚：YAML 失败则 drop 当前表（前面的表已成功不回滚）
        # 注意：写 YAML 用 d_for_yaml（原始完整定义，含跨库外键），而非过滤后的 d
        def _write_yaml():
            data = _sm._load_config()
            d_for_yaml["business_name"] = d_for_yaml.get("business_name", d_for_yaml["name"])
            d_for_yaml["description"] = d_for_yaml.get("description", d_for_yaml["name"])
            if overwrite:
                data["tables"] = [t for t in data.get("tables", []) if t["name"].lower() != name.lower()]
            data.setdefault("tables", []).append(d_for_yaml)
            _sm._save_config(data)
        fail = _sm._save_with_rollback(_write_yaml, lambda: drv.drop_table(name),
            rollback_desc=f"DB回滚失败 drop_table({name})",
            fail_message="配置写入失败，已回滚DB: {e}")
        if fail:
            results.append(f"{name}: {fail['message']}")
            continue
        results.append(f"{name}: {sync_result}")
    return "\n".join(results)


@require_consistency
def recreate_table(table: str, columns: str) -> dict:
    data = _sm._load_config()
    target_idx = None
    for i, t in enumerate(data.get("tables", [])):
        if t["name"].lower() == table.lower(): target_idx = i; break
    if target_idx is None: return {"ok": False, "message": MSG_TABLE_NOT_FOUND.format(name=table)}
    col_defs = []; fk_info = None
    for pair in columns.split(","):
        pair = pair.strip()
        if not pair: continue
        parts = pair.split(":")
        col = parts[0].strip()
        typ = (parts[1] if len(parts) > 1 else "TEXT").strip().upper()
        entry = {"name": col, "type": typ}
        for extra in parts[2:]:
            ex = extra.strip()
            ex_l = ex.lower()
            if ex_l == "pk": entry["pk"] = True
            elif ex_l == "nn": entry["not_null"] = True
            elif ex_l == "uq": entry["unique"] = True
            elif ex_l.startswith("default="): entry["default"] = ex.split("=", 1)[1]
            elif ex_l.startswith("fk(") and ex_l.endswith(")"): fk_info = (col, ex[3:-1])
        col_defs.append(entry)
    new_config = {"name": table, "columns": col_defs, "business_name": table, "description": table}
    if fk_info:
        new_config.setdefault("foreign_keys", []).append({"columns": [fk_info[0]], "references": fk_info[1], "ref_columns": ["id"]})
    # 先 DB 后 YAML + 回滚：DB 重建不可逆，YAML 失败则恢复原配置
    backup = copy.deepcopy(data)
    _sm.get_driver().recreate_table(new_config)
    data["tables"][target_idx] = new_config
    fail = _sm._save_with_rollback(lambda: _sm._save_config(data), lambda: _sm._save_config(backup),
        fail_message="DB已重建但配置写入失败，已恢复原配置。请重试或手动检查: {e}")
    if fail: return fail
    return {"ok": True, "message": f"已重建表 {table}"}

@require_consistency(allow_heal="drop")
def drop_table(table: str = "", all: bool = False) -> dict:
    """删除表。all=True 时删除全部表"""
    if all:
        return clear_database(drop_tables=True)
    # 系统表/内部表豁免（前置到入口，覆盖正常路径与"DB 多余表"分支）：
    # 防止误删基础设施表（如 users/roles/sqlite_/meta_）——security_review MEDIUM
    # 大小写归一化：SQLite 表名大小写不敏感，drop_table("USERS") 不能绕过豁免（HIGH）
    _SYS_TABLES = {"users", "roles", "permissions", "role_permissions", "sessions"}
    _tbl_lower = table.lower()
    if _tbl_lower in _SYS_TABLES or _tbl_lower.startswith(("sqlite_", "meta_")):
        return {"ok": False, "message": f"表 {table} 是系统表，不允许删除"}
    data = _sm._load_config()
    refs = _sm._check_fk_references(table)
    if refs:
        names = "、".join(r.get("table", r.get("name", "?")) for r in refs)
        return {"ok": False, "message": f"表 {table} 被表 {names} 的外键引用，请先解除该外键约束再删除"}
    # 联邦数据库：查找表的 datasource 配置（在从配置移除前获取）
    table_config = next((t for t in data.get("tables", []) if t["name"] == table), {})
    ds_name = table_config.get("datasource", "")
    # 校验表存在性：优先查 YAML（标准路径），YAML 无此表但 DB 有 → 属于"DB 多余表"
    # 修复场景（allow_heal="drop" 放行后）→ 仍执行 DB drop，跳过当前行业 YAML 侧。
    # （否则"删多余表"会死在 748 行的"表不存在"提前返回，DB 表从未删除——核心死锁）
    yaml_has = any(t.get("name") == table for t in data.get("tables", []))
    if not yaml_has:
        try:
            db_has = table in {t.lower() for t in _sm.get_driver().list_tables()}
        except Exception:
            db_has = False
        if not db_has:
            return {"ok": False, "message": f"表 {table} 不存在"}
        # 装饰器已放行（allow_heal="drop"：DB 多余表场景）→ 只删 DB + 跨行业 YAML
        if ds_name:
            from core.datasource_manager import DataSourceManager
            DataSourceManager().get_driver(ds_name).drop_table(table)
        else:
            _sm.get_driver().drop_table(table)
        _deleted_holder = {}
        def _rollback_extra():
            # 重写 holder 中已删的其他行业 yaml（当前行业本无此表，无需恢复配置）
            for path, content in _deleted_holder.get("deleted", {}).items():
                try:
                    Path(path).write_text(content, encoding="utf-8")
                except Exception as e:
                    logger.error("回滚跨行业 YAML 失败 %s: %s", path, e)
        fail = _sm._save_with_rollback(
            lambda: _sm._commit_table_delete(table, _deleted_holder),
            _rollback_extra,
            fail_message="DB已删表但配置写入失败，请手动检查: {e}")
        if fail: return fail
        return {"ok": True, "message": f"已删除表 {table}"}
    before = len(data.get("tables", []))
    # 先 DB 后 YAML + 回滚：DB 删表不可逆，YAML 失败则恢复原配置（必须在修改 data 前深拷贝）
    backup = copy.deepcopy(data)
    data["tables"] = [t for t in data["tables"] if t["name"] != table]
    if len(data["tables"]) == before:
        return {"ok": False, "message": f"表 {table} 不存在"}
    # 联邦数据库：路由到表所属数据源
    if ds_name:
        from core.datasource_manager import DataSourceManager
        DataSourceManager().get_driver(ds_name).drop_table(table)
    else:
        _sm.get_driver().drop_table(table)
    # 跨行业同步：同一张表可能存在于多个行业模板的 schemas/*.yaml，
    # 只删当前行业会导致其他行业 YAML 残留 → 正向不一致（配置有表、DB 无表）。
    # 修复：扫描所有行业目录，移除同名表的 yaml 配置（含快照，保证可回退）。
    # 回滚对称：_commit_table_delete 先收集「其他行业已删文件→原内容」置入局部 holder，
    # 再执行删除；即使 unlink 中途抛异常，holder 已含完整映射可回滚。
    # （局部闭包传递，不用模块级共享状态，避免并发 drop 互相覆盖）
    _deleted_holder = {}
    def _rollback_all():
        # 1) 当前行业：恢复原配置（含快照机制）
        try:
            _sm._save_config(backup)
        except Exception as e:
            logger.error("回滚当前行业 YAML 失败: %s", e)
        # 2) 其他行业：重写已删文件
        for path, content in _deleted_holder.get("deleted", {}).items():
            try:
                Path(path).write_text(content, encoding="utf-8")
            except Exception as e:
                logger.error("回滚跨行业 YAML 失败 %s: %s", path, e)
    fail = _sm._save_with_rollback(
        lambda: _sm._commit_table_delete(table, _deleted_holder),
        _rollback_all,
        fail_message="DB已删表但配置写入失败，已恢复原配置。请重试或手动检查: {e}")
    if fail: return fail
    return {"ok": True, "message": f"已删除表 {table}"}


def _commit_table_delete(table: str, holder: dict | None = None) -> None:
    """删表的 YAML 提交：当前行业走 _save_config（含快照），其他行业删除同名 yaml。

    holder（可选，局部闭包字典）：透传给 _remove_table_config_across_industries，
    由其在 unlink 循环之前把「其他行业已删文件→原内容」置入 holder["deleted"]——
    即使 unlink 中途抛异常，回滚也有据（security_review MEDIUM 修复）。
    """
    _remove_table_config_across_industries(table, holder=holder)


def _remove_table_config_across_industries(table: str, industries_root: Path | None = None,
                                           holder: dict | None = None) -> dict:
    """跨所有行业移除指定表的 YAML 配置（当前行业 + 其他行业模板），消除正向不一致。

    - 当前行业：走 _save_config 的原子写入 + 快照机制（保留历史）
    - 其他行业：直接删除对应 schemas/<table>.yaml（这些行业不操作此表，保留快照成本高，
      且删除是幂等的——表已从 DB 删除，配置必须同步消失）

    安全：表名必须先通过 is_valid_identifier 校验（防路径遍历越界删除——
    表名含 ../ 等字符时可逃逸 industries 目录删除任意 .yaml）。

    参数 industries_root：industries 根目录（测试注入用；缺省为真实路径）。
    参数 holder（局部闭包字典）：阶段2 unlink 循环之前，先把「已删文件→原内容」写入
    holder["deleted"]——即使 unlink 中途抛异常，调用方也能拿全量映射回滚
    （security_review MEDIUM 修复）。
    返回：{其他行业已删文件绝对路径(str): 原内容(str)}，供调用方失败时回滚重写。
    """
    if not is_valid_identifier(table):
        raise ValueError(f"非法的表名: {table!r}（仅允许字母/数字/下划线，最长 64 字符）")
    deleted: dict = {}
    ind_dir = industries_root or (_PROJECT_ROOT / "industries")
    # 当前行业：走标准 _save_config（含快照 + 原子写）
    data = _sm._load_config()
    before = len(data.get("tables", []))
    data["tables"] = [t for t in data.get("tables", []) if t["name"] != table]
    if len(data["tables"]) != before:
        _sm._save_config(data)
    # 其他行业（两阶段，防中途异常丢记录）：
    #   阶段1：先扫描收集所有待删文件的原内容（读失败即抛，此时未删任何文件）
    #   阶段2：再统一删除（此时映射已完整，即使删除中途异常，调用方也能拿全量回滚）
    # 表名已过 is_valid_identifier，拼接 f"{table}.yaml" 无路径逃逸。
    _targets: list[Path] = []
    for ind in ind_dir.iterdir():
        if not ind.is_dir() or ind.name in ("__pycache__", "templates", "custom"):
            continue
        schema_dir = ind / "schemas"
        if not schema_dir.exists():
            continue
        f = schema_dir / f"{table}.yaml"
        if f.exists():
            deleted[str(f.resolve())] = f.read_text(encoding="utf-8")
            _targets.append(f)
    # unlink 前把完整映射置入 holder（即使下面 unlink 中途抛异常，回滚有据）
    if holder is not None:
        holder["deleted"] = deleted
    for f in _targets:
        f.unlink()
    return deleted

@require_consistency
def rename_table(table: str, new_name: str) -> dict:
    data = _sm._load_config()
    t = next((x for x in data["tables"] if x["name"].lower() == table.lower()), None)
    if not t: return {"ok": False, "message": f"表 {table} 不存在"}
    if any(x["name"].lower() == new_name.lower() for x in data.get("tables", [])):
        return {"ok": False, "message": f"表 {new_name} 已存在，无法重命名"}
    t["name"] = new_name
    # 同步更新引用了本表的其他表（在 foreign_keys 列表中查找）
    for other in data.get("tables", []):
        for fk in other.get("foreign_keys", []):
            if fk.get("references","").lower() == table.lower():
                fk["references"] = new_name
    # 先 DB 后 YAML + 回滚：DB rename 可逆，YAML 失败则 rename 回去
    _sm.get_driver().rename_table(table, new_name)
    fail = _sm._save_with_rollback(lambda: _sm._save_config(data), lambda: _sm.get_driver().rename_table(new_name, table),
        rollback_desc=f"DB回滚失败 rename_table({new_name}→{table})",
        fail_message="配置写入失败，已回滚DB: {e}")
    if fail: return fail
    return {"ok": True, "message": f"已重命名 {table} → {new_name}"}

@require_consistency
def create_index(table: str, columns: str, unique: bool = True) -> dict:
    data = _sm._load_config()
    target = next((t for t in data.get('tables', []) if t['name'].lower() == table.lower()), None)
    if not target:
        return {'ok': False, 'message': f'表 {table} 不存在'}
    idx_name = f"idx_{table}_{columns.replace(',','_').replace(' ','')}"
    existing = target.get('indexes', [])
    if any(i.get('name','') == idx_name for i in existing):
        old = next(i for i in existing if i.get('name','') == idx_name)
        if old.get('unique') != unique:
            existing.remove(old)
            _sm.get_driver().drop_index(idx_name)
        else:
            return {'ok': False, 'message': f'索引 {idx_name} 已存在'}
    existing.append({'name': idx_name, 'columns': [c.strip() for c in columns.split(',')], 'unique': unique})
    target['indexes'] = existing
    # 先 DB 后 YAML + 回滚：DB 建索引可逆，YAML 失败则 drop_index
    dr = _sm.get_driver().create_index(table, columns, unique)
    if "已创建" not in str(dr):
        return {"ok": False, "message": str(dr)}
    fail = _sm._save_with_rollback(lambda: _sm._save_config(data), lambda: _sm.get_driver().drop_index(idx_name),
        rollback_desc=f"DB回滚失败 drop_index({idx_name})",
        fail_message="配置写入失败，已回滚DB: {e}")
    if fail: return fail
    return {"ok": True, "message": f"已创建索引 {idx_name}"}
def drop_index(name: str) -> dict:
    data = _sm._load_config()
    for t in data.get("tables", []):
        idxs = t.get("indexes", [])
        for i, idx in enumerate(idxs):
            if idx.get("name","") == name or idx.get("name","") == f"idx_{t['name']}_{name}":
                # 先 DB 后 YAML + 回滚：DB 删索引不可逆，YAML 失败则恢复原配置（必须在修改 data 前深拷贝）
                backup = copy.deepcopy(data)
                idxs.pop(i)
                t["indexes"] = idxs
                _sm.get_driver().drop_index(name)
                fail = _sm._save_with_rollback(lambda: _sm._save_config(data), lambda: _sm._save_config(backup),
                    fail_message="DB已删索引但配置写入失败，已恢复原配置。请重试或手动检查: {e}")
                if fail: return fail
                return {"ok": True, "message": f"已删除索引 {name}"}
    return {"ok": False, "message": f"索引 {name} 不存在"}


@require_consistency(allow_heal="add")
def add_column(table: str, column: str, col_type: str = "TEXT", not_null: bool = False) -> dict:
    ct, _prec = _normalize_type(col_type)
    if not ct:
        return {"ok": False, "message": f"不支持的类型: {col_type}"}
    data = _sm._load_config()
    target = next((t for t in data.get("tables", []) if t["name"].lower() == table.lower()), None)
    if not target: return {"ok": False, "message": f"表 {table} 不存在于配置中"}
    if any(c["name"].lower() == column.lower() for c in target.get("columns", [])):
        # YAML 已有该字段：DB 也已有 → 真重复；DB 缺 → 只补 DB（自愈，不动 YAML）
        try:
            from core.datasource_manager import DataSourceManager as _DSM_h
            _drv_h = _DSM_h().get_driver_for_table(table)
            db_cols = {c["name"].lower() for c in _drv_h.get_columns(table)}
        except Exception:
            db_cols = set()
        if column.lower() in db_cols:
            return {"ok": False, "message": "此字段已存在"}
        r = _drv_h.add_column(table, column, ct, _prec, not_null=not_null)
        if not r.get("ok"):
            return r
        return {"ok": True, "message": f"已补齐缺失字段（自愈一致）: {column} ({ct})"}
    entry = {"name": column, "type": ct}
    if _prec: entry["precision"] = _prec
    if not_null: entry["not_null"] = True
    target.setdefault("columns", []).append(entry)
    data.setdefault("field_dict", {})[column] = {"alias": [column], "type": ct}  if not _prec else {"alias": [column], "type": ct, "precision": _prec}
    # 先 DB 后 YAML + 回滚：DB 加列可逆，YAML 失败则 drop column
    # 联邦数据库：按表名路由到对应数据源的物理 Driver
    from core.datasource_manager import DataSourceManager as _DSM_ac
    _dsm_ac = _DSM_ac()
    try:
        drv = _dsm_ac.get_driver_for_table(table)
    except Exception:
        drv = _sm.get_driver()  # 回退到默认驱动
    r = drv.add_column(table, column, ct, _prec, not_null=not_null)
    if not r.get("ok"):
        return r
    def _rollback_add_column():
        # 回滚是系统纠错动作，必须无条件执行——绕过契约/权限层直达物理驱动
        # （权限拦截针对用户意图；若回滚也被拦，YAML 写失败会留下孤儿列）。
        # 20260804 权限矩阵：ContractDriver/FederatedDriver 的 execute 均已上闸。
        raw = getattr(drv, "raw_driver", drv)
        raw.execute(f'ALTER TABLE {safe_table_sql(table)} DROP COLUMN {safe_column_sql(column)}'); raw.commit()
    fail = _sm._save_with_rollback(lambda: _sm._save_config(data), _rollback_add_column,
        rollback_desc=f"DB回滚失败 DROP COLUMN {table}.{column}",
        fail_message="配置写入失败，已回滚DB: {e}")
    if fail: return fail
    return {"ok": True, "message": f"已加入新字段：{column} ({ct}{_prec if _prec else ''})"}

@require_consistency
def drop_column(table: str, column: str, force: bool = False) -> dict:
    data = _sm._load_config()
    target = next((t for t in data.get("tables", []) if t["name"].lower() == table.lower()), None)
    if not target: return {"ok": False, "message": f"表 {table} 不存在于配置中"}
    if _sys_err := _guard_sys_column(column, "删除"): return _sys_err
    # 列级 FK 检查（recreate_table 会自动保留 FK）
    refs = _sm._check_fk_references(table, column)
    if refs:
        names = "、".join(r.get("table", r.get("name", "?")) for r in refs)
        return {"ok": False, "message": f"字段 {column} 被表 {names} 的外键引用，请先解除该外键约束再操作"}
    # 检查本列是否是本表的外键字段（在 foreign_keys 列表中查找）
    fk_match = None
    for fk in target.get("foreign_keys", []):
        if column.lower() in [x.lower() for x in fk.get("columns", [])]:
            fk_match = fk
            break
    if fk_match:
        if force:
            target["foreign_keys"] = [f for f in target.get("foreign_keys", []) if f != fk_match]
        else:
            ref_table = fk_match.get("references", "")
            return {"ok": False, "confirm": True, "message": f"字段 {column} 是外键字段，当前关联表【{ref_table}】。确认删除请再次执行并设置 force=true，系统将自动解除外键并删除字段"}
    before = len(target.get("columns", []))
    # 先 DB 后 YAML + 回滚：DB recreate 不可逆，YAML 失败则恢复原配置（必须在修改 data 前深拷贝）
    backup = copy.deepcopy(data)
    target["columns"] = [c for c in target.get("columns", []) if c["name"] != column]
    if len(target["columns"]) == before:
        return {"ok": False, "message": MSG_FIELD_NOT_FOUND.format(name=column)}
    data.get("field_dict", {}).pop(column, None)
    _sm.get_driver().recreate_table(target)
    fail = _sm._save_with_rollback(lambda: _sm._save_config(data), lambda: _sm._save_config(backup),
        fail_message="DB已变更但配置写入失败，已恢复原配置。请重试或手动检查: {e}")
    if fail: return fail
    return {"ok": True, "message": f"已从 {table} 删除字段 {column}"}

@require_consistency
def modify_column(table: str, column: str, new_type: str, force: bool = False) -> dict:
    nt, _prec = _normalize_type(new_type)
    if not nt:
        return {"ok": False, "message": f"不支持的类型: {new_type}"}
    data = _sm._load_config()
    target = next((t for t in data.get("tables", []) if t["name"].lower() == table.lower()), None)
    if not target: return {"ok": False, "message": f"表 {table} 不存在于配置中"}
    found = next((c for c in target.get("columns", []) if c["name"].lower() == column.lower()), None)
    if not found: return {"ok": False, "message": f"字段 {column} 不存在"}
    if _sys_err := _guard_sys_column(column, "修改类型"): return _sys_err
    old_type = found["type"]
    if old_type == nt: return {"ok": False, "message": f"字段 {column} 已经是 {nt}"}
    # 检查 FK：禁止修改外键字段的数据类型
    for fk in target.get("foreign_keys", []):
        if column.lower() in [x.lower() for x in fk.get("columns", [])]:
            ref_table = fk.get("references", "")
            return {"ok": False, "message": f"字段 {column} 是外键字段，当前关联表【{ref_table}】。修改数据类型会导致外键约束失效，不允许修改"}
    refs = _sm._check_fk_references(table, column)
    if refs:
        names = "、".join(r.get("table", r.get("name", "?")) for r in refs)
        return {"ok": False, "message": f"字段 {column} 被表 {names} 的外键引用，修改数据类型会导致外键失效，不允许修改"}
    # 先 DB 后 YAML + 回滚：DB 改类型不可逆，YAML 失败则恢复原配置（必须在修改 data 前深拷贝）
    backup = copy.deepcopy(data)
    found["type"] = nt
    if not force:
        from core.drivers.checks import validate_type_change
        v = validate_type_change(old_type, nt)
        if v:
            found["type"] = old_type
            # 显式 need_force 自报（双轨：execute_tool 据此弹 force 确认卡）
            return {"ok": False, "need_force": True, "message": v}

    data.setdefault("field_dict", {}).setdefault(column, {})["type"] = nt
    drv = _sm.get_driver()
    # force 必须传到底（ContractDriver 有独立风险评估，不传则重试仍被 RiskError 阻断）
    drv.modify_column(table, column, nt, force=force)
    fail = _sm._save_with_rollback(lambda: _sm._save_config(data), lambda: _sm._save_config(backup),
        fail_message="DB已变更但配置写入失败，已恢复原配置。请重试或手动检查: {e}")
    if fail: return fail
    return {"ok": True, "message": f"已修改 {table}.{column}: {old_type} -> {nt}"}


@require_consistency
def alter_precision(table: str, column: str, precision_str: str, force: bool = False) -> dict:
    try:
        parts = [int(p.strip()) for p in precision_str.split(",")]
        new_precision = tuple(parts)
    except Exception:
        return {"ok": False, "message": f"精度格式错误: {precision_str}，应为 总长,小数位 如 12,2"}
    data = _sm._load_config()
    target = next((t for t in data.get("tables", []) if t["name"].lower() == table.lower()), None)
    if not target: return {"ok": False, "message": f"表 {table} 不存在于配置中"}
    found = next((c for c in target.get("columns", []) if c["name"].lower() == column.lower()), None)
    if not found: return {"ok": False, "message": f"字段 {column} 不存在"}
    if _sys_err := _guard_sys_column(column, "修改精度"): return _sys_err
    old_prec = found.get("precision", None)
    # 精度缩小风险检查
    if isinstance(old_prec, str):
        try: old_prec = tuple(int(p.strip()) for p in old_prec.split(","))
        except: old_prec = None
    if old_prec and not force and any(new_precision[i] < old_prec[i] for i in range(min(len(new_precision), len(old_prec)))):
        return {"ok": False, "need_force": True, "message": f"修改精度可能导致数据截断：{old_prec} → {new_precision}，确认请使用 force=True"}
    # 先 DB 后 YAML + 回滚：DB 改精度不可逆，YAML 失败则恢复原配置（必须在修改 data 前深拷贝）
    backup = copy.deepcopy(data)
    # precision 存为 list，避免字符串迭代问题和 YAML tuple 兼容问题
    found["precision"] = list(new_precision)
    if column in data.get("field_dict", {}):
        data["field_dict"][column]["precision"] = list(new_precision)
    # force 传到底（ContractDriver 有独立的收紧风险评估）
    _sm.get_driver().alter_precision(table, column, new_precision, force=force)
    fail = _sm._save_with_rollback(lambda: _sm._save_config(data), lambda: _sm._save_config(backup),
        fail_message="DB已变更但配置写入失败，已恢复原配置。请重试或手动检查: {e}")
    if fail: return fail
    return {"ok": True, "message": f"已修改 {table}.{column} 精度: {old_prec} -> {new_precision}"}


# ── 业务层暴露给 Agent 的接口（避免 Agent 直接调 Driver）──




@require_consistency
def set_not_null(table: str, column: str) -> dict:
    data = _sm._load_config()
    target = next((t for t in data.get("tables", []) if t["name"].lower() == table.lower()), None)
    if not target: return {"ok": False, "message": f"表 {table} 不存在于配置中"}
    found = next((c for c in target.get("columns", []) if c["name"].lower() == column.lower()), None)
    if not found: return {"ok": False, "message": f"字段 {column} 不存在"}
    was_nn = found.get("not_null")
    if was_nn:
        return {"ok": True, "message": f"字段 {column} 已经是非空，正在确保数据库同步"}
    found["not_null"] = True
    # 先 DB 后 YAML：DB 成功后再写配置
    _sm.get_driver().recreate_table(target)
    _sm._save_config(data)
    return {"ok": True, "message": MSG_FIELD_NN_SET.format(table=table, field=column)}
