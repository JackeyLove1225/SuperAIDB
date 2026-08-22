"""行业配置校验器（U-1/P2-7）——配置即代码，加载期强校验

规则（每条违规报出具体位置，便于修复）：
  R1 FK 必须引用 id 列（ref_columns 缺省视为 ["id"]；显式声明必须含 id）
  R2 每表/每列必须有 business_name 或 description（缺了 AI 路由/提取质量崩）
  R3 表名复数 snake_case；列名 snake_case；FK 列应为 {引用表名单数}_id
  R4 db_mapping.yml 的 table_mapping/field_mapping 目标表/字段必须存在
  R5 few-shot 示例必填字段齐全（decompose: query+sub_tasks；router: input+行为/对象键）
  R6 YAML 必须可解析且为非空 dict（坏 YAML 不静默跳过）

用法：python -m core.industry_linter [industries_dir]
返回 0=全部通过；1=有违规（逐项打印）。
"""
import re
import sys
from pathlib import Path

import yaml

_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
# R3 复数约定的例外：不可数集合名词本身即集合概念（labor/equipment 这类表名合规）
_UNCOUNTABLE_NOUNS = {"labor", "equipment", "information", "machinery", "furniture", "inventory"}


def _singular(table: str) -> str:
    """复数表名 → 单数（books→book, classes→class, order_items→order_item）"""
    if table.endswith("ies"):
        return table[:-3] + "y"
    if table.endswith("sses"):
        return table[:-2]
    if table.endswith("s") and not table.endswith("ss"):
        return table[:-1]
    return table


def _lint_schemas(industry_dir: Path, errors: list):
    schemas_dir = industry_dir / "schemas"
    if not schemas_dir.is_dir():
        return
    table_names = set()
    for f in sorted(schemas_dir.glob("*.yaml")) + sorted(schemas_dir.glob("*.yml")):
        loc = f"{industry_dir.name}/schemas/{f.name}"
        # R6 YAML 可解析且非空
        try:
            schema = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"[R6] {loc}: YAML 解析失败: {e}")
            continue
        if not isinstance(schema, dict) or not schema:
            errors.append(f"[R6] {loc}: 内容为空或不是映射")
            continue
        tname = schema.get("name", f.stem)
        table_names.add(tname)

        # R3 表名规范
        if not _SNAKE.match(tname):
            errors.append(f"[R3] {loc}: 表名 {tname!r} 非 snake_case")
        elif not tname.endswith("s") and tname.rsplit("_", 1)[-1] not in _UNCOUNTABLE_NOUNS:
            errors.append(f"[R3] {loc}: 表名 {tname!r} 非复数（约定复数 snake_case）")

        # R2 表级 business_name/description
        if not (schema.get("business_name") or schema.get("description")):
            errors.append(f"[R2] {loc}: 表 {tname!r} 缺 business_name/description")

        col_names = set()
        for col in schema.get("columns", []) or []:
            cn = col.get("name", "")
            col_names.add(cn)
            if not _SNAKE.match(cn):
                errors.append(f"[R3] {loc}: 字段 {cn!r} 非 snake_case")
            if not (col.get("business_name") or col.get("description")):
                errors.append(f"[R2] {loc}: 字段 {tname}.{cn} 缺 business_name/description")

        # R1/R3 FK 校验
        for fk in schema.get("foreign_keys", []) or []:
            ref_table = fk.get("references", "")
            ref_cols = fk.get("ref_columns") or ["id"]
            if "id" not in ref_cols:
                errors.append(f"[R1] {loc}: 外键 {fk.get('columns')} → {ref_table}{ref_cols}，"
                              f"引用列必须含 id")
            for col in fk.get("columns", []) or []:
                if col not in col_names:
                    errors.append(f"[R4] {loc}: 外键列 {col!r} 不在表 {tname} 的字段中")
                elif ref_table and col != f"{_singular(ref_table)}_id":
                    errors.append(f"[R3] {loc}: 外键列 {col!r} 不符合 "
                                  f"{{引用表名单数}}_id 约定（应为 {_singular(ref_table)}_id）")
        schema["_col_names"] = col_names  # 供 R4 跨表校验

    # R4 跨表：FK 引用表必须存在
    for f in sorted(schemas_dir.glob("*.yaml")) + sorted(schemas_dir.glob("*.yml")):
        loc = f"{industry_dir.name}/schemas/{f.name}"
        try:
            schema = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(schema, dict):
            continue
        for fk in schema.get("foreign_keys", []) or []:
            ref_table = fk.get("references", "")
            if ref_table and ref_table not in table_names:
                errors.append(f"[R4] {loc}: 外键引用的表 {ref_table!r} 不存在于本行业 schemas")


