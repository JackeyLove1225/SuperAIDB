"""语义路由层：AI 举证 + 代码双向验证选表（证据闭环，防幻觉）"""
import json as _json
from core.logger import get_logger

logger = get_logger(__name__)

def route_tables(config, sample_text: str, ai) -> tuple:
    """语义路由（举证+代码验证）：AI 结合各表业务描述与文件内容选择提取目标表

    白盒设计（证据闭环）：
    1. AI 必须为每个选中的表举证：匹配到的文本片段（evidence_text）和对应字段（evidence_field）
    2. 代码做两道事实校验（不依赖 AI 自觉）：
       - evidence_text 必须真实出现在文件样本中（防幻觉证据）
       - evidence_field 必须真实存在于该表字段/业务名中（防编造字段）
    3. 未选中但字段名/业务名出现在样本中的表 → 记"疑似漏选"日志（防漏提信号）
    4. 全部 verdict 写日志，可审计可排错

    Returns: (routed: set, reason: str)
    失败或全部证据不成立时降级为全部表（宁可多提不漏提）。
    """

    table_lines = []
    for t in config.tables:
        cols = "、".join(
            f"{c['name']}({c.get('business_name') or c.get('description', c['name'])})"
            for c in t.get("columns", [])[:10] if c["name"] != "id"
        )
        biz = t.get("business_name", "")
        desc = t.get("description", "")
        table_lines.append(
            f"- {t['name']}（{biz}）{('— ' + desc) if desc else ''}\n  字段: {cols}"
        )

    prompt = f"""你是数据录入路由专家。请判断这份文件的内容应该录入到数据库的哪些表中。

数据库中的表及其业务含义：
{chr(10).join(table_lines)}

文件内容样本：
{sample_text[:2000]}

规则：
- 只选择与样本内容业务相关的表，无关的表不要选
- 必须为每个选中的表举证：evidence_text=样本中真实出现的原文片段（≤20字），
  evidence_field=该表中与内容对应的字段名
- 如果样本与所有表都无关，tables 返回空列表
- 返回 JSON：
{{"tables": [{{"name": "表名", "evidence_text": "原文片段", "evidence_field": "字段名"}}],
  "reason": "一句话说明选择理由"}}
只返回 JSON。"""

    all_names = {t["name"] for t in config.tables}
    # 表字段索引（业务名也算合法字段引用）
    table_fields = {}
    for t in config.tables:
        fields = set()
        for c in t.get("columns", []):
            fields.add(c["name"])
            if c.get("business_name"):
                fields.add(c["business_name"])
        table_fields[t["name"]] = fields

    try:
        content = ai.chat(
            "你是数据录入路由专家，擅长理解表结构业务含义并匹配文件内容。只返回 JSON。",
            prompt,
        )
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = _json.loads(content)
    except Exception as e:
        logger.warning("语义路由失败（%s），降级为全表提取", e)
        return all_names, f"降级: 路由异常 {e}"

    reason = result.get("reason", "")
    verified = set()
    # 全角/空白归一化：PDF 等真实文档常含全角字符（定 额 编 号／Ａ１）与多余空白，
    # AI 举证通常是半角紧凑形式，直接子串匹配会误判证据不存在
    def _norm(s: str) -> str:
        out = []
        for ch in str(s):
            o = ord(ch)
            out.append(chr(o - 0xFEE0) if 0xFF01 <= o <= 0xFF5E else ch)
        return "".join(out).lower().replace(" ", "").replace("　", "")
    sample_lower = _norm(sample_text)
    for item in result.get("tables", []):
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        if name not in all_names:
            logger.warning("语义路由: 表 %s 不在库中，丢弃（AI 幻觉表名）", name)
            continue
        # 证据校验 1：evidence_text 必须真实出现在样本中
        ev_text = str(item.get("evidence_text", "")).strip()
        ev_ok_text = bool(ev_text) and _norm(ev_text) in sample_lower
        # 证据校验 2：evidence_field 必须真实存在于该表字段/业务名中
        ev_field = str(item.get("evidence_field", "")).strip()
        ev_ok_field = (not ev_field) or (ev_field in table_fields.get(name, set()))
        if ev_ok_text and ev_ok_field:
            verified.add(name)
            logger.info("语义路由: 选中 %s（证据: '%s' → 字段 %s ✓）", name, ev_text[:20], ev_field)
        else:
            logger.warning(
                "语义路由: 丢弃 %s（AI 误判：证据未通过验证 text✓=%s field✓=%s）",
                name, ev_ok_text, ev_ok_field)

    if not verified:
        logger.warning("语义路由: 无有效选中（%s），降级为全表提取", reason)
        return all_names, f"降级: {reason}"

    # 疑似漏选信号：未被选中的表，其字段名/业务名出现在样本中
    for t in config.tables:
        name = t["name"]
        if name in verified:
            continue
        hits = [f for f in table_fields.get(name, set())
                if len(f) >= 2 and f.lower() in sample_lower]
        if hits:
            logger.warning("语义路由: 疑似漏选 %s（样本中命中字段 %s）", name, hits[:5])

    logger.info("语义路由: 最终选中 %s — %s", sorted(verified), reason)
    return verified, reason


# 兼容别名（runner 门面再导出等存量引用；新代码用 route_tables）
_route_tables = route_tables
