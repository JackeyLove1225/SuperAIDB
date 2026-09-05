"""SuperAIDB 全量回归测试运行器

用法：
  python tests/run_all.py              # 运行全部测试
  python tests/run_all.py --quick      # 只运行离线测试层（CI 用）
  python tests/run_all.py --layer 6    # 只运行指定层
  python tests/run_all.py --list       # 列出所有测试层

测试层：唯一真源是下方 TEST_LAYERS 表（层号/文件/描述/是否离线），
不在此处维护第二份清单（口径守护 test_39 与本文件同源比对，
历史分组清单已于 20260825 移除——它曾列出已下线的层，腐坏无人知）。
"""
import sys, os, subprocess, time, argparse, tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# 脚本从 tests/ 启动时 sys.path 只含 tests/——本项目根必须显式入路径，
# 否则 run_all 进程内 import config/core 全部 ImportError（隐性后果：
# check_api_available 曾因此恒 False，API 依赖层在服务在线时也静默全跳）
sys.path.insert(0, BASE_DIR)

# 测试层定义
# (层号, 文件名, 描述, 是否需要API服务, 是否纳入quick, 预计耗时秒, 额外命令行参数)
TEST_LAYERS = [
    ("1", "test_01_compile.py",        "编译检查",         False, True,  5,   []),
    ("2", "test_02_tools.py",          "工具注册",         False, True,  8,   []),
    ("5", "test_05_excel.py",          "Excel解析",        False, True,  5,   []),
    ("6", "test_06_federation.py",     "联邦数据库",       False, True,  15,  []),
    ("7", "test_07_industry.py",       "行业管理+术语",    False, True,  60,  []),  # TestClient 化（20260825），无需服务端口
    ("8", "test_08_schema_consistency.py", "Schema一致性", False, True,  10,  []),
    ("14", "test_14_sql_safety.py",     "SQL注入安全",       False, True,  30,  []),
    ("15", "test_15_industry_lint.py",  "行业配置校验",       False, True,  3,   []),
    ("16", "test_16_universality.py",   "通用性验收",         False, True,  3,   []),
    ("17", "test_17_permission.py",     "数据源权限控制",     False, True,  6,   []),
    ("18", "test_18_pipeline_mapping.py", "管线映射层固化",    False, True,  5,   []),
    ("19", "test_19_record_dml.py",     "记录级DML",          False, True,  5,   []),
    ("20", "test_20_config_freshness.py", "配置新鲜度(解耦)",  False, True,  5,   []),
    ("21", "test_21_auth.py",            "身份认证全链路",      False, True,  20,  []),  # PBKDF2 成本使然，CI 共享 runner 实测 15s+
    ("26", "test_30_tree_metadata.py",   "3.3树模块化+元数据",    False, True,  8,   []),
    ("28", "test_32_mcp_bridge.py",      "MCP能力面+人审跨进程链",    False, True,  20,  []),
    ("33", "test_33_consistency_fix.py", "一致性加固(4致命点)",     False, True,  5,   []),
    ("34", "test_34_crypto.py",          "数据库加密边界",          False, True,  10,  []),
    ("35", "test_35_daemon.py",          "数据守护进程",            False, True,  20,  []),
    ("37", "test_37_mgmt_api.py",        "管理API面(TestClient)",   False, True,  15,  []),
    ("39", "test_39_doc_coherence.py",   "文档口径守护",            False, True,  5,   []),
    ("40", "test_40_instruct.py",      "硬路由元工具",            False, True,  10,  []),
    ("38", "test_38_launcher.py",        "launcher 运维面",         False, True,  30,  []),
    ("29", "test_24_multi_mutate.py",    "多表改删(树路由)",        False, True,  10,  []),
    ("30", "test_25_force_gate.py",      "force确认闸",             False, True,  10,  []),
    ("31", "test_26_designer_delete_gate.py", "设计器删除预检",      False, True,  10,  []),
]


def check_api_available():
    """检查 Management API 是否可用"""
    try:
        import urllib.request
        from config.settings import settings as _st
        r = urllib.request.urlopen(f"http://127.0.0.1:{_st.MGMT_PORT}/api/health", timeout=3)
        return r.status == 200
    except Exception:
        return False


