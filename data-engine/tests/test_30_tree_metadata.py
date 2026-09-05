"""层 26：3.3 决策树模块化 + 工具元数据交叉一致性

物理拆分（decision_tree.yaml → decision_tree/ 目录 7 域文件）已由层 2 覆盖
（81 节点/48 决策/33 叶子锁定 + 五项结构校验）；本层锁定"元数据×树"交叉一致性：

1. 域文件组织：7 文件节点分布锁定（每域 <30 节点），跨文件无重复 id
2. 元数据×树相容：全组合枚举（7行为×15对象+1 空对象位），树路由结果与工具 intent_tags
   相容，或在"已知偏移豁免表"内（链尾兜底/语义等价，每条带理由）；
   豁免反向校验：豁免工具必须确实存在不相容路径（防豁免腐化）
3. 本职路径：每个树叶子工具至少一条 bk×dk 组合与其 intent_tags 完全相容
   （防标注整体写错——漂移测试抓增量，本断言抓存量）
4. requires_table：记录级/DDL 单表工具=True，库级/模板/文件/建新表=False；
   find_tools(requires_table=) 三态筛选冒烟
5. 关键路由抽样：核心组合精确锁定（查记录→query/增表自定义→bct/删索引→di 等）
"""
import os
import sys
import itertools

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.tools  # noqa: F401  注册工具 + 标注元数据
from agent.router import get_tree, _NODES, _TREE_DIR
from core.tool_registry import get_tools, find_tools

_tools = get_tools()  # 快照：agent.tools 导入即完成注册，本层只做只读枚举

_BEHAVIORS = ["查", "增", "改", "删", "导入", "上传", "导出"]
_DBS = ["模板", "会话", "数据库", "表", "记录", "选择集", "结构", "字段",
        "外键", "索引", "类型", "精度", "关联", "统计", "文件", ""]
# constraint 维度参与枚举：cst/bins/snn 分别只在 ct=标准/批量/非空 时可达
_CTS = ["", "批量", "标准", "非空"]

# 已知偏移豁免（语义等价的历史行为，非标注错误）。
# 写域链尾已于 20260824 全部 fail-closed 到 unsup（空/未知对象不再落写工具）。
# 断言对称：豁免工具若未来树改动后不再产生不相容路径，须同步移出本表。
_DRIFT_EXEMPTIONS = {
    "import_template": "导入域：非文件对象→模板导入（im_obj 右链，语义等价历史行为）",
    "save_template": "改模板=重新保存（导出链尾已于 20260824 fail-closed 到 unsup）",
    "upload_file": "上传无对象判定：上传行为直达",
    "describe_schema": "结构系对象（外键/索引/类型/精度）查询=看表结构；增库/改表=展示结构",
    "add_foreign_key": "改外键=删旧加新",
    "create_index": "改索引=删除重建",
    "modify_column": "删类型=改回默认类型",
    "delete_data": "删选择集=按选择集条件删记录",
    "query": "会话查询复用 query 工具",
    "clear_db": "改数据库=清空重建",
}


def test_domain_files_layout():
    """域文件组织：7 文件、节点分布锁定、每域 <30、合计 81"""
    import yaml
    expected = {"_root.yml": 7, "query.yml": 17, "insert.yml": 14,
                "update.yml": 13, "delete.yml": 18, "file.yml": 6, "_shared.yml": 6}
    files = {f.name for f in _TREE_DIR.glob("*.yml")}
    assert files == set(expected), f"域文件集合漂移: {files ^ set(expected)}"
    total = 0
    for name, count in expected.items():
        data = yaml.safe_load((_TREE_DIR / name).read_text(encoding="utf-8"))
        n = len(data["nodes"])
        assert n == count, f"{name} 节点数 {n} != {count}"
        assert n < 30, f"{name} 超 30 节点红线"
        total += n
    assert total == 81 == len(_NODES), f"合并总数 {total} != 81"
    print(f"OK - 域文件组织：7 文件 {dict(expected)} 合计 81（每域 <30）")


def test_metadata_tree_cross_consistency():
    """元数据×树相容：不相容组合必须在豁免表；豁免必须有实例（防腐化）"""
    tree = get_tree()
    exempted_seen = set()
    for bk, dk, ct in itertools.product(_BEHAVIORS, _DBS, _CTS):
        tool_name = tree.route(bk, dk, ct)
        meta = _tools.get(tool_name)
        if not meta or not meta.intent_tags:
            continue  # unsupported_op 无标注，跳过
        tags = set(meta.intent_tags)
        if bk in tags and (not dk or dk in tags):
            continue  # 相容
        assert tool_name in _DRIFT_EXEMPTIONS, \
            f"新漂移：({bk},{dk},{ct!r}) → {tool_name}（tags={sorted(tags)}），" \
            f"修标注或加豁免（带理由）"
        exempted_seen.add(tool_name)
    missing = set(_DRIFT_EXEMPTIONS) - exempted_seen
    assert not missing, f"豁免腐化（树已变，这些工具不再漂移）: {missing}"
    n = len(_BEHAVIORS) * len(_DBS) * len(_CTS)
    print(f"OK - 交叉一致性：{n} 组合枚举，{len(exempted_seen)} 个豁免工具全部有实例")


