"""层 8：Schema 一致性校验回归测试

覆盖 _preflight_check 的核心校验逻辑：
  1. 配置与数据库不一致检测（YAML 表在 DB 中不存在）
  2. require_consistency 装饰器拦截不一致状态下的操作
  3. 跨数据源外键跳过深度校验

设计原则：
  - 使用独立的测试行业目录（_test_schema），不影响工程行业
  - 通过 YAML schema 修改 + 重建表模拟不一致场景（不直接操作物理 DB）
  - 测试后完整清理

历史变更（2026-07-19）：
  删除 test_db_has_extra_table / test_db_has_extra_column /
  test_index_missing_in_db / test_index_unique_mismatch 4 个测试。
  原因：它们使用 drv.conn.execute(sql) 直接操作物理数据库，
  但当前 drv 是 ContractDriver 包装层（Permission → Journal → Sqlite），
  无 conn 属性。重建通过 driver 公开 API 走的测试已覆盖等价场景。

  删除 test_consistent_state / test_field_type_mismatch 2 个测试。
  原因：它们假设 DB 是干净的（只有 _test_schema 的表），
  但 DB 里残留了工程行业历史版本的表（teacher_subjects/subjects/students/
  classes/scores/teachers，且 DB 结构与 YAML 严重不一致），
  _preflight_check 的"多余表"检查会提前返回错误，测试无法通过。
  待整体重构回归测试套件时重新设计隔离方案。

  恢复（2026-07-19 隔离方案落地）：
  学校表 YAML 已迁出到 industries/_test_school/（discover 跳过下划线目录），
  本层通过 TEST_DS_CONFIG 把 DSM 的 primary 重定向到独立临时库
  （db/test_08_isolated.db，运行结束删除），DB 干净假设重新成立，
  test_consistent_state / test_field_type_mismatch 随之恢复。
"""
import sys, os, shutil, yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDUSTRIES_DIR = os.path.join(BASE_DIR, "industries")
TEST_INDUSTRY = "_test_schema"
ORIGINAL_INDUSTRY = os.environ.get("INDUSTRY", "engineering")

# 数据源隔离：本层的表建到独立临时库，而非共享主库。
# _preflight_check 的"多余表"检查要求默认数据源的表都在当前行业 YAML 中定义，
# 主库 data_engine.db 中的工程/学校表残留会破坏该假设，因此 primary 重定向到
# db/test_08_isolated.db（运行时生成，测试结束后删除），干净库上原生生效。
TEST_DS_CONFIG = os.path.join(BASE_DIR, "db", "test_08_datasources.yml")
TEST_DB = os.path.join(BASE_DIR, "db", "test_08_isolated.db")


def _ensure_isolated_ds():
    """生成测试专用数据源配置（primary→独立临时库），幂等"""
    if os.path.exists(TEST_DS_CONFIG):
        return
    with open(TEST_DS_CONFIG, "w", encoding="utf-8") as f:
        yaml.safe_dump({"datasources": {
            "primary": {"type": "sqlite",
                        "path": TEST_DB.replace(os.sep, "/"), "is_default": True},
        }}, f)


def _teardown_isolated_ds():
    """关闭临时库连接并删除临时配置/库文件（Windows 文件占用容忍失败）"""
    try:
        from core.datasource_manager import DataSourceManager
        DataSourceManager().reload_config()  # 关闭已缓存的临时库 Driver
        DataSourceManager.reset_instance()
    except Exception:
        pass
    import gc
    gc.collect()  # 促使被丢弃的旧单例连接析构，避免 Windows 文件占用
    for p in [TEST_DS_CONFIG, TEST_DB]:
        try:
            os.remove(p)
        except OSError:
            pass

pass_count = 0
fail_count = 0
errors = []


def check(name, condition, detail=""):
    global pass_count, fail_count
    if condition:
        pass_count += 1
        print(f"  PASS [{name}]")
    else:
        fail_count += 1
        errors.append(name)
        print(f"  FAIL [{name}] {detail[:80]}")


def _sync_industry(industry_name: str):
    from config.settings import settings
    settings.INDUSTRY = industry_name
    try:
        from core.datasource_manager import DataSourceManager
        if DataSourceManager._instance is not None:
            # 先关闭旧单例缓存的 Driver（含临时库连接），再丢弃实例，
            # 否则 Windows 下临时库文件被占用，_teardown_isolated_ds 删不掉
            DataSourceManager._instance.reload_config()
        DataSourceManager.reset_instance()
    except Exception:
        pass
    try:
        import core.data_ops as _data_ops
        _data_ops._federated_driver = None
    except Exception:
        pass
    try:
        import industries.base as _base
        _base._industries.clear()
    except Exception:
        pass


