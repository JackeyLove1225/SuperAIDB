"""统一中间格式提取与 schema 自描述映射（通用提取管线核心）

设计（替代 fc_schema 的行业提示词路径）：
1. extract_intermediate：AI 把任意文字结构化为行业无关的统一中间格式
   {kv: {键:值}, tables: [{headers, rows}], prose: [...]}——没有"票据/清单"类型概念，
   只有键值对/表格/纯文字三种封闭结构
2. map_to_schemas：中间格式 → 行业表。不做语义大模型调用，靠
   business_name/字段名/术语词典别名 的确定性匹配（白盒可审计）
3. 映射规则存档：mapping_rules.yml 存"源键→表.字段"的确认结果，
   命中即 100% 置信，实现"确认一次，之后自动"
"""
import json
from core.logger import get_logger
from pathlib import Path

import yaml

from pipeline.constants import (
    TIER2_BATCH_UNITS, TIER2_OVERLAP_UNITS,
    PIPELINE_MAX_LLM_CALLS, PIPELINE_MAX_TOKENS_PER_CALL,
)

logger = get_logger(__name__)

# ── 统一中间格式提取（行业无关，单次 LLM 调用）──

_UNIFIED_PROMPT = """你是数据结构化引擎。把下面的文本结构化为统一中间格式 JSON。

只返回 JSON（不要 markdown 代码块），结构固定为：
{{
  "kv": {{ "键": "值" }},
  "tables": [{{ "headers": ["列1", "列2"], "rows": [["值1", "值2"]] }}],
  "prose": ["纯叙述句1", "纯叙述句2"]
}}

规则：
1. kv：文本中"键: 值"形式的事实（如 发票号: 123、供应商: XX公司）。没有则为 {{}}
2. tables：文本中的表格（含类表格的对齐数据），headers 用原文表头，
   rows 是字符串数组的数组，不要类型转换（数字也保持字符串）
3. prose：不属于 kv 和表格的纯叙述文字（保留原句，供向量检索）
4. 忠实原文，不脑补、不改写、不补全缺失值（缺失用 ""）
5. 转置布局必须先转置再输出：若首列是属性名、每列是一条记录（如清单表：
   首行是编号、下面每行是一个费用项），转成"每行一条记录、首行是属性名"的标准布局，
   headers 放属性名、rows 每行一条记录；colspan/rowspan 展开的重复单元格要去重
6. 一个表格区域含多段不同结构时（如上半是项目属性、下半是材料明细），
   拆成多个独立 table 分别输出，各自的 headers 只含本段属性名
7. 明细类数据（材料/人工/机械等从属于主记录的行）每行必须带所属主记录的标识列
   （如 订单编号），并按主记录逐条展开——同一材料属于多个主记录时每个主记录各一行
{targets}
【文本】
{text}"""


def _targets_hint(schemas: list) -> str:
    """目标表提示（表级业务名 + 列业务名，帮 AI 对齐拆分粒度——
    输出仍是统一中间格式，字段名不改写，确定性映射不受影响）"""
    if not schemas:
        return ""
    lines = ["8. 本次目标表（按业务语义把表格区域拆到这些表；每行都必须带主记录标识列的值）："]
    for t in schemas:
        bn = t.get("business_name") or t.get("name", "")
        cols = "、".join(str(c.get("business_name") or c.get("name", ""))
                         for c in (t.get("columns") or [])[:10] if not c.get("pk"))
        lines.append(f"   - {bn}（列：{cols}）")
    return "\n".join(lines) + "\n"

# 提取失败的有限重试次数（空响应/JSON 非法多为偶发，重试一次即可大部分恢复）
_MAX_ATTEMPTS = 2


