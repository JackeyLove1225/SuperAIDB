"""向量数据库工厂

单例优化：get_vector_store() 缓存实例，避免每次调用都重新初始化 ChromaDB
（PersistentClient 初始化会打开 sqlite + 加载 HNSW 索引，是重操作）

失败语义：初始化失败不再 print 后返回 None（静默+8 处各自判空），
而是返回 NullVectorStore——实例携带失败原因，任何实际操作抛出明确错误；
`bool(NullVectorStore)` 为 False，兼容存量 `if not vs` 判空写法。
"""
from core.logger import get_logger

from config.settings import settings as s

logger = get_logger(__name__)

# 全局单例缓存
_vector_store_instance = None


class NullVectorStore:
    """向量库不可用时的占位实例：携带失败原因，任何实际操作抛出明确错误。

    - `bool(vs)` 为 False：存量 `if not vs:` 判空分支行为不变
    - `vs.reason`：失败原因，供上层显式上报
    - 直接调用 add/search/count/list_collections：raise RuntimeError（原因明确）
    """

    def __init__(self, reason: str):
        self.reason = reason

    def __bool__(self):
        return False

    def _fail(self):
        raise RuntimeError(f"向量数据库不可用: {self.reason}")

    def add(self, *args, **kwargs):
        self._fail()

    def search(self, *args, **kwargs):
        self._fail()

    def count(self, *args, **kwargs):
        self._fail()

    def list_collections(self, *args, **kwargs):
        self._fail()


def get_vector_store():
    """获取向量数据库实例（单例）

    首次调用时根据 VECTOR_STORE_TYPE 创建实例并缓存，
    后续调用直接返回缓存实例。
    初始化失败/类型未实现时返回 NullVectorStore（不会返回 None）。
    """
    global _vector_store_instance
    if _vector_store_instance is not None:
        return _vector_store_instance

    t = s.VECTOR_STORE_TYPE
    if t == "chroma":
        try:
            from .chroma_store import ChromaStore
            _vector_store_instance = ChromaStore()
        except Exception as e:
            logger.error("向量数据库初始化失败: %s", e)
            _vector_store_instance = NullVectorStore(f"chroma 初始化失败: {e}")
    elif t in ("pgvector", "postgresql"):
        # 预留：pgvector 后端尚未实现（.pgvector_store 模块不存在）。
        # 将来实现后在此 import 并实例化 PGVectorStore。
        _vector_store_instance = NullVectorStore(
            f"向量数据库类型 {t} 尚未实现（预留），当前仅支持 chroma")
    else:
        _vector_store_instance = NullVectorStore(f"未知的向量数据库类型: {t}")
    return _vector_store_instance


def reset_vector_store():
    """重置向量数据库实例（行业切换时经 registry 统一触发）"""
    global _vector_store_instance
    _vector_store_instance = None


# 自注册到重置注册表（P-H：行业切换遍历即覆盖，不漏员）
from core.registry import register_reset

register_reset("vector_store", reset_vector_store)
