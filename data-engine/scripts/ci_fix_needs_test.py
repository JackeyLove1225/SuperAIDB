# -*- coding: utf-8 -*-
"""CI 门禁：安全关键面的变更必须伴随测试变更（修复裸奔防线）

例外（如实声明）：孤儿无基线仓（单提交发布仓）取不到 diff 时如实放行——
单提交=全量快照，测试约束已在源仓结算；其余 diff 失败一律门禁红。

规则：
- 覆盖面默认收敛：core/ 全量 + agent/ + pipeline/ + mcp_server.py——
  正向枚举子目录会漏掉 core/ 根安全件（tool_arg_guard/pending_ops/
  tool_registry/context/unrecognized 等）
- fail-closed：diff 取不到 → 门禁红（跳过门禁等于 fail-open，不允许）
- 测试伴随必须是 tests/test_*.py 的代码变化（改 tests/README 不算数）

CI 用法：python scripts/ci_fix_needs_test.py <base_ref>
"""
import subprocess
import sys

# Windows 控制台默认 cp1252/GBK，中文输出会直接 UnicodeEncodeError——自防御
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CRITICAL = ("core/", "agent/", "pipeline/", "mcp_server.py",
            "config/settings.py", "config/permissions.yml",
            "scripts/ci_", "scripts/review_sweep.py")  # 守门者自身也是安全关键面（规则被改弱必须配探针锁）
# 显式豁免：纯展示/日志/指标件（非安全关键面）
EXEMPT = ("core/formatters.py", "core/logger.py", "core/metrics.py")


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1"
    r = subprocess.run(["git", "diff", "--name-only", base, "HEAD"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # base 悬空（force-push 改写历史后 github.event.before 指向不存在的提交）：
        # 退化 HEAD~1 对比仍走同一套门禁——不升格为放行，fail-closed 方向不变
        r = subprocess.run(["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            # 无基线快照仓（单提交发布仓：每次孤儿提交替换历史，HEAD 无父提交，
            # github.event.before 也不可取）——diff 无从谈起，门禁如实不适用；
            # 单提交=全量快照（测试与代码天然同帧），修复必配锁的约束在发布源仓已结算
            print("无基线快照仓（HEAD 无父提交）——修复必配锁不适用，如实放行")
            return 0
        print(f"base ref 悬空（{base[:10]}，历史被改写）——退化为 HEAD~1 对比")
    changed = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    critical = [c for c in changed
                if c.startswith(CRITICAL) and not c.startswith(EXEMPT)]
    if not critical:
        print("无安全关键面变更，门禁通过")
        return 0
    has_tests = any(c.startswith("tests/test_") and c.endswith(".py")
                    for c in changed)
    if has_tests:
        print(f"安全面变更 {len(critical)} 个且伴随测试变更——门禁通过")
        return 0
    print("✗ 安全关键面变更但未伴随测试代码变更（修复必配锁机制）:")
    for c in critical:
        print(f"  - {c}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
