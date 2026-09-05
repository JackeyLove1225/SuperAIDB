"""工具参数生成后校验（单一事实源）

execute_tool 边界（core.tool_registry）在 handler 执行前统一调用——
MCP 直连与历史图路径同闸（图编排已于 20260824 下线，本闸为唯一存活调用点）。
无此闸时 AI 会拿带噪/假想表名直达驱动层，报错又丢名难自查——
"批量插入报引用的表不存在"类错误的根因。

拦截四类高发错误：假想表名 / 假想字段名 / 中文列名直传 / 外键引用非 id 列。
表名归一（_norm_table）是确定性纠正：声明的业务名精确命中（YAML 元数据）、
噪声尾巴剥离、唯一前缀/包含候选——仅当能唯一解析回真实表名才改参数，
不能就如实报不存在并附可用表清单，不在执行层猜。
"""
import json as _json

# 边界闸的豁免工具：其 table 参数语义不是"必须存在的既有表"
# - drop_table："全部/所有表格"等批量关键词由 handler 语义转换为清库，
#   存在性校验会把合法批量删除拦在半路（20260804 修复的语义不回退）
_EXEMPT_TOOLS = frozenset({"drop_table"})


def enumerate_objects():
    """枚举数据库、表、列，供 FC 构建与生成后校验

    数据来源：YAML schema 优先（有元数据），数据库实际表补充（YAML 未定义的表）。
    确保即使行业 schema 未配置，AI 也能看到数据库中的实际表。
    """
    from config.settings import settings as _s
    from core.schema_matcher import load_schemas as _ls
    schemas = _ls()

    # 数据库实际表补充（YAML 未定义的表，从 DB 获取字段）
    from core.data_ops import get_driver as _get_fed_driver
    try:
        drv = _get_fed_driver()
        db_tables = drv.list_tables()
        existing_names = {s["name"] for s in schemas}
        for tname in db_tables:
            if tname.startswith("sqlite_") or tname in existing_names:
                continue
            try:
                cols = drv.get_columns(tname)
                schemas.append({
                    "name": tname,
                    "columns": [{"name": c["name"], "type": c.get("type", "")} for c in cols],
                    "foreign_keys": [],
                })
            except Exception:
                continue
    except Exception:
        pass  # 单表列枚举失败则跳过该表补充（YAML 已覆盖的表不受影响）

    db = [_s.SQLITE_DB_PATH]
    tables = sorted(set(t["name"] for t in schemas))
    cols = sorted(set(c["name"] for s in schemas for c in s.get("columns", [])))
    types = ["INTEGER", "TEXT", "FLOAT", "VARCHAR"]
    return db, schemas, tables, cols, types


def _biz_name_map(schemas) -> dict:
    """声明业务名 → 表名（YAML 元数据的确定性映射，非猜测）

    仅收录全量表中唯一无歧义的业务名；重名业务名不收（收了就是猜）。
    """
    pairs = [(s.get("business_name"), s["name"]) for s in schemas
             if s.get("business_name") and s.get("name")]
    from collections import Counter
    dup = Counter(b for b, _ in pairs)
    return {b: n for b, n in pairs if dup[b] == 1}


