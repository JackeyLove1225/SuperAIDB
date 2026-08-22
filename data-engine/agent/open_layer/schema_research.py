"""schema 研究（OODA）——建库流程的设计阶段

体系B 骨架（单目标研究循环）+ 体系A 纪律（事实→判断→验证→有限修正）：

1. 观察：代码采集事实——多点采样 + 列基数统计（distinct 计数）。
   AI 不再只凭首页 2500 字符猜实体，而是先看到"每列有多少不同值"的硬数据。
2. 分析：AI 基于事实+样本提出 schema 设计（含实体关系声明）。
3. 验证：代码把声明回查数据——
   - 声明 1:N 拆分 → 源数据必须存在"重复出现的键列"（1 < distinct < 行数）
   - 只设计 1 张表 → 源数据不存在多个强重复实体列（否则判"疑似未拆分"）
   验证不过 → 带 verdict 让 AI 修正（最多 MAX_DESIGN_ROUNDS 轮）。
4. 全程日志：每轮事实、AI 声明、验证 verdict 均可审计。

机器不可读的格式（PDF/图片等）降级：跳过基数统计与验证，只靠样本+人工确认兜底，
并在日志中明确标注。
"""

import json
from core.logger import get_logger
from pathlib import Path

logger = get_logger(__name__)

MAX_DESIGN_ROUNDS = 2  # 验证失败后最多让 AI 修正的轮数


# ═══════════════════════════════════════════════════════════════
# 观察：列基数统计（代码采集事实，可单测）
# ═══════════════════════════════════════════════════════════════

def compute_cardinality_facts(server_path: str, max_rows: int = 5000) -> dict:
    """计算机读文件的列基数事实

    Returns:
        {
          "machine_readable": bool,
          "total_rows": int,
          "columns": [{"name", "distinct", "non_null", "distinct_ratio", "samples"}],
          "note": str,          # 不可机读时的说明
        }
    """
    path = Path(server_path)
    ext = path.suffix.lower()
    if ext not in (".xlsx", ".xls", ".csv"):
        return {"machine_readable": False, "total_rows": 0, "columns": [],
                "note": f"{ext} 不支持机读统计，跳过基数验证"}

    try:
        header, rows = _read_tabular(path, ext, max_rows)
    except Exception as e:
        return {"machine_readable": False, "total_rows": 0, "columns": [],
                "note": f"机读失败: {e}"}

    if not header or not rows:
        return {"machine_readable": True, "total_rows": 0, "columns": [], "note": "空表"}

    total = len(rows)
    columns = []
    for ci, col_name in enumerate(header):
        if not col_name:
            continue
        values = []
        for r in rows:
            if ci < len(r) and r[ci] not in (None, ""):
                values.append(str(r[ci]))
        distinct_vals = set(values)
        columns.append({
            "name": str(col_name),
            "distinct": len(distinct_vals),
            "non_null": len(values),
            "distinct_ratio": round(len(distinct_vals) / total, 3) if total else 0,
            "samples": sorted(distinct_vals)[:5],
        })

    return {"machine_readable": True, "total_rows": total, "columns": columns, "note": ""}


