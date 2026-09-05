"""查询结果排版（自 db_chat 抽出的公共排版实现）

多表/单表 Markdown 排版曾是 DBChat._format_multi_table 私有方法，
data_ops（3 处）/join_executor/tools.query 寄生调用（私有跨境 + 互耦源）。
收敛于此：纯函数，零 DBChat 依赖；DBChat._format_multi_table 保留为
薄委托（测试 mock 面不动）。
"""
from core.logger import get_logger

logger = get_logger(__name__)


def format_multi_table(results: dict, table_map: dict | None = None,
                       field_names: dict | None = None) -> str:
    """格式化多表查询结果为 Markdown 表格文本。

    Args:
        results: {表名: [行 dict]}
        table_map: {表名: schema}——display 排版配置来源；
            None 时经 schema_matcher.load_schemas 现取（与历史行为一致）
        field_names: {英文字段: 中文名} 别名映射——仅 DBChat.set_schema
            路径有真实映射；联邦/工具路径历史上恒空（保持原语义，不在
            本次收敛中夹带行为变更）
    """
    if table_map is None:
        from core.schema_matcher import load_schemas
        table_map = {t["name"]: t for t in load_schemas()}
    field_names = field_names or {}

    def cn(col):
        return field_names.get(col, col)

    out = []
    for table_name, rows in results.items():
        if not rows:
            out.append(f"**{table_name}**\n\n暂无数据")
            continue
        cols = list(rows[0].keys())
        # 空列保护：行非空但字段为空（_select_fields 回退失败的兜底）
        if not cols:
            out.append(f"**{table_name}**\n\n共 {len(rows)} 条记录（字段信息缺失）")
            continue

        # 单列 → MD 表格
        if len(cols) == 1:
            vals = [str(r[cols[0]]) for r in rows if r.get(cols[0]) is not None]
            if vals:
                out.append("")
                out.append(f"**表格：{table_name}**")
                out.append("")
                out.append("| 序号 | " + cn(cols[0]) + " |")
                out.append("|------|---|")
                for i, r in enumerate(rows):
                    out.append(f"| {i+1} | " + str(r.get(cols[0], "")) + " |")
                out.append("")
            out.append("")
            out.append("> 共 " + str(len(rows)) + " 条记录")
            continue

        # 多列 → Markdown 表格（排版策略读 schema YAML 的 display 配置）
        disp = (table_map.get(table_name) or {}).get("display", {}) or {}
        if disp.get("layout") == "kv":
            # KV 布局：每行一条记录、字段竖排（宽表/主表适用）
            for r in rows:
                out.append("")
                out.append("| 字段 | 值 |")
                out.append("| --- | --- |")
                for c in cols:
                    v = r.get(c)
                    if v is not None and str(v).strip() and str(v).strip() != "None":
                        out.append(f"| {cn(c)} | {v} |")
                out.append("")
        else:
            # 多列表格布局（隐藏列读 display.hide_columns，默认只隐藏 id）
            hidden = set(disp.get("hide_columns", ["id"]))
            headers = [cn(c) for c in cols if c not in hidden]
            out.append("")
            out.append(f"**表格：{table_name}**")
            out.append("")
            out.append("| 序号 | " + " | ".join(headers) + " |")
            out.append("|------|" + " | ".join(["---"] * len(headers)) + " |")
            for i, r in enumerate(rows):
                vals = [str(r.get(c, "") or "") for c in cols if c not in hidden]
                out.append(f"| {i+1} | " + " | ".join(vals) + " |")
            out.append("")

    return "\n".join(out).strip() or "没有找到匹配的数据"