def run_test_layer(layer_num, filename, desc, needs_api, est_time, extra_args=None, daemon_ok=False):
    """运行单个测试层，返回 (passed, exit_code, elapsed)"""
    filepath = os.path.join(TESTS_DIR, filename)
    if not os.path.exists(filepath):
        # 声明了却找不到文件 = 套件完整性破坏（重命名/误删），必须红而非跳过——
        # 否则"文件改名 + CI 假绿"无从察觉
        print(f"  [FAIL] 层{layer_num} {desc} — 声明的测试文件不存在: {filename}")
        return False, -3, 0

    print(f"\n{'='*60}")
    print(f"层 {layer_num}: {desc} ({filename})")
    print(f"{'='*60}")

    start = time.time()
    try:
        layer_env = dict(os.environ)
        # 选择集文件化后是跨进程共享态——每层独立文件防层间/历次运行串台
        sel = os.path.join(tempfile.gettempdir(), f"superaidb_selections_{layer_num}.json")
        if os.path.exists(sel):
            os.remove(sel)
        layer_env["SUPERAIDB_SELECTIONS_FILE"] = sel
        # 操作密码闸测试模式（core/operator_gate.py 的开发/CI 便利——
        # 层内大量结构高危直调需要凭证；生产运行不携带此变量）
        layer_env["SUPERAI_TEST_MODE"] = "1"
        if filename != "test_35_daemon.py" and not daemon_ok:
            # 离线层一律进程内直连（确定性）；daemon 路径由层 35 专项验证。
            # 产品运行时 DAEMON_MODE 默认 true（最终形态），测试不跟随。
            # --daemon-ok：生产装配抽样（CI 用）——不强制直连，层按产品默认
            # daemon 模式跑（生产默认路径不能只有层 35 在守）
            layer_env["DAEMON_MODE"] = "false"
        cmd = [sys.executable, filepath, *(extra_args or [])]
        if os.environ.get("RUN_ALL_COVERAGE") == "1":
            # 覆盖率模式：层进程经 coverage 跑（--parallel-mode 产出
            # .coverage.* 分片，事后 coverage combine 汇总）
            cmd = [sys.executable, "-m", "coverage", "run", "--parallel-mode",
                   "--source=core,agent,industries,pipeline", filepath, *(extra_args or [])]
        result = subprocess.run(
            cmd,
            cwd=BASE_DIR,
            capture_output=False,
            env=layer_env,
            timeout=est_time * 3,  # 超时设为预计耗时的3倍
        )
        elapsed = time.time() - start
        passed = (result.returncode == 0)
        return passed, result.returncode, elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        print(f"  [TIMEOUT] 层{layer_num} 超时（{elapsed:.0f}s）")
        return False, -1, elapsed
    except Exception as e:
        elapsed = time.time() - start
        print(f"  [ERROR] 层{layer_num} 运行异常: {e}")
        return False, -2, elapsed


def _swap_user_configs():
    """回归期间把运行期可被 UI 改写的用户态配置换成仓库默认值，
    返回还原函数（finally 中调用）。

    背景：permissions.yml 是热生效的活配置——用户在本机权限页做的实验
    （如给 primary 禁 ddl）会被读活配置的测试层当真，把回归跑红。
    回归测的是"代码 + 仓库默认配置"，不是用户的本机实验。
    还原失败不掩盖测试结论（仅告警）。"""
    import subprocess as _sp
    path = os.path.join(BASE_DIR, "config", "permissions.yml")
    try:
        default = _sp.run(["git", "show", "HEAD:config/permissions.yml"],
                          cwd=BASE_DIR, capture_output=True, text=True).stdout
    except Exception:
        default = ""
    if not default or not os.path.exists(path):
        return lambda: None  # 非 git 环境/文件不存在：不隔离
    live = open(path, encoding="utf-8").read()
    if live == default:
        return lambda: None  # 本来就是默认，无需换
    with open(path, "w", encoding="utf-8") as f:
        f.write(default)

    def _restore():
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(live)
        except OSError as e:
            print(f"⚠ 用户配置恢复失败（{path}）: {e}——请检查权限配置是否被回归改写")
    return _restore


