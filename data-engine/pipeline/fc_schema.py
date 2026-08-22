"""FC schema 构建层：行业 YAML 配置 → FC 提取 schema/FK 提示（纯配置驱动）"""
from core.logger import get_logger

logger = get_logger(__name__)

def _find_main_table(config):
    """识别主表：被其他表外键引用最多的表"""
    ref_count = {}
    for t in config.tables:
        for fk in t.get("foreign_keys", []):
            ref = fk.get("references", "")
            if ref:
                ref_count[ref] = ref_count.get(ref, 0) + 1
    if not ref_count:
        return config.tables[0]["name"] if config.tables else ""
    return max(ref_count, key=ref_count.get)


def _find_code_field(config, table_name):
    """识别业务编码字段：表的唯一索引字段（非 id）
    用于主表-明细表的关联"""
    for t in config.tables:
        if t["name"] == table_name:
            # 优先找唯一索引字段
            for idx in t.get("indexes", []):
                if idx.get("unique", True) and idx.get("columns"):
                    code_field = idx["columns"][0]
                    if code_field != "id":
                        return code_field
            # 没有唯一索引，找第一个非 id 的 VARCHAR 字段
            for c in t.get("columns", []):
                if c["name"] != "id" and c.get("type", "").upper() in ("VARCHAR", "TEXT", "CHAR"):
                    return c["name"]
            break
    return ""


def _find_fk_to_main(config, table_name, main_table):
    """找到某表指向主表的外键字段名"""
    for t in config.tables:
        if t["name"] == table_name:
            for fk in t.get("foreign_keys", []):
                if fk.get("references") == main_table:
                    cols = fk.get("columns", [])
                    if cols:
                        return cols[0]
    return ""


def _find_main_fks_to_base(config, main_table, extraction_tables):
    """主表指向基础表（非提取表）的外键列表

    这些外键列在 FC schema 中会被跳过，用基础表的业务编码字段（虚拟字段）代替。
    pipeline 入库时把虚拟字段值查基础表转换为外键 id。

    Returns: [(fk_col, ref_table, ref_code), ...]
        - fk_col: 主表外键列名（如 region_id）
        - ref_table: 基础表名（如 region）
        - ref_code: 基础表的业务编码字段名（如 region_code）
    """
    result = []
    for t in config.tables:
        if t["name"] != main_table:
            continue
        for fk in t.get("foreign_keys", []):
            ref_table = fk.get("references", "")
            if not ref_table or ref_table == main_table:
                continue
            if ref_table in extraction_tables:
                continue  # 指向本次提取的主表，由明细表逻辑处理
            fk_cols = fk.get("columns", [])
            if not fk_cols:
                continue
            ref_code = _find_code_field(config, ref_table)
            if ref_code:
                result.append((fk_cols[0], ref_table, ref_code))
    return result


def _get_extraction_tables(config, drv=None):
    """识别需要从文档提取的表（自动判断，不依赖特定行业表结构）

    优先用数据存在性判断：
    - 有数据的表 = 基础表（用户已预插入，如 region/category）
    - 空表 = 提取表（需要从文档提取）

    全部为空或全部有数据时，退化到外键拓扑推断：
    - 被引用最多的表 = 主表
    - 有外键指向主表的表 = 明细表
    - 主表 + 明细表 = 提取表

    Args:
        config: 行业配置
        drv: 数据库驱动（可选，用于检查数据存在性）
    Returns:
        set of table names
    """
    all_tables = [t["name"] for t in config.tables]

    # 优先：数据存在性判断
    if drv:
        empty, non_empty = set(), set()
        for tname in all_tables:
            if not drv.table_exists(tname):
                empty.add(tname); continue
            try:
                rows = drv.query(f"SELECT COUNT(*) as c FROM {tname}")
                if rows and rows[0].get("c", 0) > 0:
                    non_empty.add(tname)
                else:
                    empty.add(tname)
            except Exception:
                empty.add(tname)
        # 部分有数据 → 用数据存在性区分
        if non_empty and empty:
            return empty

    # 退化：外键拓扑推断（主表 + 有外键指向主表的明细表）
    main_table = _find_main_table(config)
    extraction = {main_table}
    for t in config.tables:
        tname = t["name"]
        if tname == main_table:
            continue
        for fk in t.get("foreign_keys", []):
            if fk.get("references") == main_table:
                extraction.add(tname)
                break
    return extraction


