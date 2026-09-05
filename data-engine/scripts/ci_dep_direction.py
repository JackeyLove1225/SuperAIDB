"""CI 依赖方向守护：分层铁律机器钉死（AST 解析，非正则——形态学盲区灭族）

规则集（违规即 exit 1）：
1. core/** ↛ agent/**：core 是被编排层（引擎），agent 是编排层。反向边
   经依赖倒置注册解耦（见 core/registry.py、core/data_ops.register_tree_router）。
2. core/** ↛ pipeline/**：提取管线是上游业务面，引擎不感知。
3. core/** ↛ industries/**（唯一豁免 industries.base）：industries/ 是配置层
   （行业知识包：schema/字典/提示词），core 只允许消费其只读加载器单源
   （industries.base）；其他任何 industries 模块（业务逻辑）不得被引擎引用。
4. core/drivers/** 与 core/permission/** 与 core/contract/** 白名单制：
   栈底层只许向下依赖中立模块；向上边一律禁达——上层知识经依赖倒置钩子
   注入（register_* 系列，注册方为 DataSourceManager.__init__ 与
   schema_matcher 模块自注册）。仓内顶层包（config/mcp_server/db/tests/
   scripts）同样不得被栈底引用（非豁免即拒）；industries.base 与规则 3
   同豁免（配置层只读加载器，栈底亦可消费）；未来的新顶层包若要被栈底
   引用，须先加入本白名单语义并说明理由（fail-open 边界如实声明）。
5. 动态导入/执行：白名单作用域 exec/eval/__import__/import_module 全禁
   （AST Call 判定，直呼/属性形/import 别名形/再赋值归一四形态）；core 全域 exec/eval 全禁，
   import_module/__import__ 按首参字面量关键词过滤（agent/pipeline/
   industries 才禁；变量间接/字符串拼接属已声明边界，stdlib 豁免）。

实现说明：AST 遍历 Import/ImportFrom 节点——裸尾/多名同行/分号链/括号多行/
相对导入/点号空白等行级形态在 AST 层天然归一，不留正则形态学盲区。
发现合理需求时先讨论分层，再谈豁免（豁免必须是具体模块级，不接受目录级）。

用法：python scripts/ci_dep_direction.py
"""
import ast
import os
import sys
from pathlib import Path

# Windows 控制台默认 cp1252/GBK——中文输出自防御
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent

# (作用目录, 禁达包, 豁免模块, 规则说明)
_RULES = [
    ("core", {"agent"}, None,
     "core ↛ agent（引擎不感知编排层，反向边走依赖倒置注册 core/registry.py）"),
    ("core", {"pipeline"}, None,
     "core ↛ pipeline（提取管线是上游业务面）"),
    ("core", {"industries"}, "base",
     "core ↛ industries（配置层只读加载器 industries.base 豁免，其余业务模块禁达）"),
]

# 规则 4 白名单制：栈底层只许向下依赖中立模块
_SCOPE_ALLOWED = {
    "core/drivers": ("sql_safe", "checks", "check_templates", "sql_lex",
                     "exceptions", "crypto", "logger", "drivers"),
    "core/permission": ("sql_safe", "sql_lex", "exceptions", "logger",
                        "file_contract", "config_hub", "crypto", "permission"),
    "core/contract": ("checks", "drivers", "exceptions", "logger",
                      "operator_gate", "permission", "sql_safe", "contract"),
}


