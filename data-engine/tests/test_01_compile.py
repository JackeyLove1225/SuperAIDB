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
