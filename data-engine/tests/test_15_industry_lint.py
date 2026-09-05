"""层 15：行业配置校验——配置即代码，违规显式拦截"""
import sys; sys.path.insert(0, ".")
import tempfile
from pathlib import Path


def test_all_industries_pass():
    """现存所有行业目录必须通过 lint"""
    from core.industry_linter import lint_all
    industries_dir = Path(__file__).resolve().parent.parent / "industries"
    report = lint_all(industries_dir)
    assert not report, f"行业配置存在违规: {report}"
    print("OK - 现存行业全部通过配置校验")


def test_linter_catches_all_rule_types():
    """6 类违规各造一例，必须全部被拦截并报出具体位置"""
    from core.industry_linter import lint_industry

    with tempfile.TemporaryDirectory() as tmp:
        ind = Path(tmp) / "bad_ind"
        (ind / "schemas").mkdir(parents=True)
        (ind / "config").mkdir()
        (ind / "prompts").mkdir()

        # R2 缺 business_name + R3 表名非复数 + R1 FK 引用非 id + R3 FK 列名不规范
        (ind / "schemas" / "order.yaml").write_text(chr(10).join([
            "name: order",
            "columns:",
            "- name: id",
            "  type: INTEGER",
            "- name: user_ref",
            "  type: INTEGER",
            "foreign_keys:",
            "- columns: [user_ref]",
            "  references: users",
            "  ref_columns: [code]",
            "",
        ]), encoding="utf-8")
        # R4 FK 引用不存在的表
        (ind / "schemas" / "items.yaml").write_text(chr(10).join([
            "name: items",
            "business_name: 条目",
            "columns:",
            "- name: id",
            "  type: INTEGER",
            "  business_name: 主键",
            "- name: ghost_id",
            "  type: INTEGER",
            "  business_name: 幽灵引用",
            "foreign_keys:",
            "- columns: [ghost_id]",
            "  references: ghosts",
            "",
        ]), encoding="utf-8")
        # R6 坏 YAML
        (ind / "schemas" / "broken.yaml").write_text("{{{{ not yaml [[[", encoding="utf-8")
        # R4 mapping 目标表不存在
        (ind / "config" / "db_mapping.yml").write_text(chr(10).join([
            "table_mapping:",
            "  订单表: nonexistent_table",
            "",
        ]), encoding="utf-8")
        # R5 示例缺必填字段
        (ind / "prompts" / "prompts.yml").write_text(chr(10).join([
            "decompose_examples:",
            "- query: 缺 sub_tasks",
            "router_examples:",
            "- input: 缺行为对象键",
            "",
        ]), encoding="utf-8")

        errors = lint_industry(ind)
        text = chr(10).join(errors)
        for rule in ("[R1]", "[R2]", "[R3]", "[R4]", "[R5]", "[R6]"):
            assert rule in text, f"{rule} 类违规未被拦截: {errors}"
        # 报出具体位置
        assert "order.yaml" in text and "db_mapping.yml" in text and "prompts.yml" in text
    print("OK - 6 类违规全部被拦截并报出位置")


def test_waivers_work():
    """豁免文件：带 reason 的豁免生效，缺 reason 不生效"""
    from core.industry_linter import lint_industry

    with tempfile.TemporaryDirectory() as tmp:
        ind = Path(tmp) / "w_ind"
        (ind / "schemas").mkdir(parents=True)
        (ind / "schemas" / "order.yaml").write_text(chr(10).join([
            "name: order",
            "business_name: 订单",
            "columns:",
            "- name: id",
            "  type: INTEGER",
            "  business_name: 主键",
            "",
        ]), encoding="utf-8")
        errs = lint_industry(ind)
        assert any("[R3]" in e and "非复数" in e for e in errs), "无豁免时应报非复数"
        (ind / ".lint_waivers").write_text(chr(10).join([
            "waivers:",
            "  - rule: R3",
            "    match: order",
            "    reason: order 作名词表订单时为单数形态惯例",
            "",
        ]), encoding="utf-8")
        errs2 = lint_industry(ind)
        assert not any("[R3]" in e and "非复数" in e for e in errs2), "带 reason 豁免应生效"
    print("OK - 豁免机制：带 reason 生效、无豁免报错")


if __name__ == "__main__":
    test_all_industries_pass()
    test_linter_catches_all_rule_types()
    test_waivers_work()
    print("\n=== ALL INDUSTRY LINT TESTS PASSED ===")
