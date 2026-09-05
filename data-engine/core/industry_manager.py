"""行业配置管理——导入/导出/创建/列表

行业目录结构：
    industries/{industry_name}/
        config/config.yml
        fields/fields.yml
        prompts/prompts.yml
        schemas/*.yaml (可选)
        __init__.py (可选)

导入导出格式：ZIP 压缩包，包含上述目录结构
"""

import os
import shutil
import zipfile
import yaml
from pathlib import Path


# 行业根目录
INDUSTRIES_DIR = Path(__file__).resolve().parent.parent / "industries"

# 新建行业的默认模板（单点定义）
DEFAULT_TEMPLATE = "construction_engineering"

# 内置行业判定：无 .user_created 标记文件即内置（不允许删除）。
# 行业清单由目录发现（discover_industries），无需硬编码行业名清单。
USER_CREATED_MARK = ".user_created"


def is_builtin_industry(name: str) -> bool:
    """内置行业（不允许删除）：无用户创建标记的行业目录"""
    return not (INDUSTRIES_DIR / name / USER_CREATED_MARK).exists()

# 必需的配置文件
REQUIRED_FILES = ["config/config.yml", "fields/fields.yml", "prompts/prompts.yml"]


def list_industries() -> list[dict]:
    """列出所有可用行业

    Returns:
        [{"name": str, "display_name": str, "has_schemas": bool, "is_builtin": bool, "description": str}]
    """
    industries = []
    if not INDUSTRIES_DIR.exists():
        return industries

    for item in sorted(INDUSTRIES_DIR.iterdir()):
        if not item.is_dir() or item.name.startswith("_") or item.name.startswith("."):
            continue
        if item.name in ("__pycache__", "templates"):
            continue

        # 读取配置获取显示名和描述
        display_name = item.name
        description = ""
        config_path = item / "config" / "config.yml"
        if config_path.exists():
            try:
                config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                display_name = config.get("display_name", config.get("name", item.name))
                description = config.get("description", "")
            except Exception:
                pass  # 展示名/描述读不出则用目录名兜底

        # 检查是否有 schemas 目录
        schemas_dir = item / "schemas"
        has_schemas = schemas_dir.exists() and any(schemas_dir.glob("*.yaml"))

        industries.append({
            "name": item.name,
            "display_name": display_name,
            "description": description,
            "has_schemas": has_schemas,
            "is_builtin": is_builtin_industry(item.name),
            "schema_count": len(list(schemas_dir.glob("*.yaml"))) if has_schemas else 0,
        })

    return industries


def export_industry(industry_name: str) -> dict:
    """导出行业配置为 ZIP 文件

    Returns:
        {"ok": bool, "path": str, "message": str}
    """
    industry_dir = INDUSTRIES_DIR / industry_name
    if not industry_dir.exists() or not industry_dir.is_dir():
        return {"ok": False, "message": f"行业 '{industry_name}' 不存在"}

    # 导出目录
    export_dir = INDUSTRIES_DIR.parent / "exports"
    export_dir.mkdir(exist_ok=True)

    zip_path = export_dir / f"{industry_name}_config.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(industry_dir):
            # 跳过 __pycache__
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for file in files:
                if file.endswith(".pyc"):
                    continue
                file_path = Path(root) / file
                arcname = file_path.relative_to(industry_dir)
                zf.write(file_path, arcname)

    # 获取文件大小
    size_kb = round(zip_path.stat().st_size / 1024, 1)

    return {
        "ok": True,
        "path": str(zip_path),
        "filename": zip_path.name,
        "size_kb": size_kb,
        "message": f"行业 '{industry_name}' 配置已导出（{size_kb}KB）",
    }