def _lint_mapping(industry_dir: Path, errors: list):
    map_path = industry_dir / "config" / "db_mapping.yml"
    if not map_path.exists():
        return
    loc = f"{industry_dir.name}/config/db_mapping.yml"
    try:
        mapping = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        errors.append(f"[R6] {loc}: YAML 解析失败: {e}")
        return
    schemas_dir = industry_dir / "schemas"
    table_names = {f.stem for f in schemas_dir.glob("*.yaml")} | {f.stem for f in schemas_dir.glob("*.yml")}
    for biz, tname in (mapping.get("table_mapping") or {}).items():
        if tname not in table_names:
            errors.append(f"[R4] {loc}: table_mapping[{biz!r}] → {tname!r} 表不存在")
    for biz, target in (mapping.get("field_mapping") or {}).items():
        # 支持 "table.field" 或 {"table": ..., "field": ...}
        t = target.split(".")[0] if isinstance(target, str) else (target or {}).get("table", "")
        if t and t not in table_names:
            errors.append(f"[R4] {loc}: field_mapping[{biz!r}] → {target!r} 表不存在")


def _lint_prompts(industry_dir: Path, errors: list):
    prompts_path = industry_dir / "prompts" / "prompts.yml"
    if not prompts_path.exists():
        return
    loc = f"{industry_dir.name}/prompts/prompts.yml"
    try:
        prompts = yaml.safe_load(prompts_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        errors.append(f"[R6] {loc}: YAML 解析失败: {e}")
        return
    # R4 terminology.table_aliases 的键必须是真实表
    schemas_dir = industry_dir / "schemas"
    table_names = {f.stem for f in schemas_dir.glob("*.yaml")} | {f.stem for f in schemas_dir.glob("*.yml")}
    for tname in ((prompts.get("terminology") or {}).get("table_aliases") or {}):
        if tname not in table_names:
            errors.append(f"[R4] {loc}: terminology.table_aliases 的 {tname!r} 表不存在")
    # R5 decompose 示例必填 query + sub_tasks
    for i, ex in enumerate(prompts.get("decompose_examples") or []):
        if not isinstance(ex, dict) or not ex.get("query") or "sub_tasks" not in ex:
            errors.append(f"[R5] {loc}: decompose_examples[{i}] 缺 query/sub_tasks 必填字段")
    # R5 router 示例必填 input + behavior_key + db_category_key
    for i, ex in enumerate(prompts.get("router_examples") or []):
        if not isinstance(ex, dict) or not ex.get("input") \
                or not ex.get("behavior_key") or not ex.get("db_category_key"):
            errors.append(f"[R5] {loc}: router_examples[{i}] 缺 input/behavior_key/db_category_key 必填字段")


def _load_waivers(industry_dir: Path) -> list:
    """读取行业目录下的 .lint_waivers（白盒豁免：规则+匹配子串+原因，缺一不豁免）

    格式：waivers: [{rule: R3, match: "sheep", reason: "sheep 单复数同形"}]
    """
    wp = industry_dir / ".lint_waivers"
    if not wp.exists():
        return []
    try:
        data = yaml.safe_load(wp.read_text(encoding="utf-8")) or {}
        return [w for w in data.get("waivers", []) if w.get("rule") and w.get("match") and w.get("reason")]
    except Exception:
        return []


def lint_industry(industry_dir: Path) -> list:
    """校验单个行业目录，返回违规清单（空=通过）"""
    errors: list = []
    _lint_schemas(industry_dir, errors)
    _lint_mapping(industry_dir, errors)
    _lint_prompts(industry_dir, errors)
    waivers = _load_waivers(industry_dir)
    if waivers:
        errors = [e for e in errors if not any(
            e.startswith(f"[{w['rule']}]") and w["match"] in e for w in waivers)]
    return errors


def lint_all(industries_dir: Path) -> dict:
    """校验目录下所有行业（含 _test 测试行业），返回 {行业名: [违规]}"""
    result = {}
    for d in sorted(industries_dir.iterdir()):
        if not d.is_dir() or d.name in ("__pycache__",):
            continue
        if not ((d / "schemas").is_dir() or (d / "config").is_dir() or (d / "prompts").is_dir()):
            continue
        errs = lint_industry(d)
        if errs:
            result[d.name] = errs
    return result


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "industries"
    report = lint_all(root)
    if not report:
        print("行业配置校验: 全部通过")
        sys.exit(0)
    for name, errs in report.items():
        print(f"行业 {name}: {len(errs)} 项违规")
        for e in errs:
            print(f"  {e}")
    sys.exit(1)