def _norm_table(v: str, all_tables, biz_map) -> str:
    """表名归一（确定性）：LLM 产出的 table 参数常带噪声尾巴（'表'/'批量插入'…）
    或直接用声明的业务名（如"定额项目主表"）——能唯一解析回真实表名就纠正，
    不能就如实报不存在——不在执行层猜。

    候选方向只保留"输入以真实表名开头且尾巴含非 ASCII（CJK 噪声）"
    （v.startswith(t)：'t_order批量插入2条' 这类噪声尾巴场景）；
    纯 ASCII 尾巴不归一——users2/users_backup 是"像真名的后缀"而非噪声
    （startswith 会静默把写操作改写到 users）。
    反向子串（t in v）同病在此一并封堵。"""
    v = (v or "").strip()
    if not v or v in all_tables:
        return v
    if v in biz_map:
        return biz_map[v]  # 声明业务名精确命中（YAML 元数据，不是猜）
    core = v.rstrip("表 ")
    if core in all_tables:
        return core

    def _is_noise_tail(rest: str) -> bool:
        """尾巴含非 ASCII（中文等）才算噪声——纯 ASCII 尾巴（数字/下划线/字母）
        视为真实表名的一部分，不归一（users2 不得改写进 users）"""
        rest = rest.strip()
        return bool(rest) and any(ord(ch) > 127 for ch in rest)

    cands = [t for t in all_tables if v.startswith(t) and _is_noise_tail(v[len(t):])]
    return cands[0] if len(cands) == 1 else v


def _table_columns(schemas, tname):
    """表的真实列名集合（YAML schema 元数据）；表不在 schema 中返回空集"""
    for s in schemas:
        if s.get("name") == tname:
            return {c["name"] for c in s.get("columns", [])}
    return set()


def _field_aliases():
    """fields.yml 业务别名集合（字段英文名 + 中文别名）

    真身：industries/base.py IndustryConfig.field_dict——
    行业目录 fields/fields.yml 的规范加载器（目录签名新鲜度，改配置免重启）。
    此前引用的 core.data_ops._load_fields_config 已不存在（僵尸引用），
    ImportError 被静默吞掉导致别名集恒空、业务别名参数被误报不存在。
    """
    try:
        from industries.base import get_current_industry
        fc = get_current_industry().field_dict
        out = set()
        for k, v in (fc or {}).items():
            if isinstance(v, dict):
                out.add(k)
                out.update(v.get("alias", []) or [])
        return out
    except Exception:
        return set()


def _check_table_refs(args, all_tables, biz_map):
    """表存在性校验：table / main_table / ref_table / join_tables

    先表名归一纠正（写回 args 并记日志），再按固定顺序逐项校验；
    返回 (归一后的 table, problems)——校验顺序即 problems 顺序，不可调换。
    """
    problems = []
    raw_table = args.get("table", "")
    table = _norm_table(raw_table, all_tables, biz_map)
    if table != raw_table:
        args["table"] = table
        from core.logger import info as _li
        _li("表名归一纠正", raw=raw_table, resolved=table)
    if table and table not in all_tables:
        problems.append(f"表 '{table}' 不存在")
    for key in ("main_table", "ref_table"):
        t = args.get(key, "")
        if t and t not in all_tables:
            problems.append(f"表 '{t}' 不存在")
    if args.get("join_tables"):
        for t in [x.strip() for x in str(args["join_tables"]).split(",") if x.strip()]:
            if t not in all_tables:
                problems.append(f"关联表 '{t}' 不存在")
    return table, problems


def _check_field_refs(tool_name, args, table, schemas):
    """字段存在性校验：column/agg_field/group_by/order_by 须命中真实列或业务别名"""
    problems = []
    real = _table_columns(schemas, table) if table else set()
    alias = _field_aliases()
    for key in ("column", "agg_field", "group_by", "order_by"):
        v = str(args.get(key, "") or "")
        if not v or not table or not real:
            continue
        # add_column 的字段本就不该存在（存在性检查只适用于操作既有字段的工具）
        if tool_name == "add_column" and key == "column":
            continue
        for f in [x.strip() for x in v.split(",") if x.strip()]:
            base = f.split(".")[-1].split(" ")[0]
            if base in ("*", "COUNT(*)"):
                continue
            if base not in real and base not in alias:
                problems.append(f"字段 '{base}' 在表 {table} 中不存在")
    return problems


