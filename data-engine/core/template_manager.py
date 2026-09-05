"""模板管理模块——职责单一，每个函数只做一件事

负责 industries/templates/ 目录下的模板文件管理。
各行业独立，通过 settings.INDUSTRY 动态确定目标 schema.yaml。
"""

import os, yaml
from pathlib import Path

from core.tool_result import ToolResult


def _atomic_write(path: Path, content: str):
    """原子写入：先写临时文件，再 os.replace 原子替换"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


# ── 内部路径 ──

def _templates_dir() -> Path:
    """模板文件目录（按行业/表分目录，与 industries/{name}/schemas/ 结构一致）"""
    from config.settings import settings
    return Path(__file__).parent.parent / "industries" / "templates" / settings.INDUSTRY / "schemas"


def _schemas_dir() -> Path:
    """当前行业的 schemas 目录"""
    from config.settings import settings
    return Path(__file__).parent.parent / "industries" / settings.INDUSTRY / "schemas"


# ── 子功能 1/3：列出可用模板 ──

def list_templates() -> list[dict]:
    """列出 templates/ 下所有可用模板（每表一个文件）"""
    tmpl_dir = _templates_dir()
    if not tmpl_dir.exists():
        return []

    results = []
    for p in sorted(tmpl_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            results.append({
                "name": data.get("name", p.stem),
                "file": p.stem,
                "business_name": data.get("business_name", ""),
                "columns": len(data.get("columns", [])),
            })
        except Exception:
            continue
    return results


# ── 子功能 2/3：导入模板到当前行业的 schema.yaml ──

def import_template(table_name: str, target_table: str = "") -> "ToolResult":
    """将模板中的单张表导入当前行业的 schemas/ 目录（双轨：data 带模板元数据）"""
    tmpl_dir = _templates_dir()
    if not tmpl_dir.exists():
        return ToolResult.fail("模板目录不存在", code="NOT_FOUND",
                               reason="template_dir_missing")

    templates = list_templates()
    if not templates:
        return ToolResult.fail("模板目录为空或没有有效模板", code="NOT_FOUND",
                               reason="template_dir_empty")

    if not table_name:
        lines = ["可用单表模板："]
        for t in sorted(templates, key=lambda x: x["name"]):
            lines.append(f"  {t['name']}（{t['business_name']}，{t['columns']} 字段）")
        return ToolResult.ok("\n".join(lines), templates=templates,
                             count=len(templates))

    matched = None
    clean = table_name.replace("模板", "").replace(" ", "").lower()
    for t in templates:
        if t["name"].lower() == table_name.lower() or t["file"].lower() == table_name.lower():
            matched = t; break
    if not matched:
        for t in templates:
            if clean in t["name"].lower() or clean in t["file"].lower() or t["name"].lower().startswith(clean):
                matched = t; break
    if not matched:
        names = ", ".join(t["name"] for t in templates)
        return ToolResult.fail(f"未找到表「{table_name}」，可用：{names}",
                               code="NOT_FOUND", reason="template_not_found",
                               available=[t["name"] for t in templates])

    tmpl_path = tmpl_dir / f"{matched['file']}.yaml"
    table_data = yaml.safe_load(tmpl_path.read_text(encoding="utf-8"))

    schemas_dir = _schemas_dir()
    schemas_dir.mkdir(parents=True, exist_ok=True)
    dst_name = target_table or matched['name']
    dst_path = schemas_dir / f"{dst_name}.yaml"
    _atomic_write(dst_path,
        yaml.dump(table_data, allow_unicode=True, default_flow_style=False, sort_keys=False))
    return ToolResult.ok(
        f"已导入表「{dst_name}」（{matched['business_name']}，{matched['columns']} 字段）",
        table=dst_name, template=matched["file"], columns=matched["columns"])


def save_template(table_name: str) -> "ToolResult":
    """将当前行业 schemas/ 中的单张表另存为模板（双轨）"""
    if not table_name or not table_name.strip():
        return ToolResult.fail("表名不能为空", code="VALIDATION",
                               reason="missing_params")

    name = table_name.strip().replace(" ", "_")
    src = _schemas_dir() / f"{name}.yaml"
    if not src.exists():
        return ToolResult.fail(f"当前行业没有表「{name}」", code="NOT_FOUND",
                               reason="table_not_found", table=name)

    dst = _templates_dir() / f"{name}.yaml"
    dst.write_bytes(src.read_bytes())
    return ToolResult.ok(f"已保存表「{name}」为模板", table=name, template=name)


def delete_template(name: str) -> "ToolResult":
    """删除模板目录中的单表模板文件（双轨）"""
    tmpl_dir = _templates_dir()
    if not tmpl_dir.exists():
        return ToolResult.fail("模板目录不存在", code="NOT_FOUND",
                               reason="template_dir_missing")
    target = tmpl_dir / f"{name}.yaml"
    if not target.exists():
        return ToolResult.fail(f"模板不存在: {name}", code="NOT_FOUND",
                               reason="template_not_found", template=name)
    target.unlink()
    return ToolResult.ok(f"已删除模板: {name}", template=name)
