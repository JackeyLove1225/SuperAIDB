"""数据库驱动包——纯 Driver 实现类"""
def _get_driver():
    """内部使用：获取驱动实例。仅限 core/ 内部模块，外部代码请走 Steward"""
    from core.steward import Steward
    return Steward()._get_driver()
