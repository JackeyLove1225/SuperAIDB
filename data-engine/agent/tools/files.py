"""文件域——文件处理/上传/向量检索/导出工具 handler。

process_file / upload_file / search_documents / list_vector_collections /
export_data。
"""
from pathlib import Path
import time as _time
import shutil
from core.logger import get_logger

from core.tool_result import ToolResult
from pipeline.constants import TIER2_BATCH_UNITS
from core.contract.security_contract import safe_table_sql

from agent.tools._shared import _guard_file_path

logger = get_logger(__name__)


def _resolve_filepath(filepath):
    """解析目标文件路径：缺省回落最近上传、收容闸校验、裸文件名兜底

    返回 (filepath, None)；任一校验不过返回 (filepath, 失败 ToolResult)。
    """
    from config.settings import settings
    if not filepath:
        filepath = settings.current_file
        if not filepath:
            return filepath, ToolResult.fail("请先上传文件，或在指令中指定文件路径",
                                             code="VALIDATION", reason="missing_params")
    # 路径收容闸（20260822）：工作区外路径一律拒绝
    _bad = _guard_file_path(filepath)
    if _bad:
        return filepath, ToolResult.fail(_bad, code="SECURITY", reason="path_out_of_workspace")
    # 裸文件名兜底：AI 给的 filepath 可能只是文件名（没带服务器路径），
    # 在 uploads/ 下按文件名递归查找最近上传的匹配文件
    if filepath and not Path(filepath).exists():
        from config.settings import settings as _s
        upload_root = Path(getattr(_s, "UPLOAD_DIR", "") or "uploads")
        if not upload_root.is_absolute():
            # 拆包后本文件在 agent/tools/ 下，多退一层，仍锚定 data-engine 根
            upload_root = Path(__file__).resolve().parent.parent.parent / upload_root
        base = Path(filepath).name
        candidates = (sorted(upload_root.rglob(base), key=lambda p: p.stat().st_mtime, reverse=True)
                      if upload_root.exists() else [])
        if len(candidates) > 1:
            # 同名多版本不静默选最新——报错列出候选，让 AI/用户明确指定完整路径
            listing = "；".join(
                f"{cand}（{_time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime(cand.stat().st_mtime))}）"
                for cand in candidates[:5])
            return filepath, ToolResult.fail(
                f"找到 {len(candidates)} 个同名文件 '{base}'，无法确定使用哪个：{listing}"
                f"。请指定完整路径后重试。",
                code="VALIDATION", reason="ambiguous_filepath",
                candidates=[str(c) for c in candidates[:5]])
        if candidates:
            logger.info("filepath 裸文件名解析: %s → %s", filepath, candidates[0])
            filepath = str(candidates[0])
    return filepath, None


def _ingest_text_file(filepath):
    """纯文本类文件（txt/md/csv/json）切段直进向量库，不走表格提取 pipeline"""
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="ignore")
        chunks = [text[i:i + 1500] for i in range(0, len(text), 1500)
                  if text[i:i + 1500].strip()]
        if not chunks:
            return ToolResult.fail(f"文件 {Path(filepath).name} 无有效文本内容",
                                   code="VALIDATION", reason="empty_content")
        from core.vector_store import get_vector_store
        vs = get_vector_store()
        col = Path(filepath).stem
        metas = [{"source": Path(filepath).name, "page": i + 1}
                 for i in range(len(chunks))]
        vs.add(col, chunks, metas)
        return ToolResult.ok(
            f"文本文件已入向量库: {Path(filepath).name} → 集合 {col}，"
            f"共 {len(chunks)} 段（纯文本无需建表，可直接文档问答）",
            collection=col, chunks=len(chunks))
    except Exception as e:
        return ToolResult.fail(f"文本入库失败: {e}", code="UNKNOWN",
                               reason="ingest_failed")


