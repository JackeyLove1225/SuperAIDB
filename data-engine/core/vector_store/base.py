"""向量数据库抽象基类"""
from abc import ABC, abstractmethod


class VectorStore(ABC):
    @abstractmethod
    def add(self, collection: str, texts: list[str], metadatas: list[dict] = None):
        pass

    @abstractmethod
    def search(self, collection: str, query: str, top_k: int = 10) -> list[dict]:
        pass
