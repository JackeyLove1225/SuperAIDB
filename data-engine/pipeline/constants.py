"""文档处理管线批粒度常量（白盒集中，消灭字面量）

设计定稿（2026-07-20，见 docs/终版Demo前优化清单.md 一·五节）：

- tier-1 代码提取（原始文件→文字流）：每批最多 TIER1_BATCH_UNITS 个流单元
  flush 一次向量库。流单元按格式定义：PDF=页，Excel=TIER1_EXCEL_ROWS 行块，
  Word=TIER1_DOCX_PARAS 段批。
- tier-2 AI 识别（文字→统一结构化 {"tables":[{"name","rows"}]}）：
  每次 AI 调用最多 TIER2_BATCH_UNITS 个流单元，并带上一批末尾
  TIER2_OVERLAP_UNITS 个单元作上下文（跨页/跨块表格表头不断裂）。

调整批粒度只改本文件。不做 .env 运行时覆盖：常量的意义是白盒集中、
可 grep、可审查、git 留痕，运行时覆盖会制造隐蔽行为。
"""

# ── tier-1 代码提取 ──
TIER1_BATCH_UNITS = 5    # 每次 flush 向量库的流单元数（PDF 页 / Excel 行块 / Word 段批）
TIER1_EXCEL_ROWS = 500   # Excel 每个流单元的最大行数（大 sheet 按此切块，跨块携带表头）
TIER1_DOCX_PARAS = 50    # Word 每个流单元的最大段落数

# ── tier-2 AI 结构化 ──
TIER2_BATCH_UNITS = 3    # 每次 AI 调用的流单元数
TIER2_OVERLAP_UNITS = 1  # 上下文重叠单元数（上一批末尾 N 单元，标注"请勿提取"）

# ── LLM 预算护栏 ──
# 防大文件入库时 FC 调用次数/成本失控：超预算即中止并汇报进度，剩余部分可续入
PIPELINE_MAX_LLM_CALLS = 200      # 单次入库任务允许的 FC 调用上限
PIPELINE_MAX_TOKENS_PER_CALL = 65536  # 单次 FC 调用的 max_tokens 上限
