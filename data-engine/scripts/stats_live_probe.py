"""3.0 统计·token 用量采集探针：真实问句跑图，产出 llm_usage.jsonl

阶段一（已采集 24 条，见 2026-08-06 03:26 批次）：6 句只读问句，
覆盖 decompose / agent_loop（记录级查询全走统一循环）。

阶段二（本次）：补齐路线图四角色口径的缺口——
- synthesize：execute 路径复杂任务（记录查询 + DDL 混合 → basic → execute → LLM 综合）
  副作用控制：DDL 选 create_index（gate=none 直接执行），探针首尾 sqlite3 直连
  DROP INDEX IF EXISTS 幂等清理，零数据污染。
- extract_param：mutate_data 的 _extract_mutation_ops（闸前落账）。
  风险控制：条件 unit_price < 0 必然 0 候选 → mutate_data 如实报"未找到"，
  不触发核武闸、不写库、无挂起。
- research：deep_research（分析类指令，OODA 只读检索）。
- review：unrecognized_review（确定性关键词触发；propose_examples 落账后
  interrupt 挂起，GraphInterrupt 异常由探针兜住，不阻断后续）。
- extract_file：脚本级 batch_process 微型流（1 个流单元，语义路由+提取 2 次调用；
  batch_process 只 yield 数据不入库）。

用法：
  python scripts/stats_live_probe.py            # 只跑阶段二（默认）
  python scripts/stats_live_probe.py --phase1   # 重跑阶段一（重复采集，慎用）
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUERIES_PHASE1 = [
    "查询数据库有哪些表",
    "查询 quota_items 表有多少条记录",
    "查询 quota_materials 中 unit_price 大于 100 的记录",
    "查询定额主表与材料明细表的关联数据",
    "安装了哪些数据库",
    "先查询 quota_items 有多少条记录，再查询 quota_materials 有多少条记录，最后对比两者",
]

QUERIES_PHASE2 = [
    ("synthesize", "查询 quota_items 表有多少条记录，并给 quota_items 表的 quota_code 字段创建索引 idx_probe_stats"),
    ("extract_param", "把 quota_materials 中 unit_price 小于 0 的记录的 material_spec 改为 PROBE_TMP"),
    ("research", "深度研究：分析 quota_items 与 quota_materials 两表的数据关联与数据质量，给出洞察"),
    ("review", "看看没识别的问题"),
]

_PROBE_INDEX = "idx_probe_stats"

# 定额样例文本（extract_file 微型流；对应 quota_items/quota_materials 结构）
_SAMPLE_TEXT = """定额编号：A1-1  定额名称：人工挖基坑土方（三类土，深度2m以内）  计量单位：m3
全费用基价：25.50 元  人工费：20.10  材料费：0.00  机械费：3.20  管理费：1.40  利润：0.80

定额编号：A1-2  定额名称：人工挖基坑土方（三类土，深度4m以内）  计量单位：m3
全费用基价：31.20 元  人工费：25.60  材料费：0.00  机械费：3.20  管理费：1.60  利润：0.80

材料明细（A1-1）：
材料名称：标准砖  规格：240x115x53mm  单位：千块  消耗量：0.532  单价：385.00
材料名称：水泥砂浆  规格：M5  单位：m3  消耗量：0.228  单价：210.00
"""


def _drop_probe_index(tag: str) -> None:
    """sqlite3 直连幂等清理探针索引（绕过契约层角色校验——清理动作非 AI 行为）"""
    try:
        from config.settings import settings
        conn = sqlite3.connect(settings.SQLITE_DB_PATH)
        conn.execute(f"DROP INDEX IF EXISTS {_PROBE_INDEX}")
        conn.commit()
        conn.close()
        print(f"  [{tag}] 索引 {_PROBE_INDEX} 已清理")
    except Exception as e:
        print(f"  [{tag}] 索引清理失败（如实记录，人工兜底）: {str(e)[:120]}")


def _run_queries(queries) -> None:
    from agent.open_layer.graph import run_open_agent
    for i, item in enumerate(queries, 1):
        tag, q = item if isinstance(item, tuple) else ("", item)
        print(f"\n[{i}/{len(queries)}]{('(' + tag + ')') if tag else ''} {q}")
        try:
            ans = run_open_agent(q)
            print(f"  → {str(ans)[:100]}")
        except Exception as e:
            # GraphInterrupt（人审闸挂起）等如实记录，不阻断后续探针
            print(f"  → 挂起/失败（如实记录）: {type(e).__name__}: {str(e)[:120]}")


def _probe_extract_file() -> None:
    """extract_file 角色：1 个流单元走 batch_process（路由+提取 2 次 LLM 调用，不入库）"""
    print("\n[extract_file] 微型文本流走 batch_process（1 批，不入库）")
    try:
        from config.settings import settings
        from industries.base import discover_industries, get_industry
        from core.ai_runtime.ai_client import AIClient
        from pipeline.extraction import batch_process
        discover_industries()
        cfg = get_industry(settings.INDUSTRY)
        ai = AIClient.get_instance()
        stream = iter([(0, _SAMPLE_TEXT, 0)])
        for batch_num, data in batch_process(None, cfg, stream, ai, page_limit=1, overlap=0):
            n_rows = sum(len(t.get("rows", [])) for t in data.get("tables", []))
            print(f"  → 第 {batch_num} 批提取 {n_rows} 行（仅统计，未入库）")
    except Exception as e:
        print(f"  → 失败（如实记录）: {type(e).__name__}: {str(e)[:120]}")


def main():
    if "--phase1" in sys.argv:
        print("== 阶段一（重复采集警告）==")
        _run_queries(QUERIES_PHASE1)
        return

    print("== 阶段二：四角色缺口补采 ==")
    _drop_probe_index("前置清理")  # 保证 create_index 子任务成功（synthesize 需 2 条 results）
    _run_queries(QUERIES_PHASE2)
    _probe_extract_file()
    _drop_probe_index("收尾清理")

    from core.llm_usage import LOG_PATH
    n = sum(1 for _ in open(LOG_PATH, encoding="utf-8")) if LOG_PATH.exists() else 0
    print(f"\n用量落账: {LOG_PATH}（{n} 条）")


if __name__ == "__main__":
    main()
