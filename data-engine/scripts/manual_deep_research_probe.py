"""体系B深度研究 —— 手动探索脚本（非自动化测试）

⚠ 注意：本脚本不是自动化测试，没有任何断言，只打印 LLM 输出，
  结果是否正确需人工判断。请勿纳入 pytest / run_all.py 自动回归。
  （原 tests/test_11_deep_research.py，2026-07-19 移出 tests/）

前置条件：
  - Management API 服务已启动（端口 2025）
  - 已配置可用的 LLM API Key（如 DeepSeek）
  - 数据库中存在 acc_medical 行业及就诊数据

运行：
  cd data-engine
  python scripts/manual_deep_research_probe.py

人工观察点：
1. local模式：本地数据库分析是否合理
2. ask_user：信息不足时是否主动提问
3. 任务可视化：任务清单是否实时打勾
4. 回归：体系A基础指令是否仍然准确
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["INDUSTRY"] = "acc_medical"

from config.settings import settings
settings.INDUSTRY = "acc_medical"

# 重置单例
try:
    from core.datasource_manager import DataSourceManager
    DataSourceManager._instance = None
except Exception:
    pass
try:
    import core.data_ops as _data_ops
    _data_ops._federated_driver = None
except Exception:
    pass
try:
    import industries.base as _base
    _base._industries.clear()
except Exception:
    pass


def test_db_status():
    """检查数据库状态"""
    from core.data_ops import _get_driver
    drv = _get_driver()
    tables = drv.list_tables()
    print(f"当前数据库表({len(tables)}): {tables}")
    for t in tables:
        try:
            cnt = drv.query(f'SELECT COUNT(*) as c FROM "{t}"')
            print(f"  {t}: {cnt[0]['c']}条")
        except Exception as e:
            print(f"  {t}: 查询失败 {e}")


def test_research_local():
    """测试体系B local模式"""
    print("\n" + "=" * 60)
    print("测试1: 体系B local模式 - 分析就诊数据")
    print("=" * 60)

    from agent.open_layer.graph import run_open_agent
    result = run_open_agent("分析最近就诊数据，发现有什么值得关注的趋势或问题")
    print("\n结果：")
    print(result[:4000])
    return result


def test_research_blocked():
    """测试体系B blocked场景（数据库缺表）"""
    print("\n" + "=" * 60)
    print("测试2: 体系B blocked场景 - 需要建表")
    print("=" * 60)

    from agent.open_layer.graph import run_open_agent
    result = run_open_agent("分析药品库存周转率，找出滞销药品")
    print("\n结果：")
    print(result[:3000])
    return result


def test_research_hybrid():
    """测试体系B hybrid模式"""
    print("\n" + "=" * 60)
    print("测试3: 体系B hybrid模式 - 本地数据对比外部标准")
    print("=" * 60)

    from agent.open_layer.graph import run_open_agent
    result = run_open_agent("我们的用药结构是否符合最新临床指南")
    print("\n结果：")
    print(result[:4000])
    return result


def test_basic_regression():
    """回归测试：体系A基础指令"""
    print("\n" + "=" * 60)
    print("测试4: 体系A回归 - 基础指令")
    print("=" * 60)

    from agent.open_layer.graph import run_open_agent
    result = run_open_agent("查询所有就诊记录")
    print("\n结果：")
    print(result[:1000])
    return result


if __name__ == "__main__":
    print("=" * 60)
    print("体系B深度研究测试")
    print("=" * 60)

    test_db_status()

    # 测试体系B
    test_research_local()

    # 测试blocked
    test_research_blocked()

    # 测试hybrid
    test_research_hybrid()

    # 回归测试
    test_basic_regression()

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)
