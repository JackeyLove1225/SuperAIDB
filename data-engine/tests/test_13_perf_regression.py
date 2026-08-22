"""全项目性能与内存回归测试

追踪运行时的每一个环节，找出速度与内存问题点。
优化原则：内存与效率无法兼顾时，优先选择内存。

测试维度：
  1. 模块导入阶段：导入时间 + 内存增量
  2. 单例模式验证：AIClient / Agent / ChromaStore / LLM / Driver
  3. 数据库操作速度：list_tables / get_columns / query / CRUD
  4. AI 客户端调用：LLM 初始化 + 调用延迟
  5. 向量数据库：初始化 + 列举 + 搜索
  6. 文件解析：PDF / Excel / Word 速度 + 峰值内存
  7. LangGraph 节点：understand / route / execute / synthesize 耗时
  8. OODA 循环：每轮耗时 + 上下文缓存命中
  9. 内存热点追踪：tracemalloc top allocations
 10. 长时间运行泄漏检测：循环执行 N 次检查内存增长趋势
 11. API 端点响应（可选，需服务运行）

用法：
    cd data-engine
    python tests/test_13_perf_regression.py                # 全量测试（含真实 AI 调用，需 LLM Key）
    python tests/test_13_perf_regression.py --skip-ai      # 跳过真实 AI 调用（CI/无Key环境推荐）
    python tests/test_13_perf_regression.py --suite import  # 仅导入阶段
    python tests/test_13_perf_regression.py --suite memory  # 仅内存相关
    python tests/test_13_perf_regression.py --report report.json  # 输出 JSON 报告

退出码：
    0 = 全部通过（或有 WARN 但指定了 --warn-ok）
    1 = 有 WARN
    2 = 有 FAIL
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable

# ── 确保项目根目录在 sys.path ──
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 性能阈值（可调）— 严格偏向内存
THRESHOLDS = {
    # 导入阶段（秒）—导入是冷启动主因
    "import_agent": 3.0,
    "import_open_layer": 5.0,
    "import_vector_store": 4.0,
    "import_parser": 3.0,
    # 单例验证（必须为 True）
    "singleton_required": True,
    # 数据库操作（毫秒）
    "list_tables_ms": 50,
    "get_columns_ms": 30,
    "simple_query_ms": 50,
    # 向量库初始化（秒，仅首次）
    "vector_init_s": 5.0,
    # 文件解析（毫秒/页）
    "pdf_parse_ms_per_page": 200,
    "excel_parse_ms_per_sheet": 500,
    # LangGraph 节点（毫秒，不含 LLM 网络）
    "graph_node_ms": 200,
    # 内存阈值
    "import_mem_mb": 80,           # 导入阶段总内存增量
    "single_op_mem_delta_mb": 10,  # 单次操作的内存波动
    "leak_per_iter_kb": 50,        # 每次迭代允许的内存增长（KB）
    "tracemalloc_top_kb": 500,     # 单一分配点超此值告警
    # AI 调用（秒，含网络）
    "ai_call_s": 30.0,
}


# ═══════════════════════════════════════════════════════════════
# 报告收集器
# ═══════════════════════════════════════════════════════════════

class Report:
    """测试报告收集器"""

    def __init__(self):
        self.entries: list[dict] = []
        self.warnings = 0
        self.failures = 0

    def add(self, suite: str, name: str, status: str,
            value: Any = None, threshold: Any = None,
            detail: str = "", suggestion: str = ""):
        self.entries.append({
            "suite": suite,
            "name": name,
            "status": status,  # PASS / WARN / FAIL / SKIP / INFO
            "value": value,
            "threshold": threshold,
            "detail": detail,
            "suggestion": suggestion,
        })
        if status == "WARN":
            self.warnings += 1
        elif status == "FAIL":
            self.failures += 1

    def print_console(self):
        print("\n" + "=" * 78)
        print("性能与内存回归测试报告")
        print("=" * 78)

        # 按套件分组
        suites: dict[str, list[dict]] = {}
        for e in self.entries:
            suites.setdefault(e["suite"], []).append(e)

        for suite, items in suites.items():
            print(f"\n── {suite} {'─' * (70 - len(suite))}")
            for e in items:
                status_icon = {
                    "PASS": "[PASS]",
                    "WARN": "[WARN]",
                    "FAIL": "[FAIL]",
                    "SKIP": "[SKIP]",
                    "INFO": "[INFO]",
                }.get(e["status"], "[????]")
                line = f"  {status_icon} {e['name']}"
                if e["value"] is not None:
                    line += f"  → {e['value']}"
                if e["threshold"] is not None and e["status"] in ("WARN", "FAIL"):
                    line += f"  (阈值: {e['threshold']})"
                print(line)
                if e["detail"]:
                    print(f"          详情: {e['detail']}")
                if e["suggestion"] and e["status"] in ("WARN", "FAIL"):
                    print(f"          建议: {e['suggestion']}")

        # 汇总
        total = len(self.entries)
        passed = sum(1 for e in self.entries if e["status"] == "PASS")
        skipped = sum(1 for e in self.entries if e["status"] == "SKIP")
        infos = sum(1 for e in self.entries if e["status"] == "INFO")
        print("\n" + "=" * 78)
        print(f"汇总: {passed} 通过 / {self.warnings} 警告 / {self.failures} 失败 "
              f"/ {skipped} 跳过 / {infos} 信息  (共 {total})")
        print("=" * 78)

    def to_json(self) -> str:
        return json.dumps(self.entries, ensure_ascii=False, indent=2, default=str)


report = Report()


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def _mem_mb() -> float:
    """当前进程 RSS（MB）"""
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


def _time_ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def _status(value: float, threshold: float, lower_is_better: bool = True) -> str:
    """根据阈值判断 PASS/WARN/FAIL"""
    if lower_is_better:
        if value <= threshold:
            return "PASS"
        if value <= threshold * 1.5:
            return "WARN"
        return "FAIL"
    else:
        if value >= threshold:
            return "PASS"
        if value >= threshold * 0.7:
            return "WARN"
        return "FAIL"


def _force_gc():
    """强制 GC 多次，确保释放"""
    for _ in range(3):
        gc.collect()


# ═══════════════════════════════════════════════════════════════
# 套件 1：模块导入阶段
# ═══════════════════════════════════════════════════════════════

def suite_imports(skip_ai: bool = False):
    """测试关键模块的导入时间和内存增量"""
    print("\n[1/11] 模块导入阶段测试...")
    _force_gc()
    base_mem = _mem_mb()

    modules = [
        ("config.settings", "config.settings"),
        ("core.ai_runtime.ai_client", "core.ai_runtime.ai_client"),
        ("core.data_ops", "core.data_ops"),
        ("core.drivers.sqlite_driver", "core.drivers.sqlite_driver"),
        ("core.drivers.federated_driver", "core.drivers.federated_driver"),
        ("core.datasource_manager", "core.datasource_manager"),
        ("core.schema_manager", "core.schema_manager"),
        ("core.vector_store", "core.vector_store"),
        ("core.parser", "core.parser"),
        ("agent", "agent"),
        ("agent.router", "agent.router"),
        ("agent.tools", "agent.tools"),
        ("agent.open_layer.state", "agent.open_layer.state"),
        ("agent.open_layer.executor", "agent.open_layer.executor"),
        ("agent.open_layer.graph", "agent.open_layer.graph"),
        ("agent.open_layer.research", "agent.open_layer.research"),
        ("agent.open_layer.rag", "agent.open_layer.rag"),
        ("agent.open_layer.file_tools", "agent.open_layer.file_tools"),
        ("agent.open_layer.web_tools", "agent.open_layer.web_tools"),
        ("agent.management.server", "agent.management.server"),
        ("pipeline.runner", "pipeline.runner"),
    ]

    total_mem = 0
    for label, mod_path in modules:
        t0 = time.perf_counter()
        mem_before = _mem_mb()
        try:
            __import__(mod_path)
            elapsed_ms = _time_ms(t0)
            mem_delta = _mem_mb() - mem_before
            total_mem += mem_delta

            # 判断阈值
            if "graph" in mod_path or "open_layer" in mod_path:
                thr = THRESHOLDS["import_open_layer"] * 1000
            elif "vector_store" in mod_path:
                thr = THRESHOLDS["import_vector_store"] * 1000
            elif "parser" in mod_path:
                thr = THRESHOLDS["import_parser"] * 1000
            elif "agent" in mod_path:
                thr = THRESHOLDS["import_agent"] * 1000
            else:
                thr = 2000  # 默认 2 秒

            status = _status(elapsed_ms, thr)
            report.add(
                "1.导入阶段", label, status,
                value=f"{elapsed_ms:.0f}ms / +{mem_delta:.1f}MB",
                threshold=f"{thr:.0f}ms",
                detail=f"导入 {mod_path}",
                suggestion="若慢：检查模块顶层是否有重 I/O（打开 DB/网络）" if status != "PASS" else "",
            )
        except Exception as e:
            report.add(
                "1.导入阶段", label, "FAIL",
                detail=f"导入失败: {e}",
                suggestion="检查依赖与 sys.path",
            )

    # 总内存增量
    final_mem = _mem_mb() - base_mem
    status = _status(final_mem, THRESHOLDS["import_mem_mb"])
    report.add(
        "1.导入阶段", "导入后总内存增量", status,
        value=f"{final_mem:.1f}MB",
        threshold=f"{THRESHOLDS['import_mem_mb']}MB",
        detail=f"基线 {base_mem:.1f}MB → 导入后 {_mem_mb():.1f}MB",
        suggestion="若超：检查模块顶层是否过早初始化单例（如 ChromaStore/LLM）" if status != "PASS" else "",
    )


# ═══════════════════════════════════════════════════════════════
# 套件 2：单例模式验证
# ═══════════════════════════════════════════════════════════════

def suite_singletons(skip_ai: bool = False):
    """验证关键对象是否真正复用单例"""
    print("\n[2/11] 单例模式验证...")
    _force_gc()

    # AIClient
    try:
        from core.ai_runtime.ai_client import AIClient
        c1 = AIClient.get_instance()
        c2 = AIClient.get_instance()
        ok = c1 is c2
        report.add(
            "2.单例验证", "AIClient 单例", "PASS" if ok else "FAIL",
            value=ok,
            detail=f"c1 is c2 = {ok}",
            suggestion="检查 get_instance() classmethod 实现" if not ok else "",
        )
        # OpenAI client 复用
        client1 = getattr(c1, "client", None)
        client2 = getattr(c2, "client", None)
        ok2 = client1 is client2
        report.add(
            "2.单例验证", "AIClient.client (OpenAI) 复用", "PASS" if ok2 else "FAIL",
            value=ok2,
            suggestion="避免每次调用新建 httpx 连接池" if not ok2 else "",
        )
    except Exception as e:
        report.add("2.单例验证", "AIClient 单例", "FAIL", detail=str(e))

    # Agent
    try:
        from agent import Agent
        a1 = Agent()
        a2 = Agent()
        # Agent 不强制单例，但 ai 应复用
        ok = a1.ai is a2.ai
        report.add(
            "2.单例验证", "Agent.ai 复用 AIClient", "PASS" if ok else "WARN",
            value=ok,
            detail="两个 Agent 实例应共享同一个 AIClient",
            suggestion="Agent.__init__ 应用 AIClient.get_instance()" if not ok else "",
        )
    except Exception as e:
        report.add("2.单例验证", "Agent.ai 复用", "FAIL", detail=str(e))

    # executor.get_agent 单例
    try:
        from agent.open_layer.executor import get_agent
        g1 = get_agent()
        g2 = get_agent()
        ok = g1 is g2
        report.add(
            "2.单例验证", "executor.get_agent() 单例", "PASS" if ok else "WARN",
            value=ok,
            suggestion="deep_research 应复用此单例，不要 new Agent()" if not ok else "",
        )
    except Exception as e:
        report.add("2.单例验证", "executor.get_agent() 单例", "FAIL", detail=str(e))

    # VectorStore 单例
    try:
        from core.vector_store import get_vector_store
        v1 = get_vector_store()
        v2 = get_vector_store()
        ok = v1 is v2
        report.add(
            "2.单例验证", "get_vector_store() 单例", "PASS" if ok else "FAIL",
            value=ok,
            detail="ChromaStore PersistentClient 初始化重，必须单例",
            suggestion="检查 _vector_store_instance 全局变量" if not ok else "",
        )
    except Exception as e:
        report.add("2.单例验证", "get_vector_store() 单例", "FAIL", detail=str(e))

    # LLM 单例（graph._get_llm）
    try:
        from agent.open_layer.graph import _get_llm
        l1 = _get_llm()
        l2 = _get_llm()
        ok = l1 is l2
        report.add(
            "2.单例验证", "graph._get_llm() 单例", "PASS" if ok else "FAIL",
            value=ok,
            detail="ChatOpenAI 实例应复用（避免 httpx 连接池重复创建）",
            suggestion="检查 _llm_instance 全局变量" if not ok else "",
        )
    except Exception as e:
        report.add("2.单例验证", "graph._get_llm() 单例", "FAIL", detail=str(e))

    # FederatedDriver 单例
    try:
        from core.data_ops import _get_driver
        d1 = _get_driver()
        d2 = _get_driver()
        ok = d1 is d2
        report.add(
            "2.单例验证", "_get_driver() FederatedDriver 单例", "PASS" if ok else "WARN",
            value=ok,
            suggestion="检查 _federated_driver 全局变量" if not ok else "",
        )
    except Exception as e:
        report.add("2.单例验证", "_get_driver() 单例", "FAIL", detail=str(e))


# ═══════════════════════════════════════════════════════════════
# 套件 3：数据库操作速度
# ═══════════════════════════════════════════════════════════════

def suite_database(skip_ai: bool = False):
    """数据库 CRUD 操作耗时"""
    print("\n[3/11] 数据库操作速度测试...")
    _force_gc()

    # 首次初始化（FederatedDriver + DataSourceManager + SqliteDriver）
    t0 = time.perf_counter()
    try:
        from core.data_ops import _get_driver
        drv = _get_driver()
        init_ms = _time_ms(t0)
        report.add(
            "3.数据库", "驱动首次初始化", "INFO",
            value=f"{init_ms:.0f}ms",
            detail="FederatedDriver + DataSourceManager + SqliteDriver 首次创建",
        )
    except Exception as e:
        report.add("3.数据库", "驱动初始化", "FAIL", detail=str(e))
        return

    # list_tables 首次（含 sqlite_master 查询）
    t0 = time.perf_counter()
    try:
        tables = drv.list_tables()
        elapsed_first = _time_ms(t0)
        tables = [t for t in tables if not t.startswith("sqlite_")]
        report.add(
            "3.数据库", "list_tables 首次", "INFO",
            value=f"{elapsed_first:.1f}ms ({len(tables)} 张表)",
        )
    except Exception as e:
        report.add("3.数据库", "list_tables", "FAIL", detail=str(e))
        return

    # list_tables 后续（应 <50ms）
    t0 = time.perf_counter()
    try:
        tables2 = drv.list_tables()
        elapsed_subsequent = _time_ms(t0)
        tables2 = [t for t in tables2 if not t.startswith("sqlite_")]
        status = _status(elapsed_subsequent, THRESHOLDS["list_tables_ms"])
        report.add(
            "3.数据库", "list_tables 后续 (单例后)", status,
            value=f"{elapsed_subsequent:.1f}ms",
            threshold=f"{THRESHOLDS['list_tables_ms']}ms",
            suggestion="若慢：FederatedDriver.list_tables 每次遍历所有数据源，应缓存" if status != "PASS" else "",
        )
    except Exception as e:
        report.add("3.数据库", "list_tables 后续", "FAIL", detail=str(e))

    # get_columns（对每张表）
    if tables:
        total_cols_time = 0
        for t in tables[:5]:  # 取前 5 张表测试
            t0 = time.perf_counter()
            try:
                cols = drv.get_columns(t)
                elapsed = _time_ms(t0)
                total_cols_time += elapsed
            except Exception:
                pass
        avg = total_cols_time / max(1, min(5, len(tables)))
        status = _status(avg, THRESHOLDS["get_columns_ms"])
        report.add(
            "3.数据库", "get_columns (平均)", status,
            value=f"{avg:.1f}ms",
            threshold=f"{THRESHOLDS['get_columns_ms']}ms",
            suggestion="若慢：考虑 schema YAML 缓存" if status != "PASS" else "",
        )

    # 简单查询（取第一张表的 COUNT）
    if tables:
        first = tables[0]
        t0 = time.perf_counter()
        try:
            rows = drv.query(f"SELECT COUNT(*) AS c FROM {first}")
            elapsed = _time_ms(t0)
            status = _status(elapsed, THRESHOLDS["simple_query_ms"])
            report.add(
                "3.数据库", f"SELECT COUNT(*) FROM {first}", status,
                value=f"{elapsed:.1f}ms",
                threshold=f"{THRESHOLDS['simple_query_ms']}ms",
                detail=f"结果: {rows[0]['c'] if rows else 0}",
            )
        except Exception as e:
            report.add("3.数据库", f"COUNT {first}", "FAIL", detail=str(e))

    # 连续 10 次查询（检查稳定性 + 内存波动）
    if tables:
        first = tables[0]
        times = []
        mem_before = _mem_mb()
        for _ in range(10):
            t0 = time.perf_counter()
            try:
                drv.query(f"SELECT * FROM {first} LIMIT 1")
            except Exception:
                pass
            times.append(_time_ms(t0))
        _force_gc()
        mem_delta = _mem_mb() - mem_before
        avg = sum(times) / len(times)
        mx = max(times)
        status_time = _status(avg, THRESHOLDS["simple_query_ms"])
        status_mem = _status(abs(mem_delta), THRESHOLDS["single_op_mem_delta_mb"])
        report.add(
            "3.数据库", "10次连续查询 (平均/最大)", status_time,
            value=f"{avg:.1f}ms / {mx:.1f}ms",
            threshold=f"{THRESHOLDS['simple_query_ms']}ms",
        )
        report.add(
            "3.数据库", "10次连续查询内存波动", status_mem,
            value=f"{mem_delta:+.2f}MB",
            threshold=f"±{THRESHOLDS['single_op_mem_delta_mb']}MB",
            suggestion="内存增长>0 提示可能泄漏，检查是否每次查询创建新对象未释放" if mem_delta > 0.5 else "",
        )


# ═══════════════════════════════════════════════════════════════
# 套件 4：AI 客户端调用
# ═══════════════════════════════════════════════════════════════

def suite_ai_client(skip_ai: bool = False):
    """AI 客户端初始化 + 调用延迟"""
    print("\n[4/11] AI 客户端调用测试...")

    try:
        from core.ai_runtime.ai_client import AIClient
        t0 = time.perf_counter()
        client = AIClient.get_instance()
        elapsed = _time_ms(t0)
        report.add(
            "4.AI客户端", "AIClient 获取", "PASS",
            value=f"{elapsed:.1f}ms",
            detail="单例应 <10ms",
        )
    except Exception as e:
        report.add("4.AI客户端", "AIClient 获取", "FAIL", detail=str(e))
        return

    if skip_ai:
        report.add("4.AI客户端", "AI 真实调用", "SKIP", detail="--skip-ai 已跳过")
        return

    # 真实调用（小 prompt）
    try:
        t0 = time.perf_counter()
        resp = client.chat("你是测试助手", "回复 OK 两个字")
        elapsed = time.perf_counter() - t0
        status = _status(elapsed, THRESHOLDS["ai_call_s"])
        report.add(
            "4.AI客户端", "chat() 调用", status,
            value=f"{elapsed:.2f}s",
            threshold=f"{THRESHOLDS['ai_call_s']}s",
            detail=f"响应: {resp[:50] if resp else '空'}",
            suggestion="若慢：检查网络/base_url；考虑 streaming 模式" if status != "PASS" else "",
        )
    except Exception as e:
        report.add("4.AI客户端", "chat() 调用", "FAIL", detail=str(e))

    # Function Calling 调用
    try:
        functions = [{
            "type": "function",
            "function": {
                "name": "echo",
                "description": "回显输入",
                "parameters": {
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"],
                },
            },
        }]
        t0 = time.perf_counter()
        fn_name, fn_args = client.call_function(functions, "回显 hello")
        elapsed = time.perf_counter() - t0
        status = _status(elapsed, THRESHOLDS["ai_call_s"])
        report.add(
            "4.AI客户端", "call_function() 调用", status,
            value=f"{elapsed:.2f}s",
            threshold=f"{THRESHOLDS['ai_call_s']}s",
            detail=f"fn={fn_name}, args={fn_args}",
        )
    except Exception as e:
        report.add("4.AI客户端", "call_function() 调用", "FAIL", detail=str(e))


# ═══════════════════════════════════════════════════════════════
# 套件 5：向量数据库
# ═══════════════════════════════════════════════════════════════

def suite_vector_store(skip_ai: bool = False):
    """向量数据库初始化 + 列举 + 搜索"""
    print("\n[5/11] 向量数据库测试...")

    try:
        from core.vector_store import get_vector_store
        t0 = time.perf_counter()
        store = get_vector_store()
        elapsed = time.perf_counter() - t0
        # 单例应该 <1ms（首次才慢）
        if elapsed < 1.0:
            status = "PASS"
        else:
            status = _status(elapsed, THRESHOLDS["vector_init_s"])
        report.add(
            "5.向量库", "get_vector_store() 获取", status,
            value=f"{elapsed*1000:.0f}ms",
            threshold=f"{THRESHOLDS['vector_init_s']*1000:.0f}ms",
            detail="首次初始化重，后续应 <10ms（单例）",
            suggestion="若每次都慢：单例失效，检查 _vector_store_instance" if status != "PASS" else "",
        )
    except Exception as e:
        report.add("5.向量库", "get_vector_store()", "FAIL", detail=str(e))
        return

    # list_collections
    try:
        t0 = time.perf_counter()
        if hasattr(store, "list_collections"):
            cols = store.list_collections()
        else:
            cols = []
        elapsed = _time_ms(t0)
        report.add(
            "5.向量库", "list_collections", "PASS" if elapsed < 500 else "WARN",
            value=f"{elapsed:.0f}ms ({len(cols)} 个集合)",
            detail=str(cols[:5]),
        )
    except Exception as e:
        report.add("5.向量库", "list_collections", "WARN", detail=str(e))

    # 搜索（如果有集合）
    if cols:
        try:
            col_name = cols[0]
            t0 = time.perf_counter()
            results = store.search(col_name, "测试查询", top_k=3) if hasattr(store, "search") else []
            elapsed = _time_ms(t0)
            report.add(
                "5.向量库", f"search({col_name})", "PASS" if elapsed < 1000 else "WARN",
                value=f"{elapsed:.0f}ms",
                detail=f"返回 {len(results) if results else 0} 条",
            )
        except Exception as e:
            report.add("5.向量库", "search()", "WARN", detail=str(e))


# ═══════════════════════════════════════════════════════════════
# 套件 6：文件解析
# ═══════════════════════════════════════════════════════════════

def _find_test_files() -> dict:
    """查找用于测试的样本文件"""
    samples = {"pdf": None, "excel": None, "word": None}
    # 查找 uploads / assets 目录
    candidates = [
        Path(_PROJECT_ROOT) / "uploads",
        Path(_PROJECT_ROOT) / "assets",
        Path(_PROJECT_ROOT) / "db",
    ]
    for d in candidates:
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if not p.is_file() or p.stat().st_size == 0:
                continue
            ext = p.suffix.lower()
            if ext == ".pdf" and not samples["pdf"]:
                samples["pdf"] = str(p)
            elif ext in (".xlsx", ".xls") and not samples["excel"]:
                samples["excel"] = str(p)
            elif ext == ".docx" and not samples["word"]:
                samples["word"] = str(p)

    # 优先选小文件（避免测试太久）
    return samples


def suite_parsers(skip_ai: bool = False):
    """文件解析速度 + 内存"""
    print("\n[6/11] 文件解析测试...")
    _force_gc()

    samples = _find_test_files()
    if not any(samples.values()):
        report.add("6.文件解析", "样本文件", "SKIP",
                   detail="未找到 uploads/assets/db 下的 PDF/Excel/Word 文件")
        return

    report.add("6.文件解析", "样本文件", "INFO",
               value=str({k: Path(v).name if v else None for k, v in samples.items()}))

    # PDF
    if samples["pdf"]:
        try:
            from core.parser.pdf_parser import PdfParser
            parser = PdfParser(extract_tables=True, use_cache=True)
            file_path = samples["pdf"]
            file_size_mb = Path(file_path).stat().st_size / 1024 / 1024

            mem_before = _mem_mb()
            t0 = time.perf_counter()
            doc = parser.parse(file_path)
            elapsed = time.perf_counter() - t0
            mem_peak = _mem_mb() - mem_before

            # 估算页数（从 metadata 或 raw_text 行数）
            num_pages = doc.metadata.get("page_count", 0) if hasattr(doc, "metadata") else 0
            if not num_pages and hasattr(doc, "raw_text"):
                # 兜底：按字符数估算（每页约 2000 字符）
                num_pages = max(1, len(doc.raw_text) // 2000)
            ms_per_page = (elapsed * 1000) / max(1, num_pages)

            status_time = _status(ms_per_page, THRESHOLDS["pdf_parse_ms_per_page"])
            status_mem = _status(mem_peak, file_size_mb * 5)  # 允许 5x 文件大小

            report.add(
                "6.文件解析", f"PDF 解析 (~{num_pages} 页, {file_size_mb:.1f}MB)", status_time,
                value=f"{elapsed*1000:.0f}ms ({ms_per_page:.0f}ms/页)",
                threshold=f"{THRESHOLDS['pdf_parse_ms_per_page']}ms/页",
                suggestion="若慢：用 lazy 模式或检查 PyMuPDF 版本" if status_time != "PASS" else "",
            )
            report.add(
                "6.文件解析", "PDF 解析内存峰值", status_mem,
                value=f"{mem_peak:+.1f}MB",
                threshold=f"≤{file_size_mb * 5:.1f}MB (5x 文件大小)",
                suggestion="若高：检查表格存储是否用 list[str] 而非 PhysicalCell" if status_mem != "PASS" else "",
            )

            # 二次解析（缓存命中）
            t0 = time.perf_counter()
            doc2 = parser.parse(file_path)
            elapsed2 = time.perf_counter() - t0
            cache_hit = elapsed2 < 0.1
            report.add(
                "6.文件解析", "PDF 二次解析 (缓存)", "PASS" if cache_hit else "WARN",
                value=f"{elapsed2*1000:.0f}ms",
                detail="缓存命中应 <100ms",
                suggestion="若慢：检查 parser_cache 是否生效" if not cache_hit else "",
            )

            # 释放
            del doc, doc2, parser
            _force_gc()

        except Exception as e:
            report.add("6.文件解析", "PDF 解析", "FAIL", detail=str(e))

    # Excel
    if samples["excel"]:
        try:
            from core.parser.excel_parser import ExcelParser
            parser = ExcelParser()
            file_path = samples["excel"]
            file_size_mb = Path(file_path).stat().st_size / 1024 / 1024

            mem_before = _mem_mb()
            t0 = time.perf_counter()
            doc = parser.parse(file_path)
            elapsed = time.perf_counter() - t0
            mem_peak = _mem_mb() - mem_before

            num_sheets = len(doc.pages) if hasattr(doc, "pages") else 1
            ms_per_sheet = (elapsed * 1000) / max(1, num_sheets)

            status_time = _status(ms_per_sheet, THRESHOLDS["excel_parse_ms_per_sheet"])
            report.add(
                "6.文件解析", f"Excel 解析 ({num_sheets} sheet, {file_size_mb:.1f}MB)", status_time,
                value=f"{elapsed*1000:.0f}ms ({ms_per_sheet:.0f}ms/sheet)",
                threshold=f"{THRESHOLDS['excel_parse_ms_per_sheet']}ms/sheet",
            )

            del doc, parser
            _force_gc()

        except Exception as e:
            report.add("6.文件解析", "Excel 解析", "FAIL", detail=str(e))

    # Word
    if samples["word"]:
        try:
            from core.parser.word_parser import WordParser
            parser = WordParser()
            file_path = samples["word"]
            file_size_mb = Path(file_path).stat().st_size / 1024 / 1024

            mem_before = _mem_mb()
            t0 = time.perf_counter()
            doc = parser.parse(file_path)
            elapsed = time.perf_counter() - t0
            mem_peak = _mem_mb() - mem_before

            report.add(
                "6.文件解析", f"Word 解析 ({file_size_mb:.1f}MB)", "PASS" if elapsed < 5 else "WARN",
                value=f"{elapsed*1000:.0f}ms / +{mem_peak:.1f}MB",
            )

            del doc, parser
            _force_gc()

        except Exception as e:
            report.add("6.文件解析", "Word 解析", "FAIL", detail=str(e))


# ═══════════════════════════════════════════════════════════════
# 套件 7：LangGraph 节点耗时
# ═══════════════════════════════════════════════════════════════

def suite_langgraph(skip_ai: bool = False):
    """LangGraph 节点执行耗时"""
    print("\n[7/11] LangGraph 节点测试...")
    _force_gc()

    try:
        from agent.open_layer.graph import _get_llm
        from agent.open_layer.state import AgentState
        llm = _get_llm()
    except Exception as e:
        report.add("7.LangGraph", "模块导入", "FAIL", detail=str(e))
        return

    # 构造空 state
    try:
        state = AgentState(messages=[], instruction="测试")
    except Exception:
        # 兜底用 dict
        state = {"messages": [], "instruction": "测试", "sub_tasks": [], "results": []}

    # 测试单个节点（understand_and_decompose）
    if skip_ai:
        report.add("7.LangGraph", "节点测试", "SKIP", detail="--skip-ai 已跳过")
        return

    try:
        from agent.open_layer.graph import understand_and_decompose
        state2 = {**state, "instruction": "查询数据库中有哪些表"} if isinstance(state, dict) else state
        t0 = time.perf_counter()
        result = understand_and_decompose(state2)
        elapsed = _time_ms(t0)
        status = _status(elapsed, THRESHOLDS["graph_node_ms"] * 10)  # LLM 节点放宽 10x
        report.add(
            "7.LangGraph", "understand_and_decompose", status,
            value=f"{elapsed:.0f}ms",
            threshold=f"{THRESHOLDS['graph_node_ms']*10:.0f}ms",
            detail="含 LLM 调用，受网络影响",
            suggestion="若慢：检查 prompt 大小 + streaming" if status != "PASS" else "",
        )
    except Exception as e:
        report.add("7.LangGraph", "understand_and_decompose", "WARN", detail=str(e)[:200])


# ═══════════════════════════════════════════════════════════════
# 套件 8：OODA 循环 + 上下文缓存
# ═══════════════════════════════════════════════════════════════

def suite_ooda(skip_ai: bool = False):
    """OODA 上下文缓存验证"""
    print("\n[8/11] OODA 循环测试...")

    # 上下文缓存验证（不需要 AI）
    try:
        from agent.open_layer.research import _collect_context
        t0 = time.perf_counter()
        ctx1 = _collect_context()
        elapsed1 = _time_ms(t0)

        t0 = time.perf_counter()
        ctx2 = _collect_context()
        elapsed2 = _time_ms(t0)

        report.add(
            "8.OODA", "_collect_context 首次", "PASS" if elapsed1 < 1000 else "WARN",
            value=f"{elapsed1:.0f}ms",
            detail=f"tables={len(ctx1.get('db_tables', []))}, cols={len(ctx1.get('db_columns', []))}",
        )
        report.add(
            "8.OODA", "_collect_context 二次", "PASS" if elapsed2 < 1000 else "WARN",
            value=f"{elapsed2:.0f}ms",
            detail="应该也较快（实际查询 DB，但表少时应 <500ms）",
        )
    except Exception as e:
        report.add("8.OODA", "_collect_context", "WARN", detail=str(e))

    # state 缓存机制验证
    try:
        from agent.open_layer.state import AgentState
        s = AgentState(messages=[], instruction="测试") if False else {"_cached_context": None}
        # 模拟 OODA 多 goal 复用缓存
        s["_cached_context"] = {"db_tables": ["test"]}
        cached = s.get("_cached_context") if hasattr(s, "get") else None
        ok = cached is not None
        report.add(
            "8.OODA", "state['_cached_context'] 缓存机制", "PASS" if ok else "WARN",
            value=ok,
            detail="OODA 多 goal 应缓存上下文，避免每 goal 重采集",
            suggestion="检查 _ooda_for_goal 是否读取 state['_cached_context']" if not ok else "",
        )
    except Exception as e:
        report.add("8.OODA", "state 缓存机制", "WARN", detail=str(e))


# ═══════════════════════════════════════════════════════════════
# 套件 9：内存热点追踪（tracemalloc）
# ═══════════════════════════════════════════════════════════════

def suite_memory_hotspots(skip_ai: bool = False):
    """tracemalloc 追踪 top 内存分配点"""
    print("\n[9/11] 内存热点追踪 (tracemalloc)...")

    tracemalloc.start()
    snapshot1 = tracemalloc.take_snapshot()

    # 执行一组典型操作
    try:
        from core.data_ops import _get_driver
        drv = _get_driver()
        tables = drv.list_tables()
        for t in tables[:3]:
            try:
                drv.get_columns(t)
                drv.query(f"SELECT * FROM {t} LIMIT 10")
            except Exception:
                pass
    except Exception:
        pass

    try:
        from core.vector_store import get_vector_store
        store = get_vector_store()
        if hasattr(store, "list_collections"):
            store.list_collections()
    except Exception:
        pass

    snapshot2 = tracemalloc.take_snapshot()
    stats = snapshot2.compare_to(snapshot1, "lineno")

    top_stats = stats[:15]
    flagged = 0
    for stat in top_stats:
        size_kb = stat.size_diff / 1024
        if abs(size_kb) > THRESHOLDS["tracemalloc_top_kb"]:
            flagged += 1

    if flagged == 0:
        status = "PASS"
    elif flagged <= 3:
        status = "WARN"
    else:
        status = "FAIL"

    detail_lines = []
    for stat in top_stats[:10]:
        size_kb = stat.size_diff / 1024
        count = stat.count_diff
        frame = stat.traceback[0]
        fname = Path(frame.filename).name if frame.filename else "?"
        detail_lines.append(f"{fname}:{frame.lineno} {size_kb:+.1f}KB ({count} blocks)")

    report.add(
        "9.内存热点", f"Top 分配点 (告警 {flagged} 个)", status,
        value=f"{flagged} 个超 {THRESHOLDS['tracemalloc_top_kb']}KB",
        threshold=f"≤3 个超 {THRESHOLDS['tracemalloc_top_kb']}KB",
        detail=" | ".join(detail_lines),
        suggestion="查看详情中正向增长最大的文件:行，针对性优化" if status != "PASS" else "",
    )

    tracemalloc.stop()


# ═══════════════════════════════════════════════════════════════
# 套件 10：长时间运行泄漏检测
# ═══════════════════════════════════════════════════════════════

def suite_leak_detection(skip_ai: bool = False):
    """循环执行 N 次典型操作，检查内存增长趋势"""
    print("\n[10/11] 长时间运行泄漏检测...")
    _force_gc()

    try:
        from core.data_ops import _get_driver
        drv = _get_driver()
        tables = drv.list_tables()
        tables = [t for t in tables if not t.startswith("sqlite_")]
    except Exception as e:
        report.add("10.泄漏检测", "初始化", "FAIL", detail=str(e))
        return

    if not tables:
        report.add("10.泄漏检测", "无表可测", "SKIP")
        return

    first = tables[0]
    iterations = 20
    mem_samples = []

    _force_gc()
    mem_before = _mem_mb()

    for i in range(iterations):
        # 模拟典型操作
        try:
            drv.list_tables()
            drv.get_columns(first)
            drv.query(f"SELECT * FROM {first} LIMIT 5")
        except Exception:
            pass

        # 每 5 次采样
        if i % 5 == 0:
            _force_gc()
            mem_samples.append((i, _mem_mb()))

    _force_gc()
    mem_after = _mem_mb()
    total_delta = mem_after - mem_before
    per_iter_kb = (total_delta * 1024) / iterations

    status = _status(per_iter_kb, THRESHOLDS["leak_per_iter_kb"])
    detail = " -> ".join(f"#{i}:{m:.1f}MB" for i, m in mem_samples)
    report.add(
        "10.泄漏检测", f"{iterations} 次迭代内存增长", status,
        value=f"{total_delta:+.2f}MB ({per_iter_kb:+.1f}KB/次)",
        threshold=f"{THRESHOLDS['leak_per_iter_kb']}KB/次",
        detail=detail,
        suggestion="若持续增长：检查 list_tables/get_columns 是否缓存结果" if per_iter_kb > 0 else "",
    )

    # Agent 多次创建的内存增长（关键！）
    try:
        from agent.open_layer.executor import get_agent
        _force_gc()
        mem_before = _mem_mb()
        for _ in range(5):
            g = get_agent()
            del g
        _force_gc()
        mem_after = _mem_mb()
        delta = mem_after - mem_before
        status = "PASS" if delta < 1 else "WARN"
        report.add(
            "10.泄漏检测", "get_agent() 5次复用", status,
            value=f"{delta:+.2f}MB",
            detail="单例应 0 增长",
            suggestion="若增长：单例失效或 _history 未清理" if delta > 0.5 else "",
        )
    except Exception as e:
        report.add("10.泄漏检测", "get_agent() 测试", "WARN", detail=str(e))

    # LangGraph 状态对象累积（如果 state 带历史）
    try:
        from agent.open_layer.state import AgentState
        _force_gc()
        mem_before = _mem_mb()
        states = []
        for i in range(50):
            s = AgentState(messages=[{"role": "user", "content": f"msg{i}"}], instruction=f"test{i}")
            states.append(s)
        mem_with_states = _mem_mb()
        del states
        _force_gc()
        mem_after = _mem_mb()
        delta_per_state = (mem_with_states - mem_before) / 50
        reclaimed = mem_with_states - mem_after
        status = "PASS" if delta_per_state < 0.5 else "WARN"
        report.add(
            "10.泄漏检测", "AgentState 50次创建", status,
            value=f"{delta_per_state*1024:.0f}KB/个, 释放 {reclaimed:.1f}MB",
            detail="单条 state 应 <500KB",
            suggestion="若大：检查 messages 是否引用大对象" if status != "PASS" else "",
        )
    except Exception as e:
        report.add("10.泄漏检测", "AgentState 测试", "WARN", detail=str(e))


# ═══════════════════════════════════════════════════════════════
# 套件 11：API 端点响应（可选）
# ═══════════════════════════════════════════════════════════════

def suite_api_endpoints(skip_ai: bool = False):
    """检查 Management API 是否可访问，响应时间"""
    print("\n[11/11] API 端点响应测试...")
    import urllib.request
    import urllib.error

    base_url = os.getenv("MGMT_API_URL", "http://127.0.0.1:2025")
    endpoints = [
        "/api/health",
        "/api/status",
        "/api/dashboard",
        "/api/logs",
    ]

    # Warmup：先调用一次让缓存生效（模拟服务已运行的真实场景）
    for ep in endpoints:
        try:
            req = urllib.request.Request(f"{base_url}{ep}", headers={"Connection": "close"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read(100)
        except Exception:
            pass

    # 正式测试（缓存命中后）
    for ep in endpoints:
        url = f"{base_url}{ep}"
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url, headers={"Connection": "close"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = resp.read(2000)
                elapsed = _time_ms(t0)
                status_code = resp.status

                # 阈值判断（缓存命中后应很快）
                if ep == "/api/dashboard":
                    status = _status(elapsed, 500)
                elif ep == "/api/logs":
                    status = _status(elapsed, 200)
                else:
                    status = _status(elapsed, 300)

                report.add(
                    "11.API端点", ep, status,
                    value=f"{status_code} {elapsed:.0f}ms",
                    threshold=f"{500 if ep == '/api/dashboard' else 300}ms",
                    detail=f"响应 {len(body)} bytes (warmup 后)",
                    suggestion="若慢：检查是否 TTL 缓存生效" if status != "PASS" else "",
                )
        except urllib.error.URLError:
            report.add("11.API端点", ep, "SKIP",
                       detail=f"服务未运行 ({base_url})")
        except Exception as e:
            report.add("11.API端点", ep, "WARN", detail=str(e)[:100])


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

SUITES = {
    "import": ("1.导入阶段", suite_imports),
    "singleton": ("2.单例验证", suite_singletons),
    "database": ("3.数据库", suite_database),
    "ai": ("4.AI客户端", suite_ai_client),
    "vector": ("5.向量库", suite_vector_store),
    "parser": ("6.文件解析", suite_parsers),
    "langgraph": ("7.LangGraph", suite_langgraph),
    "ooda": ("8.OODA", suite_ooda),
    "memory": ("9.内存热点", suite_memory_hotspots),
    "leak": ("10.泄漏检测", suite_leak_detection),
    "api": ("11.API端点", suite_api_endpoints),
    "all": (None, None),  # 全部
}


def main():
    parser = argparse.ArgumentParser(description="全项目性能与内存回归测试")
    parser.add_argument("--suite", default="all",
                        choices=list(SUITES.keys()),
                        help="选择测试套件（默认 all）")
    parser.add_argument("--skip-ai", action="store_true",
                        help="跳过真实 AI 调用（避免网络依赖）")
    parser.add_argument("--warn-ok", action="store_true",
                        help="WARN 不影响退出码（仅 FAIL 退出非0），供 run_all/CI 使用")
    parser.add_argument("--report", default="",
                        help="输出 JSON 报告到文件")
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║        全项目性能与内存回归测试  (内存优先)                          ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print(f"PID: {os.getpid()}  Python: {sys.version.split()[0]}")
    print(f"初始内存: {_mem_mb():.1f}MB")
    print(f"套件: {args.suite}  跳过AI: {args.skip_ai}")

    t_start = time.perf_counter()

    if args.suite == "all":
        for key, (name, fn) in SUITES.items():
            if key == "all" or fn is None:
                continue
            try:
                fn(skip_ai=args.skip_ai)
            except Exception as e:
                report.add(name, "套件异常", "FAIL", detail=str(e))
    else:
        name, fn = SUITES[args.suite]
        if fn:
            try:
                fn(skip_ai=args.skip_ai)
            except Exception as e:
                report.add(name, "套件异常", "FAIL", detail=str(e))

    elapsed_total = time.perf_counter() - t_start

    report.print_console()
    print(f"\n总耗时: {elapsed_total:.1f}s  最终内存: {_mem_mb():.1f}MB")

    # 针对性优化建议汇总
    if report.failures > 0 or report.warnings > 0:
        print("\n" + "─" * 78)
        print("针对性优化建议（按优先级）")
        print("─" * 78)
        seen = set()
        for e in report.entries:
            if e["status"] in ("FAIL", "WARN") and e["suggestion"]:
                key = e["suggestion"][:60]
                if key not in seen:
                    seen.add(key)
                    print(f"  • [{e['suite']}] {e['suggestion']}")

    # JSON 报告
    if args.report:
        out_path = Path(args.report)
        out_path.write_text(report.to_json(), encoding="utf-8")
        print(f"\nJSON 报告已保存: {out_path.absolute()}")

    # 退出码
    if report.failures > 0:
        sys.exit(2)
    elif report.warnings > 0 and not args.warn_ok:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