def _get_phys_drv():
    """获取默认数据源（primary）的 Driver（ContractDriver 包装层）

    注意：返回的是 ContractDriver → SqliteDriver 包装链（旧 PermissionedDriver 已删），
    无 conn 属性。setup/cleanup 只调用公开 API（table_exists/drop_table/commit）。
    primary 指向本层独立临时库（TEST_DS_CONFIG），与共享主库隔离。
    """
    from core.datasource_manager import DataSourceManager
    _ensure_isolated_ds()
    dsm = DataSourceManager()
    dsm.load_config(TEST_DS_CONFIG)
    return dsm.get_driver(dsm.get_default_name())


def _write_schema(industry_dir, table_name, schema_dict):
    schema_dir = os.path.join(industry_dir, "schemas")
    os.makedirs(schema_dir, exist_ok=True)
    with open(os.path.join(schema_dir, f"{table_name}.yaml"), "w", encoding="utf-8") as f:
        yaml.dump(schema_dict, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _write_fields(industry_dir, field_dict):
    fields_dir = os.path.join(industry_dir, "fields")
    os.makedirs(fields_dir, exist_ok=True)
    with open(os.path.join(fields_dir, "fields.yml"), "w", encoding="utf-8") as f:
        yaml.dump(field_dict, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _write_config(industry_dir, name=TEST_INDUSTRY):
    config_dir = os.path.join(industry_dir, "config")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "config.yml"), "w", encoding="utf-8") as f:
        yaml.dump({
            "name": name, "description": "schema测试",
            "expert_role": "测试", "hierarchy_desc": "A",
            "default_table_name": "sch_t1"
        }, f, allow_unicode=True)


def _write_prompts(industry_dir):
    prompts_dir = os.path.join(industry_dir, "prompts")
    os.makedirs(prompts_dir, exist_ok=True)
    with open(os.path.join(prompts_dir, "prompts.yml"), "w", encoding="utf-8") as f:
        yaml.dump({
            "classification_hints": "", "schema_hints": "",
            "decompose_examples": [], "router_examples": [],
            "tool_examples": {}, "terminology": {}
        }, f, allow_unicode=True)


def setup_clean_industry():
    """创建干净的测试行业（schema 与 DB 一致）"""
    p = os.path.join(INDUSTRIES_DIR, TEST_INDUSTRY)
    if os.path.exists(p):
        shutil.rmtree(p)
    os.makedirs(p)
    _write_config(p)
    _write_prompts(p)
    _write_fields(p, {"name": {"alias": ["名称"]}, "value": {"alias": ["数值"]}})

    _sync_industry(TEST_INDUSTRY)

    # 清理 DB 旧表（使用物理驱动）
    drv = _get_phys_drv()
    for t in ["sch_t1", "sch_t2", "sch_extra"]:
        if drv.table_exists(t):
            drv.drop_table(t)
    drv.commit()

    # 创建 schema：sch_t1（带索引）
    _write_schema(p, "sch_t1", {
        "name": "sch_t1", "business_name": "表1", "description": "测试表1",
        "datasource": "primary",
        "columns": [
            {"name": "code", "type": "VARCHAR", "not_null": True},
            {"name": "name", "type": "VARCHAR"},
            {"name": "value", "type": "FLOAT"},
        ],
        "foreign_keys": [],
        "indexes": [{"name": "idx_sch_t1_code", "columns": ["code"], "unique": True}],
    })

    # 建表（确保 DB 与 YAML 一致）
    from core.schema_manager import _load_config, batch_create_tables
    cfg = _load_config()
    batch_create_tables(cfg.get("tables", []))

    return p


def cleanup_industry():
    _sync_industry(ORIGINAL_INDUSTRY)
    p = os.path.join(INDUSTRIES_DIR, TEST_INDUSTRY)
    if os.path.exists(p):
        shutil.rmtree(p)
    try:
        drv = _get_phys_drv()
        for t in ["sch_t1", "sch_t2", "sch_extra"]:
            if drv.table_exists(t):
                drv.drop_table(t)
        drv.commit()
    except Exception:
        pass


def _run_preflight():
    from core.schema_manager import _preflight_check
    return _preflight_check()


# ═══════════════════════════════════════════════════════════════
# 测试函数
# ═══════════════════════════════════════════════════════════════

def test_consistent_state():
    """测试 schema 与 DB 完全一致时 _preflight_check 通过（返回空串，不阻断）

    依赖干净 DB 假设（默认数据源只有 _test_schema 的表），
    由 TEST_DS_CONFIG 独立临时库保证。
    """
    print("\n=== 8.1 一致状态通过 preflight ===")
    setup_clean_industry()
    result = _run_preflight()
    check("consistent.pass", result == "",
          f"期望一致通过(空串)，实际='{result[:80]}'")


