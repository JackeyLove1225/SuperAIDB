"""数据理解与入库流水线——编排层（P2-1 阶段拆分）

阶段分工（各阶段独立可测）：
- pipeline/parsing.py     解析层：文件 → 文本流（统一流单元契约）
- pipeline/fc_schema.py   FC schema 构建层：行业 YAML → 提取 schema/FK 提示
- pipeline/extraction.py  提取层：文本流 → 结构化数据（FC + overlap + 完整性校验）
- pipeline/routing.py     语义路由层：AI 举证 + 代码验证选表
- pipeline/ingestion.py   入库层：结构化数据 → 关系库（savepoint/saga/行级容错）
- pipeline/runner.py      编排层（本文件）：阶段编排 + 对账报告
"""
import sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from core.ai_runtime.ai_client import AIClient
from core.logger import info as log_info, get_logger
from core.contract.security_contract import SecurityContract, safe_table_sql, safe_column_sql
from pipeline.constants import (TIER1_BATCH_UNITS, TIER1_EXCEL_ROWS, TIER1_DOCX_PARAS,
                                TIER2_BATCH_UNITS, TIER2_OVERLAP_UNITS,
                                PIPELINE_MAX_LLM_CALLS, PIPELINE_MAX_TOKENS_PER_CALL)

# ── 门面导出（向后兼容：tools.py/tests 原从 pipeline.runner 导入的符号不变）──
from pipeline.parsing import (  # noqa: F401
    excel_to_text_stream, pdf_to_text_stream, docx_to_text_stream,
    pdf_to_text_stream_with_ocr, image_to_text_stream,
    MAX_FILE_SIZE_MB, _check_file_size)
from pipeline.fc_schema import (  # noqa: F401
    _find_main_table, _find_code_field, _find_fk_to_main, _find_main_fks_to_base,
    _get_extraction_tables, _build_fc_schema, _build_fk_hint, _get_extraction_rules)
from pipeline.extraction import (  # noqa: F401
    batch_process, _estimate_data_rows, _count_extracted_rows,
    _collect_extracted_values, _strip_table_page_lines)
from pipeline.ingestion import (  # noqa: F401
    _convert_main_virtual_codes, _ingest_group_via_saga, write_batch_groups)
from pipeline.routing import _route_tables  # noqa: F401

logger = get_logger(__name__)