def _run_extract_pipeline(filepath, page_start, page_end, batch_size, overwrite, tables, fields=""):
    """提取分派：调 pipeline.runner.run，异常按语义映射为显式失败回执

    返回 (result, None)；失败返回 (None, ToolResult)。
    """
    from config.settings import settings
    from pipeline.runner import run as _run
    # 用户输入 1-indexed 流单元序号，pipeline 用 0-indexed
    ps = max(0, page_start - 1)
    # page_end 未指定时默认全量：整份文件按 batch_size 自动分批处理完，无需人工续批确认
    # （防内存靠流式分批：每批 batch_size 页打一次 LLM 调用，任意页数都一样跑）
    page_limit = (page_end - page_start + 1) if page_end > 0 else None
    only_tables = [s.strip() for s in tables.split(",") if s.strip()] or None
    focus_fields = [s.strip() for s in fields.split(",") if s.strip()] or None
    try:
        return _run(filepath, industry=settings.INDUSTRY, page_start=ps,
                    page_limit=page_limit, batch_size=batch_size, overwrite=overwrite,
                    only_tables=only_tables, fields=focus_fields), None
    except ValueError as e:
        return None, ToolResult.fail(f"处理失败: {e}", code="VALIDATION",
                                     reason="pipeline_rejected")
    except Exception as e:
        # 403 语义不在工具出口吞掉：ingestion 层精心上抛的
        # PermissionDenied 若在此映射为 UNKNOWN，审计链断在工具边界，
        # 且 AI 会按可重试故障白烧调用
        from core.permission import PermissionDenied
        if isinstance(e, PermissionDenied):
            return None, ToolResult.fail(f"权限不足：{e}", code="CONTRACT",
                                         reason="permission_denied")
        return None, ToolResult.fail(f"处理异常: {str(e)[:200]}", code="UNKNOWN",
                                     reason="pipeline_exception")


def _record_unmapped(lines, result, filepath, tables):
    """未映射项（源键/表头无法映射到任何表字段）如实列出，并把带系统猜测的
    候选项挂起为待确认映射（用户回"对/忽略/是X表Y字段"即存档补录）"""
    unmapped = result.get("unmapped", [])
    if not unmapped:
        return
    lines.append(f"未映射 {len(unmapped)} 项（数据未入库，可告诉我映射关系后补录）:")
    for u in unmapped[:5]:
        kind = u.get("kind", "?")
        key = u.get("key") or u.get("headers") or "?"
        lines.append(f"  - [{kind}] {str(key)[:60]}")
    if len(unmapped) > 5:
        lines.append(f"  ... 共 {len(unmapped)} 项")
    # 挂起待确认映射：附系统猜测
    try:
        from config.settings import settings
        from core.context import get_context
        from pipeline.unified import suggest_mapping, _field_index
        from industries.base import discover_industries, get_industry
        discover_industries()
        _cfg = get_industry(settings.INDUSTRY)
        _aliases = (_cfg.terminology or {}).get("table_aliases", {}) or {}
        _fidx = _field_index(_cfg.tables, _aliases)
        pending = []
        for u in unmapped:
            key = u.get("key") or ""
            if not key:
                continue
            gt, gf, score = suggest_mapping(key, _fidx)
            pending.append({"kind": u["kind"], "key": key, "item": u,
                            "guess_table": gt, "guess_field": gf, "score": score})
        if pending:
            get_context().save("pending_unmapped", {
                "filepath": filepath, "items": pending,
                "tables": tables or "",
            })
            lines.append("映射确认（逐项回复，或一次说完如“1对 2忽略”）:")
            for i, p in enumerate(pending[:5], 1):
                guess = (f"我猜是 {p['guess_table']}.{p['guess_field']}"
                         f"（置信 {p['score']}）" if p["guess_field"] else "没有候选")
                lines.append(f"  {i}. \"{p['key']}\"——{guess}，对吗？")
    except Exception as _e:
        logger.warning("挂起待确认映射失败（不影响主流程）: %s", _e)


