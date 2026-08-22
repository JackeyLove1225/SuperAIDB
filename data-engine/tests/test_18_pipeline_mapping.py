"""层 18：管线映射层固化（demo E2E 大修的行为锁）

背景（2026-07-29 demo E2E 修复）：以下行为全部在真实端到端事故中修复，
本层把每个行为固化为离线纯函数用例——任何改动导致退化时立即红：

1.  转置表（首列属性名、每列一条记录）旋转后映射出干净主表行
2.  旋转后同名属性列去重（rowspan 展开，如"项目"分名称行/规格行）
3.  colspan 不规则表（两列映射同一字段）整张拒映射，不产垃圾行
4.  全局歧义词（"单位"）在候选表上下文内唯一即可配
5.  "费用(元)"不误配 base_price（中间子串全局不可配+已占用字段不再配）
6.  pk 系统主键不作映射目标
7.  链接列保留：带码明细表 → 明细表行 + 主表编码随行（分组锚点）
8.  全角数字按数值列 NFKC 归一（不被 CHECK 拒收）
9.  匹配键空白归一（'定 额 编 号' 命中 quota_code）
10. 业务编码值归一（Ａ１⁃２５ → A1-25）
11. 提取失败有限重试 + _error 显性标记（不静默丢批）
12. 组内同码去重留字段最全行，跳过行进 failures 但不触发 systemic_error
13. 目标词子词兜底（'材料价格'→定额材料明细表；歧义不猜）
14. 恰半混合表（2/4 命中）拒映射——宁 unmapped 不产污染行
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 行业无关的夹具 schema（主表+明细表，含唯一业务码和外键）──
SCHEMAS = [
    {
        "name": "quota_items", "business_name": "定额项目主表",
        "columns": [
            {"name": "id", "type": "INTEGER", "pk": True, "business_name": "主键"},
            {"name": "quota_code", "type": "TEXT", "unique": True, "business_name": "定额编号"},
            {"name": "quota_name", "type": "TEXT", "business_name": "定额项目名称"},
            {"name": "unit", "type": "TEXT", "business_name": "计量单位"},
            {"name": "base_price", "type": "FLOAT", "business_name": "全费用基价"},
            {"name": "artificial_cost", "type": "FLOAT", "business_name": "人工费"},
            {"name": "material_cost", "type": "FLOAT", "business_name": "材料费"},
            {"name": "machine_cost", "type": "FLOAT", "business_name": "机械费"},
        ],
        "foreign_keys": [],
    },
    {
        "name": "quota_materials", "business_name": "定额材料明细表",
        "columns": [
            {"name": "id", "type": "INTEGER", "pk": True, "business_name": "主键"},
            {"name": "quota_item_id", "type": "INTEGER", "business_name": "所属定额项目"},
            {"name": "material_name", "type": "TEXT", "business_name": "材料名称"},
            {"name": "material_spec", "type": "TEXT", "business_name": "材料规格"},
            {"name": "unit", "type": "TEXT", "business_name": "计量单位"},
            {"name": "consumption_quantity", "type": "FLOAT", "business_name": "消耗量"},
            {"name": "unit_price", "type": "FLOAT", "business_name": "材料单价"},
        ],
        "foreign_keys": [{"columns": ["quota_item_id"],
                          "references": "quota_items", "ref_columns": ["id"]}],
    },
]


def _map(tables, kv=None, rules=None):
    from pipeline.unified import map_to_schemas
    return map_to_schemas({"kv": kv or {}, "tables": tables, "prose": []},
                          SCHEMAS, {}, rules or {})


def test_transpose_rotation():
    """1+9. 转置表旋转 + 匹配键空白归一"""
    m = _map([{"headers": ["定 额 编 号", "A1-25", "A1-26"],
               "rows": [["项目", "沟铸铁盖板安装", "沟铸铁盖板安装"],
                        ["全费用(元)", "11656.79", "17732.05"],
                        ["人工费(元)", "598.79", "886.51"]]}])
    rows = {t["name"]: t["rows"] for t in m["tables"]}.get("quota_items", [])
    assert len(rows) == 2, f"转置后应出 2 行主表: {rows}"
    assert rows[0]["quota_code"] == "A1-25", "首列编码应旋转成编码列"
    assert rows[0]["quota_name"] == "沟铸铁盖板安装"
    assert rows[0]["base_price"] == "11656.79"
    print("OK - 转置表旋转映射干净主表行（含空格表头归一）")


def test_rowspan_dedup_after_rotation():
    """2. 旋转后同名属性列去重（保首列，重复列如实进 unmapped）"""
    m = _map([{"headers": ["定额编号", "A1-25"],
               "rows": [["项目", "沟铸铁盖板安装"],
                        ["项目", "宽 32"],
                        ["人工费(元)", "598.79"]]}])
    rows = {t["name"]: t["rows"] for t in m["tables"]}.get("quota_items", [])
    assert len(rows) == 1 and rows[0]["quota_name"] == "沟铸铁盖板安装", \
        f"同名属性列应保留首列（主名称）: {rows}"
    assert any(u.get("reason") == "rowspan_dup_column" for u in m["unmapped"]), \
        "被去重的规格列应如实进 unmapped"
    print("OK - rowspan 同名属性列去重（保首列+如实上报）")


def test_colspan_irregular_rejected():
    """3. colspan 不规则表拒映射（防垃圾行进库的总闸门）"""
    m = _map([{"headers": ["定额编号", "定额编号", "定额编号", "A1-25"],
               "rows": [["项目", "项目", "项目", "沟铸铁盖板安装"],
                        ["人工费(元)", "", "", "598.79"]]}])
    assert not m["tables"], f"colspan 不规则表不得产出任何行: {m['tables']}"
    assert any(u.get("kind") == "table" for u in m["unmapped"]), "整张应进 unmapped"
    print("OK - colspan 不规则表拒映射进 unmapped，零垃圾行")


def test_ambiguity_resolved_in_table_context():
    """4. 全局歧义词'单位'在表上下文唯一即配"""
    m = _map([{"headers": ["定额编号", "项目", "单位", "人工费(元)"],
               "rows": [["A1-33", "砌块墙", "10m3", "1234.56"]]}])
    rows = {t["name"]: t["rows"] for t in m["tables"]}.get("quota_items", [])
    assert rows and rows[0].get("unit") == "10m3", f"'单位'应在 quota_items 上下文配 unit: {rows}"
    print("OK - 全局歧义词表内消解（单位→本表计量单位）")


def test_fees_not_mismapped_to_base_price():
    """5. '费用(元)' 不得误配 base_price；'全费用(元)' 可配"""
    m = _map([{"headers": ["定额编号", "全费用(元)", "费用(元)"],
               "rows": [["A1-33", "5432.10", "999.99"]]}])
    rows = {t["name"]: t["rows"] for t in m["tables"]}.get("quota_items", [])
    assert rows and rows[0].get("base_price") == "5432.10", "全费用(元) 应配 base_price"
    assert "999.99" not in [str(v) for r in rows for v in r.values()], \
        f"费用(元) 不得进任何字段: {rows}"
    assert any(u.get("key") == "费用(元)" for u in m["unmapped"]), "费用(元) 应如实 unmapped"
    print("OK - '费用(元)'不误配 base_price（前缀/后缀+占用保护）")


def test_pk_never_mapping_target():
    """6. pk 主键不作映射目标（源数据含 id/主键 列也进不了主键）"""
    m = _map([{"headers": ["id", "定额编号", "人工费(元)"],
               "rows": [["5", "A1-33", "1234.56"]]}])
    rows = {t["name"]: t["rows"] for t in m["tables"]}.get("quota_items", [])
    assert rows, "其他列命中应正常映射"
    assert "id" not in rows[0], f"主键列不得出现在映射行: {rows[0]}"
    print("OK - pk 主键禁映射（与写入层禁手工 id 呼应）")


def test_link_column_carried():
    """7. 链接列保留（明细行带主表编码 → 分组+外键解析的锚点）"""
    m = _map([{"headers": ["定额编号", "名称", "单位", "单价(元)", "消耗量"],
               "rows": [["A1-6", "蒸压灰砂砖240×115×53", "千块", "390.00", "5.332"],
                        ["A1-6", "干混砌筑砂浆DMM10", "t", "280.00", "4.148"]]}])
    rows = {t["name"]: t["rows"] for t in m["tables"]}.get("quota_materials", [])
    assert len(rows) == 2, f"应映射到明细表: {m}"
    assert all(r.get("quota_code") == "A1-6" for r in rows), \
        f"链接列 quota_code 必须随行: {rows}"
    assert rows[0]["material_name"].startswith("蒸压灰砂砖")
    assert rows[1]["consumption_quantity"] == "4.148"
    print("OK - 带码明细表映射到明细表+链接列随行")


def test_fullwidth_numeric_normalized():
    """8. 全角数字按数值列 NFKC 归一"""
    m = _map([{"headers": ["定额编号", "全费用(元)", "项目"],
               "rows": [["A1-10", "５５９８．８８", "空心砖墙"]]}])
    rows = {t["name"]: t["rows"] for t in m["tables"]}.get("quota_items", [])
    assert rows and rows[0]["base_price"] == "5598.88", \
        f"数值列应 NFKC 归一: {rows}"
    print("OK - 全角数字归一（文本列不动）")


def test_code_value_normalized():
    """10. 业务编码值归一（全角/异体横线/空格 → 统一码形）"""
    from pipeline.unified import _norm_code_value
    assert _norm_code_value("Ａ１⁃２５") == "A1-25"
    assert _norm_code_value("A 1-2 5") == "A1-25"
    assert _norm_code_value("Ａ１—２６") == "A1-26"
    print("OK - 编码值归一（唯一键幂等的前提）")


def test_extract_retry_and_error_marker():
    """11. 提取失败有限重试 + _error 显性标记（不静默丢批）"""
    from pipeline.unified import extract_intermediate

    class _FlakyAI:
        def __init__(self):
            self.n = 0

        def chat(self, *a, **k):
            self.n += 1
            if self.n == 1:
                return ""  # 首次返空（思考模式事故形态）
            return '{"kv": {}, "tables": [], "prose": ["x"]}'

    ai = _FlakyAI()
    r = extract_intermediate("一些文本", ai)
    assert r["prose"] == ["x"] and ai.n == 2, "首次返空应重试第二次成功"

    class _DeadAI:
        def chat(self, *a, **k):
            return "不是JSON"

    r2 = extract_intermediate("一些文本", _DeadAI())
    assert r2.get("_error"), "持续失败必须带 _error 显性标记"
    assert r2["tables"] == [], "失败批不得产出数据"
    print("OK - 提取重试+_error 标记（静默丢批根治）")


def test_intragroup_dedup_richest_wins():
    """12. 组内同码去重留字段最全行；跳过行不触发 systemic_error"""
    from unittest.mock import patch
    from pipeline.ingestion import write_batch_groups

    inserted = []

    class _Drv:
        def begin(self, name): pass
        def rollback(self, name): pass
        def commit(self): pass
        def query(self, sql): return [{"id": 1}]
        def insert(self, table, rows, overwrite=False):
            inserted.extend((table, dict(r)) for r in rows)
            return {"ok": True, "count": len(rows)}

    data = {"tables": [{"name": "quota_items", "rows": [
        {"quota_code": "A1-1", "quota_name": "水"},  # 碎片行
        {"quota_code": "A1-1", "quota_name": "砖基础", "base_price": "7528.19",
         "artificial_cost": "2517.66"},  # 字段最全行
        {"quota_code": "A1-1", "quota_name": "砖基础", "base_price": "7528.19",
         "artificial_cost": "2517.66"},  # 完全重复行
    ]}]}
    all_results = {"failures": [], "conflicts": []}
    with patch("core.data_ops._get_driver", return_value=_Drv()):
        write_batch_groups(data, None, _Drv(), "quota_items", "quota_code",
                           False, all_results)
    main_rows = [r for t, r in inserted if t == "quota_items"]
    assert len(main_rows) == 1, f"同码 3 行应去重为 1 行: {main_rows}"
    assert main_rows[0].get("base_price") == "7528.19", "必须保留字段最全行"
    assert len(all_results["failures"]) >= 1, "跳过行应进 failures 如实上报"
    assert "systemic_error" not in all_results, "去重跳过不是组级失败，不得误报系统性错误"
    print("OK - 组内同码去重留最全行+不误报系统性错误")


def test_route_subword_fallback():
    """13. 目标词子词兜底（'材料价格'→定额材料明细表；歧义不猜）"""
    # 适配空行业：用 mock 提供 fixture schema（不依赖真实 engineering 行业）
    from unittest.mock import patch
    from core.target_resolve import resolve_tables_by_terms
    fake_terms = (SCHEMAS, {
        "quota_items": ["定额", "定额表", "主表"],
        "quota_materials": ["材料明细", "材料表"],
    })
    with patch("core.target_resolve._load_industry_terms", return_value=fake_terms):
        targets, unmatched = resolve_tables_by_terms(["定额项目", "材料价格"])
        assert targets == ["quota_items", "quota_materials"], \
            f"子词兜底应命中主表+材料表: {targets}, {unmatched}"
        assert unmatched == []
    print("OK - 目标词子词兜底（材料价格→定额材料明细表）")


def test_half_coverage_rejected():
    """14. 恰半/低覆盖混合表拒映射（宁 unmapped 不产污染行）"""
    m = _map([{"headers": ["定额编号", "规格", "颜色", "产地"],
               "rows": [["A1-1", "240", "红", "武汉"]]}])
    assert not m["tables"], f"1/4 覆盖不得映射: {m['tables']}"
    assert any(u.get("kind") == "table" and u.get("reason") == "low_coverage"
               for u in m["unmapped"])
    print("OK - 低覆盖混合表拒映射")


if __name__ == "__main__":
    test_transpose_rotation()
    test_rowspan_dedup_after_rotation()
    test_colspan_irregular_rejected()
    test_ambiguity_resolved_in_table_context()
    test_fees_not_mismapped_to_base_price()
    test_pk_never_mapping_target()
    test_link_column_carried()
    test_fullwidth_numeric_normalized()
    test_code_value_normalized()
    test_extract_retry_and_error_marker()
    test_intragroup_dedup_richest_wins()
    test_route_subword_fallback()
    test_half_coverage_rejected()
    print("\n=== 层 18 全部通过：管线映射层行为已固化 ===")
