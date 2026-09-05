"""离线预置 embedding 模型（D10 交付链）

全新机器/弱网环境首次使用 RAG（向量检索）前跑一遍，把模型下载成本从
"用户首次提问时卡住"挪到部署时一次性完成：

  python scripts/prefetch_models.py                     # Chroma 内置默认模型
  python scripts/prefetch_models.py bge-small-zh-v1.5   # 指定 sentence-transformers 型号
                                                           （与 config/.env 的 EMBEDDING_MODEL 对齐）
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    model = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if not model:
        # Chroma 内置默认（all-MiniLM-L6-v2 的 onnx 变体）：触发一次 dummy embed 即下载
        print("预置 Chroma 内置默认 embedding 模型…")
        try:
            import chromadb
            ef = None
            try:
                from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
                ef = DefaultEmbeddingFunction()
            except Exception:
                pass  # 老版本 chromadb 无此类——走 collection 默认通道
            client = chromadb.EphemeralClient()
            kwargs = {"embedding_function": ef} if ef else {}
            coll = client.get_or_create_collection("prefetch_probe", **kwargs)
            coll.add(ids=["probe"], documents=["预热"])
            coll.query(query_texts=["预热"], n_results=1)
            print("✓ 内置模型就绪")
            return 0
        except Exception as e:
            print(f"✗ 内置模型预置失败: {e}")
            return 1
    print(f"预置 sentence-transformers 模型: {model} …")
    try:
        from sentence_transformers import SentenceTransformer
        SentenceTransformer(model)
        print(f"✓ 模型 {model} 就绪（已入 HF 缓存）")
        return 0
    except ImportError:
        print("✗ 需要 sentence-transformers：pip install sentence-transformers")
        return 1
    except Exception as e:
        print(f"✗ 模型 {model} 下载失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
