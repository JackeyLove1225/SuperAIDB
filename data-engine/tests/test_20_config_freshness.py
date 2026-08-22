"""层 20：配置新鲜度（ConfigHub 真解耦）用例

判据：改文件即生效，不重启、不 reset、不跨进程广播。
锁定对象：PermissionPolicy / DataSourceManager / industries.base / settings.INDUSTRY 热键。
"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERM_YML = os.path.join(ROOT, "config", "permissions.yml")
DS_YML = os.path.join(ROOT, "config", "datasources.yml")
ENV = os.path.join(ROOT, "config", ".env")


def _bak(p):
    b = p + ".bak_t20"
    if os.path.exists(p):
        shutil.copy2(p, b)
    return b


def _restore(p, b):
    if os.path.exists(b):
        shutil.move(b, p)
    elif os.path.exists(p):
        os.remove(p)


def test_permission_hot_reload():
    """权限规则改文件即生效（fail-closed 语义保留）"""
    from core.permission.policy import PermissionPolicy, Operation, PermissionDenied
    b = _bak(PERM_YML)
    try:
        pol = PermissionPolicy.get_instance()
        pol.check("primary", Operation.INSERT)
        with open(PERM_YML, "w", encoding="utf-8") as f:
            f.write("default: full\ndatasources:\n  primary:\n    mode: read_only\n")
        time.sleep(0.02)
        try:
            pol.check("primary", Operation.INSERT)
            raise AssertionError("改文件后 insert 仍放行——热生效失败")
        except PermissionDenied:
            pass
        os.remove(PERM_YML)
        pol.check("primary", Operation.INSERT)
    finally:
        _restore(PERM_YML, b)
    print("OK - 权限规则热生效（改文件即拦截，删文件即恢复）")


def test_datasource_hot_reload():
    """数据源配置改文件即生效（未变源连接保留，变源重建）"""
    from core.datasource_manager import DataSourceManager
    b = _bak(DS_YML)
    try:
        dsm = DataSourceManager()
        dsm.load_config()
        with open(DS_YML, "a", encoding="utf-8") as f:
            f.write("  t20_scratch:\n    type: sqlite\n    path: ./db/test_dml.db\n    is_default: false\n")
        time.sleep(0.02)
        names = [d["name"] for d in dsm.list_datasources()]
        assert "t20_scratch" in names, f"新增数据源未热生效: {names}"
    finally:
        _restore(DS_YML, b)
    names = [d["name"] for d in DataSourceManager().list_datasources()]
    assert "t20_scratch" not in names, f"删除数据源未热生效: {names}"
    print("OK - 数据源热生效（新增即见、删除即消）")


def test_industry_hot_key():
    """settings.INDUSTRY 热键 + 行业目录签名新鲜度"""
    from config.settings import settings
    from industries.base import get_current_industry, _load_fresh
    b = _bak(ENV)
    # 先读出原值（.env 可能不存在——此时原值为空，恢复后应读代码默认值）
    orig = ""
    if os.path.exists(ENV):
        orig = next((l.split("=", 1)[1].strip()
                     for l in open(ENV, encoding="utf-8").read().splitlines()
                     if l.startswith("INDUSTRY=")), "")
    try:
        lines = open(ENV, encoding="utf-8").read().splitlines() if os.path.exists(ENV) else []
        if not any(l.startswith("INDUSTRY=") for l in lines):
            lines.append("INDUSTRY=")  # 无 .env 时先补键，替换逻辑才有锚点
        with open(ENV, "w", encoding="utf-8") as f:
            f.write("\n".join("INDUSTRY=construction_engineering" if l.startswith("INDUSTRY=") else l
                              for l in lines) + "\n")
        time.sleep(0.02)
        assert settings.INDUSTRY == "construction_engineering", f"INDUSTRY 热键未生效: {settings.INDUSTRY}"
        cfg = get_current_industry()
        assert cfg.name == "construction_engineering", f"行业切换未生效: {cfg.name}"
    finally:
        _restore(ENV, b)
    # 恢复后应读回原值（不硬编码持久值）；无 .env 时读代码默认值（与 settings.py 同口径）
    expected = orig or "construction_engineering"
    assert settings.INDUSTRY == expected, f"恢复后应读回 {expected}: {settings.INDUSTRY}"
    cfg2 = _load_fresh("construction_engineering")
    assert cfg2 is not None, "construction_engineering 行业读取异常"
    # 定额库 Schema 包随行业发布（4 表：quota_items/quota_labor/quota_machines/quota_materials）
    names = sorted(t.get("name") for t in cfg2.tables)
    assert names == ["quota_items", "quota_labor", "quota_machines", "quota_materials"], \
        f"定额库 Schema 包应为 4 表: {names}"
    print("OK - INDUSTRY 热键 + 行业目录签名新鲜度")


def test_metadb_rebind():
    """MetaDB 行业切换自动换绑（不 reset 不重启）"""
    from config.settings import settings
    from core.graph.meta_db import MetaDB, _resolve_meta_db_path
    b = _bak(ENV)
    try:
        inst1 = MetaDB.get_instance()
        p1 = inst1._db_path
        lines = open(ENV, encoding="utf-8").read().splitlines() if os.path.exists(ENV) else []
        if not any(l.startswith("INDUSTRY=") for l in lines):
            lines.append("INDUSTRY=")  # 无 .env 时先补键，替换逻辑才有锚点
        with open(ENV, "w", encoding="utf-8") as f:
            f.write("\n".join("INDUSTRY=flower_industry" if l.startswith("INDUSTRY=") else l
                              for l in lines) + "\n")
        time.sleep(0.02)
        inst2 = MetaDB.get_instance()
        assert inst2._db_path != p1, f"MetaDB 未换绑: {inst2._db_path}"
        assert "flower_industry" in inst2._db_path
    finally:
        _restore(ENV, b)
    inst3 = MetaDB.get_instance()
    assert "engineering" in inst3._db_path
    print("OK - MetaDB 行业切换自动换绑")


if __name__ == "__main__":
    test_permission_hot_reload()
    test_datasource_hot_reload()
    test_industry_hot_key()
    test_metadb_rebind()
    print("\n=== 层 20 全部通过：配置新鲜度（ConfigHub 真解耦）===")
