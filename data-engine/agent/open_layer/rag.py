"""RAG 文档检索——开放式 AI 的文档理解能力

通过 ChromaDB 向量数据库检索已上传文档的文本内容。
与数据库操作（P1→树→P2）分离：RAG 负责文档内容问答，不操作关系数据库。

检索策略：混合检索（向量 + 关键词）+ 结果去重
- 向量检索：ChromaDB 原生语义检索
- 关键词重排：对候选结果按关键词命中率加分
- 去重：相同来源+页码的片段只保留最相关的
"""

import sys
import os
import re
from pathlib import Path

# 确保项目根目录在 sys.path 中
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 隔离运行环境可能未安装 chromadb，
# 需添加系统 Python 的 site-packages 作为回退路径，让 chromadb 及其依赖（opentelemetry 等）可被导入。
# 优先使用 site.getsitepackages()，回退到常见路径推断。
try:
    import site
    _system_sites = site.getsitepackages()
except Exception:
    _system_sites = []
if not _system_sites:
    _candidate = os.path.join(sys.base_prefix, 'Lib', 'site-packages')
    if os.path.isdir(_candidate):
        _system_sites = [_candidate]
for _s in _system_sites:
    if _s not in sys.path:
        sys.path.append(_s)


def _get_vector_store():
    """获取向量数据库实例"""
    from core.vector_store import get_vector_store
    return get_vector_store()


def list_document_collections() -> list[str]:
    """列出所有已入库的文档集合（collection 名 = 文件名无扩展名）"""
    vs = _get_vector_store()
    if not vs:
        return []
    try:
        return vs.list_collections()
    except Exception:
        return []


# ── 关键词提取与匹配 ──

# 中文停用词（高频无意义词）
_STOP_WORDS = frozenset({
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没",
    "看", "好", "自", "己", "这", "那", "它", "他", "她", "们", "什么",
    "怎么", "哪个", "哪些", "哪个", "请", "帮", "查询", "查一下", "告诉",
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to",
    "for", "of", "and", "or", "not", "from", "by", "with", "as",
})


def _extract_keywords(query: str) -> list[str]:
    """从查询中提取关键词

    策略：
    1. 英文/数字：按单词分词，转小写，过滤停用词
    2. 中文：先按停用词字符分割为短语，再提取 2-gram（二元组）
       - 停用词字符（如"的"、"了"、"在"）作为自然分隔符
       - 对每个短语提取 2-gram，过滤含停用词字符的 gram
    """
    keywords = []

    # 提取所有"词块"（中文连续字符 / 英文单词 / 数字串）
    tokens = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+|\d+', query)

    # 中文停用词单字符集合（用于分割和过滤）
    cn_stop_chars = set("的了在是我就不人都一个上也到说要去你会看着好自己和这那他她它们")
    # 常见中文动词/量词（作为分割符）
    cn_separators = set("查查询找请帮告诉下一上下左右中")

    for tok in tokens:
        # 英文：转小写，过滤停用词和过短词
        if tok.isascii():
            tok_lower = tok.lower()
            if len(tok_lower) >= 2 and tok_lower not in _STOP_WORDS:
                keywords.append(tok_lower)
            continue

        # 中文处理
        # 1. 用停用词字符和分隔符将句子拆分为短语
        phrase = []
        phrases = []
        for ch in tok:
            if ch in cn_stop_chars or ch in cn_separators:
                if phrase:
                    phrases.append("".join(phrase))
                    phrase = []
            else:
                phrase.append(ch)
        if phrase:
            phrases.append("".join(phrase))

        # 2. 对每个短语提取关键词
        for p in phrases:
            if len(p) >= 2:
                # 短语（2-3字）直接作为关键词
                if len(p) <= 3:
                    keywords.append(p)
                else:
                    # 长短语提取 2-gram
                    for i in range(len(p) - 1):
                        gram = p[i:i + 2]
                        # 过滤含停用词字符的 gram
                        if not any(c in cn_stop_chars for c in gram):
                            keywords.append(gram)
                    # 也保留 3-gram（如果短语够长）
                    if len(p) >= 4:
                        for i in range(len(p) - 2):
                            gram3 = p[i:i + 3]
                            if not any(c in cn_stop_chars for c in gram3):
                                keywords.append(gram3)

    return keywords


def _keyword_score(text: str, keywords: list[str]) -> float:
    """计算文本与关键词的匹配得分

    得分 = 匹配的关键词数 / 总关键词数（0~1）
    每个关键词在文本中出现即得分，多次出现不额外加分。
    """
    if not keywords:
        return 0.0
    text_lower = text.lower()
    matched = 0
    for kw in keywords:
        if kw.lower() in text_lower:
            matched += 1
    return matched / len(keywords)


