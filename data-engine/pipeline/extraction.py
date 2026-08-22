"""提取层：文本流 → 结构化数据（FC 提取 + overlap 上下文 + 完整性校验）"""
from core.logger import get_logger

import os

from pipeline.constants import (TIER2_BATCH_UNITS, TIER2_OVERLAP_UNITS,
                                PIPELINE_MAX_LLM_CALLS, PIPELINE_MAX_TOKENS_PER_CALL)
from pipeline.fc_schema import (_get_extraction_rules, _build_fc_schema, _build_fk_hint)
from pipeline.routing import _route_tables

logger = get_logger(__name__)

def batch_process(file_path, config, stream, ai, page_limit=TIER2_BATCH_UNITS,
                  overlap=TIER2_OVERLAP_UNITS, output_dir=None, route=True,
                  max_tokens=PIPELINE_MAX_TOKENS_PER_CALL, budget=None):
    """budget：可选的可变 dict，跨调用共享预算状态（P1-6）。
    键：calls(已用 LLM 调用数) / units(已处理完成的流单元数，供续入定位) / stopped(是否超预算中止)
    / max_calls(预算上限)。超预算时生成器提前结束并置 stopped=True——
    已入库批次不受影响，剩余部分可凭 units 续入。未传则用全量默认预算。"""
    if budget is None:
        budget = {"calls": 0, "units": 0, "stopped": False,
                  "max_calls": PIPELINE_MAX_LLM_CALLS}
    """分批处理：文本流→AI提取→yield (batch_num, data_dict)
    使用 Function Calling，AI 直接输出结构化数据。
    所有表名/字段名/业务规则从配置读取，无硬编码。

    overlap：把上一批次末尾 overlap 页作为"上下文参考"附进 prompt（明确标注
    不要重复提取），解决跨页表格在批次边界被切断（表头在上批末尾、无头数据
    在本批开头）导致的漏提。AI 只对当前批次页面做提取，上下文页不参与提取，
    因此正常情况不会因重叠产生重复数据。
    兜底：即使 AI 未遵守指令重复提取了上下文中的行，run() 入库侧按业务编码
    分组后由 insert_rows → drv.insert 的唯一键冲突检测拦截（见
    sqlite_driver.SqliteDriver.insert / _get_unique_key_column）：重复行所在组
    整组标记 conflict 跳过入库，不会重复写库。
    注意：该兜底仅在行业 YAML 为表声明了唯一业务键时生效（单字段
    indexes: [{columns: [xxx], unique: true}] 或列级 unique: true），
    未声明唯一约束的行业配置下兜底不触发。"""
    from core.data_ops import _get_driver, insert_rows

    # 从配置读取提取规则
    extraction_prompt, skip_if_zero = _get_extraction_rules(config)

    # 构建 FC 定义（纯配置驱动，传入 drv 用于数据存在性判断）
    drv = _get_driver()
    props, required_tables, main_table, code_field = _build_fc_schema(config, drv)
    functions = [{
        "type": "function",
        "function": {
            "name": "output_data",
            "description": f"Output extracted data for {len(required_tables)} tables",
            "parameters": {
                "type": "object",
                "properties": props,
                "required": [],  # 表级不强制必填：AI 可省略与文件无关的表（语义路由+业务描述驱动）
            }
        }
    }]

    # 构建外键映射提示
    fk_hint = _build_fk_hint(config, drv)

    # 拼接 system_prompt
    system_prompt = extraction_prompt
    # 注入各表业务说明，让 AI 在理解业务背景的前提下提取
    biz_lines = []
    for t in config.tables:
        biz = t.get("business_name", "")
        desc = t.get("description", "")
        biz_lines.append(f"- {t['name']}（{biz}）" + (f" — {desc}" if desc else ""))
    if biz_lines:
        system_prompt += (
            "\n可提取的表及其业务含义：\n" + "\n".join(biz_lines)
            + "\n只从与文件内容业务相关的表中提取，无关的表请省略或返回空数组。"
        )
    if fk_hint:
        system_prompt = system_prompt + "\n" + fk_hint

    batch_num = 0
    total = page_limit or float("inf")
    start = 0
    prev_tail = []  # 上一批次末尾 overlap 页文本（仅作上下文，不参与提取）

    while start < total:
        # 收集 page_limit 页
        chunk = []
        for pi, text, tc in stream:
            if text:
                chunk.append(text)
            if len(chunk) >= page_limit:
                break
        if not chunk:
            break

        # 批次级语义路由（混合域文件：每个批次按自身内容选表，证据闭环）
        batch_functions = functions
        batch_meta = None
        if route:
            sample = chunk[0][:1500] if chunk else ""
            from core.llm_usage import set_role as _usage_role
            with _usage_role("extract_file"):
                routed, _r_reason = _route_tables(config, sample, ai)
            # 按路由结果重建批次级提取 schema——否则行业 FK 拓扑推断会把
            # 新表/无外键表排除在提取目标之外（props 由全行业 config 生成）
            import dataclasses as _dc
            batch_cfg = _dc.replace(config, tables=[t for t in config.tables if t.get("name") in routed])
            batch_props, _, b_main, b_code = _build_fc_schema(batch_cfg, drv)
            if not batch_props:
                batch_num += 1
                logger.info("  第 %d 批: %d 页，语义路由无相关表，跳过提取", batch_num, len(chunk))
                yield batch_num, {"tables": []}
                budget["units"] += len(chunk)  # 跳过批也算处理完成，可安全续入
                prev_tail = chunk[-overlap:] if overlap > 0 else []
                continue
            batch_meta = {"main_table": b_main, "code_field": b_code}
            batch_functions = [{
                "type": "function",
                "function": {
                    "name": "output_data",
                    "description": f"Output extracted data for {len(batch_props)} tables",
                    "parameters": {
                        "type": "object",
                        "properties": batch_props,
                        "required": [],
                    }
                }
            }]

        prompt_text = "\n\n---\n\n".join(chunk)

        # overlap 上下文：跨页表格（表头在上批末尾、数据续在本批）时给 AI 表头上下文。
        # 上下文页明确标注"不要重复提取"，AI 只对当前批次页面做提取。
        if overlap > 0 and prev_tail:
            context_text = "\n\n---\n\n".join(prev_tail)
            ai_prompt = (
                "【上下文参考｜请勿提取】以下是上一批次末尾的内容，仅供理解跨页表格的上下文"
                "（例如表头在上一批、数据续在本批）。其中的数据已提取过，请不要对其重复提取：\n\n"
                f"{context_text}\n\n"
                "════════════════════════════════\n\n"
                "【当前批次｜请提取】请仅对以下内容提取结构化数据：\n\n"
                f"{prompt_text}"
            )
        else:
            ai_prompt = prompt_text

        # 保存原始文本到文件（供补提用）——只存当前批次页面，不含上下文参考
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            with open(f"{output_dir}/batch_{batch_num+1:03d}_raw.txt", "w", encoding="utf-8") as f:
                f.write(prompt_text)

        batch_num += 1
        logger.info("  第 %d 批: %d 页（overlap 上下文 %d 页）", batch_num, len(chunk),
                    len(prev_tail) if overlap > 0 else 0)

        # LLM 预算护栏（P1-6）：超预算即中止，已入库批次保留，剩余可续入
        if budget["calls"] >= budget["max_calls"]:
            budget["stopped"] = True
            logger.warning("LLM 预算超限（已达 %d 次调用上限），已处理 %d 个流单元，中止剩余批次",
                           budget["max_calls"], budget["units"])
            return

        # Function Calling — AI 直接输出结构化数据
        from core.llm_usage import set_role as _usage_role
        with _usage_role("extract_file"):
            fn_name, fn_args = ai.call_function(
                batch_functions, ai_prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
            )
        budget["calls"] += 1
        logger.info("  LLM 调用 #%d: prompt %d 字符，输出约 %d 字符",
                    budget["calls"], len(ai_prompt), len(str(fn_args)))

        # P1-7 提取完整性校验：行数显著低于文本估计时自动重提一次取多者
        # （同页两次提取 6 行 vs 14 行的不确定性兜底；重提受预算护栏约束）
        _rows_n = _count_extracted_rows(fn_args)
        _est_rows = _estimate_data_rows(chunk)
        if _est_rows >= 4 and _rows_n < _est_rows * 0.5 and budget["calls"] < budget["max_calls"]:
            with _usage_role("extract_file"):
                _fn2, _args2 = ai.call_function(
                    batch_functions, ai_prompt,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                )
            budget["calls"] += 1
            _rows2 = _count_extracted_rows(_args2)
            logger.info("  提取行数校验: 首次 %d 行 vs 文本估计 %d 行，重提得 %d 行",
                        _rows_n, _est_rows, _rows2)
            if _rows2 > _rows_n:
                fn_name, fn_args = _fn2, _args2

        # 转成 pipeline 内部格式 {"tables": [{"name": ..., "rows": [...]}]}
        tables_out = []
        for t in config.tables:
            tname = t["name"]
            if tname in fn_args:
                filtered = fn_args[tname]
                # 零值过滤：只有当行确实有该字段且值为零/空时才过滤
                # （字段不存在的行不应被过滤，否则会误删没有该字段的表的所有行）
                if skip_if_zero:
                    filtered = [
                        r for r in filtered
                        if not any(f in r and r[f] in (0, "0", 0.0, "") for f in skip_if_zero)
                    ]
                tables_out.append({"name": tname, "rows": filtered})
        data = {"tables": tables_out}

        # 调试日志：打印每张表返回的行数和第一条数据
        for td in tables_out:
            rows = td.get("rows", [])
            if rows:
                logger.info("    AI返回 %s: %d 条, 首条字段: %s", td['name'], len(rows), list(rows[0].keys()))
            else:
                logger.info("    AI返回 %s: 0 条", td['name'])

        # 主明细比例合理性检查（P1-7）：主表 0 行但明细 >0 行通常意味着主表漏提
        _main = batch_meta["main_table"] if batch_meta else main_table
        _main_rows = next((len(td["rows"]) for td in tables_out if td["name"] == _main), 0)
        _detail_rows = sum(len(td["rows"]) for td in tables_out if td["name"] != _main)
        if _main_rows == 0 and _detail_rows > 0:
            logger.warning("  提取比例异常: 主表 %s 0 行但明细共 %d 行（主表可能漏提，明细将无法关联）",
                           _main, _detail_rows)

        # 记录本批次末尾 overlap 页，供下一批次作上下文参考
        prev_tail = chunk[-overlap:] if overlap > 0 else []

        if batch_meta:
            data["_batch_meta"] = batch_meta
        yield batch_num, data
        budget["units"] += len(chunk)  # 本批处理完成才计入；超预算中止的批不计，续入从此开始


