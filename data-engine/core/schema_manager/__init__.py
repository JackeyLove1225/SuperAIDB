"""Schema management（facade）

拆分布局（20260822，facade 模式——import 面零变化）：
原 core/schema_manager.py（1568 行）按职责拆到本子包——
types.py（类型映射）、_shared.py（系统列保护/驱动入口/内部路径/配置读写等域间公共助手）、
consistency.py（一致性校验 + require_consistency 守卫）、snapshot.py（快照/恢复）、
crud.py（建表/删表/改列/索引）、fk.py（外键管理）、repair.py（修复/清库）、
describe.py（schema 展示）。本 facade 只做 re-export：
`from core.schema_manager import X` / `core.schema_manager.X` / `import core.schema_manager`
的调用方零感知（含下划线私有名——测试直接引用，全部保真）。

patch 兼容约定（测试依赖，勿绕开）：_preflight_check / _load_config / get_driver /
_save_config / _save_with_rollback / _all_industry_yaml_tables / _last_extra_tables /
_check_fk_references / _commit_table_delete / _unwrap_sqlite_conn 会被 tests 在
core.schema_manager 上 patch/赋值（test_08/25/33）。子模块内对这些名字的引用一律在
调用时经本 facade 取值（子模块 `from core import schema_manager as _sm`，`_sm.X(...)`），
因此 patch 在导入完成后依然生效。共享状态 _last_extra_tables 直接留在本 facade，
子模块经 _sm._last_extra_tables 读写，保证测试的赋值与断言始终命中同一份。
"""
import os, yaml, copy, shutil
from core.logger import get_logger
from datetime import datetime
from pathlib import Path

logger = get_logger(__name__)
from config.settings import settings
from functools import wraps
from core.constants import (MSG_TABLE_NOT_FOUND, MSG_FIELD_NOT_FOUND,
    MSG_SYS_COL_PROTECTED, MSG_CONSISTENCY_BLOCK, MSG_CONSISTENCY_FIELD_DIFF,
    MSG_CONSISTENCY_INDEX_MISSING, MSG_FIELD_NN_FAILED, MSG_FIELD_NN_SET)
from core.steward import Steward
from core.contract.security_contract import (
    safe_table_sql, safe_column_sql, safe_index_sql, safe_pragma_arg, SecurityContract,
    is_valid_identifier
)

# 最近一次 _preflight_check 发现的「DB 有、YAML 无」多余表集合（供 allow_heal="drop" 识别）
# 拆包后保留在 facade：tests 直接读写 core.schema_manager._last_extra_tables（test_33），
# 子模块（consistency）一律经 _sm._last_extra_tables 读写，两边永远命中同一份状态。
_last_extra_tables: set = set()

# ═══ 实现 re-export（import 面与拆前完全一致，含私有名——测试直接引用）═══
from .types import (
    TYPE_ALIASES,
    ALLOWED_TYPES,
    _normalize_type,
)
from ._shared import (
    _SYS_COLUMNS,
    _guard_sys_column,
    get_driver,
    _unwrap_sqlite_conn,
    _get_industry_dir,
    _get_schema_dir,
    _get_fields_path,
    _load_config,
    _atomic_write,
    _save_config,
    _save_with_rollback,
)
from .consistency import (
    _all_industry_yaml_tables,
    _check_tables,
    _check_columns,
    _check_foreign_keys,
    _check_indexes,
    _preflight_check,
    require_consistency,
)
from .snapshot import (
    SNAPSHOT_KEEP,
    _get_snapshot_dir,
    _snapshot_schemas,
    restore_schema_templates,
)
from .crud import (
    list_tables,
    create_table,
    _topo_sort_tables,
    _prepare_columns,
    _filter_cross_ds_fks,
    _create_table_indexes,
    batch_create_tables,
    recreate_table,
    drop_table,
    _commit_table_delete,
    _remove_table_config_across_industries,
    rename_table,
    create_index,
    drop_index,
    add_column,
    drop_column,
    modify_column,
    alter_precision,
    set_not_null,
)
from .fk import (
    _check_fk_references,
    add_foreign_key,
    drop_foreign_key,
)
from .repair import (
    repair_tables,
    create_standard_tables_with_check,
    clear_database,
)
from .describe import (
    _load_schema_map,
    _build_fk_display,
    _format_table_row,
    _build_table_info_from_db,
    describe_schema_format,
)

# import 子模块会在包命名空间留下同名属性（types/crud/...），逐一移除，
# 保持 dir(core.schema_manager) 与拆前逐名一致（验收依赖）；sys.modules 中的子模块不受影响。
globals().pop("types", None)
globals().pop("_shared", None)
globals().pop("consistency", None)
globals().pop("snapshot", None)
globals().pop("crud", None)
globals().pop("fk", None)
globals().pop("repair", None)
globals().pop("describe", None)