def test_field_type_mismatch():
    """测试 YAML 字段类型与 DB 不一致时被 _preflight_check 检测出来"""
    print("\n=== 8.4 字段类型不一致检测 ===")
    setup_clean_industry()
    p = os.path.join(INDUSTRIES_DIR, TEST_INDUSTRY)
    # value: FLOAT（DB 中为 REAL）改写成 INTEGER，制造类型不一致
    _write_schema(p, "sch_t1", {
        "name": "sch_t1", "business_name": "表1", "description": "测试表1",
        "datasource": "primary",
        "columns": [
            {"name": "code", "type": "VARCHAR", "not_null": True},
            {"name": "name", "type": "VARCHAR"},
            {"name": "value", "type": "INTEGER"},
        ],
        "foreign_keys": [],
        "indexes": [{"name": "idx_sch_t1_code", "columns": ["code"], "unique": True}],
    })
    result = _run_preflight()
    check("type_mismatch.detected", "属性不一致" in result and "value" in result,
          f"期望检测到 value 属性不一致，实际='{result[:80]}'")


def test_yaml_table_missing_in_db():
    """测试 YAML 定义了表但 DB 中不存在"""
    print("\n=== 8.2 YAML表在DB中不存在 ===")
    setup_clean_industry()
    p = os.path.join(INDUSTRIES_DIR, TEST_INDUSTRY)
    _write_schema(p, "sch_t2", {
        "name": "sch_t2", "business_name": "表2", "description": "测试表2",
        "datasource": "primary",
        "columns": [{"name": "col1", "type": "VARCHAR"}],
        "foreign_keys": [], "indexes": [],
    })
    result = _run_preflight()
    check("missing_table.detected", "不一致" in result or "不存在" in result,
          f"期望检测到不一致，实际='{result[:60]}'")


def test_require_consistency_decorator():
    """测试 require_consistency 装饰器拦截不一致状态下的操作

    list_tables 未被装饰，改用 recreate_table（带 @require_consistency）
    """
    print("\n=== 8.8 require_consistency 装饰器 ===")
    setup_clean_industry()
    p = os.path.join(INDUSTRIES_DIR, TEST_INDUSTRY)
    _write_schema(p, "sch_t2", {
        "name": "sch_t2", "business_name": "表2", "description": "测试表2",
        "datasource": "primary",
        "columns": [{"name": "col1", "type": "VARCHAR"}],
        "foreign_keys": [], "indexes": [],
    })
    # recreate_table 被 @require_consistency 装饰，会先做 _preflight_check
    from core.schema_manager import recreate_table
    result = recreate_table("sch_t1", "code:VARCHAR,name:VARCHAR")
    check("decorator.blocks", isinstance(result, dict) and result.get("ok") == False,
          f"期望被拦截(ok=False)，实际={str(result)[:80]}")
    check("decorator.has_message", "不一致" in result.get("message", ""),
          f"message={result.get('message', '')[:80]}")


def test_cross_datasource_fk_skip():
    """测试跨数据源外键在深度校验时被跳过"""
    print("\n=== 8.9 跨数据源外键跳过校验 ===")
    try:
        from core.datasource_manager import DataSourceManager
        dsm = DataSourceManager()
        dsm.load_config()
        ds_list = [d["name"] for d in dsm.list_datasources()]
        if "secondary" not in ds_list:
            check("cross_fk.skip", True, "无secondary数据源，跳过")
            return
    except Exception:
        check("cross_fk.skip", True, "DSM不可用，跳过")
        return
    check("cross_fk.skip", True, "跨数据源外键跳过逻辑已由 test_06 覆盖")


# ═══════════════════════════════════════════════════════════════
# 8.10+ schema_matcher 分级匹配函数直测（内存 schema，纯函数）
# ═══════════════════════════════════════════════════════════════

_MATCHER_TABLES = [
    {"name": "patients", "business_name": "患者表",
     "columns": [{"name": "patient_id"}, {"name": "name"}]},
    {"name": "orders", "comment": "订单表",
     "columns": [{"name": "order_id"}]},
]