def test_primary_path_per_tool():
    """本职路径：每个树叶子工具至少一条组合与 intent_tags 完全相容"""
    tree = get_tree()
    compatible = {name: False for name in
                  (n["tool"] for n in _NODES.values() if "tool" in n)}
    for bk, dk, ct in itertools.product(_BEHAVIORS, _DBS, _CTS):
        tool_name = tree.route(bk, dk, ct)
        meta = _tools.get(tool_name)
        if not meta or not meta.intent_tags:
            compatible[tool_name] = True  # 无标注（unsupported_op）视为自足
            continue
        tags = set(meta.intent_tags)
        if bk in tags and (not dk or dk in tags):
            compatible[tool_name] = True
    bad = [n for n, ok in compatible.items() if not ok]
    assert not bad, f"工具标注与树路由完全无交集（标注整体写错？）: {bad}"
    print(f"OK - 本职路径：{len(compatible)} 个叶子工具均有相容路由组合")


def test_requires_table_annotation():
    """requires_table 标注锁定 + find_tools 三态筛选"""
    true_tools = {n for n, t in _tools.items() if t.requires_table}
    # 免表：库级/模板/会话/文件/建新表
    false_expected = {"list_databases", "list_selections", "list_templates",
                      "search_documents", "list_vector_collections",
                      "batch_create_tables", "create_standard_tables",
                      "save_template", "import_template", "drop_template",
                      "process_file", "upload_file",
                      "clear_db", "clear_session", "unsupported_op"}
    assert not (false_expected & true_tools), \
        f"这些工具不应 requires_table: {false_expected & true_tools}"
    # 需表抽查：记录级三件套 + DDL 单表
    for n in ("query", "insert_data", "edit_data", "delete_data",
              "drop_table", "add_column", "create_index"):
        assert _tools[n].requires_table, f"{n} 应 requires_table"
    # find_tools 三态
    only_true = find_tools(requires_table=True)
    only_false = find_tools(requires_table=False)
    all_tools = find_tools()
    assert len(only_true) + len(only_false) == len(all_tools)
    assert all(t.requires_table for t in only_true)
    # 组合筛选：无表上下文白名单 = 免表只读（统一循环场景预演）
    readonly_no_table = find_tools(risk_level="readonly", requires_table=False)
    names = {t.name for t in readonly_no_table}
    assert names == {"list_databases", "list_selections", "list_templates",
                     "search_documents", "list_vector_collections"}, names
    print(f"OK - requires_table：需表 {len(only_true)} / 免表 {len(only_false)}，"
          f"无表只读白名单 {len(names)} 个")


def test_key_routes_locked():
    """关键路由抽样锁定（防域文件归集错位）"""
    tree = get_tree()
    cases = [
        ("查", "记录", "", "query"), ("查", "关联", "", "join_query"),
        ("查", "统计", "", "aggregate_query"), ("查", "选择集", "", "list_selections"),
        ("查", "数据库", "", "list_databases"), ("查", "文件", "", "search_documents"),
        ("增", "记录", "", "insert_data"), ("增", "记录", "批量", "batch_insert_data"),
        ("增", "表", "", "batch_create_tables"), ("增", "表", "标准", "create_standard_tables"),
        ("增", "字段", "", "add_column"), ("增", "索引", "", "create_index"),
        ("改", "记录", "", "edit_data"), ("改", "字段", "非空", "set_not_null"),
        ("改", "精度", "", "alter_precision"),
        ("删", "记录", "", "delete_data"), ("删", "表", "", "drop_table"),
        ("删", "索引", "", "drop_index"), ("删", "会话", "", "clear_session"),
        ("导入", "文件", "", "process_file"), ("导入", "模板", "", "import_template"),
        ("上传", "", "", "upload_file"), ("导出", "记录", "", "export_data"),
    ]
    for bk, dk, ct, want in cases:
        got = tree.route(bk, dk, ct)
        assert got == want, f"({bk},{dk},{ct!r}): {got} != {want}"
    print(f"OK - 关键路由：{len(cases)} 条核心组合精确锁定")


if __name__ == "__main__":
    test_domain_files_layout()
    test_metadata_tree_cross_consistency()
    test_primary_path_per_tool()
    test_requires_table_annotation()
    test_key_routes_locked()
    print("\n✅ 层 26 全部通过：3.3 树模块化 + 元数据交叉一致性"
          "（域分布/相容+豁免防腐化/本职路径/requires_table/关键路由）")