def _deduplicate(results: list[dict]) -> list[dict]:
    """结果去重——相同来源+页码的片段只保留相似度最高的（distance 最小的）"""
    seen = {}  # key: (source, page) -> index in results
    deduped = []
    for r in results:
        key = (r.get("source", ""), r.get("page", ""))
        if key in seen:
            # 已存在，比较相似度，保留 distance 更小的
            existing_idx = seen[key]
            if r["distance"] < deduped[existing_idx]["distance"]:
                deduped[existing_idx] = r
        else:
            seen[key] = len(deduped)
            deduped.append(r)
    return deduped


# 候选结果总上限（所有集合合并后的硬上限，防止大文档库撑爆内存）
_MAX_CANDIDATES_TOTAL = 500


def _hybrid_search(vs, collections: list[str], query: str, top_k: int = 5) -> list[dict]:
    """混合检索：向量检索 + 关键词重排 + 去重

    流程：
    1. 向量检索：每个集合取 top_k*3 个候选（扩大召回）
    2. 关键词重排：对每个候选计算关键词匹配分，与向量相似度加权融合
    3. 去重：相同来源+页码的只保留最相关的
    4. 截断：返回 top_k*2 个最终结果

    融合公式：final_score = 0.7 * (1 - distance) + 0.3 * keyword_score

    内存优化：
    - 候选结果总上限 _MAX_CANDIDATES_TOTAL（防止大文档库撑爆内存）
    - 文本字段长度限制（防止异常大片段）
    - 关键词评分复用 text_lower 避免重复 .lower()
    """
    keywords = _extract_keywords(query)
    candidate_limit = top_k * 3  # 扩大召回量
    # 每个集合召回上限：避免集合数过多时总候选爆炸
    per_col_limit = max(candidate_limit, _MAX_CANDIDATES_TOTAL // max(1, len(collections)))

    all_results = []
    for col in collections:
        try:
            hits = vs.search(col, query, top_k=per_col_limit)
        except Exception:
            continue
        for hit in hits:
            text = hit.get("text", "")
            # 单片段文本长度限制（防止异常大 chunk 撑爆内存）
            if len(text) > 5000:
                text = text[:5000] + "...（已截断）"
            meta = hit.get("metadata", {})
            dist = hit.get("distance", 0)

            # 向量相似度（distance 越小越相似，转换为 0~1 的相似度）
            vector_sim = max(0, 1 - dist)

            # 关键词匹配分（复用 text_lower 避免重复 .lower()）
            if keywords:
                text_lower = text.lower()
                matched = sum(1 for kw in keywords if kw.lower() in text_lower)
                kw_score = matched / len(keywords)
            else:
                kw_score = 0.0

            # 融合分数（关键词为空时纯向量）
            if keywords:
                final_score = 0.7 * vector_sim + 0.3 * kw_score
            else:
                final_score = vector_sim

            all_results.append({
                "collection": col,
                "source": meta.get("source", col),
                "page": meta.get("page", "?"),
                "text": text,
                "distance": dist,
                "vector_sim": vector_sim,
                "keyword_score": kw_score,
                "final_score": final_score,
            })

            # 总候选数硬上限保护
            if len(all_results) >= _MAX_CANDIDATES_TOTAL:
                break
        if len(all_results) >= _MAX_CANDIDATES_TOTAL:
            break

    # 去重
    all_results = _deduplicate(all_results)

    # 按融合分数降序排序（分数越高越相关）
    all_results.sort(key=lambda x: x["final_score"], reverse=True)

    return all_results[:top_k * 2]


def search_documents(query: str, collection: str = "", top_k: int = 5) -> str:
    """在向量数据库中检索文档内容（混合检索：向量 + 关键词）

    Args:
        query: 检索查询（自然语言）
        collection: 指定文档集合名（文件名无扩展名），为空则搜索所有集合
        top_k: 每个集合返回的最大结果数

    Returns:
        检索结果文本（包含来源页码和相似度）
    """
    vs = _get_vector_store()
    if not vs:
        return "向量数据库未初始化，无法进行文档检索"

    collections = [collection] if collection else list_document_collections()
    if not collections:
        return "暂无已入库的文档。请先上传文件并执行文件入库操作。"

    # 混合检索
    results = _hybrid_search(vs, collections, query, top_k=top_k)

    if not results:
        return f"未在 {len(collections)} 个文档集合中找到与 '{query}' 相关的内容"

    # 格式化输出
    lines = [f"文档检索结果（{len(results)} 条，来自 {len(collections)} 个文档，混合检索：向量+关键词）：\n"]
    for i, r in enumerate(results, 1):
        # 显示综合相似度（百分比）
        sim_pct = r["final_score"]
        # 如果有关键词匹配，额外标注
        kw_marker = ""
        if r["keyword_score"] > 0:
            kw_marker = f"，关键词匹配{r['keyword_score']:.0%}"
        lines.append(
            f"--- 结果 {i}（来源: {r['source']}, 第{r['page']}页, "
            f"综合相似度: {sim_pct:.2%}{kw_marker}）---"
        )
        lines.append(r["text"][:500])
        if len(r["text"]) > 500:
            lines.append("...")
        lines.append("")

    return "\n".join(lines)