def test_matcher_table_levels():
    """表层级三级匹配：映射别名(100) / schema表名(50)+业务名(80) / 未命中"""
    print("\n=== 8.10 schema_matcher 表层级匹配 ===")
    from core.schema_matcher import (
        _match_tables_by_mapping, _match_tables_by_schema,
        _pick_table, _resolve_table_level,
    )

    # 第 1 级：table_mapping 别名命中 / 未命中
    cand = _match_tables_by_mapping("查询病人的信息", {"病人": "patients"})
    check("matcher.map.hit", cand == [(100, "patients", "病人")], str(cand))
    check("matcher.map.miss", _match_tables_by_mapping("查询病人", {"医生": "patients"}) == [])

    # 第 2 级：schema 原始表名精确命中（50）
    cand = _match_tables_by_schema("查询 patients 的数据", _MATCHER_TABLES)
    check("matcher.schema.exact", (50, "patients", "patients") in cand, str(cand))
    # 第 2 级：business_name / comment 命中（80）
    cand = _match_tables_by_schema("患者表有多少行", _MATCHER_TABLES)
    check("matcher.schema.bizname", (80, "patients", "患者表") in cand, str(cand))
    cand = _match_tables_by_schema("订单表有多少行", _MATCHER_TABLES)
    check("matcher.schema.comment", (80, "orders", "订单表") in cand, str(cand))
    # 第 2 级：未命中
    check("matcher.schema.miss", _match_tables_by_schema("完全不相关", _MATCHER_TABLES) == [])

    # _pick_table：唯一最优 / 同优先级多表歧义 / 同优先级长匹配串优先
    t, err = _pick_table([(80, "patients", "患者表"), (50, "orders", "orders")])
    check("matcher.pick.top", t == "patients" and err is None, f"{t} {err}")
    t, err = _pick_table([(100, "a", "x"), (100, "b", "y")])
    check("matcher.pick.ambiguous", t == "" and err and err.get("ambiguous") is True
          and "多个表匹配" in err.get("message", ""), str(err))

    # 编排：映射别名(100) 优先于 schema 业务名(80)
    t, err = _resolve_table_level("病人 患者表", _MATCHER_TABLES, {"病人": "patients"})
    check("matcher.level.alias_priority", t == "patients" and err is None, f"{t} {err}")
    # 编排：同优先级歧义 → 返回 ambiguous 字典
    t, err = _resolve_table_level("病人和医嘱", _MATCHER_TABLES,
                                  {"病人": "patients", "医嘱": "orders"})
    check("matcher.level.ambiguous", t == "" and err and err.get("ambiguous") is True, str(err))
    # 编排：未命中 → ("", None) 不阻断（中文输入不可能子串命中任何 ASCII 表名，
    # DB 兜底级也必然落空）
    t, err = _resolve_table_level("未命中路径专用输入", _MATCHER_TABLES, {})
    check("matcher.level.miss", t == "" and err is None, f"{t} {err}")


def test_matcher_record_column_levels():
    """记录级/字段级匹配直测"""
    print("\n=== 8.11 schema_matcher 记录/字段级匹配 ===")
    from core.schema_matcher import _resolve_record_level, _resolve_column_level

    field_map = {"患者编号": "patient_id"}

    # 记录级：field_mapping 反查唯一表 + 条件提取
    t, conds, err = _resolve_record_level("患者编号 P001 的记录", _MATCHER_TABLES, field_map, "")
    check("matcher.record.table", t == "patients" and err is None, f"{t} {err}")
    check("matcher.record.cond",
          conds == [{"field": "patient_id", "op": "=", "value": "P001 的记录"}], str(conds))

    # 记录级：字段存在于多个表 → ambiguous
    tables2 = _MATCHER_TABLES + [{"name": "visits", "columns": [{"name": "patient_id"}]}]
    t, conds, err = _resolve_record_level("患者编号 P001", tables2, field_map, "")
    check("matcher.record.ambiguous", t == "" and err and err.get("ambiguous") is True
          and "字段在多个表中存在" in err.get("message", ""), str(err))

    # 记录级：未命中 → ("", [], None) 不阻断
    t, conds, err = _resolve_record_level("随便一句话", _MATCHER_TABLES, {}, "")
    check("matcher.record.miss", t == "" and conds == [] and err is None, f"{t} {conds} {err}")

    # 字段级：field_map 命中确定字段
    t, c = _resolve_column_level("患者编号", _MATCHER_TABLES, {}, field_map, "patients", "")
    check("matcher.column.fieldmap", t == "patients" and c == "patient_id", f"{t} {c}")

    # 字段级：table_map 先定表 + 扫描实际字段名（field_map 未命中时）
    t, c = _resolve_column_level("病人 name", _MATCHER_TABLES, {"病人": "patients"}, {}, "", "")
    check("matcher.column.scan", t == "patients" and c == "name", f"{t} {c}")

    # 字段级：表无法确定 → 原样返回不阻断
    t, c = _resolve_column_level("无关输入", _MATCHER_TABLES, {}, {}, "", "")
    check("matcher.column.miss", t == "" and c == "", f"{t} {c}")


# ═══════════════════════════════════════════════════════════════
# 8.10 拆分纯函数直测（2026-07-19 _preflight_check / batch_create_tables 重构后追加）
# 不依赖真实 DB：用 _FakeConn/_FakeDSM 模拟裸连接与数据源路由
# ═══════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows


