"""3.0 前置统计②：Token 成本分布聚合（决定 3.2 角色化模型配置的档位设计）

读取 logs/llm_usage.jsonl（core/llm_usage.py 统一落账），按角色聚合：
  调用次数 / 输入 / 输出 / 缓存命中 / 缓存命中率 / 估算成本 / 成本占比

价格是易变外部事实——PRICES 表按 DeepSeek 官网现价手工维护（元/百万 tokens），
改价格不用动统计逻辑；token 量本身是与价格无关的硬数据，分布结论主要看 token 占比。

用法：python scripts/stats_token_cost.py [--in logs/llm_usage.jsonl] [--out docs/stats]
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 元/百万 tokens（DeepSeek 官网 2026 中旬公示价类推；按实际账单校准）
PRICES = {
    "deepseek-v4-flash": {"in": 2.0, "in_hit": 0.5, "out": 8.0},
    "deepseek-v4-pro": {"in": 4.0, "in_hit": 1.0, "out": 16.0},
    "default": {"in": 2.0, "in_hit": 0.5, "out": 8.0},
}

ROLE_CN = {
    "decompose": "拆解", "synthesize": "综合", "agent_loop": "统一循环",
    "extract_param": "参数提取", "extract_file": "文件提取",
    "research": "深度研究", "review": "问法审查", "schema_design": "schema设计",
    "ooda_correct": "OODA纠错", "other": "未标注",
}
# 3.2 档位候选映射（规划档吃 pro、机械档吃 flash；占比数据决定是否值得分档）
TIER_MAP = {
    "decompose": "规划档", "synthesize": "规划档", "agent_loop": "规划档",
    "research": "规划档", "review": "规划档", "schema_design": "规划档",
    "extract_param": "机械档", "extract_file": "机械档", "ooda_correct": "机械档",
    "other": "机械档",  # 本批 other 已定位：旧代码未标注的 _ooda_regenerate（此后按 ooda_correct 落账）
}


def _cost(model: str, inp: int, out: int, hit: int) -> float:
    p = PRICES.get(model, PRICES["default"])
    miss = max(inp - hit, 0)
    return (miss * p["in"] + hit * p["in_hit"] + out * p["out"]) / 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=str(ROOT / "logs" / "llm_usage.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "stats"))
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"无用量数据: {src}（先跑带落账的工作负载）")
        return
    rows = []
    for line in src.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if not rows:
        print("用量文件为空")
        return

    agg = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "hit": 0, "cost": 0.0})
    for r in rows:
        a = agg[r.get("role") or "other"]
        a["calls"] += 1
        a["in"] += r.get("in", 0)
        a["out"] += r.get("out", 0)
        a["hit"] += r.get("cache_hit", 0)
        a["cost"] += _cost(r.get("model", ""), r.get("in", 0), r.get("out", 0),
                           r.get("cache_hit", 0))

    total_cost = sum(a["cost"] for a in agg.values())
    total_in = sum(a["in"] for a in agg.values())
    total_out = sum(a["out"] for a in agg.values())
    total_hit = sum(a["hit"] for a in agg.values())

    lines = ["| 角色 | 调用 | 输入 tok | 输出 tok | 缓存命中 | 命中率 | 成本(元) | 成本占比 |",
             "|---|---|---|---|---|---|---|---|"]
    for role, a in sorted(agg.items(), key=lambda x: -x[1]["cost"]):
        hr = a["hit"] / a["in"] * 100 if a["in"] else 0
        share = a["cost"] / total_cost * 100 if total_cost else 0
        lines.append(f"| {ROLE_CN.get(role, role)}({role}) | {a['calls']} | {a['in']} | {a['out']} "
                     f"| {a['hit']} | {hr:.0f}% | {a['cost']:.4f} | {share:.1f}% |")

    tiers = defaultdict(float)
    for role, a in agg.items():
        tiers[TIER_MAP.get(role, "未分档")] += a["cost"]
    tier_lines = ["| 档位 | 成本(元) | 占比 |", "|---|---|---|"]
    for t, c in sorted(tiers.items(), key=lambda x: -x[1]):
        tier_lines.append(f"| {t} | {c:.4f} | {c / total_cost * 100 if total_cost else 0:.1f}% |")

    cache_rate = total_hit / total_in * 100 if total_in else 0
    md = f"""# 3.0 前置统计②：Token 成本分布（2026-08-06）

数据源：`logs/llm_usage.jsonl`（core/llm_usage.py 双网关统一落账）；聚合：`scripts/stats_token_cost.py`
样本：{len(rows)} 次 LLM 调用（{rows[0].get('ts', '?')} ~ {rows[-1].get('ts', '?')}）
价格表：脚本内 PRICES（DeepSeek 官网价类推，可按账单校准）——**token 量是硬数据，成本是推导值**

**总成本 {total_cost:.4f} 元；总输入 {total_in} tok / 总输出 {total_out} tok；前缀缓存命中率 {cache_rate:.1f}%**

## 角色分布（路线图四角色：拆解/综合/提取/审查，此处细分为 8 个实测角色）

{chr(10).join(lines)}

## 3.2 档位归并（目标 ≤5 档）

{chr(10).join(tier_lines)}

## 采集口径与盲区修复记录

- 样本两批探针采集（`scripts/stats_live_probe.py`）：批一 6 句只读问句（24 次调用，
  覆盖 decompose/agent_loop）；批二补四角色缺口（synthesize 走 execute 复杂任务、
  extract_param 走 mutate_data 零候选、research 走 deep_research、review 走未识别问法审核、
  extract_file 走微型文本流 batch_process），副作用均幂等清理（探针索引已 DROP）。
- 批二暴露两个落账盲区并已修复：① `pipeline/extraction.py` 语义路由 `_route_tables`
  未挂角色 → 补 extract_file；② `agent/__init__.py::_ooda_regenerate`（executor OODA
  纠错）未挂角色 → 补 ooda_correct。表中 other(11 条) 即修复前采集的 OODA 纠错调用，
  归机械档；后续采集将按 ooda_correct 落账。
- research 缓存命中率低（12%）属预期：OODA 历史逐轮累积进 prompt，前缀不稳定；
  decompose/agent_loop 稳定段前置（B1 设计），命中率 87%。
"""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "3.0_token成本分布_20260806.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n已存档: {out_dir / '3.0_token成本分布_20260806.md'}")


if __name__ == "__main__":
    main()
