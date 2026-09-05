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
    from core.graph.meta_db import MetaDB
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


def test_reset_registry():
    """注册表制重置：核心模块自登记 + reset_all 全覆盖（含 Steward 漏员回归锁）

    历史病型：行业切换手工点名重置，Steward 驱动缓存漏登记 → DDL 落旧行业库。
    断言：1) 关键模块全部自注册 2) Steward()._driver 有缓存时 reset_all 后单例作废
    3) 单个钩子失败不中断其余且如实进失败名单（不静默）
    """
    import core.steward, core.data_ops, industries.base  # noqa: F401  # 触发各自模块自注册
    import core.graph.meta_db, core.graph.schema_graph_service  # noqa: F401
    import core.context, core.datasource_manager  # noqa: F401
    from core.registry import registered, reset_all, register_reset, _hooks

    names = registered()
    for want in ("steward", "datasource_manager", "data_ops_driver_cache",
                 "industry_registry", "meta_db", "schema_graph_service", "context"):
        assert want in names, f"{want} 未自注册到重置注册表: {names}"

    # Steward 漏员回归锁：灌一个假驱动缓存，reset_all 后实例必须作废
    from core.steward import Steward
    Steward()._driver = object()
    old = Steward._instance
    failed = reset_all(context="层20自测")
    assert not failed, f"重置钩子失败: {failed}"
    assert Steward._instance is None, "reset_all 后 Steward 单例必须作废"
    assert Steward() is not old, "重置后应重建新实例"

    # 失败不中断：一个炸掉的钩子不挡后面的钩子，且名字入失败名单
    called = []
    register_reset("t20_bomb", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    register_reset("t20_after", lambda: called.append("after"))
    failed = reset_all(context="层20自测")
    assert "t20_bomb" in failed and "after" in called, \
        f"失败钩子必须如实上报且不中断后续: failed={failed} called={called}"
    # 清理自测钩子（不污染其他层——本层独立子进程，防御性恢复即可）
    _hooks[:] = [(n, f) for n, f in _hooks if not n.startswith("t20_")]
    print("OK - 重置注册表：7 钩自注册/Steward 漏员回归/失败不中断且如实上报")


def test_json_contract_first_read():
    """JsonContract 首读已存在文件必须返回真实内容（回归锁）

    病根：类型守卫写成 isinstance(data, type(self._cache))——新实例 _cache=None，
    NoneType 恒不匹配 dict → 首读吞真实内容且记录 mtime（病灶粘住）。
    本用例="新实例+已存在文件"路径，run_all 每层发新文件的历史从未踩到它。
    """
    import tempfile, json as _json
    from pathlib import Path
    from core.file_contract import JsonContract
    with tempfile.TemporaryDirectory() as tmp:
        fp = Path(tmp) / "sel.json"
        fp.write_text(_json.dumps({"1": {"table": "t", "ids": [1, 2]}}), encoding="utf-8")
        c = JsonContract(fp)
        first = c.read()
        assert first == {"1": {"table": "t", "ids": [1, 2]}}, \
            f"首读已存在文件吞了真实内容: {first!r}"
        # 二次读（缓存命中路径）也必须一致
        assert c.read() == first, "二次读漂移"
        # 写入后回读一致（写完读旧值的历史坑位不复活）
        c.write({"2": {"table": "u", "ids": [3]}})
        assert c.read() == {"2": {"table": "u", "ids": [3]}}
        # 损坏文件 fail-default
        fp.write_text("{不是JSON", encoding="utf-8")
        c2 = JsonContract(fp)
        assert c2.read() == {}, "损坏文件应回 default"
    print("OK - JsonContract 首读真实内容/二次一致/写后回读/损坏回 default")


def test_batch_window_semantics():
    """批处理窗语义（架构复核 N3 回归锁）：
    begin→notify 不点火→end 恰一次点火；批内异常已写通道仍对账；
    嵌套/重复配对后深度归零（脏深度不泄漏到下一次）"""
    from core.registry import (register_on_change, notify_change,
                               begin_batch, end_batch, _change_hooks)
    calls = []
    register_on_change("t20_chan", "probe", lambda: calls.append(1))
    try:
        begin_batch()
        notify_change("t20_chan")
        notify_change("t20_chan")
        assert calls == [], f"批窗内不得点火: {calls}"
        end_batch()
        assert calls == [1], f"批窗结束恰一次点火: {calls}"
        # 批内异常：finally 的 end_batch 仍把已记通道对账出去
        begin_batch()
        notify_change("t20_chan")
        try:
            raise RuntimeError("批内爆炸")
        except RuntimeError:
            pass
        finally:
            end_batch()
        assert calls == [1, 1], f"批内失败后已写通道必须对账: {calls}"
        # 配对纪律：begin/end 两回合后不得有残留点火（脏深度不泄漏）
        begin_batch(); end_batch(); begin_batch(); end_batch()
        assert calls == [1, 1], f"配对后脏深度泄漏: {calls}"
    finally:
        _change_hooks.pop("t20_chan", None)
    print("OK - 批处理窗：窗内不点火/收尾恰一次/批内失败照对账/配对不泄漏")


def test_reset_registry_concurrent():
    """registry 并发竞态：多线程同时 reset_all +
    register_reset 不炸不丢——锁快照语义下钩子恰好执行（计数可断言）"""
    import threading
    from core.registry import register_reset, reset_all, _hooks

    counter = {"n": 0}
    lock = threading.Lock()

    def _bump():
        with lock:
            counter["n"] += 1

    register_reset("t20_conc_probe", _bump)
    failed_all = []
    try:
        # 并发 reset_all × 8 + 并发注册 × 8 同时进行
        def _reg(i):
            register_reset(f"t20_conc_{i}", lambda: None)
        def _reset():
            failed_all.extend(reset_all("并发自测"))
        threads = ([threading.Thread(target=_reset) for _ in range(8)]
                   + [threading.Thread(target=_reg, args=(i,)) for i in range(8)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert counter["n"] >= 1, "reset_all 必须至少执行一次探针钩子"
        from core.registry import registered
        names = registered()
        assert "t20_conc_probe" in names, "并发注册后探针丢失"
        # failed 名单断言（测试复核 N2：并发下钩子失败曾被吞照样绿——
        # JsonContract.write 全程持锁后，并发 reset 的钩子失败必须为零）
        assert failed_all == [], f"并发 reset_all 的钩子失败被静默吞下: {failed_all}"
    finally:
        _hooks[:] = [(n, f) for n, f in _hooks if not n.startswith("t20_conc")]
    print("OK - 注册表并发：reset_all/register_reset 并发不炸不丢")


if __name__ == "__main__":
    test_permission_hot_reload()
    test_datasource_hot_reload()
    test_industry_hot_key()
    test_metadb_rebind()
    test_reset_registry()
    test_json_contract_first_read()
    test_batch_window_semantics()
    test_reset_registry_concurrent()
    print("\n=== 层 20 全部通过：配置新鲜度（ConfigHub 真解耦）===")