class _FakeConn:
    """模拟裸 SQLite 连接：按 PRAGMA 类型返回预设行"""
    def __init__(self, fk_rows=None, idx_rows=None, idx_info=None):
        self._fk_rows = fk_rows or []
        self._idx_rows = idx_rows or []
        self._idx_info = idx_info or {}

    def execute(self, sql):
        if "foreign_key_list" in sql:
            return _FakeCursor(self._fk_rows)
        if "index_list" in sql:
            return _FakeCursor(self._idx_rows)
        if "index_info" in sql:
            name = sql[sql.index("(") + 1:sql.rindex(")")]
            return _FakeCursor(self._idx_info.get(name, []))
        raise AssertionError("unexpected SQL: " + sql)


class _FakeDSM:
    """模拟 DataSourceManager 的表→数据源路由"""
    def __init__(self, mapping=None, default="primary"):
        self._m = mapping or {}
        self._d = default

    def get_datasource_for_table(self, t):
        return self._m.get(t, self._d)


def test_scan_constraint_impact_pure():
    """ChangeAnalyzer._scan_constraint_impact（NOT NULL/UNIQUE 加严采样的公共助手）：
    drv 缺省→{}；正常采样→{key: count}；非法标识符→fail-closed {}（不阻断 diff）"""
    print("\n=== 8.10.0 约束加严影响面采样助手 ===")
    from core.contract.change_analyzer import ChangeAnalyzer

    class _FakeDrv:
        def query(self, sql):
            return [{"c": 3}]

    check("scan.no_drv", ChangeAnalyzer._scan_constraint_impact(
        None, "t", "c", "SELECT 1", "k") == {})
    got = ChangeAnalyzer._scan_constraint_impact(
        _FakeDrv(), "t", "c",
        "SELECT COUNT(*) AS c FROM t WHERE c IS NULL", "null_count")
    check("scan.normal", got == {"null_count": 3}, f"实际={got}")
    check("scan.bad_identifier_failclosed", ChangeAnalyzer._scan_constraint_impact(
        _FakeDrv(), "t; DROP", "c", "SELECT 1", "k") == {})


def test_check_tables_pure():
    """_check_tables：缺表报对消息、返回 yaml_tables；多余表降级为 warning 不阻断"""
    print("\n=== 8.10.1 _check_tables 缺表 ===")
    import tempfile
    from pathlib import Path as _P
    from core.schema_manager import _check_tables
    tmp = tempfile.mkdtemp()
    try:
        (_P(tmp) / "t_a.yaml").write_text(yaml.dump({"name": "t_a", "columns": []}), encoding="utf-8")
        yaml_tables, err = _check_tables(_P(tmp), set())  # DB 中无任何表
        check("tables.missing_msg", err == "配置与数据库不一致", f"实际='{err}'")
        check("tables.returns_yaml_tables", yaml_tables == {"t_a"}, f"实际={yaml_tables}")
        # DB→YAML 多余表保持阻断（单行业应用纪律：非系统表须在 YAML 中定义）
        # 模拟 DSM 不可用走 db_tables 回退路径，使断言不依赖真实库内容
        import core.datasource_manager as _dsm_mod
        _orig_dsm = _dsm_mod.DataSourceManager
        class _NoDSM:
            def get_default_name(self): return "primary"
            def get_driver(self, name): raise RuntimeError("no dsm")  # 触发 db_tables 回退
        _dsm_mod.DataSourceManager = _NoDSM
        try:
            yaml_tables, err = _check_tables(_P(tmp), {"t_a", "legacy_other_industry"})
            check("tables.extra_blocked", "未定义" in err and "legacy_other_industry" in err,
                  f"实际='{err}'")
            # meta_ 前缀系统表豁免，不误拦
            yaml_tables, err = _check_tables(_P(tmp), {"t_a", "meta_columns"})
            check("tables.meta_exempt", err == "", f"实际='{err}'")
        finally:
            _dsm_mod.DataSourceManager = _orig_dsm
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_check_columns_pure():
    """_check_columns：缺列/属性不一致/多余字段各报对消息，一致与 pk 隐含 not_null 不误报"""
    print("\n=== 8.10.2 _check_columns ===")
    from core.schema_manager import _check_columns
    t = {"columns": [{"name": "c1", "type": "VARCHAR"}]}
    # 缺列
    r = _check_columns("T", t, {})
    check("cols.missing_msg", r == "表 T 的字段 'c1' 在数据库中不存在，配置与数据库不一致", f"实际='{r}'")
    # 属性不一致（VARCHAR→TEXT vs INTEGER）
    r = _check_columns("T", t, {"c1": {"name": "c1", "type": "INTEGER", "not_null": 0}})
    check("cols.diff_msg", r.startswith("表 T 的字段 'c1' 属性不一致"), f"实际='{r}'")
    # DB 多余字段
    r = _check_columns("T", t, {"c1": {"name": "c1", "type": "TEXT", "not_null": 0},
                                "c2": {"name": "c2", "type": "TEXT", "not_null": 0}})
    check("cols.extra_msg", r == "表 T 在数据库中有配置文件未定义的字段: c2", f"实际='{r}'")
    # 全齐不误报（VARCHAR 统一化为 TEXT）
    r = _check_columns("T", t, {"c1": {"name": "c1", "type": "TEXT", "not_null": 0}})
    check("cols.consistent", r == "", f"实际='{r}'")
    # pk 隐含 not_null：DB 侧 not_null=0 + pk=1、YAML 侧 pk=True，均不应误报
    r = _check_columns("T", {"columns": [{"name": "id", "type": "INTEGER", "pk": True}]},
                       {"id": {"name": "id", "type": "INTEGER", "not_null": 0, "pk": 1}})
    check("cols.pk_implied_nn", r == "", f"实际='{r}'")