def import_industry(zip_path: str, overwrite: bool = False) -> dict:
    """从 ZIP 文件导入行业配置

    Args:
        zip_path: ZIP 文件路径
        overwrite: 是否覆盖已存在的行业

    Returns:
        {"ok": bool, "industry_name": str, "message": str}
    """
    if not os.path.exists(zip_path):
        return {"ok": False, "message": "ZIP 文件不存在"}

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # 读取 ZIP 中的文件列表
            file_list = zf.namelist()

            # 验证必需文件
            found_files = set(file_list)
            missing = [f for f in REQUIRED_FILES if f not in found_files]
            if missing:
                return {"ok": False, "message": f"ZIP 缺少必需文件: {missing}"}

            # 从 config.yml 提取行业名
            config_content = zf.read("config/config.yml")
            config = yaml.safe_load(config_content) or {}
            industry_name = config.get("name", Path(zip_path).stem.replace("_config", ""))

            # 安全检查：行业名只允许字母、数字、下划线
            if not industry_name.replace("_", "").isalnum():
                return {"ok": False, "message": f"无效的行业名: {industry_name}"}

            # 检查是否已存在
            target_dir = INDUSTRIES_DIR / industry_name
            if target_dir.exists() and not overwrite:
                return {"ok": False, "message": f"行业 '{industry_name}' 已存在，需指定 overwrite=true 覆盖"}

            # 创建目录并解压
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True)

            zf.extractall(target_dir)

            # 清理 __pycache__
            for pycache in target_dir.rglob("__pycache__"):
                shutil.rmtree(pycache, ignore_errors=True)

            return {
                "ok": True,
                "industry_name": industry_name,
                "message": f"行业 '{industry_name}' 导入成功（{len(file_list)} 个文件）",
            }
    except zipfile.BadZipFile:
        return {"ok": False, "message": "无效的 ZIP 文件"}
    except Exception as e:
        return {"ok": False, "message": f"导入失败: {e}"}


def create_industry_from_template(
    industry_name: str,
    display_name: str = "",
    description: str = "",
    template: str = DEFAULT_TEMPLATE,
) -> dict:
    """从模板创建新行业

    Args:
        industry_name: 新行业名（字母/数字/下划线）
        display_name: 显示名
        description: 描述
        template: 模板行业名（默认 construction_engineering）

    Returns:
        {"ok": bool, "message": str}
    """
    # 安全检查
    if not industry_name.replace("_", "").isalnum():
        return {"ok": False, "message": "行业名只能包含字母、数字、下划线"}

    template_dir = INDUSTRIES_DIR / template
    if not template_dir.exists():
        return {"ok": False, "message": f"模板行业 '{template}' 不存在"}

    target_dir = INDUSTRIES_DIR / industry_name
    if target_dir.exists():
        return {"ok": False, "message": f"行业 '{industry_name}' 已存在"}

    try:
        # 复制模板目录
        shutil.copytree(template_dir, target_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        # 更新 config.yml
        config_path = target_dir / "config" / "config.yml"
        if config_path.exists():
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            config["name"] = industry_name
            if display_name:
                config["display_name"] = display_name
            if description:
                config["description"] = description
            config_path.write_text(
                yaml.dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

        # 写入用户创建标记（删除保护判定依据）
        (target_dir / USER_CREATED_MARK).write_text(
            f"created from template: {template}", encoding="utf-8")

        return {
            "ok": True,
            "message": f"行业 '{industry_name}' 已从模板 '{template}' 创建，请编辑配置文件完善表结构定义",
        }
    except Exception as e:
        # 清理半成品
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        return {"ok": False, "message": f"创建失败: {e}"}


def delete_industry(industry_name: str) -> dict:
    """删除行业（不允许删除内置行业）"""
    if is_builtin_industry(industry_name):
        return {"ok": False, "message": f"不允许删除内置行业 '{industry_name}'"}

    target_dir = INDUSTRIES_DIR / industry_name
    if not target_dir.exists():
        return {"ok": False, "message": f"行业 '{industry_name}' 不存在"}

    try:
        shutil.rmtree(target_dir)
        return {"ok": True, "message": f"行业 '{industry_name}' 已删除"}
    except Exception as e:
        return {"ok": False, "message": f"删除失败: {e}"}


def list_exports() -> list[dict]:
    """列出所有已导出的行业配置 ZIP 文件"""
    export_dir = INDUSTRIES_DIR.parent / "exports"
    if not export_dir.exists():
        return []

    exports = []
    for f in sorted(export_dir.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
        exports.append({
            "filename": f.name,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "created_at": f.stat().st_mtime,
        })
    return exports