def _read_tabular(path: Path, ext: str, max_rows: int):
    """读取表格文件，返回 (header, rows)。xlsx 用 read_only 模式防大文件撑内存"""
    if ext == ".csv":
        import csv as _csv
        with open(path, encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = _csv.reader(f)
            header = next(reader, [])
            rows = [r for _, r in zip(range(max_rows), reader)]
        return header, rows
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    all_rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= max_rows + 1:
            break
        all_rows.append(list(row))
    wb.close()
    if not all_rows:
        return [], []
    return all_rows[0], all_rows[1:]


def format_facts_for_prompt(facts_list: list) -> str:
    """把列基数事实格式化为 prompt 文本（facts_list: [{path, facts}]）"""
    parts = []
    for item in facts_list:
        path, facts = item["path"], item["facts"]
        if not facts.get("machine_readable"):
            parts.append(f"--- {path}：{facts.get('note', '不可机读')} ---")
            continue
        lines = [f"--- {path}：共 {facts['total_rows']} 行数据，各列基数如下 ---"]
        for c in facts["columns"]:
            lines.append(
                f"  {c['name']}: 不同值 {c['distinct']} 个"
                f"（占行数 {c['distinct_ratio']:.0%}），示例 {c['samples'][:3]}"
            )
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else "（无可用统计）"


# ═══════════════════════════════════════════════════════════════
# 验证：实体关系声明回查数据（代码验证 AI 判断）
# ═══════════════════════════════════════════════════════════════

def verify_schema_claims(schema: dict, facts_list: list) -> list:
    """验证 schema 设计声明，返回问题列表（空=全部通过）

    规则（代码可判定的硬事实）：
    0a. 外键引用的表必须存在于设计中（结构规则，与数据无关，始终执行）
    0b. 外键 ref_columns 必须且只能是 ["id"]——引用表自增主键；
        禁止引用业务编号列（如 farm_id/project_id），否则生成后校验必拦截
    1. 声明了 1:N 拆分（存在外键）→ 源数据必须存在重复键列
       （某列 1 < distinct < total_rows），否则拆分无数据支持
    2. 只设计了 1 张表 → 源数据中不应存在 ≥2 个强重复实体列
       （distinct_ratio < 0.5 且 distinct >= 3），否则判"疑似未拆分"
    """
    issues = []
    tables = schema.get("tables", [])
    table_names = {t.get("name") for t in tables}

    # 结构硬规则（与数据无关，无文件样本也必须执行）
    for t in tables:
        for fk in t.get("foreign_keys", []):
            if fk.get("references") not in table_names:
                issues.append(
                    f"表 {t.get('name')} 的外键引用了不存在的表 {fk.get('references')}"
                )
            ref_cols = fk.get("ref_columns") or []
            if ref_cols != ["id"]:
                issues.append(
                    f"表 {t.get('name')} 的外键 {fk.get('columns')} 引用列是 {ref_cols}，"
                    '必须改为 ["id"]（引用表自增主键）；外键字段名保持"引用表_id"不变即可'
                )

    # 汇总所有机读文件的列
    all_columns = []
    total_rows = 0
    for item in facts_list:
        facts = item["facts"]
        if facts.get("machine_readable"):
            all_columns.extend(facts["columns"])
            total_rows = max(total_rows, facts["total_rows"])
    machine_readable = bool(all_columns)

    if not machine_readable:
        logger.info("schema 验证: 文件不可机读，跳过数据验证（人工确认兜底）")
        return issues

    has_fk = any(t.get("foreign_keys") for t in tables)
    repeated_cols = [c for c in all_columns if 1 < c["distinct"] < total_rows]
    strong_entity_cols = [c for c in all_columns
                          if c["distinct"] >= 3 and c["distinct_ratio"] < 0.5]

    # 规则 1：声明 1:N 必须有重复键列支持
    if has_fk and not repeated_cols:
        issues.append(
            f"声明了外键关联（1:N 拆分），但源数据所有列都不同值或全相同"
            f"（共 {total_rows} 行），不存在可作为关联键的重复列，拆分缺乏数据支持"
        )

    # 规则 2：单表设计但存在多个强重复实体列 → 疑似未拆分
    if len(tables) == 1 and len(strong_entity_cols) >= 2:
        names = [c["name"] for c in strong_entity_cols]
        issues.append(
            f"只设计了 1 张表，但源数据存在 {len(strong_entity_cols)} 个强重复实体列 {names}，"
            f"疑似还有未拆分的实体（宽表镜像嫌疑）"
        )

    return issues


# ═══════════════════════════════════════════════════════════════
# OODA 主循环
# ═══════════════════════════════════════════════════════════════

_RESEARCH_PROMPT = """你是数据库结构设计专家（研究模式）。用户希望建成可查询的关系数据库。

【用户的目标描述】（设计的最高依据：无文件样本时必须严格围绕该领域设计，
禁止臆造与描述无关的业务场景——例如用户说"建筑工程"就必须围绕建筑工程的实体）：
{user_intent}

【代码采集的列基数事实】（这是硬数据，你的设计必须与之相符）：
{facts_text}

【文件清单】：
{manifest_text}

【代表性内容样本】：
{samples_text}
{issues_section}
设计规则：
1. 识别业务实体，每个实体一张表；源数据是宽表（一行混合多种实体）时必须拆成主表+明细表
2. 有基数事实时：某列 distinct 接近总行数则不是外键候选；distinct 明显小于总行数（重复出现）则很可能是主表关联键
   无基数事实（用户未提供文件）时：以【用户的目标描述】中的业务领域为准设计实体，并在 rationale 中如实说明"基于领域描述设计，上传文件后可按真实数据修正"
3. 表名用英文 snake_case 且必须是复数形式（如 projects/materials，禁止 project/material 单数）；
   业务名用中文；字段类型只用 TEXT/INTEGER/FLOAT；不要 id 字段
4. 外键字段必须是 INTEGER 且能体现引用关系（字段名用 {{引用表名单数}}_id 形式——
   引用表若也是复数，取其单数形式，如 carriers → carrier_id）；
   外键的 ref_columns 必须且只能是 ["id"]（引用表自增主键），禁止引用业务编号列
5. 清单/说明类文件不建表；表数量 2-8 张，宁精勿滥
6. 基数事实与样本冲突时，以基数事实为准
7. 每个字段（包括各表都有的 id 主键）都必须给出中文 business_name

只返回 JSON：
{{
    "tables": [
        {{
            "name": "英文表名",
            "business_name": "中文业务名",
            "description": "表的业务描述",
            "columns": [{{"name": "字段名", "type": "TEXT|INTEGER|FLOAT", "business_name": "中文字段名"}}],
            "foreign_keys": [{{"columns": ["外键字段"], "references": "引用表名", "ref_columns": ["id"]}}]
        }}
    ],
    "rationale": "设计理由（一段话，引用基数事实或领域描述说明拆分依据）"
}}"""


def research_schema(manifest_text: str, samples: list, facts_list: list,
                    feedback: str, llm, user_intent: str = "") -> dict:
    """OODA 研究循环：事实 → AI 设计 → 代码验证 → （不通过则修正）

    Args:
        manifest_text: 文件清单文本
        samples: 探索阶段样本 [{path, category, sample}]
        facts_list: [{path, facts}] 列基数统计（compute_cardinality_facts 的输出）
        feedback: 人工确认环节拒绝时的意见（重新设计时非空）
        llm: ChatOpenAI 实例
        user_intent: 用户的目标描述（无文件样本时的设计依据，防止 AI 臆造无关场景）

    Returns:
        schema dict（含 tables/rationale）；若多轮验证仍未过，返回最后一版
        并附 _verification_issues 供人工确认时参考
    """
    facts_text = format_facts_for_prompt(facts_list)
    samples_text = "\n\n".join(
        f"--- {s.get('path', '?')} ({s.get('category', '')}) ---\n{s.get('sample', '')[:2500]}"
        for s in samples
    ) or "（无样本）"

    issues_feedback = ""
    if feedback:
        issues_feedback = f"\n【用户对上一版的不满意之处（请据此调整）】\n{feedback}\n"

    last_schema = {"tables": [], "rationale": "未产出设计"}
    last_issues = []

    for rnd in range(1 + MAX_DESIGN_ROUNDS):
        logger.info("schema 研究: 第 %d 轮设计", rnd + 1)
        prompt = _RESEARCH_PROMPT.format(
            user_intent=user_intent or "（未提供——仅按文件与基数事实设计）",
            facts_text=facts_text,
            manifest_text=manifest_text or "（无）",
            samples_text=samples_text,
            issues_section=issues_feedback,
        )
        response = llm.invoke(prompt)
        content = response.content if hasattr(response, "content") else str(response)
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            schema = json.loads(content)
            if not isinstance(schema.get("tables"), list):
                raise ValueError("tables 必须是数组")
        except Exception as e:
            logger.warning("schema 研究: 第 %d 轮 JSON 解析失败: %s", rnd + 1, e)
            issues_feedback = f"\n【上一版输出无法解析为 JSON，请严格按格式输出】\n"
            continue

        last_schema = schema
        table_names = [t.get("name") for t in schema.get("tables", [])]
        logger.info("schema 研究: 第 %d 轮产出 %d 张表 %s", rnd + 1, len(table_names), table_names)

        # 代码验证（体系A 纪律：AI 判断必须过事实校验）
        issues = verify_schema_claims(schema, facts_list)
        if not issues:
            logger.info("schema 研究: 第 %d 轮验证通过 ✓", rnd + 1)
            schema.pop("_verification_issues", None)
            return schema

        last_issues = issues
        for issue in issues:
            logger.warning("schema 研究: 验证问题 — %s", issue)
        issues_feedback = (
            "\n【代码验证发现上一版的问题，请修正】\n"
            + "\n".join(f"- {i}" for i in issues) + "\n"
        )

    # 多轮仍未通过：返回最后一版，附问题清单供人工确认裁决
    logger.warning("schema 研究: %d 轮后仍未通过验证，提交人工确认裁决", 1 + MAX_DESIGN_ROUNDS)
    last_schema["_verification_issues"] = last_issues
    return last_schema