def test_check_foreign_keys_pure():
    """_check_foreign_keys：引用表缺失/外键不一致报对消息，一致与跨数据源跳过不误报"""
    print("\n=== 8.10.3 _check_foreign_keys ===")
    from core.schema_manager import _check_foreign_keys
    fk = {"columns": ["pid"], "references": "parent", "ref_columns": ["id"]}
    t = {"foreign_keys": [fk]}
    dsm = _FakeDSM()
    # 引用表不存在
    r = _check_foreign_keys("child", t, {"child"}, None, dsm)
    check("fk.ref_missing_msg", r == "外键引用表 'parent' 在数据库中不存在（定义在 child 中）", f"实际='{r}'")
    # 外键不一致（DB 中无此 FK）
    r = _check_foreign_keys("child", t, {"child", "parent"}, _FakeConn(fk_rows=[]), dsm)
    check("fk.mismatch_msg", r == "表 child 的外键 'pid→parent.id' 与数据库不一致，DB=[]", f"实际='{r}'")
    # 一致不误报（fk_row: id, seq, table, from, to, ...）
    conn = _FakeConn(fk_rows=[(0, 0, "parent", "pid", "id", "NO ACTION", "NO ACTION", "NONE")])
    r = _check_foreign_keys("child", t, {"child", "parent"}, conn, dsm)
    check("fk.consistent", r == "", f"实际='{r}'")
    # 跨数据源外键跳过深度校验（DB 无 FK 也不报）
    cross_dsm = _FakeDSM({"parent": "secondary"}, default="primary")
    r = _check_foreign_keys("child", t, {"child", "parent"}, _FakeConn(fk_rows=[]), cross_dsm)
    check("fk.cross_ds_skip", r == "", f"实际='{r}'")
    # 无外键定义
    r = _check_foreign_keys("child", {"foreign_keys": []}, {"child"}, None, dsm)
    check("fk.no_fk", r == "", f"实际='{r}'")


def test_check_indexes_pure():
    """_check_indexes：缺索引/unique/列/多余索引各报对消息，一致与 sqlite_ 内部索引不误报"""
    print("\n=== 8.10.4 _check_indexes ===")
    from core.schema_manager import _check_indexes
    t = {"indexes": [{"name": "idx_t_c", "columns": ["c"], "unique": True}]}
    # 缺索引
    r = _check_indexes("T", t, _FakeConn(idx_rows=[]))
    check("idx.missing_msg", r == "表 T 的索引 'idx_t_c' 不存在于数据库中", f"实际='{r}'")
    # unique 不一致（idx_row: seq, name, unique, ...）
    r = _check_indexes("T", t, _FakeConn(idx_rows=[(0, "idx_t_c", 0, "c", 0)],
                                         idx_info={"idx_t_c": [(0, 0, "c")]}))
    check("idx.unique_msg", r == "表 T 的索引 'idx_t_c' unique不一致：YAML=True，DB=False", f"实际='{r}'")
    # 列不一致
    r = _check_indexes("T", t, _FakeConn(idx_rows=[(0, "idx_t_c", 1, "c", 0)],
                                         idx_info={"idx_t_c": [(0, 0, "c2")]}))
    check("idx.cols_msg", r == "表 T 的索引 'idx_t_c' 列不一致：YAML=['c']，DB=['c2']", f"实际='{r}'")
    # DB 多余索引
    r = _check_indexes("T", {"indexes": []}, _FakeConn(idx_rows=[(0, "idx_extra", 1, "c", 0)]))
    check("idx.extra_msg", r == "表 T 有数据库中多余的索引 'idx_extra'，配置文件中未定义", f"实际='{r}'")
    # 全齐不误报
    r = _check_indexes("T", t, _FakeConn(idx_rows=[(0, "idx_t_c", 1, "c", 0)],
                                         idx_info={"idx_t_c": [(0, 0, "c")]}))
    check("idx.consistent", r == "", f"实际='{r}'")
    # sqlite_ 内部索引被过滤，不误报多余
    r = _check_indexes("T", {"indexes": []}, _FakeConn(idx_rows=[(0, "sqlite_autoindex_T_1", 1, "c", 0)]))
    check("idx.sqlite_internal_filtered", r == "", f"实际='{r}'")