def _append_ingest_stats(lines, all_tables, before, drv, failed_batches):
    """执行后各表行数对比 → 追加"数据入库统计"段，返回 table_diffs（供对账复查）"""
    lines.append("数据入库统计:")
    any_new = False
    table_diffs = {}
    for t in all_tables:
        try:
            after_count = drv.query(f'SELECT COUNT(*) as c FROM {safe_table_sql(t)}')[0]['c']
        except Exception:
            after_count = 0
        diff = after_count - before[t]
        if diff != 0:
            table_diffs[t] = diff
        if diff > 0:
            any_new = True
            lines.append(f"  {t}: +{diff} 条 (共{after_count}条)")
        elif after_count > 0:
            # 零新增也如实显示 +0（只报裸总数会让用户误以为本次入了 N 条）
            lines.append(f"  {t}: +0 条 (共{after_count}条)")
    if not any_new and not failed_batches:
        lines.append("  本次无新数据入库（可能全部与已有数据重复，或内容未匹配到目标表）。")
    return table_diffs


def _build_process_result(filepath, result, before, all_tables, drv, page_start, page_end, tables):
    """结果装配：执行后行数对比、友好摘要文本、双轨负载 ToolResult"""
    ext = Path(filepath).suffix.lower()
    if ext in (".xlsx", ".xls"):
        unit = "块"
    elif ext == ".docx":
        unit = "段"
    else:
        unit = "页"
    end_display = page_end or "全部"
    total_info = f"（共 {result.get('pages', '?')}{unit}）" if page_end == 0 else ""
    lines = [f"处理完成: {Path(filepath).name}，第{page_start}-{end_display}{unit}{total_info}"]
    lines.append(f"批次数: {len(result.get('batches', []))}")
    # 系统性错误显著上报（与"行坏数据"区分：这是代码/配置问题，不是数据问题）
    _sys_err = result.get("systemic_error")
    if _sys_err:
        lines.append(f"❌ 系统性错误：全部 {_sys_err['groups']} 组写入失败，原因相同——"
                     f"{_sys_err['reason']}。这不是数据问题，请检查系统/配置，"
                     f"修复后重试（本批数据未丢失，可重新入库）。")
    if result.get("budget_stopped"):
        _bs = result["budget_stopped"]
        lines.append(f"⚠️ LLM 调用预算超限（{_bs['llm_calls']} 次），本次处理到第 "
                     f"{page_start + _bs['units_done'] - 1}{unit}后中止（已入库部分保留）。"
                     f"继续请说「入库第{page_start + _bs['units_done']}{unit}起的区间」。")
    # 提取失败批次（AI 结构化重试后仍失败）：这些页的数据未入库，必须显性告知——
    # 绝不能以"处理完成"掩盖数据缺口
    _failed = result.get("failed_batches") or []
    if _failed:
        _bn = "、".join(str(f["batch"]) for f in _failed)
        lines.append(f"❌ 第 {_bn} 批提取失败（共 {len(result.get('batches', []))} 批）："
                     f"对应{unit}的数据未入库。原因：{_failed[0]['reason'][:80]}。"
                     f"多为 AI 服务偶发异常，重发同一指令即可重试（已入库数据幂等不重复）。")
    if result.get('conflicts'):
        lines.append(f"冲突: {len(result['conflicts'])} 条 (编码: {', '.join(str(x) for x in result['conflicts'][:5])})")
    # 行级失败清单（独立数据的坏行：好行已入库，坏行及原因列出供修正后补录）
    failures = result.get("failures", [])
    if failures:
        lines.append(f"⚠️ 跳过 {len(failures)} 行坏数据（好行已正常入库）:")
        for f in failures[:5]:
            row_str = str(f.get("row", ""))[:60]
            lines.append(f"  [{f.get('table')}] {row_str} — {f.get('reason', '')[:50]}")
        if len(failures) > 5:
            lines.append(f"  ... 共 {len(failures)} 行")
    _record_unmapped(lines, result, filepath, tables)
    table_diffs = _append_ingest_stats(lines, all_tables, before, drv, _failed)
    # 双轨负载：管线统计整体进 data（effects=各表行数增量，供对账复查）
    _sys_err = result.get("systemic_error")
    tr = ToolResult("\n".join(lines), {
        "ok": not bool(_sys_err),
        "code": "UNKNOWN" if _sys_err else "OK",
        "file": str(filepath),
        "pages": result.get("pages"),
        "batches": len(result.get("batches", [])),
        "failed_batches": result.get("failed_batches") or [],
        "conflicts": result.get("conflicts") or [],
        "failures": result.get("failures", []),
        "unmapped": result.get("unmapped", []),
        "budget_stopped": result.get("budget_stopped"),
        "table_diffs": table_diffs,
    })
    if _sys_err:
        tr.data["reason"] = "systemic_error"
    if table_diffs:
        tr.data["effects"] = {
            "table": sorted(table_diffs), "action": "INSERT",
            "affected": sum(v for v in table_diffs.values() if v > 0),
            "affected_ids": [], "changed_fields": [],
            "table_diffs": table_diffs,
        }
    return tr


