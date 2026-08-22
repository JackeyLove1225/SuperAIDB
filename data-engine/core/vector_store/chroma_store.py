"""Chroma 向量数据库实现——本地持久化

Embedding 模型：
- 默认（EMBEDDING_MODEL 为空）：Chroma 内置 onnxruntime 版 all-MiniLM-L6-v2（英文向）。
  首次使用需联网下载模型权重（缓存于 ~/.cache/chroma/onnx_models），离线环境需预置该缓存。
- 配置 EMBEDDING_MODEL 后：通过 chromadb 的 SentenceTransformerEmbeddingFunction 接入
  sentence-transformers 模型（中文场景推荐 BAAI/bge-small-zh-v1.5）。
  sentence-transformers 是可选依赖（不在 requirements.txt），未安装时给出中文警告并降级回内置默认模型。
  首次使用同样需联网下载（缓存于 HF 缓存目录，可用 HF_HOME 指定），离线环境需预置。

入库幂等：
- id 由 (source, page) 稳定生成（无页码元数据时退化为文本内容哈希），add 采用 upsert 语义，
  同一文件重复入库不会翻倍，重跑只会覆盖同页向量。
"""

import hashlib
from pathlib import Path

from config.settings import settings
from core.logger import info as log_info, warning as log_warning
from .base import VectorStore


class ChromaStore(VectorStore):
    """使用 chromadb 的 PersistentClient，数据持久化到磁盘"""

    def __init__(self):
        import chromadb
        chroma_path = Path(settings.CHROMA_PATH).resolve()
        chroma_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(chroma_path))
        # 性能优化：embedding 模型惰性加载——首次向量操作才构建
        # （避免每次启动都加载 BAAI/bge 模型造成的 ~500MB 常驻与启动 CPU 峰值）
        self._embedding_function = None
        log_info(f"ChromaStore 初始化: {chroma_path}")

    @property
    def _ef(self):
        """惰性获取 embedding function（首次调用时才加载模型）"""
        if self._embedding_function is None:
            self._embedding_function = self._build_embedding_function()
        return self._embedding_function

    @staticmethod
    def _build_embedding_function():
        """按配置构建 embedding function；未配置/依赖缺失/构建失败时返回 None（Chroma 内置默认）"""
        model = (getattr(settings, "EMBEDDING_MODEL", "") or "").strip()
        if not model:
            return None  # 保持 Chroma 内置默认模型，行为与历史一致
        device = (getattr(settings, "EMBEDDING_DEVICE", "") or "").strip()
        try:
            from chromadb.utils.embedding_functions import (
                SentenceTransformerEmbeddingFunction,
            )
            kwargs = {"model_name": model}
            if device:
                kwargs["device"] = device
            ef = SentenceTransformerEmbeddingFunction(**kwargs)
            log_info(f"Embedding 模型: {model}" + (f" (device={device})" if device else ""))
            return ef
        except ImportError:
            log_warning(
                f"EMBEDDING_MODEL={model} 需要 sentence-transformers，但未安装"
                f"（pip install sentence-transformers）。已降级为 Chroma 内置默认模型"
            )
            return None
        except Exception as e:
            # 模型名错误/离线无缓存等——降级而不是让 RAG 整体不可用
            log_warning(f"Embedding 模型 {model} 加载失败: {e}。已降级为 Chroma 内置默认模型")
            return None

    @staticmethod
    def _sanitize_collection_name(name: str) -> str:
        """Chroma 集合名约束：3-512 字符 [a-zA-Z0-9._-] 且字母数字开头结尾。
        中文/非法字符清洗后过短或为空时，用 doc_<hash> 兜底（保持同文件同名映射稳定）。"""
        import re as _re
        cleaned = _re.sub(r"[^a-zA-Z0-9._-]", "_", name or "").strip("._-")
        if len(cleaned) < 3:
            cleaned = f"doc_{hashlib.md5((name or 'doc').encode('utf-8')).hexdigest()[:8]}"
        return cleaned[:512]

    def _get_collection(self, collection: str):
        """获取或创建 collection（embedding function 在建 collection 时绑定）"""
        collection = self._sanitize_collection_name(collection)
        ef = self._ef
        if ef is not None:
            return self._client.get_or_create_collection(
                name=collection, embedding_function=ef)
        return self._client.get_or_create_collection(name=collection)

    @staticmethod
    def _make_id(text: str, meta: dict) -> str:
        """生成稳定 id：优先 (source, page)，否则用文本内容哈希

        同一文件同一页重复入库得到相同 id，配合 upsert 实现幂等。
        """
        source = (meta or {}).get("source", "")
        page = (meta or {}).get("page")
        if source and page is not None:
            raw = f"{source}_p{page}"
        else:
            raw = f"content:{text}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def add(self, collection: str, texts: list[str], metadatas: list[dict] = None):
        """添加文本到向量数据库（upsert 语义，幂等）

        Args:
            collection: 集合名（如文件名）
            texts: 文本列表
            metadatas: 元数据列表（如页码、来源等）
        """
        if not texts:
            return
        col = self._get_collection(collection)
        if metadatas is None:
            metadatas = [{} for _ in texts]
        # 稳定 id + upsert：同一 (source, page) 重复入库覆盖而非追加，文档数不翻倍
        ids = [self._make_id(t, m) for t, m in zip(texts, metadatas)]
        if any(metadatas):
            # chroma 要求 metadata 为非空 dict：部分为空时补占位，全空时整体省略
            metadatas = [m if m else {"source": ""} for m in metadatas]
            col.upsert(documents=texts, metadatas=metadatas, ids=ids)
        else:
            col.upsert(documents=texts, ids=ids)
        log_info(f"向量入库: {collection} +{len(texts)} 条 (总计 {col.count()})")

    def search(self, collection: str, query: str, top_k: int = 10) -> list[dict]:
        """搜索向量数据库

        Returns:
            [{"text": "...", "metadata": {...}, "distance": 0.123}, ...]
        """
        col = self._get_collection(collection)
        if col.count() == 0:
            return []
        results = col.query(query_texts=[query], n_results=min(top_k, col.count()))
        out = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        for doc, meta, dist in zip(docs, metas, dists):
            out.append({"text": doc, "metadata": meta, "distance": dist})
        return out

    def list_collections(self) -> list[str]:
        """列出所有 collection"""
        return [c.name for c in self._client.list_collections()]

    def delete_collection(self, collection: str):
        """删除 collection"""
        self._client.delete_collection(name=collection)

    def count(self, collection: str) -> int:
        """返回 collection 中的文档数

        纯元数据操作，不绑定 embedding function——否则控制台等只读页面
        会为一个 count 付出模型加载（网络校验+权重，可达数十秒）的代价
        """
        collection = self._sanitize_collection_name(collection)
        try:
            col = self._client.get_collection(name=collection)
        except Exception:
            return 0
        return col.count()