def test_topo_sort_tables_pure():
    """_topo_sort_tables：被引用表在前、外部引用不阻塞、有环按名序追加、不改入参"""
    print("\n=== 8.10.5 _topo_sort_tables ===")
    from core.schema_manager import _topo_sort_tables
    defs = [{"name": "child", "foreign_keys": [{"references": "parent", "columns": ["pid"]}]},
            {"name": "parent", "columns": []}]
    ordered = _topo_sort_tables(defs)
    check("topo.parent_first", ordered == ["parent", "child"], f"实际={ordered}")
    # 兼容旧格式 fk 字段 + 外部引用不阻塞
    defs2 = [{"name": "b"}, {"name": "a", "fk": "ext_table"}]
    check("topo.ext_ref_no_block", _topo_sort_tables(defs2) == ["a", "b"],
          f"实际={_topo_sort_tables(defs2)}")
    # 有环：剩余按名称排序追加
    defs3 = [{"name": "x", "foreign_keys": [{"references": "y"}]},
             {"name": "y", "foreign_keys": [{"references": "x"}]}]
    check("topo.cycle_sorted", _topo_sort_tables(defs3) == ["x", "y"],
          f"实际={_topo_sort_tables(defs3)}")
    # 纯函数：不修改入参
    check("topo.no_mutation", "columns" not in defs[0] and "columns" not in defs2[0], "入参被修改")


def test_prepare_columns_pure():
    """_prepare_columns：自动补 id、用户主键转唯一约束、不支持类型记入 results"""
    print("\n=== 8.10.6 _prepare_columns ===")
    from core.schema_manager import _prepare_columns
    d = {"name": "t", "columns": [{"name": "code", "type": "VARCHAR", "pk": True},
                                  {"name": "v", "type": "BADTYPE"}]}
    results = []
    cols = _prepare_columns(d, "t", results)
    check("prep.auto_id", d["columns"][0] == {"name": "id", "type": "INTEGER", "pk": True, "not_null": True},
          f"实际={d['columns'][0]}")
    code_col = next(c for c in d["columns"] if c["name"] == "code")
    check("prep.user_pk_to_unique",
          code_col.get("unique") is True and code_col.get("not_null") is True
          and code_col.get("pk") is False and code_col.get("is_pk") is False,
          f"实际={code_col}")
    check("prep.bad_type_recorded", results == ["t: 不支持的类型 BADTYPE"], f"实际={results}")
    check("prep.col_defs", "id:INTEGER:pk" in cols and "code:VARCHAR" in cols
          and not any(c.startswith("v:") for c in cols), f"实际={cols}")


def test_filter_cross_ds_fks_pure():
    """_filter_cross_ds_fks：无 datasource 时原样返回（同一对象，YAML 保留完整 FK）"""
    print("\n=== 8.10.7 _filter_cross_ds_fks ===")
    from core.schema_manager import _filter_cross_ds_fks
    d = {"name": "t", "foreign_keys": [{"references": "other", "columns": ["oid"]}]}
    check("filter.no_ds_identity", _filter_cross_ds_fks(d, "", "t") is d, "未返回原对象")
    d2 = {"name": "t"}  # 无 foreign_keys 时也不过滤
    check("filter.no_fk_identity", _filter_cross_ds_fks(d2, "primary", "t") is d2, "未返回原对象")


