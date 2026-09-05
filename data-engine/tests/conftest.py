"""pytest 共享配置

- 夹具卫生：test_08 的 _test_schema 刮削行业在每个测试模块前清理
  （run_all 脚本模式下由 test_08 自己的 finally 清理；pytest 模式由这里兜底）
"""
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True, scope="module")
def _clean_scratch_industries():
    """每个测试模块前清理刮削行业夹具（test_08 的 _test_schema）；
    模块结束后恢复行业进程态——test_08 用例会切 settings.INDUSTRY 到
    _test_schema 且 pytest 下不走其 cleanup_industry()，目录被清后
    INDUSTRY 仍指向已删除行业，连带 MetaDB/DataSourceManager 绑错库，
    污染后续所有依赖行业态的测试（test_15/16/19/20/22 级联失败）。"""
    for name in ("_test_schema",):
        d = ROOT / "industries" / name
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    yield
    # 清进程内行业覆盖（不能 settings.INDUSTRY = x——setter 会写 _industry_override
    # 焊死 .env 热键；正确恢复是清 override，让热键回到 .env 驱动）
    from config.settings import settings
    settings._industry_override = None
    try:
        from core.graph.meta_db import MetaDB
        MetaDB.reset_instance()
    except Exception:
        pass  # teardown 尽力而为：模块未加载/无实例时无可重置
    try:
        from core.datasource_manager import DataSourceManager
        DataSourceManager.reset_instance()
    except Exception:
        pass  # 同上
    try:
        import core.data_ops as _ops
        _ops.reset_driver_cache()  # 公开入口（不再戳 _federated_driver 私有）
    except Exception:
        pass  # 同上
    try:
        import industries.base as _base
        _base.reset_registry()  # 公开入口（不再戳 _industries 私有）
    except Exception:
        pass  # 同上
