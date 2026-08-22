"""未识别问法自学习——采集池、样例提议、确认存档

边界（用户架构红线）：
- 树结构（decision_tree.yaml 的行为×对象拓扑）永远不动
- 自学习只维护映射层：问法 → 行为键×对象键 的样例（prompts.yml decompose_examples）
- 需要树里不存在的新行为/新对象时，不虚构节点，如实上报由人来定结构
"""
from core.logger import get_logger
from pathlib import Path

import yaml

logger = get_logger(__name__)

_POOL_FILE = "unrecognized_queries.yml"

# 决策树的固定键集（提议时约束 AI 只能映射到既有节点，禁止虚构）
BEHAVIOR_KEYS = ["查", "增", "删", "改", "导入", "导出", "上传"]
OBJECT_KEYS = ["记录", "表", "字段", "数据库", "文件", "会话", "结构", "模板",
               "用户", "权限", "行业", "配置", "备份", "日志", "文档"]

# 采集触发：执行结果含这些路由失败特征（白盒，不凭感觉）
_FAIL_MARKERS = ("无可匹配", "无法确定", "不存在", "不支持", "无法理解", "找不到")


def _pool_path(industry: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    p = root / "industries" / industry / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p / _POOL_FILE


def load_pool(industry: str) -> list:
    p = _pool_path(industry)
    if not p.exists():
        return []
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or []
    except Exception:
        return []


def _write_pool(industry: str, items: list) -> None:
    _pool_path(industry).write_text(
        yaml.dump(items, allow_unicode=True), encoding="utf-8")


def should_collect(result_text: str) -> bool:
    """判定执行结果是否为'未识别/路由失败'（白盒特征词）"""
    if not result_text:
        return False
    return any(m in result_text for m in _FAIL_MARKERS)


def record(industry: str, query: str, outcome: str) -> int:
    """把一条未识别问法记入采集池（去重，返回当前池大小）

    隐私克制：明显的密码/身份证号形态不入池（打码）
    """
    import re
    text = (query or "").strip()
    if not text:
        return len(load_pool(industry))
    # 隐私打码：连续 6 位以上数字（疑似证件/密码/电话长号）
    text = re.sub(r"\d{6,}", "<数字>", text)
    items = load_pool(industry)
    for it in items:
        if it.get("query") == text:
            it["count"] = it.get("count", 1) + 1
            it["last_outcome"] = outcome[:120]
            _write_pool(industry, items)
            return len(items)
    import time
    items.append({"query": text, "count": 1,
                  "last_outcome": outcome[:120],
                  "first_seen": time.strftime("%Y-%m-%d %H:%M:%S")})
    _write_pool(industry, items)
    return len(items)


def clear_pool(industry: str) -> None:
    _write_pool(industry, [])


_PROPOSE_PROMPT = """你是路由样例设计器。下面是一批用户的"未识别问法"（决策树没有命中的问法）。
请为每一条给出路由映射：这个问法应该拆成什么子任务，走哪个行为键×对象键。

【硬约束——树结构固定，只许映射到既有键】
- 行为键只能从：{behaviors} 中选
- 对象键只能从：{objects} 中选
- 当前行业的真实表：{tables}（子任务 query 里引用表名只能用这些）
- 若某问法需要树里不存在的行为/对象，该条的 out_of_scope 置 true 并说明，不虚构

【未识别问法】
{queries}

只返回 JSON 数组（不要 markdown）：
[
  {{
    "query": "原问法",
    "is_complex": false,
    "sub_tasks": [{{"type": "db", "query": "子任务自然语言", "behavior_key": "查", "db_category_key": "记录"}}],
    "out_of_scope": false,
    "note": "设计说明（一句话）"
  }}
]"""


def propose_examples(industry: str, ai, max_items: int = 10) -> list:
    """AI 为池中问法提议路由样例（decompose_examples 格式）"""
    from config.settings import settings
    items = load_pool(industry)[:max_items]
    if not items:
        return []
    from core.schema_matcher import _load_schemas
    tables_str = "、".join(s.get("name", "") for s in _load_schemas()) or "（无表）"
    queries = "\n".join(f"{i + 1}. {it['query']}" for i, it in enumerate(items))
    prompt = _PROPOSE_PROMPT.format(
        behaviors="、".join(BEHAVIOR_KEYS), objects="、".join(OBJECT_KEYS),
        tables=tables_str, queries=queries)
    resp = ai.invoke(prompt)
    content = resp.content if hasattr(resp, "content") else str(resp)
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    import json
    data = json.loads(content)
    return data if isinstance(data, list) else []


def archive_examples(industry: str, entries: list) -> int:
    """确认的样例追加进 prompts.yml decompose_examples（只动映射层，不碰树结构）

    返回纳入条数。out_of_scope 的条目不纳入（如实返回由人来定结构）。
    """
    root = Path(__file__).resolve().parent.parent
    pp = root / "industries" / industry / "prompts" / "prompts.yml"
    pp.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if pp.exists():
        data = yaml.safe_load(pp.read_text(encoding="utf-8")) or {}
    examples = data.get("decompose_examples") or []
    added = 0
    for e in entries:
        if e.get("out_of_scope"):
            continue
        if not e.get("query") or not e.get("sub_tasks"):
            continue
        examples.append({
            "query": e["query"],
            "is_complex": bool(e.get("is_complex", False)),
            "sub_tasks": e["sub_tasks"],
        })
        added += 1
    data["decompose_examples"] = examples
    pp.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    logger.info("未识别样例纳入: 行业 %s 追加 %d 条 decompose_examples", industry, added)
    return added