def extract_intermediate(text: str, ai, schemas: list = None) -> dict:
    """AI 把一批文字结构化为统一中间格式（kv/tables/prose，行业无关）

    schemas：可选目标表定义（仅作表级拆分提示，输出仍是统一中间格式）
    失败语义（不静默丢批）：
    - 有限重试 _MAX_ATTEMPTS 次（空响应/JSON 非法都按失败重试）
    - 仍失败则返回带 "_error" 键的结构，由调用方显性上报（哪些页数据未入库）
    """
    if not text or not text.strip():
        return {"kv": {}, "tables": [], "prose": []}
    prompt = _UNIFIED_PROMPT.format(text=text[:60000],
                                    targets=_targets_hint(schemas or []))
    last_err = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            content = ai.chat("你是数据结构化引擎，严格按 JSON 输出。",
                              prompt,
                              max_tokens=PIPELINE_MAX_TOKENS_PER_CALL)
            content = (content or "").strip()
            if not content:
                raise ValueError("AI 返回空内容")
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(content)
            return {
                "kv": data.get("kv") or {},
                "tables": data.get("tables") or [],
                "prose": data.get("prose") or [],
            }
        except Exception as e:
            last_err = str(e)[:120]
            logger.warning("统一格式提取失败（第 %d/%d 次）: %s", attempt, _MAX_ATTEMPTS, last_err)
    return {"kv": {}, "tables": [], "prose": [], "_error": last_err}


# ── 映射规则存档（确认一次，之后自动）──