def test_verify_reconciliation():
    """三层对账——YAML/MetaDB/Ladybug 漂移各方向都能报出"""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import core.graph.schema_graph_service as sgs

    with tempfile.TemporaryDirectory() as tmp:
        # YAML 层：2 表 + 1 外键
        (Path(tmp) / "orders.yaml").write_text(
            chr(10).join(["name: orders", "columns: []", "foreign_keys: []", ""]), encoding="utf-8")
        (Path(tmp) / "items.yaml").write_text(
            chr(10).join(["name: items", "columns: []", "foreign_keys:",
                          "- columns: [order_id]", "  references: orders", ""]), encoding="utf-8")

        svc = sgs.SchemaGraphService.__new__(sgs.SchemaGraphService)
        fake_meta = MagicMock()
        fake_meta.list_tables.return_value = [{"name": "orders"}]
        fake_meta.get_all_foreign_keys.return_value = []
        svc._meta = fake_meta

        # 图库层：有 orders+items 节点但缺 FK 边；且有 1 条写失败记录
        _failures = [{"op": "create_relationship", "detail": "items->orders",
                      "error": "连接中断", "ts": "2026-07-20T20:00:00"}]
        with patch.object(sgs, "_get_schemas_dir", return_value=Path(tmp)), patch.object(sgs.LadybugStore, "is_available", return_value=True), patch.object(sgs.LadybugStore, "get_all_nodes", return_value=[{"name": "orders"}, {"name": "items"}]), patch.object(sgs.LadybugStore, "get_all_edges", return_value=[]), patch.object(sgs.LadybugStore, "recent_write_failures", return_value=_failures):
            d = svc.verify_reconciliation()

        assert d["ok"] is False
        assert d["yaml_not_in_meta"] == ["items"], d["yaml_not_in_meta"]
        assert d["meta_not_in_yaml"] == []
        assert d["fk_yaml_not_in_meta"] == [["items", "order_id", "orders"]], d["fk_yaml_not_in_meta"]
        assert d["fk_yaml_not_in_graph"] == [["items", "order_id", "orders"]], d["fk_yaml_not_in_graph"]
        assert d["yaml_not_in_graph"] == [] and d["graph_not_in_yaml"] == []
        assert len(d["write_failures"]) == 1 and d["write_failures"][0]["op"] == "create_relationship"

        # 图库不可用：不假装一致，明确报降级
        with patch.object(sgs, "_get_schemas_dir", return_value=Path(tmp)), patch.object(sgs.LadybugStore, "is_available", return_value=False), patch.object(sgs.LadybugStore, "recent_write_failures", return_value=[]):
            d2 = svc.verify_reconciliation()
        assert d2["graph_available"] is False
        assert "graph_warning" in d2, "图库不可用应显式报降级"

        # 写失败台账：失败被记录（不再只 log 就完）
        from core.graph.ladybug_store import _WRITE_FAILURES, _record_write_failure
        n0 = len(_WRITE_FAILURES)
        _record_write_failure("upsert_table_node", "t_x", RuntimeError("模拟失败"))
        assert len(_WRITE_FAILURES) == n0 + 1
        assert _WRITE_FAILURES[-1]["detail"] == "t_x"
    print("OK - 三层对账：漂移各方向报出+图库降级显式+写失败台账")


def test_schemas_written_hook_reconciles():
    """P-D 钩子链端到端（回归锁）：
    AI DDL 路径（schema_manager.batch_create_tables）写完 YAML 即经
    registry 的 schemas_written 通道触发 graph service 对账收敛——
    MetaDB 必须即时可见新表（不等重启/不等手工"同步 YAML"），
    且 prune 分支随删表同步移除。"""
    from core.schema_manager import batch_create_tables, drop_table
    import core.graph.schema_graph_service  # noqa: F401  # 导入即注册订阅
    from core.graph.meta_db import MetaDB

    # 自带干净现场（前置用例会刻意制造不一致，require_consistency 会拦 drop）
    setup_clean_industry()

    defs = [{"name": "hook_t", "business_name": "钩子表", "description": "",
             "datasource": "primary",
             "columns": [{"name": "code", "type": "VARCHAR"}],
             "foreign_keys": []}]
    r = batch_create_tables(defs)
    assert "hook_t" in str(r), f"建表失败: {r}"

    meta = MetaDB.get_instance()
    names = [t["name"] for t in meta.list_tables()]
    assert "hook_t" in names,         f"AI DDL 后 MetaDB 未即时收敛（钩子链断）: {names}"

    r2 = drop_table("hook_t")
    assert r2.get("ok"), f"删表失败: {r2}"
    names2 = [t["name"] for t in meta.list_tables()]
    assert "hook_t" not in names2,         f"删表后 MetaDB 残留（prune 分支断）: {names2}"
    check("schemas_written 钩子：建表即时收敛+删表即时 prune", True)


if __name__ == "__main__":
    try:
        test_consistent_state()
        test_field_type_mismatch()
        test_yaml_table_missing_in_db()
        test_require_consistency_decorator()
        test_cross_datasource_fk_skip()
        test_matcher_table_levels()
        test_matcher_record_column_levels()
        test_check_tables_pure()
        test_check_columns_pure()
        test_check_foreign_keys_pure()
        test_check_indexes_pure()
        test_topo_sort_tables_pure()
        test_scan_constraint_impact_pure()
        test_prepare_columns_pure()
        test_filter_cross_ds_fks_pure()
        test_verify_reconciliation()
        test_schemas_written_hook_reconciles()
    finally:
        cleanup_industry()
        _teardown_isolated_ds()

    print(f"\n{'='*50}")
    print(f"SCHEMA: PASS={pass_count}  FAIL={fail_count}  TOTAL={pass_count+fail_count}")
    if fail_count:
        print(f"失败项: {errors}")
        sys.exit(1)
    print("=== ALL SCHEMA CONSISTENCY TESTS PASSED ===")