# ── 完整流水线 ──

def _estimate_data_rows(texts) -> int:
    """从流单元文本估计数据行数——启发式（P1-7 提取完整性校验用）

    行满足：≥2 个空白分隔 token 且至少一个 token 含数字。
    仅用于触发"重提一次取多者"的判断，不做正确性裁决；
    对纯叙述文本可能高估（含数字的句子），代价是该批多一次重提调用。
    """
    n = 0
    for text in texts:
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("【"):
                continue
            toks = s.split()
            if len(toks) >= 2 and any(any(ch.isdigit() for ch in t) for t in toks):
                n += 1
    return n


def _count_extracted_rows(fn_args) -> int:
    """统计 FC 输出参数的提取总行数"""
    if not isinstance(fn_args, dict):
        return 0
    return sum(len(v) for v in fn_args.values() if isinstance(v, list))


def _collect_extracted_values(batches) -> set:
    """从批次提取结果收集所有已提取单元格值（≥2 字符，供剥离匹配用）——纯函数"""
    values = set()
    for b in batches:
        bdata = b.get("data", {})
        b_tables = bdata.get("tables", []) if isinstance(bdata, dict) else bdata
        for td in b_tables:
            for row in td.get("rows", []):
                for v in (row.values() if isinstance(row, dict) else []):
                    sv = str(v).strip()
                    if len(sv) >= 2:
                        values.add(sv)
    return values


def _strip_table_page_lines(table_page_texts, extracted_values):
    """含表单元文本剥离——纯函数，可独立测试

    用已提取的单元格值回扫单元文本：含已提取值的行=业务表格行（已进关系库），
    剔除；不含的行保留。AI 未提取的表格（说明性零散表格）的行不在
    extracted_values 里，原文保留 → 进向量库（判定3 语义）。

    Args:
        table_page_texts: [(unit_no, unit_text), ...] 含表流单元
        extracted_values: set[str] 已提取单元格值
    Returns:
        (stripped_pages, stripped_total)
        stripped_pages: [(unit_no, 剥离后文本), ...]（全剔除的单元不出现）
        stripped_total: 剔除的总行数
    """
    stripped_pages = []
    stripped_total = 0
    for pi, text in table_page_texts:
        kept, removed = [], 0
        for line in text.splitlines():
            if line.strip() and any(val in line for val in extracted_values):
                removed += 1
                continue
            kept.append(line)
        stripped_total += removed
        remaining = chr(10).join(kept).strip()
        if remaining:
            stripped_pages.append((pi, remaining))
    return stripped_pages, stripped_total


