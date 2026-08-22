"""3.1 规划层确定性短路：关键词 + schema 命中 + 决策树直达 → 跳过 LLM 拆解

设计红线（与 3.0 模拟器 scripts/stats_shortcircuit.py 变体 B 对齐）：
- 只产意图标签（behavior_key/db_category_key/constraint），不产参数——
  记录级子任务由 route_by_task_type 分流 agent_run（循环 LLM 自提取参数）；
  非记录级走 execute → _execute_single 标签透传 → 同一棵树路由 →
  FC AI 提取参数（extract_param，flash）。参数理解仍归 AI，短路只替代拆解。
- dk 必须非空（_execute_single 标签透传要求 behavior+object 双全，
  缺一会落进 P1 AI 解析——与短路语义不符，宁可 fail-open）。
- fail-open：任何一步不确定（行为多族/表歧义/表缺失/树不支持）→ 返回 None，
  调用方回退完整 LLM 拆解，行为与 3.1 之前完全一致。
- 词表单一事实源：config/shortcircuit.yml（模拟器同读，禁双写）。
- 总开关：settings.SHORTCIRCUIT_ENABLED（env，默认开；关闭即整体回退 LLM 拆解）。

安全边界：短路不改变任何执行护栏——核武人审闸（execute_tool 层）、
mutate_data 候选集分流（agent_run 内）、P0 生成后校验全部原样在位，
因为短路产出物与 LLM 拆解产出物同构（同字段、同分流、同树路由）。
"""
from core.logger import get_logger
from pathlib import Path

import yaml

from agent.router import (
    text_behavior_override, text_db_override, get_tree, _BEHAVIOR_KEYWORDS)
from core.schema_matcher import (
    _load_schemas, _load_biz_mapping,
    _match_tables_by_mapping, _match_tables_by_schema, _resolve_table_level)

logger = get_logger(__name__)

_RULES_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "shortcircuit.yml"
_rules_cache: dict | None = None


def _rules() -> dict:
    """加载短路词表（带缓存；配置损坏时降级为空规则=整体不短路，fail-open）"""
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache
    try:
        data = yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8")) or {}
        _rules_cache = {
            "object": [(k, tuple(v)) for k, v in (data.get("object_rules") or {}).items()],
            "constraint": [(k, tuple(v)) for k, v in (data.get("constraint_rules") or {}).items()],
            "head_verb": [(k, tuple(v)) for k, v in (data.get("head_verb_rules") or {}).items()],
        }
    except Exception as e:
        logger.warning("短路词表加载失败（%s），本进程不再短路", e)
        _rules_cache = {"object": [], "constraint": [], "head_verb": []}
    return _rules_cache


def _reset_rules_cache() -> None:
    """测试钩子：清空词表缓存（改 yml 后重载）"""
    global _rules_cache
    _rules_cache = None


def _behavior(q: str) -> tuple[str, str]:
    """行为判定：生产词表优先；句首单字动词补充（与生产多字词合并查重，防真复合误判）"""
    bk = text_behavior_override(q)
    if bk:
        return bk, "text_override"
    qs = q.strip()
    hits = {bk0 for bk0, words in _rules()["head_verb"] for w in words if qs.startswith(w)}
    if len(hits) == 1:
        # 句首单字 + 句中多字不同族 → 真复合指令，不短路
        prod_hits = {bk0 for bk0, words in _BEHAVIOR_KEYWORDS
                     if any(w in qs for w in words)}
        if len(hits | prod_hits) == 1:
            return hits.pop(), "rule:head_verb"
    return "", ""


def _infer_object(q: str, table: str) -> tuple[str, str]:
    """对象判定：生产铁证 → 候选词表（顺序即优先级）→ 有表默认记录"""
    dk = text_db_override(q)
    if dk:
        return dk, "text_override"
    for obj, words in _rules()["object"]:
        if any(w in q for w in words):
            return obj, f"rule:{words[0]}"
    if table:
        return "记录", "table_default"
    return "", ""


