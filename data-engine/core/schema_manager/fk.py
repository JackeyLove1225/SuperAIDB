"""外键管理域：FK 引用检测 + 增删外键（20260822 拆包：core/schema_manager.py 同名片段纯搬家，逻辑零变化）

patch 兼容（测试依赖，勿绕开）：_load_config / _save_config / _save_with_rollback /
get_driver 会被 tests 在 facade（core.schema_manager）上 patch/赋值（test_25/33），
本模块一律在调用时经 _sm.X 访问。
"""
import copy

from core.contract.security_contract import safe_pragma_arg

from core import schema_manager as _sm  # facade 回旋引用：仅调用时取值，见文件头说明
from .consistency import require_consistency


# ── 外键引用检测（从数据库元数据读取，不依赖 YAML，跨数据库兼容）──

def _check_fk_references(table: str, column: str = "") -> list[dict]:
    """检查哪些表的外键引用了本表（或本表字段）。返回引用表列表，空列表=无引用"""
    try:
        drv = _sm.get_driver()
        all_refs = drv.get_referencing_tables(table)
        if column:
            return [r for r in all_refs if r.get("from_col") == column]
        return all_refs
    except Exception:
        return []  # 数据库不支持时返回空，不阻断操作


@require_consistency
def add_foreign_key(table: str, column: str, ref_table: str, force: bool = False) -> dict:
    data = _sm._load_config()
    target = next((t for t in data.get('tables', []) if t['name'].lower() == table.lower()), None)
    if not target:
        return {'ok': False, 'message': f'表 {table} 不存在于配置中'}
    # 先 DB 后 YAML + 回滚：DB 加外键不可逆（recreate），YAML 失败则恢复原配置（必须在修改 data 前深拷贝）
    backup = copy.deepcopy(data)
    col_names = [c['name'] for c in target.get('columns', [])]
    if column not in col_names:
        target.setdefault('columns', []).append({'name': column, 'type': 'INTEGER'})
    for t in data.get('tables', []):
        if t['name'].lower() == ref_table.lower():
            ref_table = t['name']
            break
    # 所有外键统一指向主键 id
    ref_col = 'id'
    # 引用表主键类型探测：ContractDriver 包装层无 .conn 属性（直接访问必 AttributeError），
    # PRAGMA 改走公开 query() 接口（Permission/Journal 链对 query 直透传，不拦 PRAGMA），
    # 按表路由数据源以支持联邦库；返回 dict 行（PRAGMA table_info 列名 name/type）。
    from core.datasource_manager import DataSourceManager
    _ref_drv = DataSourceManager().get_driver_for_table(ref_table)
    ref_type = 'INTEGER'
    try:
        for _r in _ref_drv.query(f"PRAGMA table_info({safe_pragma_arg(ref_table)})"):
            if str(_r.get('name', '')).lower() == 'id':
                ref_type = _r.get('type') or ref_type
                break
    except Exception:
        # 非 SQLite 数据源不支持 PRAGMA，回退到公开元数据接口 get_columns
        for _c in _ref_drv.get_columns(ref_table):
            if str(_c.get('name', '')).lower() == 'id':
                ref_type = _c.get('type') or ref_type
                break
    fk_type = next((c.get('type','TEXT') for c in target.get('columns',[]) if c['name'].lower() == column.lower()), 'TEXT')
    def _family(t) -> str:
        t = str(t).upper().split('(')[0]
        if t in ('INTEGER','INT','BIGINT','SMALLINT','SERIAL','BOOL'): return 'INTEGER'
        if t in ('FLOAT','REAL','DOUBLE','DECIMAL','NUMERIC'): return 'FLOAT'
        return 'TEXT'
    if _family(fk_type) != _family(ref_type) and not force:
        # 类型不一致可 force 放行（与 ContractDriver 同口径）——
        # 显式 need_force 自报，execute_tool 据此弹 force 确认卡
        return {'ok': False, 'need_force': True,
                'message': f'外键字段 {column} 类型为 {fk_type}，目标主键 {ref_table}.{ref_col} 类型为 {ref_type}，'
                           f'两者不一致，强行设置可能导致关联查询异常。确认请使用 force=True'}
    target.setdefault('foreign_keys', [])
    fk_entry = {'columns': [column], 'references': ref_table, 'ref_columns': [ref_col]}
    if fk_entry not in target['foreign_keys']:
        target['foreign_keys'].append(fk_entry)
    # force 传到底（ContractDriver 有独立的类型/孤儿数据扫描）
    _sm.get_driver().add_foreign_key(table, column, ref_table, force=force)
    fail = _sm._save_with_rollback(lambda: _sm._save_config(data), lambda: _sm._save_config(backup),
        fail_message="DB已变更但配置写入失败，已恢复原配置。请重试或手动检查: {e}")
    if fail: return fail
    return {'ok': True, 'message': f'已设置外键: {table}.{column} -> {ref_table}.{ref_col}'}
def drop_foreign_key(table: str, constraint_name: str, force: bool = False) -> dict:
    data = _sm._load_config()
    target = next((t for t in data.get("tables", []) if t["name"].lower() == table.lower()), None)
    if not target:
        return {"ok": False, "message": f"表 {table} 不存在于配置中"}
    fks = target.get("foreign_keys", [])
    if not fks:
        return {"ok": False, "message": f"表 {table} 没有外键"}
    # 找到要删除的外键
    to_del = None
    for fk in fks:
        cols = fk.get("columns", [])
        if (constraint_name and constraint_name.lower() in [x.lower() for x in cols]) or (not constraint_name and len(fks) == 1):
            to_del = fk
            break
    if not to_del:
        cols_list = ", ".join(str(fk.get("columns", [])) for fk in fks)
        return {"ok": False, "message": f"表 {table} 的外键字段：{cols_list}，请指定要删除的字段名"}
    fk_col = to_del["columns"][0]
    ref_table = to_del.get("references", "")
    if not force:
        return {"ok": False, "confirm": True, "message": f"字段 {fk_col} 是外键字段，当前关联表【{ref_table}】。确认删除请再次执行并设置 force=true"}
    # 先 DB 后 YAML + 回滚：DB recreate/drop 不可逆，YAML 失败则恢复原配置（必须在修改 data 前深拷贝）
    backup = copy.deepcopy(data)
    # 删除 FK 元数据及字段本身
    target["columns"] = [c for c in target.get("columns", []) if c["name"] != fk_col]
    data.get("field_dict", {}).pop(fk_col, None)
    target["foreign_keys"] = [f for f in fks if f != to_del]
    drv = _sm.get_driver()
    if not target.get("columns"):
        # 唯一列被删 → 整表随外键一起删除
        drv.drop_table(table)
    else:
        # 传完整 target 配置（含剩余 foreign_keys），避免丢失其他 FK
        drv.recreate_table(target)
    drv.commit()
    fail = _sm._save_with_rollback(lambda: _sm._save_config(data), lambda: _sm._save_config(backup),
        fail_message="DB已变更但配置写入失败，已恢复原配置。请重试或手动检查: {e}")
    if fail: return fail
    return {"ok": True, "message": f"已删除外键 {table}.{fk_col}"}
