"""层 1：编译锁 —— 所有 .py 文件 py_compile 通过

排除目录（大量第三方库或自动生成文件）：
  - .libs       本地隔离环境，第三方包
  - __pycache__ Python 字节码缓存
  - .venv/venv  虚拟环境
  - node_modules 前端依赖
  - .git        Git 元数据
"""
import py_compile
import os
import sys

# 需要排除的目录名（出现于 os.walk 的 dirs 中即跳过整棵子树）
EXCLUDED_DIRS = {".libs", "__pycache__", ".venv", "venv", "node_modules",
                 ".git", ".next", ".idea", ".vscode", "dist", "build"}

failed = []
total = 0
for root, dirs, files in os.walk("."):
    # 原地修改 dirs 以剪枝（os.walk 不会再进入被移除的子目录）
    dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
    for f in files:
        if not f.endswith(".py"):
            continue
        path = os.path.join(root, f)
        total += 1
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            failed.append((path, str(e)))

if failed:
    for path, err in failed:
        print(f"FAIL {path}: {err[:80]}")
    sys.exit(1)

print(f"OK - all {total} .py files compile (excluded: {', '.join(sorted(EXCLUDED_DIRS))})")


# ── 函数长度棘轮（AST 实测，只降不升）──
# 生产目录（core/agent/pipeline/industries）函数行数硬帽：
# - 全局帽 120 行（治理后实测最大 117——超帽即红，防回弹；后续治理逐级下调）
# - 点名帽 80 行：治理完成的函数不得回弹超 80（文件:函数 二元组——
#   改名/挪文件即未命中告警，不得静默出集）
import ast as _ast

# 锚定仓根（绕开 run_all 直接 cd tests 跑本文件时，相对路径会让棘轮静默空转）
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_NAMED_UNDER_80 = {  # (文件后缀, 函数名)
    ("pipeline/ingestion.py", "write_batch_groups"),
    ("core/db_chat.py", "ask"),
    ("agent/tools/files.py", "process_file"),
    ("core/data_ops/nl_mutate.py", "mutate_natural"),
    ("core/data_ops/join_sql.py", "join_query"),
    ("core/data_ops/agg_sql.py", "aggregate_query"),
    ("pipeline/runner.py", "run"),
    ("pipeline/extraction.py", "batch_process"),
    ("pipeline/ingestion.py", "_ingest_group_attached"),
    ("pipeline/ingestion.py", "_ingest_group_single_db"),
    ("pipeline/ingestion.py", "_ingest_group_via_saga"),
    ("agent/tools/files.py", "_build_process_result"),
    ("core/data_ops/nl_mutate.py", "_multi_ops_confirmed"),
    ("core/data_ops/agg_sql.py", "_build_agg_sql"),
    ("core/tool_registry.py", "execute_tool"),
    ("core/tool_registry.py", "_describe_nuke_impact"),
    ("pipeline/unified.py", "map_to_schemas"),
    ("pipeline/unified.py", "handle_mapping_confirmation"),
    ("core/federation/join_executor.py", "federated_join"),
    ("agent/management/launcher.py", "start"),
    ("agent/management/routers/dashboard.py", "get_dashboard"),
    ("agent/tools/query.py", "_query_with_fallback"),
    ("core/tool_arg_guard.py", "validate_tool_args"),
    # 安全关键面（回弹到 119 即合法的风险敞口，点名压死）
    ("core/tool_registry.py", "_nuke_confirmed"),
    ("core/tool_registry.py", "_force_confirmed"),
    ("agent/management/server.py", "verify_api_key"),
    ("agent/tools/instruct.py", "execute_instruction"),
}
_GLOBAL_CAP = 120

_len_violations = []
_named_violations = []
_named_seen = set()
for root in ("core", "agent", "pipeline", "industries"):
    if not os.path.isdir(root):
        continue
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f).replace("\\", "/")
            try:
                tree = _ast.parse(open(p, encoding="utf-8").read())
            except SyntaxError:
                continue  # 语法错误由上方 py_compile 段抓
            for node in _ast.walk(tree):
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                        and node.end_lineno:
                    n = node.end_lineno - node.lineno + 1
                    if n > _GLOBAL_CAP:
                        _len_violations.append(f"{p}:{node.lineno} {node.name} ({n} 行)")
                    for suffix, fname in _NAMED_UNDER_80:
                        if p.endswith(suffix) and node.name == fname:
                            _named_seen.add((suffix, fname))
                            if n > 80:
                                _named_violations.append(
                                    f"{p}:{node.lineno} {node.name} ({n} 行)")

# 点名集失配告警：改名/挪文件不得静默逃逸（守护不可空转）
_named_missing = _NAMED_UNDER_80 - _named_seen
if _named_missing:
    for suffix, fname in sorted(_named_missing):
        print(f"FAIL 函数长度棘轮: 点名函数未找到 {suffix}::{fname}（改名须同步棘轮集）")
    sys.exit(1)

if _len_violations or _named_violations:
    for v in _len_violations + _named_violations:
        print(f"FAIL 函数长度棘轮: {v}")
    sys.exit(1)
print(f"OK - 函数长度棘轮（全局 ≤{_GLOBAL_CAP}，点名 {len(_NAMED_UNDER_80)} 个 ≤80）")
