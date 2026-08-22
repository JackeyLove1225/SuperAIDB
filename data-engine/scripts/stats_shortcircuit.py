"""3.0 前置统计①：短路命中率模拟器（路线图红线：<50% 放弃 3.1，>70% 才做）

仿真 3.1 的设想形态——简单问句"关键词 + schema 命中 + 决策树直达工具"，
跳过 LLM 拆解。对每条真实问句判定能否确定性地路由到具体工具。

两个变体（决策依据 = B，A 为下界）：
  A 生产规则：text_behavior_override / text_db_override / _resolve_table_level
    / 树 route + DB 自建表兜底仿真（level-3 是生产既有行为：7 月语料的
    t100/test1 等表已随库重建消失，只能按"当时执行成功"追溯其存在）
  B 生产 + 3.1 候选规则（模拟器先行量化，命中时标注贡献来源）：
    - 对象关键词 _OBJECT_RULES（统计/数据库/字段/索引/外键/模板/文件/选择集/记录…）
    - 约束关键词 _CONSTRAINT_RULES（批量 → batch_insert_data）
    - 行为扩展 _BEHAVIOR_EXT（句首单字动词 查/加/删/改；7 月语料高频用法，
      生产词表只收多字词——"加快/加重"类误命中是生产侧不收单字的原因，
      句首锚定规避）
    - DB 自建表兜底仿真说明见变体 A。引号内值 token（'TEST-902'）与
      schema 字段名不算表候选；介词锚定（向X/在X中/X表）优先于全局唯一。

语料（真实问句，两个来源分开统计——未识别池全是失败样本，天然有偏）：
  - db/json/conversation_*.json  历史会话（含成功与失败，结果用于兜底仿真）
  - industries/*/config/unrecognized_queries.yml  未识别问法池

用法：python scripts/stats_shortcircuit.py [--out docs/stats]
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("INDUSTRY", "engineering")  # 语料全部产出于工程行业

from agent.router import (
    text_behavior_override, text_db_override, get_tree, _BEHAVIOR_KEYWORDS)
from core.schema_matcher import (
    _load_schemas, _load_biz_mapping,
    _match_tables_by_mapping, _match_tables_by_schema, _resolve_table_level,
)

# ── 3.1 候选规则（单一事实源 config/shortcircuit.yml，与产品 shortcircuit.py 同读）──
# 顺序即优先级：越具体越靠前。"列"不收（与"列出"冲突）；"表格"不收
# （"表格X的数据"是记录级，歧义大）；句首单字动词见 head_verb_rules。
import yaml as _yaml


def _load_candidate_rules() -> dict:
    data = _yaml.safe_load(
        (ROOT / "config" / "shortcircuit.yml").read_text(encoding="utf-8")) or {}
    return {
        "object": [(k, tuple(v)) for k, v in (data.get("object_rules") or {}).items()],
        "constraint": [(k, tuple(v)) for k, v in (data.get("constraint_rules") or {}).items()],
        "head_verb": [(k, tuple(v)) for k, v in (data.get("head_verb_rules") or {}).items()],
    }


_RULES = _load_candidate_rules()
_OBJECT_RULES = _RULES["object"]
_CONSTRAINT_RULES = _RULES["constraint"]
_BEHAVIOR_EXT = _RULES["head_verb"]

_IDENT = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
# 介词/名词锚定：表名常紧贴这些字（向t100插入 / 在t1中 / test1表 / 查询t1）
_ANCHOR_PRE = re.compile(r"(?:向|往|在|从|把|将|对|查询|查看)\s*([a-zA-Z_][a-zA-Z0-9_]*)")
_ANCHOR_POST = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(?=表|表格|中|里|内)")
_SKIP_TOKENS = {  # 永远不算表候选的标识符
    "select", "where", "from", "insert", "update", "delete", "and", "or",
    "text", "int", "integer", "float", "varchar", "id",
}


def _behavior(q: str, extended: bool) -> tuple[str, str]:
    """行为判定：生产规则优先；extended 时补句首单字动词。返回 (bk, 来源)"""
    bk = text_behavior_override(q)
    if bk or not extended:
        return bk, "text_override" if bk else ""
    qs = q.strip()
    hits = {bk0 for bk0, words in _BEHAVIOR_EXT for w in words if qs.startswith(w)}
    if len(hits) == 1:
        # 与生产词表合并查重：句首单字 + 句中多字不同族 → 真复合，不命中
        prod_hits = {bk0 for bk0, words in _BEHAVIOR_KEYWORDS
                     if any(w in qs for w in words)}
        all_hits = hits | prod_hits
        if len(all_hits) == 1:
            return hits.pop(), "rule:head_verb"
    return "", ""


def _infer_object(q: str, table: str, extended: bool) -> tuple[str, str]:
    dk = text_db_override(q)
    if dk:
        return dk, "text_override"
    if extended:
        for obj, words in _OBJECT_RULES:
            if any(w in q for w in words):
                return obj, f"rule:{words[0]}"
    if table:
        return "记录", "table_default"
    return "", "none"


def _table_token_fallback(q: str, outcome: str, tables: list) -> str:
    """DB 自建表兜底仿真：当时执行成功（结果无失败字样）的前提下——
    介词锚定的唯一标识符优先；无锚定时全局恰一个未知标识符才算命中"""
    if not outcome or "操作失败" in outcome or "不存在" in outcome:
        return ""
    quoted = set()
    for m in re.finditer(r"['\"]([^'\"]+)['\"]", q):
        quoted.update(_IDENT.findall(m.group(1)))
    fields = {c["name"].lower() for t in tables for c in t.get("columns", [])}

    def _ok(tok: str) -> bool:
        return (tok.lower() not in _SKIP_TOKENS and tok.lower() not in fields
                and tok not in quoted and len(tok) > 1)

    anchored = {t for t in _ANCHOR_PRE.findall(q) + _ANCHOR_POST.findall(q) if _ok(t)}
    if len(anchored) == 1:
        return anchored.pop()
    if len(anchored) > 1:
        return ""  # 锚定多表（如外键句 Test1/T100）→ 不猜
    cand = {t for t in _IDENT.findall(q) if _ok(t)}
    return cand.pop() if len(cand) == 1 else ""


def simulate(q: str, tables: list, table_map: dict, extended: bool,
             outcome: str = "") -> dict:
    bk, bk_src = _behavior(q, extended)
    if not bk:
        return {"hit": False, "reason": "no_unique_behavior"}
    join_words = next((w for obj, w in _OBJECT_RULES if obj == "关联"), ())
    if text_db_override(q) == "关联" or (extended and any(w in q for w in join_words)):
        cand = _match_tables_by_mapping(q, table_map) + _match_tables_by_schema(q, tables)
        found = {t for _p, t, _m in cand}
        if len(found) < 2:
            return {"hit": False, "reason": "join_tables_lt2", "bk": bk, "dk": "关联"}
        tool = get_tree().route(bk, "关联", "")
        return {"hit": tool != "unsupported_op",
                "reason": "" if tool != "unsupported_op" else "unsupported_route",
                "bk": bk, "dk": "关联", "tables": sorted(found), "tool": tool,
                "bk_src": bk_src, "dk_src": "text_override"}
    table, err = _resolve_table_level(q, tables, table_map)
    if err:
        return {"hit": False, "reason": "ambiguous_table", "bk": bk,
                "candidates": [c["table"] for c in err.get("candidates", [])]}
    tbl_src = "schema" if table else ""
    if not table:
        table = _table_token_fallback(q, outcome, tables)
        tbl_src = "db_fallback" if table else ""
    dk, dk_src = _infer_object(q, table, extended)
    # 行为条件对象（与产品 shortcircuit.py 同口径，20260806）：
    # "删除表格X"（X=已知表）= 删表不是删记录——"表格"一词两义，
    # 删语境+具体表名锚定=表结构本身
    if dk == "记录" and bk == "删" and "表格" in q and table:
        dk, dk_src = "表", "行为条件:删×表格"
    if bk in ("查", "增", "删", "改") and dk in ("", "记录") and not table:
        return {"hit": False, "reason": "no_table", "bk": bk, "dk": dk}
    # 选择集语境漂移（与产品 shortcircuit.py 同口径）：改/删/增×选择集 是条件语境，
    # 短路无法区分 → 不命中（交 LLM 打 {bk,记录}），只有 查×选择集 可短路
    if dk == "选择集" and bk != "查":
        return {"hit": False, "reason": "selection_condition_ctx", "bk": bk, "dk": dk}
    ct = ""
    if extended and bk == "增" and dk == "记录":
        ct = next((c for c, words in _CONSTRAINT_RULES if any(w in q for w in words)), "")
    tool = get_tree().route(bk, dk, ct)
    if tool == "unsupported_op":
        return {"hit": False, "reason": "unsupported_route", "bk": bk, "dk": dk}
    return {"hit": True, "reason": "", "bk": bk, "dk": dk, "ct": ct, "table": table,
            "tool": tool, "bk_src": bk_src, "dk_src": dk_src, "tbl_src": tbl_src}


def load_corpus() -> list[dict]:
    """真实问句：会话历史（带结果）+ 未识别池，去重，标注来源"""
    items, seen = [], set()

    def _add(q: str, source: str, outcome: str = ""):
        q = " ".join(q.split())
        if q and q not in seen:
            seen.add(q)
            items.append({"query": q, "source": source, "outcome": outcome})

    for f in sorted(glob.glob(str(ROOT / "db" / "json" / "conversation_*.json"))):
        try:
            entries = json.loads(Path(f).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        last_q = None
        for e in entries:
            if not isinstance(e, str):
                continue
            if e.startswith("用户"):
                last_q = (e.split("]: ", 1)[-1] if "]:" in e else e.split(":", 1)[-1]).strip()
            elif e.startswith("结果:") and last_q:
                _add(last_q, "conversation", e[3:].strip())
                last_q = None
    import yaml
    for f in sorted(glob.glob(str(ROOT / "industries" / "*" / "config" / "unrecognized_queries.yml"))):
        for it in yaml.safe_load(Path(f).read_text(encoding="utf-8")) or []:
            _add(str(it.get("query", "")).strip(), "unrecognized_pool",
                 str(it.get("last_outcome", "")))
    return items


def _report(sub: list[dict], title: str) -> str:
    n = len(sub)
    hits = [r for r in sub if r["hit"]]
    rate = len(hits) / n * 100 if n else 0
    lines = [f"\n### {title}（{n} 条，命中率 {rate:.1f}%）\n"]
    reason_cn = {
        "no_unique_behavior": "无唯一行为关键词（复合指令/无关键词/多轮指代）",
        "no_table": "记录级操作但表未命中（schema/映射/DB兜底均落空）",
        "ambiguous_table": "表匹配歧义（多表同优先级）",
        "join_tables_lt2": "关联查询确定表不足 2 张",
        "unsupported_route": "路由到 unsupported_op",
    }
    miss = Counter(r["reason"] for r in sub if not r["hit"])
    if miss:
        lines.append("| miss 原因 | 条数 | 占比 |")
        lines.append("|---|---|---|")
        for reason, c in miss.most_common():
            lines.append(f"| {reason_cn.get(reason, reason)} | {c} | {c / n * 100:.1f}% |")
    if hits:
        lines.append(f"\n命中路由分布：{dict(Counter(r.get('tool', '') for r in hits).most_common())}")
        contrib = Counter(src for r in hits for src in
                          (r.get("bk_src", ""), r.get("dk_src", ""), r.get("tbl_src", "")) if src)
        lines.append(f"规则贡献（命中条目维度，可重叠）：{dict(contrib.most_common())}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "stats"))
    args = ap.parse_args()

    tables = _load_schemas()
    table_map = (_load_biz_mapping() or {}).get("table_mapping", {})
    corpus = load_corpus()
    print(f"语料: {len(corpus)} 条去重问句（schema 表 {len(tables)} 张，映射别名 {len(table_map)} 条）")

    rows = []
    for it in corpus:
        for variant, ext in (("A", False), ("B", True)):
            r = simulate(it["query"], tables, table_map, ext, it["outcome"])
            r.update(query=it["query"], source=it["source"], variant=variant)
            rows.append(r)

    conv_b = [r for r in rows if r["source"] == "conversation" and r["variant"] == "B"]
    pool_b = [r for r in rows if r["source"] == "unrecognized_pool" and r["variant"] == "B"]
    conv_a = [r for r in rows if r["source"] == "conversation" and r["variant"] == "A"]

    def _rate(sub):
        return sum(1 for r in sub if r["hit"]) / len(sub) * 100 if sub else 0

    rate_b_all = _rate([r for r in rows if r["variant"] == "B"])
    verdict = (">70%，达到做 3.1 的红线" if _rate(conv_b) > 70
               else "<50%，按红线放弃 3.1" if _rate(conv_b) < 50
               else "50~70% 灰区，需人工裁决")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "shortcircuit_detail.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    miss_examples = {}
    for r in conv_b:
        if not r["hit"]:
            miss_examples.setdefault(r["reason"], []).append(r["query"])
    ex_lines = ["\n## miss 样例（变体 B，会话主样本，每类最多 10 条）\n"]
    for reason, qs in sorted(miss_examples.items(), key=lambda x: -len(x[1])):
        ex_lines.append(f"### {reason}（{len(qs)} 条）")
        for q in qs[:10]:
            ex_lines.append(f"- {q}")

    md = f"""# 3.0 前置统计①：短路命中率（2026-08-06）

模拟器：`scripts/stats_shortcircuit.py`（纯确定性仿真，零 LLM 调用；规则与语料口径见脚本头注释）

**主样本（会话历史）变体 B 命中率 {_rate(conv_b):.1f}%——{verdict}**
下界参考：变体 A（仅生产规则，无 3.1 候选规则）会话命中率 {_rate(conv_a):.1f}%；
全语料变体 B 命中率 {rate_b_all:.1f}%（含未识别池，失败样本有偏）。

## 变体 B（生产 + 3.1 候选规则）
{_report(conv_b, "会话历史（无偏主样本）")}
{_report(pool_b, "未识别问法池（失败样本，仅供参考）")}

## 变体 A（仅生产规则，下界）
{_report(conv_a, "会话历史")}
{chr(10).join(ex_lines)}
"""
    (out_dir / "3.0_短路命中率_20260806.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n已存档: {out_dir / '3.0_短路命中率_20260806.md'} + shortcircuit_detail.jsonl")


if __name__ == "__main__":
    main()