def _build_fc_schema(config, drv=None):
    """从 config.tables 构建 Function Calling 定义（纯配置驱动）

    只为"提取表"（主表+明细表）构建 FC，跳过基础表（region/category 等，已预插入）。
    - 跳过 id 字段（自增主键）
    - 主表外键列（指向基础表）跳过，用基础表业务编码字段代替（虚拟字段，入库时转 id）
    - 明细表外键列（指向主表）跳过，用主表业务编码字段代替（虚拟字段，入库时转 id）
    - 字段描述从 YAML 的 description/business_name 读取

    Returns: (props, required_tables, main_table, code_field)
    """
    main_table = _find_main_table(config)
    code_field = _find_code_field(config, main_table)
    extraction_tables = _get_extraction_tables(config, drv)
    # 主表指向基础表的外键：[(fk_col, ref_table, ref_code), ...]
    main_fks_to_base = _find_main_fks_to_base(config, main_table, extraction_tables)
    main_fk_cols = {fk_col for fk_col, _, _ in main_fks_to_base}

    props = {}
    required_tables = []
    for t in config.tables:
        tname = t["name"]
        if tname not in extraction_tables:
            continue  # 基础表不从文档提取，跳过
        required_tables.append(tname)
        col_props = {}
        col_required = []
        # 明细表指向主表的外键列（用主表业务编码字段代替）
        detail_fk_to_main = ""
        if tname != main_table:
            detail_fk_to_main = _find_fk_to_main(config, tname, main_table)

        for c in t.get("columns", []):
            cname = c["name"]
            if cname == "id":
                continue
            # 主表：跳过指向基础表的外键列（用虚拟业务编码字段代替）
            if tname == main_table and cname in main_fk_cols:
                continue
            # 明细表：跳过指向主表的外键列（用虚拟业务编码字段代替）
            if tname != main_table and detail_fk_to_main and cname == detail_fk_to_main:
                continue
            desc = c.get("description") or c.get("business_name") or cname
            col_props[cname] = {"type": "string", "description": desc}
            # 只有 not_null 字段才加入 required，避免 AI 因填不齐全而跳过整张表
            if c.get("not_null"):
                col_required.append(cname)

        # 主表：加上指向基础表的虚拟业务编码字段（可空，不加入 required）
        if tname == main_table:
            for fk_col, ref_table, ref_code in main_fks_to_base:
                if ref_code not in col_props:
                    col_props[ref_code] = {
                        "type": "string",
                        "description": f"所属{ref_table}的业务编码（pipeline自动转换为{fk_col}）"
                    }

        # 明细表：加上主表业务编码字段用于关联（虚拟字段，必填）
        if tname != main_table and code_field:
            if code_field not in col_props:
                col_props[code_field] = {
                    "type": "string",
                    "description": f"所属{main_table}的{code_field}（用于关联，pipeline自动转换为外键id）"
                }
                col_required.append(code_field)

        props[tname] = {
            "type": "array",
            "description": t.get("business_name", tname),
            "items": {
                "type": "object",
                "properties": col_props,
                "required": col_required,
            }
        }

    return props, required_tables, main_table, code_field


def _build_fk_hint(config, drv):
    """扫描外键定义，查询被引用基础表，构建业务编码参考提示

    只处理"提取表"中指向"基础表"的外键（如 region/category 等）。
    主表-明细表的关联用主表业务编码处理，不在这里。
    AI 填业务编码字段（虚拟字段），pipeline 入库时转换为外键 id。
    """
    main_table = _find_main_table(config)
    extraction_tables = _get_extraction_tables(config, drv)
    hints = []
    seen_refs = set()  # 避免同一基础表被多次查询

    for t in config.tables:
        # 只处理提取表的外键（基础表自身的外键不需要给 AI）
        if t["name"] not in extraction_tables:
            continue
        for fk in t.get("foreign_keys", []):
            ref_table = fk.get("references", "")
            if not ref_table or ref_table == main_table:
                continue
            if ref_table in extraction_tables:
                continue  # 指向本次提取的主表，跳过
            if ref_table in seen_refs:
                continue  # 已处理过该基础表
            if not drv.table_exists(ref_table):
                continue
            seen_refs.add(ref_table)

            rows = drv.query(f"SELECT * FROM {ref_table}")
            if not rows:
                continue

            # 找基础表的业务编码字段（用于虚拟字段填充）
            ref_code = _find_code_field(config, ref_table)
            # 找被引用表的所有非 id 字段（用于展示，帮助 AI 理解）
            ref_cfg = next((rt for rt in config.tables if rt["name"] == ref_table), None)
            if ref_cfg:
                display_cols = [c["name"] for c in ref_cfg.get("columns", []) if c["name"] != "id"]
            else:
                display_cols = [c["name"] for c in drv.get_columns(ref_table) if c["name"] != "id"]

            code_desc = f"（请把{ref_code}值填到对应虚拟字段）" if ref_code else "（无业务编码字段，无法填充）"
            hint_lines = [f"\n【{ref_table}】{code_desc}"]
            hint_lines.append(" | ".join(display_cols))
            for row in rows[:100]:
                vals = [str(row.get(cn, "")) for cn in display_cols]
                hint_lines.append(" | ".join(vals))
            if len(rows) > 100:
                hint_lines.append(f"... 共 {len(rows)} 行")
            hints.append("\n".join(hint_lines))

    if hints:
        return ("【基础表业务编码参考】请根据文档内容，把对应业务编码值填到虚拟字段"
                "（如 region_code/category_code 等），pipeline 会自动转换为外键 id：\n"
                + "\n".join(hints))
    return ""


def _get_extraction_rules(config):
    """从配置读取提取规则（system_prompt + 零值过滤字段）"""
    prompts = config.custom_prompts or {}
    extraction_prompt = prompts.get("extraction_prompt",
        "你是数据提取助手。从文档中提取结构化数据，严格按表结构输出。")
    extraction_rules = prompts.get("extraction_rules", {})
    skip_if_zero = extraction_rules.get("skip_if_zero", [])
    return extraction_prompt, skip_if_zero