def process_file(filepath="", page_start=1, page_end=0, overwrite=False, database="", batch_size=TIER2_BATCH_UNITS, tables="", fields=""):
    """处理上传的文件（PDF/Excel/Word），提取表格数据并录入数据库

    页码为 1-indexed（用户视角）：第22页=page_start=22，内部会转为 0-indexed。
    Excel 文件中 page_start/page_end 对应 sheet 序号（1-indexed）。
    Word 文件中 page_start/page_end 对应逻辑批次序号（1-indexed）。
    filepath 为空时自动使用最近上传的文件（settings.current_file）。
    tables 非空时限定提取目标表（逗号分隔表名，供全自动建库流程指定新建表，
    避免数据被错提到行业既有不相关表）。
    """
    from config.settings import settings
    filepath, err = _resolve_filepath(filepath)
    if err:
        return err

    # 纯文本类文件（txt/md/csv/json）不走表格提取 pipeline，直接入向量库
    if Path(filepath).suffix.lower() in (".txt", ".md", ".csv", ".json"):
        return _ingest_text_file(filepath)

    from pipeline.runner import _check_file_size
    from core.data_ops import get_driver
    from industries.base import discover_industries, get_industry
    discover_industries()
    cfg = get_industry(settings.INDUSTRY)
    all_tables = [t["name"] for t in cfg.tables]

    # 文件大小预检查
    try:
        _check_file_size(filepath)
    except ValueError as e:
        return ToolResult.fail(str(e), code="VALIDATION", reason="file_too_large")

    # 执行前记录各表行数
    drv = get_driver()
    before = {}
    for t in all_tables:
        try:
            before[t] = drv.query(f"SELECT COUNT(*) as c FROM {safe_table_sql(t)}")[0]['c']
        except Exception:
            before[t] = 0

    result, err = _run_extract_pipeline(filepath, page_start, page_end,
                                        batch_size, overwrite, tables, fields)
    if err:
        return err

    # 执行后查询各表行数，构建友好摘要与双轨负载
    return _build_process_result(filepath, result, before, all_tables, drv,
                                 page_start, page_end, tables)


def upload_file(filepath="", batch=False, database=""):
    if not filepath:
        return ToolResult.fail("请指定本地文件路径", code="VALIDATION",
                               reason="missing_params")
    # 路径收容闸（20260822）：批量逐个守，工作区外一律拒绝
    _cands = [p.strip() for p in filepath.split(",") if p.strip()] if batch else [filepath]
    for _c in _cands:
        _bad = _guard_file_path(_c)
        if _bad:
            return ToolResult.fail(_bad, code="SECURITY", reason="path_out_of_workspace")
    from config.settings import settings
    if batch:
        paths = [p.strip() for p in filepath.split(",") if p.strip()]
        results = []
        uploaded = []
        missing = []
        for p in paths:
            src = Path(p)
            if not src.exists():
                results.append(f"文件不存在: {p}")
                missing.append(p)
                continue
            dst = Path("uploads") / src.name
            shutil.copy2(str(src), str(dst))
            results.append(f"已上传: {dst.name}")
            uploaded.append(str(dst))
        if missing and not uploaded:
            return ToolResult.fail("\n".join(results), code="NOT_FOUND",
                                   reason="file_not_found", missing=missing)
        return ToolResult.ok("\n".join(results), uploaded=uploaded,
                             missing=missing, count=len(uploaded))
    src = Path(filepath)
    if not src.exists():
        return ToolResult.fail(f"文件不存在: {filepath}", code="NOT_FOUND",
                               reason="file_not_found")
    dst = Path("uploads") / src.name
    shutil.copy2(str(src), str(dst))
    settings.set_current_file(str(dst))
    return ToolResult.ok(f"已上传: {dst.name}", uploaded=[str(dst)], count=1)