def _rules_path(industry: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "industries" / industry / "config" / "mapping_rules.yml"


def load_mapping_rules(industry: str) -> dict:
    """读取存档映射规则 {源键: {"table": 表, "field": 字段}}"""
    p = _rules_path(industry)
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_mapping_rule(industry: str, source_key: str, table: str, field: str) -> None:
    """存档一条映射规则（用户确认过的"源键 → 表.字段"）"""
    from core.config_hub import write_yaml_atomic
    rules = load_mapping_rules(industry)
    rules[source_key] = {"table": table, "field": field}
    write_yaml_atomic(_rules_path(industry), rules, backup=False)
    logger.info("映射规则存档: %s → %s.%s（行业 %s）", source_key, table, field, industry)


# ── schema 自描述映射（确定性，白盒）──

def _field_index(schemas: list, aliases: dict) -> list:
    """构建字段索引：[{table, field, keys:{匹配词集}}]

    匹配词 = 字段英文名 + 中文业务名 + 词典别名（小写归一）

    pk 系统主键（如 id）不进索引：主键由系统生成，写入层本就禁止手动指定，
    映射层更不该把它作为目标——源数据里的任何键都不允许映射到主键。
    """
    idx = []
    for s in schemas:
        tname = s.get("name", "")
        table_alias = aliases.get(tname, []) or []
        for c in s.get("columns", []):
            if c.get("pk"):
                continue
            keys = {_norm_key(c.get("name", ""))}
            bn = _norm_key(c.get("business_name", ""))
            if bn:
                keys.add(bn)
            for a in table_alias:
                keys.add(_norm_key(a))
            keys.discard("")
            idx.append({"table": tname, "field": c.get("name", ""), "keys": keys})
    return idx


def _norm_key(s: str) -> str:
    """匹配键归一：去全部空白（OCR/PDF 常把 '订单编号' 识成 '订 单 编 号'）
    + 去尾括号单位（'全费用(元)' → '全费用'，表头带单位是通例）+ 小写"""
    import re as _re
    s = _re.sub(r"[（(][^）)]*[）)]\s*$", "", s or "")
    return "".join(s.split()).lower()


def _weak_candidates(key: str, field_idx: list, middle_ok: bool = False) -> list:
    """弱匹配候选：
    - 默认（前后缀）：中文业务词按前缀/后缀组合（'全费用'是'全费用基价'的前缀、
      '名称'是'材料名称'的后缀）——可信度高，允许全局直配
    - middle_ok=True 时追加中间子串候选（'品名'⊂'商品品名'）——歧义大，
      只允许在已选定表的上下文里用，不做全局直配（防 '费用'→'全费用基价' 误配）
    """
    k = _norm_key(key)
    if not k or len(k) < 2:
        return []
    cand = []
    for f in field_idx:
        hit = False
        for w in f["keys"]:
            if w.startswith(k) or w.endswith(k) or (len(w) >= 2 and w in k):
                hit = True
                break
            if middle_ok and len(k) >= 2 and len(w) >= 2 and k in w:
                hit = True
                break
        if hit:
            cand.append(f)
    return cand


def _match_key(key: str, field_idx: list, rules: dict) -> tuple:
    """单个源键 → (table, field, confidence)

    confidence: 1.0=存档规则/精确命中, 0.7=弱匹配唯一候选, 0=未命中/歧义, -1=存档忽略
    歧义（'单价'→材料单价/日工资单价）不猜，返回未命中，由表上下文或用户确认消解
    """
    if key in rules:
        r = rules[key]
        if r.get("ignore"):
            return None, None, -1
        return r.get("table"), r.get("field"), 1.0
    k = _norm_key(key)
    if not k:
        return None, None, 0
    for f in field_idx:
        if k in f["keys"]:
            return f["table"], f["field"], 1.0
    cand = _weak_candidates(key, field_idx)
    fields = {(c["table"], c["field"]) for c in cand}
    if len(fields) == 1:
        return cand[0]["table"], cand[0]["field"], 0.7
    return None, None, 0


def suggest_mapping(key: str, field_idx: list) -> tuple:
    """为未映射键找最近候选字段（引导用户确认的猜测）

    返回 (table, field, score)；无合适候选返回 (None, None, 0)
    """
    import difflib
    k = _norm_key(key)
    if not k:
        return None, None, 0
    best = (None, None, 0.0)
    for f in field_idx:
        for w in f["keys"]:
            if not w:
                continue
            score = difflib.SequenceMatcher(None, k, w).ratio()
            if score > best[2]:
                best = (f["table"], f["field"], score)
    if best[2] >= 0.6:
        return best[0], best[1], round(best[2], 2)
    return None, None, 0


def map_to_schemas(intermediate: dict, schemas: list, aliases: dict = None,
                   rules: dict = None) -> dict:
    """统一中间格式 → 行业表行（{"tables": [{"name", "rows"}], "unmapped": [...]}）

    - kv：按键覆盖度选最佳表（主表语义），每键映射一字段；未命中键进 unmapped
    - tables：按表头覆盖度选最佳表（≥50% 字段命中才映射，否则整张进 unmapped）
    - 存档规则命中的键以 100% 置信直接采用
    """
    aliases = aliases or {}
    rules = rules or {}
    field_idx = _field_index(schemas, aliases)
    out_tables: dict[str, list] = {}
    unmapped: list[dict] = []

    # kv：逐键映射，按命中数选最佳表
    kv = intermediate.get("kv") or {}
    if kv:
        per_table: dict[str, dict] = {}
        kv_unmapped = []
        for k, v in kv.items():
            t, f, conf = _match_key(k, field_idx, rules)
            if conf == -1:
                continue  # 存档忽略：静默跳过（用户已确认不要这个键）
            if f:
                per_table.setdefault(t, {})[f] = v
            else:
                kv_unmapped.append({"kind": "kv", "key": k, "value": v})
        # kv 通常是一组同源事实：命中 ≥2 键的表才接受，其余退回 unmapped
        best_t, best_row = "", {}
        for t, row in per_table.items():
            if len(row) > len(best_row):
                best_t, best_row = t, row
        if len(best_row) >= 2:
            out_tables.setdefault(best_t, []).append(best_row)
            unmapped.extend(kv_unmapped)
        else:
            # 命中不足：整组 kv 全部退回 unmapped（不重复追加——kv_unmapped 已是 miss 子集）
            unmapped.extend({"kind": "kv", "key": k, "value": v} for k, v in kv.items())
    # tables：按表头覆盖度映射
    for it in intermediate.get("tables") or []:
        headers = it.get("headers") or []
        rows = it.get("rows") or []
        if not headers or not rows:
            continue

        def _distinct_hits(values):
            """一组源键命中的不同 (table, field) 数（用于表头/首列的方向判断）"""
            fs = set()
            for v in values:
                _t, _f, _c = _match_key(v, field_idx, rules)
                if _f:
                    fs.add((_t, _f))
            return fs

        # 转置检测（白盒兜底）：首列命中的不同字段数多于表头 →
        # "首列是属性名、每列一条记录"的转置布局，整体旋转后再映射。
        # 前提：非空表头必须互不相同——有重复表头的是 colspan 展开的不规则表，
        # 旋转会把表头文本当数据值产出垃圾行（宁可 unmapped 也不猜）
        h_hits = _distinct_hits(headers)
        fc_hits = _distinct_hits(r[0] for r in rows if r)
        _hn = [_norm_key(h) for h in headers if _norm_key(h)]
        headers_distinct = len(set(_hn)) == len(_hn)
        if headers_distinct and len(fc_hits) >= 2 and len(fc_hits) > len(h_hits):
            grid = [headers] + [list(r) for r in rows]
            width = max(len(g) for g in grid)
            grid = [g + [""] * (width - len(g)) for g in grid]
            t_grid = [list(col) for col in zip(*grid)]
            headers, rows = t_grid[0], t_grid[1:]
            logger.info("    检测到转置表格，已旋转为标准布局后映射")
            # 旋转后同名属性列去重（rowspan 展开，如"项目"分名称行/规格行）：
            # 保留首列（通常为主名称），其余列按未映射列如实列出
            seen_h, keep_idx, dropped = set(), [], []
            for _i, _h in enumerate(headers):
                _nk = _norm_key(_h)
                if _nk and _nk in seen_h:
                    dropped.append(_h)
                    continue
                seen_h.add(_nk)
                keep_idx.append(_i)
            if dropped:
                headers = [headers[_i] for _i in keep_idx]
                rows = [[r[_i] if _i < len(r) else "" for _i in keep_idx] for r in rows]
                for _h in dropped:
                    unmapped.append({"kind": "column", "key": _h,
                                     "reason": "rowspan_dup_column"})

        # 逐表上下文消解：候选表内逐列解歧（全局歧义的列在表内唯一即可配），
        # 以"解出的列占比"选表——严格 >0.5 才映射，杜绝恰半混合表蒙对；
        # 同表两列同字段（colspan 不规则）该表直接判 0 分
        direct = []  # [(src_idx, table, field)] 首轮精确/规则/全局唯一弱匹配
        for i, h in enumerate(headers):
            t, f, conf = _match_key(h, field_idx, rules)
            if conf == -1:
                continue  # 存档忽略
            if f:
                direct.append((i, t, f))
        cand_tables = []
        for t in {t for _, t, _ in direct}:
            field_of = {}  # src_idx -> field（T 上下文解出的列）
            claimed = set()
            dup = False
            for i, _t, f in direct:
                if _t != t:
                    continue
                if f in claimed:
                    dup = True
                claimed.add(f)
                field_of[i] = f
            for i, h in enumerate(headers):
                if i in field_of:
                    continue
                ctx = [c for c in _weak_candidates(h, field_idx, middle_ok=True)
                       if c["table"] == t and c["field"] not in claimed]
                if len({(c["table"], c["field"]) for c in ctx}) == 1:
                    field_of[i] = ctx[0]["field"]
                    claimed.add(ctx[0]["field"])
            score = 0 if dup else len(field_of) / len(headers)
            exact = sum(1 for _, _t, _f in direct if _t == t)
            cand_tables.append((score, exact, t, field_of))
        cand_tables.sort(key=lambda x: (-x[0], -x[1]))
        best = cand_tables[0] if cand_tables else None

        if best and best[0] > 0.5:
            _, _, best_t, field_of = best
            # 数值列类型表（全角数字归一用）：文本层 PDF 的数字常是全角（５５９８．８８），
            # 不归一会被 CHECK 约束以"非数值"拒收——映射层按 schema 类型统一 NFKC
            num_fields = {c2.get("name")
                          for s2 in schemas if s2.get("name") == best_t
                          for c2 in (s2.get("columns") or [])
                          if str(c2.get("type", "")).upper()
                          in ("FLOAT", "REAL", "INTEGER", "INT", "NUMERIC", "DECIMAL")}
            # 链接列保留：映射到其他表唯一键（业务编码）的列随行携带——
            # 丢弃它会让明细行失去分组锚点（下游按码分组+外键回填全靠它）
            uniq = {(s2.get("name"), c2.get("name"))
                    for s2 in schemas for c2 in (s2.get("columns") or []) if c2.get("unique")}
            link_of = {i: f for i, t, f in direct
                       if t != best_t and (t, f) in uniq}
            for r in rows:
                row = {}
                for i, f in field_of.items():
                    if i < len(r):
                        # 防御：同字段多列时不用空值覆盖已取到的非空值
                        if f in row and not str(r[i]).strip():
                            continue
                        row[f] = r[i]
                for i, f in link_of.items():
                    if i < len(r) and str(r[i]).strip():
                        row.setdefault(f, r[i])
                # 全角数字归一（仅数值列；文本列保持原样）
                for _nf in num_fields:
                    if _nf in row and isinstance(row[_nf], str):
                        import unicodedata as _ud
                        row[_nf] = _ud.normalize("NFKC", row[_nf]).strip()
                if any(str(v).strip() for v in row.values()):
                    out_tables.setdefault(best_t, []).append(row)
            lost = [h for i, h in enumerate(headers)
                    if i not in field_of and i not in link_of]
            for h in lost:
                unmapped.append({"kind": "column", "key": h, "from_table_headers": headers})
        else:
            # 覆盖不足/不规则/无命中：整张进 unmapped（保留源数据供确认后免 AI 补录）
            logger.info("    源表映射覆盖不足（最佳 %s %.0f%%），进 unmapped: %s",
                        best[2] if best else "-", (best[0] * 100) if best else 0,
                        str(headers)[:80])
            unmapped.append({"kind": "table", "headers": headers,
                             "rows": rows[:500], "row_count": len(rows),
                             "reason": "low_coverage"})

    return {
        "tables": [{"name": t, "rows": rows} for t, rows in out_tables.items()],
        "unmapped": unmapped,
    }


# ── 通用批处理生成器（与 batch_process 同契约：yield (batch_num, data)）──

def batch_process_unified(file_path, config, stream, ai, industry: str,
                          page_limit=TIER2_BATCH_UNITS, overlap=TIER2_OVERLAP_UNITS,
                          max_tokens=PIPELINE_MAX_TOKENS_PER_CALL, budget=None, route=True):
    """通用提取管线：统一中间格式 + schema 自描述映射（无行业提示词）

    与旧 fc 路径的差异：
    - tier-2 用行业无关的统一中间格式（kv/tables/prose），不做按行业表的 FC 提取
    - 目标表/字段映射靠 business_name/词典/存档规则的确定性匹配（白盒可审计）
    - 语义路由保留（批次内容选表），fk 拓扑/提取提示词全部废弃
    产出契约与 batch_process 一致：yield (batch_num, {"tables": [...], "unmapped": [...]})
    """
    from pipeline.fc_schema import _find_main_table, _find_code_field
    from pipeline.routing import _route_tables

    if budget is None:
        budget = {"calls": 0, "units": 0, "stopped": False,
                  "max_calls": PIPELINE_MAX_LLM_CALLS}

    rules = load_mapping_rules(industry)
    aliases = {}
    try:
        from industries.base import discover_industries, get_industry
        discover_industries()
        cfg_ind = get_industry(industry)
        aliases = (cfg_ind.terminology or {}).get("table_aliases", {}) or {}
    except Exception as e:
        logger.warning("行业词典加载失败（映射仅用 business_name）: %s", e)

    main_table = _find_main_table(config)
    code_field = _find_code_field(config, main_table)

    batch_num = 0
    total = page_limit or float("inf")
    start = 0
    prev_tail = []

    while start < total:
        chunk = []
        for _pi, text, _tc in stream:
            if text:
                chunk.append(text)
            if len(chunk) >= page_limit:
                break
        if not chunk:
            break

        # 批次级语义路由（混合域文件：每批按自身内容选表，与旧路径一致）
        # route=False（定向提取已限定表）时跳过路由 LLM 调用
        import dataclasses as _dc
        batch_cfg = config
        batch_meta = None
        if route:
            sample = chunk[0][:1500] if chunk else ""
            routed, _ = _route_tables(config, sample, ai)
            batch_cfg = _dc.replace(config, tables=[t for t in config.tables if t.get("name") in routed])
            if not batch_cfg.tables:
                batch_num += 1
                logger.info("  第 %d 批: %d 页，语义路由无相关表，跳过提取", batch_num, len(chunk))
                yield batch_num, {"tables": []}
                budget["units"] += len(chunk)
                prev_tail = chunk[-overlap:] if overlap > 0 else []
                continue
        b_main = _find_main_table(batch_cfg)
        batch_meta = {"main_table": b_main, "code_field": _find_code_field(batch_cfg, b_main)}

        prompt_text = "\n\n---\n\n".join(chunk)
        if overlap > 0 and prev_tail:
            context_text = "\n\n---\n\n".join(prev_tail)
            prompt_text = ("【上下文参考——上批末尾内容，仅供理解跨页表格，请勿提取】\n"
                           + context_text
                           + "\n\n【本批提取内容】\n" + prompt_text)

        if budget["calls"] >= budget["max_calls"]:
            logger.warning("LLM 预算超限，中止（已处理 %d 单元）", budget["units"])
            budget["stopped"] = True
            return

        budget["calls"] += 1
        inter = extract_intermediate(prompt_text, ai, schemas=batch_cfg.tables)
        if inter.get("_error"):
            # 提取失败（重试后仍失败）：显性标记本批失败，不静默丢弃——
            # 批次继续推进，失败信息随结果上抛，最终报告明确告知哪些批未入库
            logger.warning("  第 %d 批: %d 页提取失败（数据未入库）: %s",
                           batch_num + 1, len(chunk), inter["_error"])
            yield batch_num + 1, {"tables": [], "unmapped": [],
                                  "_batch_error": inter["_error"]}
            prev_tail = chunk[-overlap:] if overlap > 0 else []
            batch_num += 1
            budget["units"] += len(chunk)
            continue
        mapped = map_to_schemas(inter, batch_cfg.tables, aliases, rules)
        data = {"tables": mapped["tables"], "unmapped": mapped["unmapped"]}
        if batch_meta:
            # 统一引擎补全主表业务编码（确定性幂等 md5，替代旧 fc 的 AI 编造码）
            _fill_missing_codes(data["tables"], batch_meta.get("main_table", ""),
                                batch_meta.get("code_field", ""))
        if batch_meta:
            data["_batch_meta"] = batch_meta

        for td in data["tables"]:
            logger.info("    统一映射 %s: %d 条", td["name"], len(td["rows"]))
        if mapped["unmapped"]:
            logger.info("    未映射项: %d（将如实汇报，不入库）", len(mapped["unmapped"]))

        prev_tail = chunk[-overlap:] if overlap > 0 else []
        yield batch_num + 1, data
        batch_num += 1
        budget["units"] += len(chunk)


# ── 映射确认后的补录（免 AI：用存档规则直接映射保留的源数据）──

def apply_rules_to_unmapped(unmapped: list, schemas: list, rules: dict,
                            aliases: dict = None) -> dict:
    """用（新确认的）映射规则把保留的源数据映射为表行

    返回 {"tables": [{"name", "rows"}], "skipped": 忽略数, "still_unmapped": [...]}
    不走任何 AI 调用——规则即代码，白盒可复算
    """
    aliases = aliases or {}
    field_idx = _field_index(schemas, aliases)
    out: dict[str, list] = {}
    skipped = 0
    still: list[dict] = []

    def _map_key(k: str):
        if k in rules:
            r = rules[k]
            if r.get("ignore"):
                return ("ignore", None)
            return ("hit", (r.get("table"), r.get("field")))
        t, f, conf = _match_key(k, field_idx, rules)
        return ("hit", (t, f)) if f else ("miss", None)

    for item in unmapped:
        kind = item.get("kind")
        if kind == "kv":
            st, tf = _map_key(item.get("key", ""))
            if st == "ignore":
                skipped += 1
            elif st == "hit":
                t, f = tf
                if t and f:
                    out.setdefault(t, [{}])
                    out[t][0][f] = item.get("value", "")
            else:
                still.append(item)
        elif kind == "column":
            # 单列补录无法还原行数据（当时没存行内容），如实说明
            still.append(item)
        elif kind == "table":
            headers = item.get("headers") or []
            rows = item.get("rows") or []
            col_maps = []
            for i, h in enumerate(headers):
                st, tf = _map_key(h)
                if st == "hit" and tf[0] and tf[1]:
                    col_maps.append((i, tf[0], tf[1]))
            hits: dict[str, int] = {}
            for _i, t, _f in col_maps:
                hits[t] = hits.get(t, 0) + 1
            best_t = max(hits, key=hits.get) if hits else ""
            if best_t and hits[best_t] / max(len(headers), 1) >= 0.5:
                field_of = {i: f for i, t, f in col_maps if t == best_t}
                for r in rows:
                    row = {}
                    for i, f in field_of.items():
                        if i < len(r):
                            row[f] = r[i]
                    if any(str(v).strip() for v in row.values()):
                        out.setdefault(best_t, []).append(row)
            else:
                still.append(item)
    return {"tables": [{"name": t, "rows": rs} for t, rs in out.items()],
            "skipped": skipped, "still_unmapped": still}


# ── 映射确认指令处理（对/忽略/是X.Y → 存档规则 + 免 AI 补录）──

def _write_rules(industry: str, rules: dict) -> None:
    from core.config_hub import write_yaml_atomic
    write_yaml_atomic(_rules_path(industry), rules, backup=False)


def save_ignore_rule(industry: str, source_key: str) -> None:
    """存档忽略规则：该源键以后静默跳过（进向量库不进关系库）"""
    rules = load_mapping_rules(industry)
    rules[source_key] = {"ignore": True}
    _write_rules(industry, rules)


def handle_mapping_confirmation(user_input: str, pending: dict) -> tuple:
    """处理用户的映射确认回复。返回 (是否命中映射指令, 回复文本)

    支持：
    - "对"/"都对"/"是的" → 确认全部有候选的猜测
    - "忽略"/"全部忽略" → 全部存忽略规则
    - "N对"/"第N个对" / "N忽略" → 逐项处理
    - "X 是 Y.Z" / "X=Y.Z" / "X 映射到 Y.Z" → 显式规则（校验表字段真实存在）
    确认/存档后：对保留的源数据免 AI 补录，汇报补录/跳过数。
    """
    import re as _re
    text = (user_input or "").strip()
    items = pending.get("items", [])
    if not text or not items:
        return False, ""

    from config.settings import settings
    from industries.base import discover_industries, get_industry
    discover_industries()
    cfg = get_industry(settings.INDUSTRY)
    aliases = (cfg.terminology or {}).get("table_aliases", {}) or {}
    fidx = _field_index(cfg.tables, aliases)
    valid = {(f["table"], f["field"]) for f in fidx}

    saved: list = []
    ignored: list = []
    remaining: list = list(items)

    def _confirm_item(p):
        if p.get("guess_field") and (p["guess_table"], p["guess_field"]) in valid:
            save_mapping_rule(settings.INDUSTRY, p["key"], p["guess_table"], p["guess_field"])
            saved.append(p)
            return True
        return False

    def _ignore_item(p):
        save_ignore_rule(settings.INDUSTRY, p["key"])
        ignored.append(p)

    # 1) 显式规则：X 是/就是/映射到/等于/→ Y.Z
    m = _re.fullmatch(r"[“\"]?(.+?)[”\"]?\s*(?:是|就是|映射到|等于|=|→)\s*([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)[。!！]?", text)
    if m:
        key, table, field = m.group(1).strip(), m.group(2), m.group(3)
        if (table, field) not in valid:
            return True, (f"映射校验失败：{table}.{field} 不存在于当前行业。"
                          f"请检查表名/字段名（区分大小写），或回“忽略”。")
        save_mapping_rule(settings.INDUSTRY, key, table, field)
        hit = next((p for p in items if p["key"] == key), None)
        saved.append(hit or {"key": key, "kind": "kv",
                             "item": {"kind": "kv", "key": key, "value": ""}})
        remaining = [p for p in items if p["key"] != key]
    # 2) 全部确认/全部忽略
    elif _re.fullmatch(r"(全部|都|全)?(是|对|嗯|好的|可以)(的)?[。!！]?", text):
        remaining = [p for p in items if not _confirm_item(p)]
    elif _re.fullmatch(r"(全部|都|全)?忽略(吧|了)?[。!！]?", text):
        for p in items:
            _ignore_item(p)
        remaining = []
    # 3) 逐项：第N个对 / N对 / 第N个忽略 / N忽略
    else:
        handled_any = False
        for mm in _re.finditer(r"(?:第\s*)?(\d+)\s*个?\s*(对|是|忽略)", text):
            idx = int(mm.group(1)) - 1
            if 0 <= idx < len(items):
                if mm.group(2) == "忽略":
                    _ignore_item(items[idx])
                else:
                    _confirm_item(items[idx])
                handled_any = True
        if not handled_any:
            return False, ""
        done = set(p["key"] for p in saved) | set(p["key"] for p in ignored)
        remaining = [p for p in items if p["key"] not in done]

    # 4) 免 AI 补录（规则即代码，白盒复算）
    from core.context import get_context
    rules = load_mapping_rules(settings.INDUSTRY)
    payloads = [p["item"] for p in saved if p.get("item")]
    ingested_lines = []
    if payloads:
        mapped = apply_rules_to_unmapped(payloads, cfg.tables, rules, aliases)
        if mapped["tables"]:
            # 补录按表补全各自编码字段（幂等：与首次入库同码时走唯一键冲突跳过）
            _fill_codes_for_tables(mapped["tables"], cfg)
            try:
                from core.data_ops import insert_rows
                for td in mapped["tables"]:
                    res = insert_rows(td["name"], td["rows"])
                    if res.get("ok"):
                        ingested_lines.append(f"  {td['name']}: +{res.get('count', len(td['rows']))} 条")
                    elif res.get("conflict"):
                        ingested_lines.append(f"  {td['name']}: 冲突跳过（唯一键重复）")
                    else:
                        ingested_lines.append(f"  {td['name']}: {res.get('message', '写入失败')[:80]}")
            except Exception as e:
                ingested_lines.append(f"  补录写入异常: {str(e)[:100]}")
    # 清空挂起（已处理完毕；remaining 仍有项则保留继续问）
    get_context().save("pending_unmapped", {"items": remaining,
                                            "filepath": pending.get("filepath", ""),
                                            "tables": pending.get("tables", "")})

    reply_parts = []
    if saved:
        reply_parts.append("已记住映射：" + "；".join(
            f"\"{p['key']}\" → {p.get('guess_table') or ''}.{p.get('guess_field') or ''}"
            if p.get("guess_field") else f"\"{p['key']}\"（显式规则）" for p in saved))
    if ignored:
        reply_parts.append(f"已忽略 {len(ignored)} 项（以后同类自动跳过，进向量库）")
    if ingested_lines:
        reply_parts.append("补录完成：\n" + "\n".join(ingested_lines))
    if remaining:
        reply_parts.append(f"还剩 {len(remaining)} 项未确认：" +
                           "、".join(f"\"{p['key']}\"" for p in remaining[:5]) +
                           "（继续说“是X表.Y字段”或“忽略”）")
    else:
        reply_parts.append("本次未映射项已全部处理完。")
    return True, "\n".join(reply_parts)


def _norm_code_value(v) -> str:
    """业务编码值归一（白盒幂等的前提：同一编码必须有同一字节形态）

    文本层 PDF/OCR 常产出全角编码（Ａ１⁃１９）或带空格（A 1-25）——
    不归一会让同一条目以不同码形重复入库（唯一键形同虚设）。
    """
    import unicodedata
    s = unicodedata.normalize("NFKC", str(v or ""))
    for dash in ("⁃", "—", "−", "–", "一"):
        s = s.replace(dash, "-")
    return "".join(s.split()).strip()


def _fill_missing_codes(tables: list, main_table: str, code_field: str) -> None:
    """统一引擎补全主表业务编码（确定性，幂等）

    旧 fc 路径的编码由 AI 编造（WHHJ 式缩写），统一引擎改为白盒：
    用行的主显示值（第一个非空字段）的 md5 前 8 位——同名必同码，
    重复入库被唯一键自然拦截（幂等），不需要 AI 也不产生随机码。
    """
    import hashlib
    if not code_field:
        return
    for td in tables:
        if td.get("name") != main_table:
            continue
        for row in td.get("rows", []):
            if row.get(code_field):
                # 已有编码：归一码形（全角/空格/异体横线），保证唯一键可拦截
                row[code_field] = _norm_code_value(row[code_field])
                continue
            src = next((str(v) for k, v in row.items()
                        if k != "id" and str(v).strip()), "")
            if src:
                row[code_field] = hashlib.md5(src.encode("utf-8")).hexdigest()[:8].upper()


def _fill_codes_for_tables(tables: list, cfg) -> None:
    """按表各自识别编码字段并补全（补录/非主表也需要，如 suppliers.supplier_code）"""
    from pipeline.fc_schema import _find_code_field as _fcf
    for td in tables:
        cf = _fcf(cfg, td.get("name", ""))
        if cf:
            _fill_missing_codes([td], td["name"], cf)
