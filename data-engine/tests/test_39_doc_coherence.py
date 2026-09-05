"""层 39：文档口径守护——README/文档的关键数字声明与代码真值
机器比对，漂移即红（防"唯一实现被残留覆盖""数字口径漂移"类问题再生）。

守护的数字：工具数 / MCP 暴露数 / 驱动接口数（抽象+共享）/ 回归层总数与离线数。
层数用动态真值比对（不写死）；工具/接口的锚点数字若变，先改代码再改本层断言。
（本文件自身的 docstring 不携带任何层数/工具数——守护者的口径不能靠自觉）
"""
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)  # 仓根入径（core.* 可导入——本层与 run_all 子进程同口径）
sys.path.insert(0, os.path.join(BASE_DIR, "tests"))


def _read(rel: str) -> str:
    with open(os.path.join(BASE_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _tool_truth():
    """工具真值：注册表计数 + MCP 排除数"""
    reg = _read("agent/tools/_registry.py")
    n_tools = len(re.findall(r"register_tool\(Tool\(", reg))
    from core.doc_truth import mcp_include_count
    n_included = mcp_include_count("mcp_server.py")
    assert n_included >= 1, "mcp_server.py 未找到 _INCLUDE 白名单（面即安全边界的默认方向）"
    return n_tools, n_included


def _driver_truth():
    """驱动接口真值——唯一实现 core.doc_truth.driver_interface_count
    （发布闸同用，防复制漂移）"""
    from core.doc_truth import driver_interface_count
    return driver_interface_count("core/drivers/base.py")


def _layer_truth():
    """回归层真值：从 run_all.TEST_LAYERS 直接读（不解析正则）"""
    from run_all import TEST_LAYERS
    total = len(TEST_LAYERS)
    quick = sum(1 for *_x, _need_api, in_quick, _est, _args in TEST_LAYERS if in_quick)
    return total, quick


def test_tool_counts():
    n_tools, n_mcp = _tool_truth()
    assert n_tools == 39, f"工具数真值漂移: {n_tools}"
    assert n_mcp == 5, f"MCP 暴露真值漂移: {n_mcp}"
    for doc in ("README.md", "ARCHITECTURE.md", "AGENTS.md"):
        text = _read(doc)
        for claim in re.findall(r"(\d+)\s*个原子工具", text):
            assert int(claim) == n_tools, f"{doc} 原子工具数漂移: {claim}"
        for claim in re.findall(r"(\d+)\s*个数据工具", text):
            assert int(claim) == n_mcp, f"{doc} MCP 工具数漂移: {claim}"
        # 措辞逃逸面收口：工具数的其他表述形态也入守护
        for claim in re.findall(r"(\d+)\s*个受护栏工具", text):
            assert int(claim) == n_tools, f"{doc} 受护栏工具数漂移: {claim}"
        for claim in re.findall(r"(\d+)\s*个?\s*register_tool\s*调用", text):
            assert int(claim) == n_tools, f"{doc} register_tool 计数漂移: {claim}"
        # 负向断言：硬路由后禁止"全部工具…暴露"/"能力面自动暴露"字样
        #（与面仅 5 个+白名单默认方向直接矛盾）
        assert not re.search(r"全部工具[^。\n]*暴露|暴露[^。\n]*全部工具", text), \
            f"{doc} 出现'全部工具…暴露'字样（与硬路由面仅 5 个直接矛盾）"
        assert "自动暴露" not in text, \
            f"{doc} 出现'自动暴露'字样（与白名单默认不上面的口径直接矛盾）"
        # 层数句式变体："跑 N 个离线层"也要等于 quick 真值
        for claim in re.findall(r"跑\s*(\d+)\s*个离线层", text):
            assert int(claim) == _layer_truth()[1], \
                f"{doc} 离线层数漂移（跑 N 个离线层句式）: {claim}"
        # "N 个工具已撤出/隐藏"句式：撤出数 = 注册数 − 面上数（39−5=34 真值）；
        # README 命中下限（删句/插注断开匹配即过闸的逃逸面——与接口数/schema 数
        # 同标准；该句式只在 README 声明，ARCHITECTURE/AGENTS 不做下限）
        _wd = re.findall(r"(\d+)\s*个工具已?撤出", text)
        if doc == "README.md":
            assert _wd, f"{doc} 撤出工具数声明缺失（守护不可静默空转）"
        for claim in _wd:
            assert int(claim) == n_tools - n_mcp, \
                f"{doc} 撤出工具数漂移: {claim}（真值 {n_tools}-{n_mcp}={n_tools-n_mcp}）"
    # 守护代码注释面：_registry.py 与 agent/__init__.py 的工具计数
    for src in ("agent/tools/_registry.py", "agent/__init__.py"):
        text = _read(src)
        for claim in re.findall(r"(\d+)\s*个\s*(?:register_tool\s*调用|原子工具)", text):
            assert int(claim) == n_tools, f"{src} 计数漂移: {claim}"
    print(f"OK - 工具口径：{n_tools} 工具/{n_mcp} MCP，三份文档+注释面一致")


def test_driver_interface_counts():
    n_abs, n_total = _driver_truth()
    assert n_abs == 27, f"抽象接口数漂移: {n_abs}"  # 棘轮锚点（刻意写死：接口增删必须显式过闸，与层数"守同源不写死"是两种守护哲学）
    for doc in ("README.md", "ARCHITECTURE.md"):
        text = _read(doc)
        hits = re.findall(r"(\d+)\s*个?(?:驱动)?(?:原子)?接口", text)
        assert hits, f"{doc} 驱动接口数声明缺失（守护不可静默空转）"
        for m in hits:
            assert int(m) == n_total, f"{doc} 驱动接口数漂移: {m} != {n_total}"
    print(f"OK - 接口口径：{n_abs} 抽象+{n_total - n_abs} 共享={n_total}，文档一致")


def test_layer_counts():
    total, quick = _layer_truth()
    # 口径=动态真值比对（不写死数字——守护的是 README/CI 与代码同源，非数字本身）
    readme = _read("README.md")
    assert f"{total} 层回归测试" in readme, f"README 总层数漂移（真值 {total}）"
    assert f"{quick} 个离线测试层" in readme, f"README 离线层数漂移（真值 {quick}）"
    tests_readme = _read("tests/README.md")
    assert f"{quick} 层" in tests_readme, "tests/README 离线层数漂移"
    ci = _read(".github/workflows/ci.yml")
    assert f"{quick} 层" in ci, "ci.yml 层数标注漂移"
    print(f"OK - 回归层口径：共 {total} 层 / {quick} 离线，README+CI 与真值一致")


def test_no_ghost_layer_refs():
    """幽灵层号机检：docs/testing/*.md 里引用的"层 N"必须存在于 TEST_LAYERS
    （层表重编号后，索引文档里的旧号引用即成幽灵——与数字口径同标准收口）"""
    import re as _re
    valid = {str(l[0]) for l in _layers()}
    for doc in ("docs/testing/全回归测试清单.md", "docs/testing/人工全量测试清单.md"):
        text = _read(doc)
        for m in _re.finditer(r"层\s*(\d{1,2})\b", text):
            assert m.group(1) in valid, \
                f"{doc} 引用幽灵层号 层{m.group(1)}（有效层号: {sorted(valid, key=int)}）"
    print("OK - docs/testing 无幽灵层号引用")


def _layers():
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("run_all", "tests/run_all.py")
    m = _ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.TEST_LAYERS


def test_no_process_codenames():
    """开发过程代号机检：生产/测试/脚本注释与文档不得残留迭代代号编年史
    （评审N轮/P1-n/T1.n/F-n/D-n/方案A-F/N轮续/上轮事故——技术陈述保留、
    归因标签清零。豁免 docs/archive/ 与 轮转/轮询/本轮 正常语义）"""
    import os as _os
    from core.codenames import CODENAME_RE, CODENAME_SCAN_EXTS as _EXTS
    pat = CODENAME_RE
    hits = []
    for scope in ("core", "agent", "pipeline", "tests", "scripts", "config",
                  "industries", "mcp_server.py", "README.md", "ARCHITECTURE.md",
                  "AGENTS.md", ".github/workflows/ci.yml", "docs"):
        if _os.path.isfile(scope):
            files = [scope]
        else:
            files = []
            for dp, dn, fn in _os.walk(scope):
                dn[:] = [d for d in dn if d not in ("__pycache__", "archive")]
                files += [_os.path.join(dp, f) for f in fn if f.endswith(_EXTS)]
        for f in files:
            if f.replace("\\", "/").endswith(("tests/test_39_doc_coherence.py", "core/codenames.py")):
                continue  # 检查器自身与正则真源（模式字面量含代号形态）
            text = open(f, encoding="utf-8", errors="ignore").read()
            for m in pat.finditer(text):
                hits.append(f"{f}: {m.group(0)}")
    assert not hits, "过程代号残留:\n" + "\n".join(hits[:20])
    print("OK - 生产/测试/脚本无过程代号残留（编年史机检）")


def test_dep_guard_probes():
    """依赖守门探针矩阵（守门者自身的回归钉）：monkeypatch BASE 到临时目录，
    逐形态断言 拦/放行——规则被改弱（形态复活）时本层即红"""
    import importlib.util as _ilu
    import tempfile as _tf
    import os as _os
    spec = _ilu.spec_from_file_location("ci_dep_direction", "scripts/ci_dep_direction.py")
    g = _ilu.module_from_spec(spec)
    spec.loader.exec_module(g)

    probes = [  # (相对探针根的路径, 内容, 期望拦)
        ("core/drivers/p1.py", "import core.tool_registry\n", True),
        ("core/drivers/p2.py", "import os, core.permission\n", True),
        ("core/drivers/p3.py", "from core import tool_registry, checks\n", True),
        ("core/drivers/p4.py", "from core import (tool_registry,)\n", True),
        ("core/drivers/p5.py", "from core import (  # 注释\n    tool_registry,\n)\n", True),
        ("core/drivers/p6.py", "from ..tool_registry import X\n", True),
        ("core/drivers/p7.py", "import core . tool_registry\n", True),
        ("core/drivers/p8.py", "import core.checks; import core.tool_registry\n", True),
        ("core/drivers/p9.py", "import importlib\nimportlib.import_module('agent.tools')\n", True),
        ("core/graph/p10.py", "from ...agent import tools\n", True),
        ("core/graph/p11.py", "from industries.loader import x\n", True),
        ("core/graph/p12.py", "import pipeline\n", True),
        ("core/drivers/ok1.py", "from core.checks import validate_where\n", False),
        ("core/drivers/ok2.py", "import core.checks\n", False),
        ("core/drivers/ok3.py", "from core.sql_safe import is_valid_identifier\n", False),
        ("core/drivers/ok4.py", "from .base import Driver\n", False),
        ("core/graph/ok5.py", "from industries.base import load_schemas\n", False),
        ("core/drivers/ok6.py", "import os, yaml\n", False),
        ("core/graph/ok7.py", "from industries import base\n", False),
        ("core/graph/p13.py", "from industries import loader\n", True),
        ("core/drivers/p14.py", "exec('import agent')\n", True),
        ("core/drivers/p15.py", "exec ('import agent')\n", True),
        ("core/drivers/p16.py", "eval('1+1')\n", True),
        ("core/drivers/ok7.py", "# 禁止使用 exec(x) 动态执行\n", False),
        ("core/drivers/ok8.py", "s = 'exec(x) 是字符串字面量'\n", False),
        ("core/drivers/p17.py", "from importlib import import_module\nimport_module('agent.tools')\n", True),
        ("core/drivers/p18.py", "import builtins\nbuiltins.exec('x')\n", True),
        ("core/graph/p19.py", "exec('import agent')\n", True),
        ("core/drivers/p20.py", "import config\n", True),
        ("core/drivers/p21.py", "import mcp_server\n", True),
        ("core/drivers/ok9.py", "import yaml\n", False),
        ("core/drivers/p22.py", "from importlib import import_module as im\nim('agent.tools')\n", True),
        ("core/drivers/p23.py", "from builtins import exec as ex\nex('x')\n", True),
        ("core/drivers/p24.py", "from importlib import import_module as im\nim('config.settings')\n", True),
        ("core/drivers/ok10.py", "from industries.base import load_schemas\n", False),
        ("core/drivers/p25.py", "from importlib import import_module\nf = import_module\nf('agent.tools')\n", True),
        ("core/drivers/p26.py", "import importlib\ng = importlib.import_module\ng('agent.tools')\n", True),
        ("core/drivers/p27.py", "from importlib import import_module as im\nf = im\nf('agent.tools')\n", True),
        ("core/drivers/p28.py", "from importlib import import_module as im\nf = im\ng = f\ng('agent.tools')\n", True),
        ("core/drivers/p29.py", "from importlib import import_module as im\nif True:\n    f = im\ng = f\ng('agent.tools')\n", True),
        # p30：冲突再绑定（同一别名先绑 import_module 后绑 exec——并集不动点不得死循环，须拦）
        ("core/drivers/p30.py", "from importlib import import_module as im\nfrom builtins import exec as ex\nf = im\nf = ex\nf('agent.tools')\n", True),
    ]
    for rel, content, expect_bad in probes:
        with _tf.TemporaryDirectory() as tmp:
            for scope in ("core/drivers", "core/permission", "core/contract", "core/graph"):
                _os.makedirs(_os.path.join(tmp, scope), exist_ok=True)
            # 每个探针独享一棵最小树（隔绝其它探针干扰）
            for scope in ("core/drivers", "core/permission", "core/contract", "core/graph"):
                for f in _os.listdir(_os.path.join(tmp, scope)):
                    _os.remove(_os.path.join(tmp, scope, f))
            target = _os.path.join(tmp, rel)
            _os.makedirs(_os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(content)
            g.BASE = g.Path(tmp)
            got_bad = g.main() != 0
            assert got_bad == expect_bad, \
                f"守门探针失效: {rel} {content!r} 期望{'拦' if expect_bad else '放'}，实际{'拦' if got_bad else '放'}"
    print(f"OK - 依赖守门探针矩阵 {len(probes)} 形态（拦/放行全符合预期）")


def test_schema_number_claims():
    """schema 数字口径（"4 表 / 28 字段 / 3 关系"）：README/根 README 与
    industries schemas YAML 动态真值比对（漂移即红，删句即报）"""
    import glob as _glob
    import yaml as _yaml
    n_tables = n_cols = n_fks = 0
    for yf in _glob.glob("industries/construction_engineering/schemas/*.yaml"):
        doc = _yaml.safe_load(open(yf, encoding="utf-8"))
        n_tables += 1
        n_cols += len(doc.get("columns", []))
        n_fks += len(doc.get("foreign_keys", []))
    for doc in ("README.md",):
        text = _read(doc)
        for pat, want, label in ((r"(\d+)\s*表", n_tables, "表"),
                                 (r"(\d+)\s*字段", n_cols, "字段"),
                                 (r"(\d+)\s*关系", n_fks, "关系")):
            hits = [int(m.group(1)) for m in re.finditer(pat, text)]
            assert hits, f"{doc} 缺 schema {label} 数声明（守护不可静默空转）"
            for n in hits:
                assert n == want, f"{doc} schema {label} 数漂移: {n} != 真值 {want}"
    print(f"OK - schema 数字口径：{n_tables} 表/{n_cols} 字段/{n_fks} 关系 与 YAML 真值一致")


def test_codename_regex_probes():
    """代号正则探针集（正则守护者自身的回归钉——正则被改弱时
    层 39 与发布闸双双静默空转，形态用例在此固化）"""
    from core.codenames import CODENAME_RE as R
    positives = ["十三轮修复", "十一轮", "二十一轮", "五轮", "两轮对比", "13轮", "收敛十二轮：xx",
                 "评审十三轮", "运维三轮", "方案C", "P1-12", "T1.3", "（D7）",
                 "上轮事故", "实测事故"]
    negatives = ["本轮", "下一轮", "上一轮", "每轮", "一轮", "轮询", "轮转",
                 "三轮询测试", "两轮转发", "每3轮询", "PBKDF2"]
    for t in positives:
        assert R.search(t), f"代号正则漏检应命中: {t!r}"
    for t in negatives:
        assert not R.search(t), f"代号正则误伤: {t!r}"
    print(f"OK - 代号正则探针 {len(positives)} 正例 + {len(negatives)} 负例全符合")


def test_deps_single_truth():
    """依赖双真源同步闸：pyproject.toml [project.dependencies] 与 requirements.txt
    的包名+版本约束必须逐条全等（漂移即红——两套清单只许一份事实）"""
    import tomllib as _tomllib
    with open(os.path.join(BASE_DIR, "pyproject.toml"), "rb") as f:
        pyproj = _tomllib.load(f)["project"]["dependencies"]
    req = []
    for line in _read("requirements.txt").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            req.append(line)
    norm = lambda s: s.replace(" ", "").lower()
    a, b = sorted(map(norm, pyproj)), sorted(map(norm, req))
    assert a == b, f"依赖双真源漂移:\npyproject 独有 {sorted(set(a)-set(b))}\nrequirements 独有 {sorted(set(b)-set(a))}"
    print(f"OK - 依赖双真源同步（{len(a)} 条逐一全等）")


def test_p1_enum_tree_coherence():
    """P1 封闭枚举（ai_extract._BEHAVIOR_KEYS/_DB_KEYS）与决策树路由标签同源——
    散文/FC schema 手抄漂移即红（枚举真源在 P1，树标签归一后必须全等）"""
    import glob as _glob
    import yaml as _yaml
    from agent.ai_extract import _BEHAVIOR_KEYS, _DB_KEYS
    behaviors, objects = set(), set()
    for yf in _glob.glob("agent/decision_tree/*.yml"):
        doc = _yaml.safe_load(open(yf, encoding="utf-8"))
        for node in (doc.get("nodes") or {}).values():
            if not isinstance(node, dict) or "c" not in node:
                continue
            ms = node.get("m")
            ms = ms if isinstance(ms, list) else [ms]
            if node["c"] == "behavior":
                behaviors.update(x for x in ms if x)
            elif node["c"] == "db":
                objects.update(x for x in ms if x)
    # 树内别名归一到 P1 规范标签（树允许多别名同节点，P1 只认规范形）
    alias = {"库": "数据库", "暂存": "选择集", "暂存数据": "选择集"}
    objects = {alias.get(o, o) for o in objects}
    assert behaviors == set(_BEHAVIOR_KEYS), \
        f"行为枚举漂移: 树 {sorted(behaviors)} vs P1 {sorted(_BEHAVIOR_KEYS)}"
    # "结构"走 fallthrough 路由（查域未匹配落 describe_schema），不在树 m: 里
    assert objects | {"结构"} == set(_DB_KEYS), \
        f"对象枚举漂移: 树 {sorted(objects)} vs P1 {sorted(_DB_KEYS)}"
    print(f"OK - P1 封闭枚举与决策树标签同源（行为 {len(behaviors)} / 对象 {len(objects) + 1}）")


if __name__ == "__main__":
    test_tool_counts()
    test_driver_interface_counts()
    test_layer_counts()
    test_no_ghost_layer_refs()
    test_no_process_codenames()
    test_dep_guard_probes()
    test_schema_number_claims()
    test_codename_regex_probes()
    test_p1_enum_tree_coherence()
    test_deps_single_truth()
    print("\n=== DOC COHERENCE PASSED ===")