def run(file_path: str, industry: str = None, page_limit: int = 5,
        overlap: int = TIER2_OVERLAP_UNITS,
        page_start: int = 0, batch_size: int = None,
        max_tokens: int = PIPELINE_MAX_TOKENS_PER_CALL,
        overwrite: bool = False, only_tables: list = None):
    """完整流水线：理解 建 schema 分批录入

    支持文件格式：PDF / Excel (.xlsx, .xls) / Word (.docx)
    文件大小限制：50MB

    only_tables：限定提取目标表（表名列表）。默认按行业全部标准表提取；
    全自动建库流程传入新建表清单，避免 AI 把数据错提到行业既有不相关表。
    """

    from industries.base import discover_industries, get_industry
    discover_industries()
    cfg = get_industry(industry)
    ai = AIClient.get_instance()

    if only_tables:
        import dataclasses
        keep = set(only_tables)
        cfg = dataclasses.replace(
            cfg, tables=[t for t in cfg.tables if t.get("name") in keep])
        logger.info("限定提取目标表: %s", sorted(keep))

    ext = Path(file_path).suffix.lower()

    # 文件大小检查
    _check_file_size(file_path)

    logger.info("开始处理: %s", Path(file_path).name)
    logger.info("行业: %s，%d 个预设标准表", industry, len(cfg.tables))
    for t in cfg.tables:
        logger.info("  表名: %s", t['name'])

    # 识别主表和业务编码字段
    main_table = _find_main_table(cfg)
    code_field = _find_code_field(cfg, main_table)
    logger.info("  主表: %s，关联字段: %s", main_table, code_field)

    # 检查标准表是否已存在
    from core.data_ops import _get_driver, insert_rows
    drv = _get_driver()
    missing = [t['name'] for t in cfg.tables if not drv.table_exists(t['name'])]
    if missing:
        return {"ok": False, "message": f"标准表 {missing} 尚未创建。请先建表。"}

    # 第二步：分批提取并录入
    logger.info("第二步：按 schema 分批次录入")
    logger.info("-" * 40)

    # ── 双批次流式设计（白盒）──
    # tier-1 代码提取（原始文件→文字）：边解析边分流，每批最多 PARSE_BATCH_PAGES 页。
    #   纯文字页（tc==0）直接进向量库；含表页（tc>0）暂存，待 AI 提取后剥离表格行再进向量库。
    # tier-2 AI 识别（文字/表格→结构化数据）：每批最多 AI_BATCH_PAGES 页，
    #   且带上一批末尾 overlap 页作上下文（跨页表格表头不断裂）。
    # 全量模式页数不限：解析器逐页惰性产出，两个 tier 各按自己的批粒度消费，无需人工干预。
    PARSE_BATCH_PAGES = TIER1_BATCH_UNITS        # tier-1 flush 粒度（常量见 pipeline/constants.py）
    AI_BATCH_PAGES = batch_size or TIER2_BATCH_UNITS  # tier-2 AI 批粒度

    if ext == ".pdf":
        raw_stream = pdf_to_text_stream_with_ocr(file_path, page_start=page_start, page_limit=page_limit)
    elif ext in (".xlsx", ".xls"):
        raw_stream = excel_to_text_stream(file_path, sheet_start=page_start, sheet_limit=page_limit)
    elif ext == ".docx":
        raw_stream = docx_to_text_stream(file_path, page_start=page_start, page_limit=page_limit)
    elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
        raw_stream = image_to_text_stream(file_path, page_start=page_start, page_limit=page_limit)
    else:
        raise ValueError(f"不支持的文件格式: {ext}，目前支持 PDF / Excel / Word(.docx) / 图片(png,jpg,bmp,webp)")

    # 文字部分录入向量数据库
    collection_name = Path(file_path).stem
    vector_error = None  # 非 None = 向量入库失败（原因字符串），流程末尾显式上报
    vector_count = 0
    vs = None               # 向量库实例（初始化为 None，防异常路径 NameError）
    table_page_texts = []   # 含表页暂存（待提取后剥离表格行再进向量库）
    try:
        from core.vector_store import get_vector_store
        vs = get_vector_store()
        if not vs:
            # NullVectorStore：携带失败原因，后续 add/search 会抛错，这里先显式记录
            vector_error = getattr(vs, "reason", None) or                 f"向量数据库未初始化（VECTOR_STORE_TYPE={settings.VECTOR_STORE_TYPE}）"
            logger.error("向量入库失败: collection=%s, 原因: %s", collection_name, vector_error)
    except Exception as e:
        vector_error = str(e)
        logger.error("向量入库失败: collection=%s, 原因: %s", collection_name, e)
        vs = None

    pages_total = 0  # 已解析页数（tier-1 生成器内累计，替代原 pages 全量列表）

    def _tier1_stream(raw):
        """tier-1：边解析边分流。每攒满 PARSE_BATCH_PAGES 页把其中的纯文字页 flush 进向量库；
        含表页暂存到 table_page_texts（提取后剥离再进向量库）。页面原样透传给 tier-2 消费。"""
        nonlocal vector_error, vector_count, pages_total
        buf_texts, buf_metas = [], []

        def _flush():
            nonlocal vector_error, vector_count
            if not buf_texts or vs is None or vector_error:
                return
            try:
                # upsert 语义（见 ChromaStore.add）：重复入库同一文件不会翻倍
                vs.add(collection_name, buf_texts, buf_metas)
                vector_count = vs.count(collection_name)
                logger.info("  向量入库: collection=%s 本批 %d 页，累计 %d 条",
                            collection_name, len(buf_texts), vector_count)
            except Exception as e:
                vector_error = str(e)
                logger.error("向量入库失败: collection=%s, 原因: %s", collection_name, e)

        for pi, text, tc in raw:
            pages_total += 1
            if text and text.strip():
                if tc and tc > 0:
                    table_page_texts.append((pi, text))
                else:
                    buf_texts.append(text)
                    buf_metas.append({"page": pi, "source": Path(file_path).name})
            if pages_total % PARSE_BATCH_PAGES == 0:
                _flush()
                buf_texts, buf_metas = [], []
            yield (pi, text, tc)
        _flush()  # 尾部不足一批的纯文字页

    all_results = {"batches": [], "conflicts": [], "failures": [], "pages": 0,
                   "unmapped": [], "failed_batches": []}
    budget = {"calls": 0, "units": 0, "stopped": False, "max_calls": PIPELINE_MAX_LLM_CALLS}
    _prog_dir = Path(settings.SQLITE_DB_PATH).parent  # 进度心跳文件目录（ingest_progress.json）

    # 通用提取管线优先（统一中间格式 + schema 自描述映射，无行业提示词）；
    # 异常时回退旧 fc 路径（行业提示词驱动）——平滑切换，不赌全量替换
    def _batches():
        try:
            from pipeline.unified import batch_process_unified
            yield from batch_process_unified(
                file_path, cfg, _tier1_stream(raw_stream), ai, settings.INDUSTRY,
                AI_BATCH_PAGES, overlap, max_tokens=max_tokens, budget=budget,
                route=(only_tables is None))
        except Exception as e:
            logger.warning("通用管线异常，回退旧 fc 路径: %s", str(e)[:120])
            yield from batch_process(file_path, cfg, _tier1_stream(raw_stream), ai,
                                     AI_BATCH_PAGES, overlap,
                                     output_dir=None, route=(only_tables is None),
                                     max_tokens=max_tokens, budget=budget)

    for batch_num, data in _batches():
        all_results["batches"].append({"batch": batch_num, "data": data})
        if data.get("unmapped"):
            all_results["unmapped"].extend(data["unmapped"])
        if data.get("_batch_error"):
            # 提取失败的批次（AI 重试后仍失败）：记录供最终报告显性告知
            all_results["failed_batches"].append(
                {"batch": batch_num, "reason": data["_batch_error"]})

        # 进度心跳：每批落盘进度文件（前端轮询展示，白盒可审计）
        try:
            import json as _json, time as _time
            (_prog_dir / "ingest_progress.json").write_text(_json.dumps({
                "file": Path(file_path).name,
                "batch": batch_num,
                "units_done": budget.get("units", 0),
                "status": "running",
                "updated_at": _time.strftime("%H:%M:%S"),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

        # 入库阶段（ingestion.py）：按主表业务编码分组写入（savepoint/saga）
        write_batch_groups(data, cfg, drv, main_table, code_field, overwrite, all_results)

    # 完成：进度文件标记完成态（前端停止轮询）
    try:
        import json as _json, time as _time
        (_prog_dir / "ingest_progress.json").write_text(_json.dumps({
            "file": Path(file_path).name, "batch": "done",
            "units_done": budget.get("units", 0), "status": "done",
            "updated_at": _time.strftime("%H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    all_results["pages"] = pages_total  # 解析总页数（tier-1 流式统计）
    if budget["stopped"]:
        # LLM 预算中止：已入库批次保留，向调用方汇报进度供续入（P1-6）
        all_results["budget_stopped"] = {"units_done": budget["units"],
                                         "llm_calls": budget["calls"]}
    # 含表页文本剥离：用已提取的单元格值回扫页面文本，剔除表格行后再进向量库
    # 语义关键（判定3）：AI 未提取的表格行（说明性零散表格）不在 extracted_values 里，
    # 原文保留进向量库——业务表进关系库、说明性表格进向量库由 AI 判定自然分流
    if table_page_texts and vs is not None and not vector_error:
        extracted_values = _collect_extracted_values(all_results["batches"])
        stripped_pages, stripped_total = _strip_table_page_lines(table_page_texts, extracted_values)
        v2_texts = [t for _, t in stripped_pages]
        v2_metas = [{"page": pi, "source": Path(file_path).name} for pi, _ in stripped_pages]
        if v2_texts:
            try:
                vs.add(collection_name, v2_texts, v2_metas)
            except Exception as e:
                vector_error = str(e)
                logger.error("向量入库失败(含表页): %s", e)
        logger.info("含表页文本剥离: 剔除表格行 %d 行，%d 页叙述文本进向量库",
                    stripped_total, len(v2_texts))
        vector_count = vs.count(collection_name)

    # 分流对账（白盒：每文件一份可审计摘要）
    logger.info(
        "入库对账: 共 %d 页（纯文字页 %d → 向量库，含表页 %d → 提取+剥离）",
        all_results["pages"], all_results["pages"] - len(table_page_texts), len(table_page_texts))

    logger.info("共 %d 批", len(all_results['batches']))
    # 向量入库结果随返回值上报（成功=条数），失败时在 SQLite 表格入库完成后显式抛出——
    # 调用方（process_file）会将 "向量入库失败: 原因" 放进最终返回给用户的结果文本，不再静默。
    # 重试是安全的：向量为 upsert 幂等，SQLite 有冲突检测。
    all_results["vector_collection"] = collection_name
    if vector_error:
        raise ValueError(
            f"向量入库失败: {vector_error}"
            f"（SQLite 表格入库已完成：{len(all_results['batches'])} 批，"
            f"冲突 {len(all_results['conflicts'])} 条，数据未丢失）"
        )
    all_results["vector_count"] = vector_count
    return all_results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="数据理解与入库流水线")
    parser.add_argument("file", nargs="?", help="文件路径")
    parser.add_argument("--industry", default=settings.INDUSTRY, help="行业")
    parser.add_argument("--page-limit", type=int, default=5, help="每批页数")
    parser.add_argument("--page-start", type=int, default=0, help="起始页")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有数据")
    parser.add_argument("--overlap", type=int, default=1, help="上下文重叠页数")
    args = parser.parse_args()

    if args.file:
        run(args.file, industry=args.industry, page_start=args.page_start,
            page_limit=args.page_limit, overlap=args.overlap, overwrite=args.overwrite)
    else:
        parser.print_help()


# ── 语义路由：AI 理解表业务背景后选择提取目标 ──
