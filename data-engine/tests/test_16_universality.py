"""层 16：通用性验收——防通用性回退的兜底测试层

1. 虚构行业全链路：YAML→建表→入库（唯一键冲突）→查询，全程无工程行业特例
2. 通用层领域词黑名单：core/pipeline/agent 代码不得含硬编码业务标识
3. 行业配置 lint 全量通过
"""
import sys; sys.path.insert(0, ".")
import tempfile
from pathlib import Path


def test_fictional_industry_full_chain():
    """虚构行业（图书借阅）全链路：2 表+FK+唯一键声明，建表/入库/查询/冲突检测"""
    from unittest.mock import patch
    from core.drivers.sqlite_driver import SqliteDriver
    from core.schema_matcher import get_unique_key_column

    books_schema = {
        "name": "books", "business_name": "图书",
        "columns": [
            {"name": "id", "type": "INTEGER", "pk": True, "not_null": True},
            {"name": "book_code", "type": "TEXT", "not_null": True, "unique": True,
             "business_name": "图书编号"},
            {"name": "title", "type": "TEXT", "business_name": "书名"},
        ],
        "foreign_keys": [],
    }
    borrow_schema = {
        "name": "borrow_records", "business_name": "借阅记录",
        "columns": [
            {"name": "id", "type": "INTEGER", "pk": True, "not_null": True},
            {"name": "book_id", "type": "INTEGER", "not_null": True,
             "business_name": "图书ID"},
            {"name": "borrower", "type": "TEXT", "business_name": "借阅人"},
        ],
        "foreign_keys": [{"columns": ["book_id"], "references": "books",
                          "ref_columns": ["id"]}],
    }

    with tempfile.TemporaryDirectory() as tmp:
        drv = SqliteDriver(f"{tmp}/t.db")
        try:
            # 建表（YAML schema → DDL，含外键）
            drv.create_table(books_schema)
            drv.create_table(borrow_schema)
            assert drv.table_exists("books") and drv.table_exists("borrow_records")
            fks = drv.conn.execute(
                "PRAGMA foreign_key_list(borrow_records)").fetchall()
            assert fks and fks[0][2] == "books", f"外键未创建: {fks}"

            # 唯一键声明被通用机制识别（虚构行业、非 quota 键名）
            with patch('core.schema_matcher._load_schemas',
                       return_value=[books_schema, borrow_schema]):
                assert get_unique_key_column("books") == "book_code", \
                    "虚构行业的 unique 声明应被通用机制识别"

                # 入库 + 冲突检测（非工程行业也生效）
                r1 = drv.insert("books", [{"book_code": "B001", "title": "设计模式"}])
                assert r1.get("ok"), f"首次入库应成功: {r1}"
                r2 = drv.insert("books", [{"book_code": "B001", "title": "重复"}])
                assert r2.get("conflict"), f"同 book_code 应被唯一键拦截: {r2}"
                r3 = drv.insert("books", [{"book_code": "B001", "title": "覆盖"}],
                                overwrite=True)
                assert r3.get("ok"), f"overwrite 应放行: {r3}"

            # 查询验证
            rows = drv.query("SELECT title FROM books WHERE book_code='B001'")
            assert len(rows) == 1 and rows[0]["title"] == "覆盖", \
                f"覆盖语义错误: {rows}"
        finally:
            for conn in drv._conns.values():
                conn.close()
    print("OK - 虚构行业全链路（建表/入库/唯一键冲突/查询）")


def test_generic_layer_no_domain_blacklist():
    """通用层代码不得含硬编码业务标识（quota/patient/medical/定额系列）"""
    blacklist = ["quota_id", "quota_header", "patient_id", "patient_code",
                 "medication", "quota_items", "quota_materials",
                 "quota_machines", "quota_labor", "定额编号"]
    root = Path(__file__).resolve().parent.parent
    hits = []
    for sub in ("core", "pipeline", "agent"):
        for f in (root / sub).rglob("*.py"):
            src = f.read_text(encoding="utf-8", errors="ignore")
            for w in blacklist:
                if w in src:
                    hits.append(f"{f.relative_to(root)}: {w}")
    assert not hits, f"通用层残留领域词（{len(hits)} 处）: {hits[:10]}"
    print("OK - 通用层无领域词黑名单命中")


def test_lint_all_industries_again():
    """行业配置 lint 全量通过（全量行业配置复核）"""
    from core.industry_linter import lint_all
    industries_dir = Path(__file__).resolve().parent.parent / "industries"
    report = lint_all(industries_dir)
    assert not report, f"行业配置违规: {report}"
    print("OK - 行业配置 lint 全量通过")


if __name__ == "__main__":
    test_fictional_industry_full_chain()
    test_generic_layer_no_domain_blacklist()
    test_lint_all_industries_again()
    print("\n=== ALL UNIVERSALITY TESTS PASSED ===")
