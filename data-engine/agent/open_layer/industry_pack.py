"""行业注册包——原行业向导的三要素并入建库流程（一次确认，行业+表+词典+路由样例齐活）

设计（替代已删除的 industry_wizard_nodes 独立向导）：
- 行业注册不再走独立的"一问一答"向导会话——用户说"创建 XX 行业的数据库"时，
  直接进建库流程（探索→设计→确认），行业配置与表结构同一次人工确认
- gen_industry_pack：基于用户意图 + 已确认 schema，一次 LLM 调用产出
  行业配置（config）+ 术语词典 + 决策树路由样例（prompts）
- write_industry_pack：落盘 industries/<name>/ 并过 industry_linter（配置即代码红线）
"""
import json
from core.logger import get_logger
import shutil
from pathlib import Path

import yaml

logger = get_logger(__name__)

_INDUSTRY_PACK_PROMPT = """你是数据库行业配置专家。用户要创建一个新行业的数据库，表结构已经设计完成。
请为该行业生成注册配置 JSON。

【用户意图】
{user_input}

【已确认的表结构】
{schema_text}

严格按以下 JSON 格式输出（不要 markdown 代码块标记）：
{{
  "config": {{
    "name": "行业英文名（snake_case，如 library_management）",
    "description": "行业描述（一句话）",
    "expert_role": "AI 专家角色描述",
    "hierarchy_desc": "数据层级描述",
    "default_table_name": "默认主表名（从已确认表中选）"
  }},
  "prompts": {{
    "classification_hints": "文件分类提示词（结合行业文档特征）",
    "schema_hints": "字段提取提示词",
    "decompose_examples": [
      {{"query": "基于真实表名的查询示例", "is_complex": false,
        "sub_tasks": [{{"type": "db", "query": "查询语句", "behavior_key": "查", "db_category_key": "记录"}}]}}
    ],
    "router_examples": [
      {{"input": "查询示例", "behavior_key": "查", "db_category_key": "记录"}}
    ],
    "terminology": {{
      "table_aliases": {{"表名": ["业务叫法"]}},
      "behavior_aliases": {{"查": ["行业表达"], "增": ["行业表达"], "删": ["行业表达"], "改": ["行业表达"]}},
      "object_aliases": {{"记录": ["行业叫法"], "表": ["行业叫法"], "字段": ["行业叫法"]}}
    }}
  }}
}}

硬性规则：
1. decompose_examples 至少 4 条（含简单/统计/跨表/复杂多步查询），router_examples 至少 3 条，
   全部使用已确认的真实表名/字段名，禁止虚构
2. 所有文字使用中文描述；只返回 JSON"""


def gen_industry_pack(llm, user_input: str, schema: dict) -> dict:
    """一次 LLM 调用产出行业注册包（config + prompts 词典/路由样例）

    失败抛异常由调用方降级（不阻断建表主流程）。
    """
    tables = schema.get("tables", [])
    schema_text = json.dumps(tables, ensure_ascii=False, indent=1)[:4000]
    prompt = _INDUSTRY_PACK_PROMPT.format(
        user_input=user_input or "（未提供）", schema_text=schema_text)
    resp = llm.invoke(prompt)
    content = resp.content if hasattr(resp, "content") else str(resp)
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    pack = json.loads(content)
    if not isinstance(pack.get("config"), dict) or not pack["config"].get("name"):
        raise ValueError("行业配置缺少 config.name")
    if not isinstance(pack.get("prompts"), dict):
        pack["prompts"] = {}
    return pack


def write_industry_pack(pack: dict, tables: list) -> tuple[str, list]:
    """落盘行业目录并过 lint。返回 (行业名, 校验问题清单)

    已存在同名行业目录时整体替换（用户已在确认卡片批准该配置）。
    """
    from core.industry_linter import lint_industry
    from core.industry_manager import INDUSTRIES_DIR, USER_CREATED_MARK

    cfg = pack.get("config", {})
    name = (cfg.get("name") or "").strip()
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"行业名 {name!r} 非法（只允许字母/数字/下划线）")

    industry_dir = INDUSTRIES_DIR / name
    if industry_dir.exists():
        shutil.rmtree(industry_dir)
    (industry_dir / "schemas").mkdir(parents=True, exist_ok=True)
    (industry_dir / "config").mkdir(exist_ok=True)
    (industry_dir / "prompts").mkdir(exist_ok=True)

    (industry_dir / "config" / "config.yml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (industry_dir / "prompts" / "prompts.yml").write_text(
        yaml.dump(pack.get("prompts", {}), allow_unicode=True), encoding="utf-8")
    for t in tables:
        tname = t.get("name", "")
        if tname:
            (industry_dir / "schemas" / f"{tname}.yaml").write_text(
                yaml.dump(t, allow_unicode=True), encoding="utf-8")
    (industry_dir / "__init__.py").write_text("", encoding="utf-8")
    (industry_dir / USER_CREATED_MARK).write_text(
        "created by build_db industry pack", encoding="utf-8")

    errors = lint_industry(industry_dir)
    return name, errors