def main():
    parser = argparse.ArgumentParser(description="SuperAIOffice 全量回归测试")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式：只运行离线测试层（跳过需API服务/AI/计时敏感的层）")
    parser.add_argument("--layer", type=str, default="",
                        help="只运行指定层（如 --layer 6），多个用逗号分隔")
    parser.add_argument("--list", action="store_true",
                        help="列出所有测试层")
    parser.add_argument("--daemon-ok", action="store_true",
                        help="生产装配抽样：不强制进程内直连，层按产品默认 daemon 模式跑（CI 用）")
    args = parser.parse_args()

    if args.list:
        print("SuperAIOffice 回归测试层级：")
        print(f"{'层':>4}  {'文件':<30} {'描述':<20} {'需API':>6} {'quick':>6} {'预计耗时':>8}")
        print("-" * 84)
        for num, fname, desc, api, quick, est, _args in TEST_LAYERS:
            print(f"{num:>4}  {fname:<30} {desc:<20} {'是' if api else '否':>6} {'是' if quick else '否':>6} {est:>6}s")
        return

    # 筛选要运行的层
    selected_layers = []
    if args.layer:
        layer_nums = [x.strip() for x in args.layer.split(",")]
        selected_layers = [l for l in TEST_LAYERS if l[0] in layer_nums]
        # 空集/部分未命中必须 fail-closed：拼错或重编号漂移的层号不得静默全绿
        #（CI 的 daemon 抽样按层号钉死，空转假绿是门禁级事故）
        hit = {l[0] for l in selected_layers}
        missed = [n for n in layer_nums if n not in hit]
        if missed:
            valid = ", ".join(str(l[0]) for l in TEST_LAYERS)
            print(f"❌ 层号未命中: {', '.join(missed)}（有效层号: {valid}）")
            sys.exit(2)
    else:
        selected_layers = list(TEST_LAYERS)

    # quick 模式过滤
    if args.quick:
        selected_layers = [l for l in selected_layers if l[4]]
        print("⚡ 快速模式：只运行离线测试层")

    # 检查 API 服务可用性
    api_available = check_api_available()
    if not api_available:
        from config.settings import settings as _st
        has_api_tests = any(l[3] for l in selected_layers)
        if has_api_tests:
            print(f"⚠ Management API 不可用（端口 {_st.MGMT_PORT}），需要API的测试层将跳过")
            print("  启动服务: cd data-engine && python agent/management/server.py")

    print(f"\n将运行 {len(selected_layers)} 个测试层")
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 运行测试
    results = []
    total_start = time.time()
    _restore_cfg = _swap_user_configs()
    for num, fname, desc, needs_api, _quick, est, extra_args in selected_layers:
        if needs_api and not api_available:
            print(f"\n{'='*60}")
            print(f"层 {num}: {desc} — 跳过（API不可用）")
            print(f"{'='*60}")
            results.append((num, desc, None, 0, "API不可用"))
            continue

        passed, exit_code, elapsed = run_test_layer(num, fname, desc, needs_api, est, extra_args, daemon_ok=args.daemon_ok)
        status = "PASS" if passed else "FAIL"
        results.append((num, desc, passed, elapsed, status))

    total_elapsed = time.time() - total_start
    _restore_cfg()  # 还原用户态配置（权限实验配置物归原主）

    # 汇总
    print(f"\n{'='*60}")
    print("回归测试汇总")
    print(f"{'='*60}")
    print(f"{'层':>4}  {'描述':<20} {'状态':>8} {'耗时':>8}")
    print("-" * 50)

    total_pass = 0
    total_fail = 0
    total_skip = 0
    for num, desc, passed, elapsed, status in results:
        if passed is None:
            total_skip += 1
            status_str = "SKIP"
        elif passed:
            total_pass += 1
            status_str = "PASS"
        else:
            total_fail += 1
            status_str = "FAIL"
        print(f"{num:>4}  {desc:<20} {status_str:>8} {elapsed:>6.1f}s")

    print("-" * 50)
    print(f"总计: {total_pass} 通过, {total_fail} 失败, {total_skip} 跳过")
    print(f"总耗时: {total_elapsed:.1f}s")
    print(f"结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    if total_fail > 0:
        print(f"\n❌ {total_fail} 个测试层失败")
        sys.exit(1)
    else:
        print(f"\n✅ 全部测试通过" + (f"（{total_skip} 层跳过）" if total_skip > 0 else ""))


if __name__ == "__main__":
    main()