def try_shortcircuit(user_input: str) -> dict | None:
    """命中返回与 LLM 拆解同构的 plan dict；未命中返回 None（fail-open）。

    返回结构（consumers：route_by_task_type / execute_sub_task / _execute_single）：
      sub_tasks: [db 单子任务（标签齐全、structured_args 为空——参数归下游）]
      is_complex: False / task_type: "basic"（记录级由分流函数自动转 agent_run）
    """
    from config.settings import settings
    if not settings.SHORTCIRCUIT_ENABLED:
        return None
    q = (user_input or "").strip()
    if not q:
        return None

    bk, bk_src = _behavior(q)
    if not bk:
        return None

    tables = _load_schemas()
    table_map = (_load_biz_mapping() or {}).get("table_mapping", {})
    obj_rules = _rules()["object"]

    # 关联查询：文本铁证或关联词命中时，需确证 ≥2 张已知表（不足则 LLM 拆解更稳）
    join_words = next((words for obj, words in obj_rules if obj == "关联"), ())
    if text_db_override(q) == "关联" or any(w in q for w in join_words):
        cand = _match_tables_by_mapping(q, table_map) + _match_tables_by_schema(q, tables)
        found = {t for _p, t, _m in cand}
        if len(found) < 2:
            return None
        tool = get_tree().route(bk, "关联", "")
        if tool == "unsupported_op":
            return None
        logger.info("确定性短路：%s×关联 → %s（表 %s，行为源 %s）", bk, tool, sorted(found), bk_src)
        return {
            "sub_tasks": [{"type": "db", "query": q, "behavior_key": bk,
                           "db_category_key": "关联", "constraint": "",
                           "structured_args": {}}],
            "is_complex": False, "task_type": "basic",
        }

    table, err = _resolve_table_level(q, tables, table_map)
    if err:  # 表歧义（多表同优先级）→ 不猜，交 LLM
        return None
    dk, dk_src = _infer_object(q, table)
    # 行为条件对象（20260806）："删除表格X"（X=已知表）= 删表不是删记录。
    # "表格"一词两义（查/改语境=记录容器），词表不收（见 yml 头部注释）；
    # 删语境+具体表名锚定=表结构本身。无此规则 dk=记录(table_default)，
    # 短路会把删表意图送进记录级循环——与 LLM 误拆同错（真事故复现路径）。
    if dk == "记录" and bk == "删" and "表格" in q and table:
        dk, dk_src = "表", "行为条件:删×表格"
    if bk in ("查", "增", "删", "改") and dk in ("", "记录") and not table:
        return None  # 记录级操作但表落空（含多轮指代）→ 交 LLM/历史上下文
    if not dk:  # 标签透传要求对象键非空，缺则 P1 语义与短路不符
        return None
    # 选择集语境漂移（router prompt 关键原则1）：只有"查"时选择集是对象（list_selections）；
    # 改/删/增×选择集 里选择集是筛选条件（语义是记录级 {bk,记录}+selection_id），
    # 短路词表无法区分条件语境，误打 {bk,选择集} 会错路由（改×选择集→alter_precision 真事故）
    if dk == "选择集" and bk != "查":
        return None
    ct = ""
    if bk == "增" and dk == "记录":
        ct = next((c for c, words in _rules()["constraint"] if any(w in q for w in words)), "")
    tool = get_tree().route(bk, dk, ct)
    if tool == "unsupported_op":
        return None

    logger.info("确定性短路：%s×%s%s → %s（表 %s，来源 行为:%s 对象:%s）",
                bk, dk, f"×{ct}" if ct else "", tool, table or "-", bk_src, dk_src)
    return {
        "sub_tasks": [{"type": "db", "query": q, "behavior_key": bk,
                       "db_category_key": dk, "constraint": ct,
                       "structured_args": {}}],
        "is_complex": False, "task_type": "basic",
    }
