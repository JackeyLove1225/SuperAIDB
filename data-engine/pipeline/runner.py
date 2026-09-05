"""数据理解与入库流水线——编排层

阶段分工（各阶段独立可测）：
- pipeline/parsing.py     解析层：文件 → 文本流（统一流单元契约）
- pipeline/fc_schema.py   FC schema 构建层：行业 YAML → 提取 schema/FK 提示
- pipeline/extraction.py  提取层：文本流 → 结构化数据（FC + overlap + 完整性校验）
- pipeline/routing.py     语义路由层：AI 举证 + 代码验证选表
- pipeline/ingestion.py   入库层：结构化数据 → 关系库（savepoint/saga/行级容错）
- pipeline/runner.py      编排层（本文件）：阶段编排 + 对账报告
"""
import dataclasses
import sys
from pathlib import Path

if __package__ in (None, ""):  # 仅脚本态（python pipeline/runner.py）需要仓根入径——导入态零全局副作用
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from core.ai_runtime.ai_client import AIClient
from core.logger import get_logger
from pipeline.constants import (TIER1_BATCH_UNITS, TIER2_BATCH_UNITS, TIER2_OVERLAP_UNITS,
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
from pipeline.routing import route_tables  # noqa: F401

logger = get_logger(__name__)


def _load_industry_config(industry, only_tables):
    """加载行业配置与 AI 客户端；only_tables 非空时把提取目标裁剪到指定表清单。"""
    from industries.base import discover_industries, get_industry
    discover_industries()
    cfg = get_industry(industry)
    ai = AIClient.get_instance()

    if only_tables:
        keep = set(only_tables)
        cfg = dataclasses.replace(
            cfg, tables=[t for t in cfg.tables if t.get("name") in keep])
        logger.info("限定提取目标表: %s", sorted(keep))
    return cfg, ai


def _preflight_schema(file_path, cfg, industry):
    """入库前置检查：文件大小校验、schema 日志、主表/编码字段识别、标准表存在性检查。

    返回 (drv, main_table, code_field, error)；error 非 None 时 run() 直接返回它。"""
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
    from core.data_ops import get_driver
    drv = get_driver()
    missing = [t['name'] for t in cfg.tables if not drv.table_exists(t['name'])]
    if missing:
        return drv, main_table, code_field, \
            {"ok": False, "message": f"标准表 {missing} 尚未创建。请先建表。"}
    return drv, main_table, code_field, None


def _open_text_stream(ext, file_path, page_start, page_limit):
    """按文件扩展名选择解析器，返回原始文本流（流单元契约见 parsing.py）。"""
    if ext == ".pdf":
        return pdf_to_text_stream_with_ocr(file_path, page_start=page_start, page_limit=page_limit)
    if ext in (".xlsx", ".xls"):
        return excel_to_text_stream(file_path, sheet_start=page_start, sheet_limit=page_limit)
    if ext == ".docx":
        return docx_to_text_stream(file_path, page_start=page_start, page_limit=page_limit)
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
        return image_to_text_stream(file_path, page_start=page_start, page_limit=page_limit)
    raise ValueError(f"不支持的文件格式: {ext}，目前支持 PDF / Excel / Word(.docx) / 图片(png,jpg,bmp,webp)")


def _init_vector_store(collection_name):
    """初始化向量库实例，返回 (vs, vector_error)。

    失败时 vs=None 且 vector_error 携带原因字符串，流程末尾由 run() 显式上报。"""
    vs = None               # 向量库实例（初始化为 None，防异常路径 NameError）
    vector_error = None     # 非 None = 向量入库失败（原因字符串）
    try:
        from core.vector_store import get_vector_store
        vs = get_vector_store()
        if not vs:
            # NullVectorStore：携带失败原因，后续 add/search 会抛错，这里先显式记录
            vector_error = getattr(vs, "reason", None) or \
                f"向量数据库未初始化（VECTOR_STORE_TYPE={settings.VECTOR_STORE_TYPE}）"
            logger.error("向量入库失败: collection=%s, 原因: %s", collection_name, vector_error)
    except Exception as e:
        vector_error = str(e)
        logger.error("向量入库失败: collection=%s, 原因: %s", collection_name, e)
        vs = None
    return vs, vector_error


def _write_progress(prog_dir, file_name, batch, units, status):
    """进度心跳落盘（ingest_progress.json，前端轮询展示，白盒可审计）。

    进度文件写失败不阻断提取主流程（观测通道降级）。"""
    try:
        import json as _json, time as _time
        (prog_dir / "ingest_progress.json").write_text(_json.dumps({
            "file": file_name,
            "batch": batch,
            "units_done": units,
            "status": status,
            "updated_at": _time.strftime("%H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # 进度文件写失败不阻断提取主流程（观测通道降级）


# ── 双批次流式设计（白盒）──
# tier-1 代码提取（原始文件→文字）：边解析边分流，每批最多 TIER1_BATCH_UNITS 页。
#   纯文字页（tc==0）直接进向量库；含表页（tc>0）暂存，待 AI 提取后剥离表格行再进向量库。
# tier-2 AI 识别（文字/表格→结构化数据）：每批最多 TIER2_BATCH_UNITS 页，
#   且带上一批末尾 overlap 页作上下文（跨页表格表头不断裂）。
# 全量模式页数不限：解析器逐页惰性产出，两个 tier 各按自己的批粒度消费，无需人工干预。


def _flush_text_pages(vs, collection_name, state, buf_texts, buf_metas):
    """把缓冲的纯文字页 flush 进向量库；失败原因记入 state["vector_error"]（不阻断主流程）。"""
    if not buf_texts or vs is None or state["vector_error"]:
        return
    try:
        # upsert 语义（见 ChromaStore.add）：重复入库同一文件不会翻倍
        vs.add(collection_name, buf_texts, buf_metas)
        state["vector_count"] = vs.count(collection_name)
        logger.info("  向量入库: collection=%s 本批 %d 页，累计 %d 条",
                    collection_name, len(buf_texts), state["vector_count"])
    except Exception as e:
        state["vector_error"] = str(e)
        logger.error("向量入库失败: collection=%s, 原因: %s", collection_name, e)


def _tier1_stream(raw, vs, collection_name, file_name, table_page_texts,
                  parse_batch_pages, state):
    """tier-1：边解析边分流。每攒满 parse_batch_pages 页把其中的纯文字页 flush 进向量库；
    含表页暂存到 table_page_texts（提取后剥离再进向量库）。页面原样透传给 tier-2 消费。
    state 为可变 dict，pages_total/vector_count/vector_error 原地累计，供调用方读回。"""
    buf_texts, buf_metas = [], []
    for pi, text, tc in raw:
        state["pages_total"] += 1
        if text and text.strip():
            if tc and tc > 0:
                table_page_texts.append((pi, text))
            else:
                buf_texts.append(text)
                buf_metas.append({"page": pi, "source": file_name})
        if state["pages_total"] % parse_batch_pages == 0:
            _flush_text_pages(vs, collection_name, state, buf_texts, buf_metas)
            buf_texts, buf_metas = [], []
        yield (pi, text, tc)
    _flush_text_pages(vs, collection_name, state, buf_texts, buf_metas)  # 尾部不足一批的纯文字页


def _batch_stream(file_path, cfg, raw_stream, ai, ai_batch_pages, overlap, max_tokens,
                  budget, route, tier1_kwargs):
    """tier-2 批流：通用提取管线优先（统一中间格式 + schema 自描述映射，无行业提示词）；
    通用管线启动期失败时回退旧 fc 路径（行业提示词驱动）——旧路径为兼容兜底保留，
    待通用引擎全量验证后下线（下线即删 batch_process 与本回退段）。
    两条路径各自现包一条新的 tier-1 流（tier1_kwargs 原样透传），回退语义不变。
    回退只在 unified 启动期失败时发生（一批未产）；中途失败直接上抛——
    已产批次已入库，fc 从头重放会造成重复批次的语义含糊。"""
    yielded = False
    try:
        from pipeline.unified import batch_process_unified
        for data in batch_process_unified(
                file_path, cfg, _tier1_stream(raw_stream, **tier1_kwargs), ai, settings.INDUSTRY,
                ai_batch_pages, overlap, max_tokens=max_tokens, budget=budget,
                route=route):
            yielded = True
            yield data
    except Exception as e:
        if yielded:
            raise  # 中途失败：已入库批次保留，如实上抛（不回放、不静默换引擎）
        logger.warning("通用管线启动失败，回退旧 fc 路径: %s", str(e)[:120])
        yield from batch_process(file_path, cfg, _tier1_stream(raw_stream, **tier1_kwargs), ai,
                                 ai_batch_pages, overlap,
                                 output_dir=None, route=route,
                                 max_tokens=max_tokens, budget=budget)


def _ingest_stripped_pages(table_page_texts, all_results, vs, collection_name, file_name,
                           vector_count, vector_error):
    """含表页文本剥离入向量库：用已提取的单元格值回扫页面文本，剔除表格行后再入库。

    语义关键：AI 未提取的表格行（说明性零散表格）不在 extracted_values 里，
    原文保留进向量库——业务表进关系库、说明性表格进向量库由 AI 判定自然分流。
    返回更新后的 (vector_count, vector_error)。"""
    if table_page_texts and vs is not None and not vector_error:
        extracted_values = _collect_extracted_values(all_results["batches"])
        stripped_pages, stripped_total = _strip_table_page_lines(table_page_texts, extracted_values)
        v2_texts = [t for _, t in stripped_pages]
        v2_metas = [{"page": pi, "source": file_name} for pi, _ in stripped_pages]
        if v2_texts:
            try:
                vs.add(collection_name, v2_texts, v2_metas)
            except Exception as e:
                vector_error = str(e)
                logger.error("向量入库失败(含表页): %s", e)
        logger.info("含表页文本剥离: 剔除表格行 %d 行，%d 页叙述文本进向量库",
                    stripped_total, len(v2_texts))
        vector_count = vs.count(collection_name)
    return vector_count, vector_error


def _finalize_run(all_results, budget, table_page_texts, vs, collection_name,
                  file_name, t1_state):
    """收尾汇报：页数/预算中止回填、含表页剥离入向量库、分流对账、向量结果上报。

    向量入库失败在 SQLite 表格入库完成后显式抛出；成功则回填 vector_count 并返回 all_results。"""
    all_results["pages"] = t1_state["pages_total"]  # 解析总页数（tier-1 流式统计）
    if budget["stopped"]:
        # LLM 预算中止：已入库批次保留，向调用方汇报进度供续入
        all_results["budget_stopped"] = {"units_done": budget["units"],
                                         "llm_calls": budget["calls"]}
    # 含表页文本剥离入向量库（语义见 _ingest_stripped_pages）
    vector_count, vector_error = _ingest_stripped_pages(
        table_page_texts, all_results, vs, collection_name, file_name,
        t1_state["vector_count"], t1_state["vector_error"])

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


def _mount_focus(cfg, fields):
    """字段聚焦挂载：聚焦属性只挂副本——行业注册表返回共享单例，
    直挂会跨调用残留（一次聚焦后同进程后续默认提取全被旧字段集静默裁剪）。
    零命中在此如实报（路径无关护栏——不静默按全表提取，那是答非所问面）"""
    if not fields:
        return cfg
    import dataclasses as _dc
    from pipeline.fc_schema import assert_focus_matched
    cfg = _dc.replace(cfg)
    cfg.focus_fields = [str(f).strip() for f in fields if str(f).strip()]
    assert_focus_matched(cfg)  # 零命中如实报——唯一实现（与 fc 提取层同源，口径不漂移）
    return cfg


def _apply_focus(tables_data, cfg):
    """字段聚焦后处理（路径无关）：按 focus_fields 裁剪行字段——
    保留聚焦列（字段名/中文业务名命中）+ id + 唯一业务键（关联/去重锚点）。
    fc/unified 两条提取路径的产物同构（[{"name","rows"}]），在此统一收口。"""
    focus = getattr(cfg, "focus_fields", None)
    if not focus or not tables_data:
        return tables_data
    fs = {f.strip().lower() for f in focus}
    from pipeline.fc_schema import protected_keys_for_table
    for td in tables_data:
        tcfg = next((t for t in cfg.tables if t["name"] == td.get("name")), None)
        if tcfg is None:
            continue
        # keep = 保护面（声明列∪主表编码∪虚拟基础表编码∪明细虚拟主表编码——
        # 与 fc_schema 同源，明细断链/外键静默丢面归零）∩（聚焦列）
        keep = set()
        for c in tcfg.get("columns", []):
            cn = c["name"]
            if cn == "id":
                continue
            if cn.lower() in fs or (c.get("business_name") or "").lower() in fs:
                keep.add(cn)
        keep |= protected_keys_for_table(cfg, tcfg["name"])
        td["rows"] = [{k: v for k, v in row.items() if k in keep}
                      for row in td.get("rows", [])]
    return tables_data


def run(file_path: str, industry: str = None, page_limit: int = 5,
        overlap: int = TIER2_OVERLAP_UNITS,
        page_start: int = 0, batch_size: int = None,
        max_tokens: int = PIPELINE_MAX_TOKENS_PER_CALL,
        overwrite: bool = False, only_tables: list = None,
        fields: list = None):
    """完整流水线：理解 建 schema 分批录入

    支持文件格式：PDF / Excel (.xlsx, .xls) / Word (.docx)
    文件大小限制：50MB

    only_tables：限定提取目标表（表名列表）。默认按行业全部标准表提取；
    全自动建库流程传入新建表清单，避免 AI 把数据错提到行业既有不相关表。
    fields：限定提取字段子集（字段名/中文业务名列表）——用户指令级
    字段聚焦（如"只把供应商和价格录进 X 表"）；指定后提取 schema 按此
    裁剪且 prompt 明示忽略其余字段，链路字段（业务编码/外键）不受影响。
    """
    cfg, ai = _load_industry_config(industry, only_tables)
    cfg = _mount_focus(cfg, fields)
    if fields and overwrite:
        logger.warning("字段聚焦+覆盖写组合：非聚焦列的既有数据将被清除"
                      "（overwrite 按编码先删全列旧行再写聚焦列——"
                      "如需保留其他列请先备份或改用追加")
    drv, main_table, code_field, err = _preflight_schema(file_path, cfg, industry)
    if err:
        return err

    # 第二步：分批提取并录入
    logger.info("第二步：按 schema 分批次录入")
    logger.info("-" * 40)

    # 双批次流式：tier-1 边解析边分流，tier-2 按批提取（机制见上方 tier 注释）
    ext = Path(file_path).suffix.lower()
    ai_batch_pages = batch_size or TIER2_BATCH_UNITS  # tier-2 AI 批粒度
    raw_stream = _open_text_stream(ext, file_path, page_start, page_limit)

    # 文字部分录入向量数据库（失败原因由 t1_state 携带，流程末尾显式上报）
    collection_name = Path(file_path).stem
    table_page_texts = []   # 含表页暂存（待提取后剥离表格行再进向量库）
    vs, vector_error = _init_vector_store(collection_name)
    t1_state = {"vector_error": vector_error, "vector_count": 0, "pages_total": 0}
    tier1_kwargs = dict(vs=vs, collection_name=collection_name,
                        file_name=Path(file_path).name,
                        table_page_texts=table_page_texts,
                        parse_batch_pages=TIER1_BATCH_UNITS,  # tier-1 flush 粒度
                        state=t1_state)

    all_results = {"batches": [], "conflicts": [], "failures": [], "pages": 0,
                   "unmapped": [], "failed_batches": []}
    budget = {"calls": 0, "units": 0, "stopped": False, "max_calls": PIPELINE_MAX_LLM_CALLS}
    _prog_dir = Path(settings.SQLITE_DB_PATH).parent  # 进度心跳文件目录（ingest_progress.json）

    for batch_num, data in _batch_stream(file_path, cfg, raw_stream, ai, ai_batch_pages,
                                         overlap, max_tokens, budget,
                                         route=(only_tables is None), tier1_kwargs=tier1_kwargs):
        if data.get("tables"):
            data["tables"] = _apply_focus(data["tables"], cfg)  # 聚焦在 runner 层
            # 收口——unified/fc 两条提取路径语义一致（fc 侧 schema 裁剪是省 token
            # 的提示层，本层是语义保证层；静默答非所问面归零）
        all_results["batches"].append({"batch": batch_num, "data": data})
        if data.get("unmapped"):
            all_results["unmapped"].extend(data["unmapped"])
        if data.get("_batch_error"):
            # 提取失败的批次（AI 重试后仍失败）：记录供最终报告显性告知
            all_results["failed_batches"].append(
                {"batch": batch_num, "reason": data["_batch_error"]})

        # 进度心跳：每批落盘进度文件（前端轮询展示，白盒可审计）
        _write_progress(_prog_dir, Path(file_path).name, batch_num,
                        budget.get("units", 0), "running")

        # 入库阶段（ingestion.py）：按主表业务编码分组写入（savepoint/saga）
        write_batch_groups(data, cfg, drv, main_table, code_field, overwrite, all_results)

    # 完成：进度文件标记完成态（前端停止轮询）
    _write_progress(_prog_dir, Path(file_path).name, "done",
                    budget.get("units", 0), "done")

    return _finalize_run(all_results, budget, table_page_texts, vs, collection_name,
                         Path(file_path).name, t1_state)


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
