"""文档/叙事真值函数——口径守护的单一实现点（single source of truth）

使用方：tests/test_39_doc_coherence.py（层 39 口径守护）、
根仓 sync_showcase.py（发布闸锚点）。同一算法两处引用，防复制漂移。
"""
import ast


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def driver_interface_count(base_py_path: str) -> tuple:
    """驱动接口真值：(抽象方法数, 总数)——类体内 @abstractmethod 计数 +
    非 dunder 共享默认实现计数（AST 动态计数，无魔数锚点）"""
    tree = ast.parse(_read_text(base_py_path))
    n_abs = n_shared = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(getattr(d, "id", "") == "abstractmethod" or
                           getattr(d, "attr", "") == "abstractmethod"
                           for d in fn.decorator_list):
                        n_abs += 1
                    elif not fn.name.startswith("__"):
                        n_shared += 1
    return n_abs, n_abs + n_shared


def mcp_include_count(mcp_server_path: str) -> int:
    """MCP 面白名单 _INCLUDE 元素数（AST 字面量读取，含 frozenset({...})
    包装形——不 exec 目标模块，不承其模块级副作用）"""
    tree = ast.parse(_read_text(mcp_server_path))
    count = None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_INCLUDE"
                        for t in node.targets)):
            continue
        if count is not None:
            raise RuntimeError("mcp_server._INCLUDE 多次赋值——计数可能取旧值，"
                               "拒绝静默（请改单点赋值）")
        v = node.value
        if isinstance(v, ast.Call) and getattr(v.func, "id", "") in (
                "frozenset", "set", "list", "tuple") and v.args:
            v = v.args[0]
        if isinstance(v, (ast.List, ast.Tuple, ast.Set)):
            count = len(v.elts)
    return count or 0