def _imports_of(path: Path) -> list:
    """AST 提取文件的全部导入边：返回 [(lineno, 顶层包, 第二级包, 完整目标)]
    相对导入按文件位置解析为绝对包（from .base → 本包；from ..x → 上级包——
    只有解析结果越出 core/ 或命中白名单外包才算逃逸，同包相对导入合法）"""
    rel_parts = path.relative_to(BASE).parts[:-1]  # 文件所在包路径，如 ('core','contract')
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return []
    edges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                edges.append((node.lineno, parts[0],
                              parts[1] if len(parts) > 1 else "", a.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # 相对导入解析：level=1 本包，level>=2 上溯 level-1 级
                up = node.level - 1
                if up > len(rel_parts):
                    target = []  # 越出仓根的怪异形态（落 <out-of-root> 哨兵——不命中任何规则即放行：裸相对导入无法命名仓内禁达包，运行期自会 ImportError，守门如实记录不拦）
                else:
                    target = list(rel_parts[:len(rel_parts) - up])
                if node.module:
                    target += node.module.split(".")
                if not target:
                    edges.append((node.lineno, "<out-of-root>", "", "." * node.level))
                    continue
                edges.append((node.lineno, target[0],
                              target[1] if len(target) > 1 else "",
                              "." * node.level + (node.module or "")))
                continue
            parts = (node.module or "").split(".")
            if len(parts) == 1 and parts[0]:
                # from X import a, b（单段模块）——导入名即第二级语义单元，逐名成边
                #（from industries import base 与 from industries.base 等价豁免；
                #  from core import tool_registry 与 from core.tool_registry 等价拦截）
                for a in node.names:
                    edges.append((node.lineno, parts[0], a.name.split(".")[0],
                                  f"{parts[0]}.{a.name}"))
                continue
            edges.append((node.lineno, parts[0],
                          parts[1] if len(parts) > 1 else "", node.module or ""))
    return edges


def _dynamic_calls(path: Path) -> list:
    """exec/eval/__import__/import_module 动态执行调用（AST 判定，四形态：
    Name 直呼、Attribute 属性形、import 别名形、Assign 再赋值归一——
    别名表先 ImportFrom 建树（from importlib import import_module as im /
    from builtins import exec as ex），再赋值链迭代至不动点（f = im、g = f、
    嵌套块内定义顶层接力——与源码顺序无关）；
    空格/跨行/注释/字符串字面量在 AST 层天然归一，无行级形态学盲区。
    返回 [(lineno, kind, 首参字面量)]——首参供调用方做关键词过滤"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return []
    _SRC = {("importlib", "import_module"): "import_module",
            ("builtins", "exec"): "exec", ("builtins", "eval"): "eval",
            ("builtins", "__import__"): "__import__"}
    aliases = {}  # name → set(kinds) 单调并集（格上只增不减，天然终止——
    # 冲突再绑定（f = im 又 f = ex）在两态间翻转的死循环面归零）
    def _alias_add(name, kind) -> bool:
        kinds = aliases.setdefault(name, set())
        if kind in kinds:
            return False
        kinds.add(kind)
        return True

    changed = True
    while changed:  # 不动点迭代（别名定义/接力在任意源码位置都可达）
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.asname and (node.module, a.name) in _SRC:
                        changed |= _alias_add(a.asname, _SRC[(node.module, a.name)])
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                # 函数引用再赋值形：f = import_module / f = importlib.import_module
                # / f = im（别名链）——目标名归一入别名表
                _v = node.value
                tgts = set()
                if isinstance(_v, ast.Name):
                    if _v.id in ("exec", "eval", "__import__", "import_module"):
                        tgts = {_v.id}
                    elif _v.id in aliases:
                        tgts = set(aliases[_v.id])
                elif isinstance(_v, ast.Attribute) and _v.attr in (
                        "exec", "eval", "__import__", "import_module"):
                    tgts = {_v.attr}
                for _tg in tgts:
                    changed |= _alias_add(node.targets[0].id, _tg)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            kind = None
            if isinstance(f, ast.Name):
                if f.id in ("exec", "eval", "__import__", "import_module"):
                    kind = f.id
                elif f.id in aliases:
                    kind = sorted(aliases[f.id])[0]  # 集合内任一禁 kind 即拦
            elif isinstance(f, ast.Attribute) and f.attr in ("exec", "eval",
                                                             "__import__",
                                                             "import_module"):
                kind = f.attr
            if kind:
                first_arg = ""
                if (kind in ("import_module", "__import__") and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    first_arg = node.args[0].value
                hits.append((node.lineno, kind, first_arg))
    return hits


def _walk_py(scope: str):
    for dirpath, dirnames, filenames in os.walk(BASE / scope):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in filenames:
            if f.endswith(".py"):
                yield Path(dirpath) / f


def main() -> int:
    bad = []

    # 规则 1-3：core/** 的 agent/pipeline/industries 边（AST 全形态）
    for scope, banned, allow_sub, desc in _RULES:
        for p in _walk_py(scope):
            for ln, top, sub, full in _imports_of(p):
                if top in banned:
                    if allow_sub and sub == allow_sub:
                        continue
                    bad.append(f"  [{desc.split('（')[0]}] "
                               f"{p.relative_to(BASE)}:{ln}: {full}"[:100])

    # 规则 4：drivers/permission/contract 白名单（栈底层只向下依赖中立模块；
    # 仓内顶层包（config/mcp_server/db/tests/scripts）同样不得被栈底引用——
    # 白名单语义是"只许向下"，非豁免顶层包默认拒）
    _REPO_TOPLEVEL = ("config", "mcp_server", "db", "tests", "scripts")
    for scope, allowed in _SCOPE_ALLOWED.items():
        tag = f"{scope} 白名单"
        for p in _walk_py(scope):
            for ln, top, pkg, full in _imports_of(p):
                if top == "core" and pkg and pkg not in allowed:
                    bad.append(f"  [{tag}] "
                               f"{p.relative_to(BASE)}:{ln}: {full}"[:100])
                elif top in _REPO_TOPLEVEL:
                    bad.append(f"  [{tag}·顶层包默认拒] "
                               f"{p.relative_to(BASE)}:{ln}: {full}"[:100])

    # 规则 5：动态导入——白名单作用域全禁（AST 判定，直呼/属性形/import 别名形/再赋值归一四形态）；
    # core 全域 exec/eval 全禁；import_module/__import__ 按首参字面量关键词
    # 过滤（agent/pipeline/industries 才禁——非字面量间接属 docstring 已声明
    # 边界，stdlib __import__ 豁免）
    for scope in _SCOPE_ALLOWED:
        for p in _walk_py(scope):
            for ln, kind, _arg in _dynamic_calls(p):
                bad.append(f"  [{scope} 禁动态执行（{kind}）] {p.relative_to(BASE)}:{ln}")
    for p in _walk_py("core"):
        for ln, kind, arg in _dynamic_calls(p):
            if kind in ("import_module", "__import__"):
                if arg and any(k in arg for k in ("agent", "pipeline", "industries")):
                    bad.append(f"  [core 禁动态导入（{kind} {arg!r}）] "
                               f"{p.relative_to(BASE)}:{ln}")
                continue  # 非字面量首参/无参：已声明边界（变量间接/拼接）
            bad.append(f"  [core 禁动态执行（{kind}）] {p.relative_to(BASE)}:{ln}")

    if bad:
        print(f"✗ 依赖方向违规（{len(bad)} 处）：")
        print("\n".join(bad))
        print("\n规则全文见本脚本 docstring；豁免只认具体模块级（如 industries.base）。")
        return 1
    print("✓ 依赖方向守护（AST）：core 零 agent/pipeline 边；industries 仅 base 豁免；"
          "drivers/permission/contract 白名单；静态导入全形态+动态导入关键词面封死"
          "（变量间接/字符串拼接属已声明边界）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