def _check_definitions(args):
    """建表定义校验：表名/字段名标识符合法，外键只能指向引用表的 id 列"""
    from core.contract.security_contract import is_valid_identifier
    problems = []
    defs = args.get("definitions")
    if defs:
        if isinstance(defs, str):
            try:
                defs = _json.loads(defs)
            except Exception:
                defs = []
        for t in defs if isinstance(defs, list) else []:
            tn = t.get("name", "")
            if not is_valid_identifier(tn):
                problems.append(f"建表名 '{tn}' 非法（须英文 snake_case）")
            for c in t.get("columns", []):
                cn = c.get("name", "")
                if not is_valid_identifier(cn):
                    problems.append(f"字段名 '{cn}' 非法（须英文 snake_case，中文业务名放 business_name）")
            for fk in t.get("foreign_keys", []):
                refcols = fk.get("ref_columns", [])
                if refcols and refcols != ["id"]:
                    problems.append(
                        f"表 {tn} 的外键 {fk.get('columns')} 引用了非 id 列 {refcols}（外键只能指向引用表的 id）")
    return problems


def _format_problems(problems, all_tables):
    """报错装配：问题清单 + 可用表清单（清单按当前角色读权限过滤）"""
    valid_tables = "、".join(sorted(all_tables)) if all_tables else "（无）"
    # 可用表清单按当前角色读权限过滤（自定义 RBAC 下被 deny 的表
    # 不进报错清单——默认 full 策略无影响，收紧后不泄露表结构面）
    try:
        from core.permission import PermissionPolicy, Operation, get_effective_role
        from core.datasource_manager import DataSourceManager
        # 用 effective（提权优先）：sudo 窗口内报错清单口径与实际可执行面一致
        _role = get_effective_role()
        if _role and _role != "system":
            _pol = PermissionPolicy.get_instance()
            _dsm = DataSourceManager()
            _kept = []
            for _t in sorted(all_tables):
                try:
                    _pol.check(_dsm.get_datasource_for_table(_t),
                               Operation.QUERY, table=_t)
                    _kept.append(_t)
                except Exception:
                    continue  # 被 deny 的不进清单
            valid_tables = "、".join(_kept) if _kept else "（无可用表）"
    except Exception:
        pass  # 过滤面故障不阻断报错本身（清单降级全量，判定不受影响）
    return ("参数校验失败（生成后校验拦截）：\n- " + "\n- ".join(problems)
            + f"\n\n可用表：{valid_tables}。请修正后重试。")


def validate_tool_args(tool_name: str, args: dict, schemas: list, all_tables) -> str:
    """生成后校验：AI 参数在执行前过确定性检查

    覆盖 structured_args 透传与 FC AI 两条路径，拦截四类高发错误：
    1. 表名不存在（假想表） 2. 字段名不存在且非别名 3. 中文列名直传 4. 外键引用非 id 列
    返回 None=通过；否则返回中文错误说明（含合法候选）。
    """
    all_tables = set(all_tables)
    biz_map = _biz_name_map(schemas)

    table, problems = _check_table_refs(args, all_tables, biz_map)
    problems += _check_field_refs(tool_name, args, table, schemas)
    problems += _check_definitions(args)

    if problems:
        return _format_problems(problems, all_tables)
    return None


def guard_for_execute(tool_name: str, kwargs: dict):
    """execute_tool 边界闸：MCP 直连与图路径同款的生成后校验

    返回 None=放行；否则返回 ToolResult（VALIDATION/arg_validation，
    文本含可用表清单，AI 可据此自我修正）。

    失败开放（fail-open）边界：表清单枚举本身故障（驱动未就绪等）时放行——
    本闸是健壮性增强，不是安全边界；表存在性的安全兜底仍在各工具/契约层。
    """
    if tool_name in _EXEMPT_TOOLS:
        return None
    try:
        _, schemas, all_tables, _, _ = enumerate_objects()
    except Exception:
        return None
    err = validate_tool_args(tool_name, kwargs, schemas, all_tables)
    if not err:
        return None
    from core.tool_result import ToolResult
    return ToolResult.fail(err, code="VALIDATION", reason="arg_validation")