def search_documents(query="", collection="", top_k=5, database=""):
    """搜索向量数据库中的文字内容（双轨：data 带 {hits, count, collections}）"""
    if not query:
        return ToolResult.fail("请输入搜索内容", code="VALIDATION",
                               reason="missing_params")
    from core.vector_store import get_vector_store
    vs = get_vector_store()
    if not vs:
        return ToolResult.fail(
            f"向量数据库未配置（{getattr(vs, 'reason', '未知原因')}）",
            code="UNKNOWN", reason="vector_store_unavailable")
    # 如果未指定 collection，搜索所有
    if collection:
        collections = [collection]
    else:
        collections = vs.list_collections()
    if not collections:
        return ToolResult.fail("向量数据库为空，请先上传文件处理",
                               code="NOT_FOUND", reason="vector_store_empty")
    results = []
    for col in collections:
        hits = vs.search(col, query, top_k=top_k)
        for h in hits:
            results.append({"collection": col, "text": h["text"][:500], "metadata": h["metadata"], "distance": round(h["distance"], 4)})
    if not results:
        return ToolResult.ok("未找到匹配内容", hits=[], count=0,
                             collections=collections)
    # 按距离排序（越小越相似）
    results.sort(key=lambda x: x["distance"])
    lines = [f"找到 {len(results)} 条匹配（按相似度排序）:"]
    for i, r in enumerate(results[:top_k]):
        meta = r["metadata"]
        lines.append(f"\n--- 结果 {i+1} (来源: {r['collection']}, 页码: {meta.get('page','?')}, 距离: {r['distance']}) ---")
        lines.append(r["text"][:300])
    return ToolResult.ok("\n".join(lines), hits=results[:top_k],
                         count=len(results), collections=collections)


def export_data_tool(table="", selection_id=0, where="", format="csv", database=""):
    """导出数据为 CSV 或 Excel 文件（双轨：data 带 {path, format, table}）"""
    from core.exporter import export_table_to_csv, export_selection_to_csv, export_table_to_excel
    fmt = (format or "csv").lower().strip()
    if fmt not in ("csv", "excel"):
        return ToolResult.fail(f"不支持的导出格式: {format}，只支持 csv 或 excel",
                               code="VALIDATION", reason="unsupported_format")
    if selection_id:
        if fmt != "csv":
            return ToolResult.fail("选择集导出暂只支持 CSV 格式",
                                   code="VALIDATION", reason="unsupported_format")
        result = export_selection_to_csv(selection_id)
    elif table:
        if fmt == "excel":
            result = export_table_to_excel(table, where=where)
        else:
            result = export_table_to_csv(table, where=where)
    else:
        return ToolResult.fail("请指定要导出的表名或选择集编号",
                               code="VALIDATION", reason="missing_params")
    if result["ok"]:
        return ToolResult.ok(f"{result['message']}（路径: {result['path']}）",
                             path=result["path"], format=fmt, table=table or None,
                             selection_id=selection_id or None)
    return ToolResult.fail(result["message"], code="UNKNOWN",
                           reason="export_failed", table=table or None)


def list_vector_collections(database=""):
    """列出向量数据库中的所有文件集合（双轨：data 带 {collections, count}）"""
    from core.vector_store import get_vector_store
    vs = get_vector_store()
    if not vs:
        return ToolResult.fail(
            f"向量数据库未配置（{getattr(vs, 'reason', '未知原因')}）",
            code="UNKNOWN", reason="vector_store_unavailable")
    cols = vs.list_collections()
    if not cols:
        return ToolResult.ok("向量数据库为空", collections=[], count=0)
    lines = ["| 序号 | 文件名 | 文档数 |", "|------|--------|--------|"]
    col_meta = []
    for i, col in enumerate(cols):
        cnt = vs.count(col)
        lines.append(f"| {i+1} | {col} | {cnt} |")
        col_meta.append({"collection": col, "doc_count": cnt})
    return ToolResult.ok("\n".join(lines), collections=col_meta, count=len(col_meta))
