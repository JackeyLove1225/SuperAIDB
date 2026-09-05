"""层 19：记录级 DML（单条/批量 增删改查，生产同构路径）

走与生产一致的 FederatedDriver（契约包装）→ data_ops 链路，
独立刮削库（tests/fixtures/datasources_dml.yml → db/test_dml.db），
覆盖全回归清单第四/五域的缺口用例：

1. 批量增：3 行全入；含 id 的行 → 整批拒绝（主键系统生成）
2. 唯一键冲突：同 code 重复插 → conflict 如实报
3. 类型校验：文本进 FLOAT 列 → 契约层拒收并指明行列
4. 全角数字：NFKC 归一后可入（与管线归一口径一致——如失败说明记录路径有缺口）
5. 批量改：条件命中多行全改，返回条数
6. 批量删：条件命中多行全删，返回条数
7. 0 命中：改/删条件不命中 → 如实报 0 条不报错
8. 删除安全：无选择集直接 delete_data → 拒绝（防全表清空）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.crypto.connection import open_db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DS_CONFIG = os.path.join(BASE_DIR, "tests", "fixtures", "datasources_dml.yml")
DB_PATH = os.path.join(BASE_DIR, "db", "test_dml.db")

_ORIG_PRIMARY = None  # daemon 模式下被覆盖的真实 primary 条目（teardown 恢复）


def _setup():
    """独立刮削库 + 测试表（生产同构 FederatedDriver）

    daemon 生产装配兼容：daemon 进程只读真实 datasources.yml——
    daemon 模式下把刮削库以 primary 身份合并进真实配置（拆时原样恢复）；
    直连模式维持夹具加载。"""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = open_db(DB_PATH)
    conn.execute("""CREATE TABLE t_demo(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE, name TEXT, price FLOAT, amount INTEGER)""")
    conn.commit()
    conn.close()
    from core.datasource_manager import DataSourceManager
    from config.settings import settings
    DataSourceManager.reset_instance()  # 先复位（清掉默认配置的单例），再建夹具/合并
    if settings.DAEMON_MODE_EFFECTIVE == "true":
        import yaml
        ds_path = os.path.join("config", "datasources.yml")
        with open(ds_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        global _ORIG_PRIMARY
        _ORIG_PRIMARY = (cfg.get("datasources") or {}).get("primary")
        cfg.setdefault("datasources", {})["primary"] = {
            "type": "sqlite", "path": DB_PATH, "is_default": True}
        with open(ds_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)
    else:
        DataSourceManager().load_config(DS_CONFIG)
    import core.data_ops as _ops
    _ops._federated_driver = None


def _teardown():
    # 先关连接再删文件（Windows 文件占用）
    try:
        import core.data_ops as _ops
        if _ops._federated_driver is not None:
            _ops._federated_driver.close()
    except Exception:
        pass
    from core.datasource_manager import DataSourceManager
    DataSourceManager.reset_instance()
    import core.data_ops as _ops
    _ops._federated_driver = None
    # daemon 模式合并进真实配置的刮削库：恢复原 primary 条目
    global _ORIG_PRIMARY
    if _ORIG_PRIMARY is not None:
        import yaml
        ds_path = os.path.join("config", "datasources.yml")
        with open(ds_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg.setdefault("datasources", {})["primary"] = _ORIG_PRIMARY
        with open(ds_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)
        _ORIG_PRIMARY = None
        DataSourceManager.reset_instance()
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


def _count(where=""):
    from core.data_ops import get_driver
    sql = "SELECT COUNT(*) AS c FROM t_demo" + (f" WHERE {where}" if where else "")
    return get_driver().query(sql)[0]["c"]


import pytest

@pytest.fixture(autouse=True, scope="module")
def _dml_env():
    """pytest 模式自动建/拆刮削库（脚本模式 __main__ 已手动调 _setup/_teardown，
    本 fixture 不参与）。module 级：用例间有状态依赖（前行插入供后行改删），
    与脚本模式"setup 一次→顺序跑→teardown"语义一致。"""
    _setup()
    yield
    _teardown()


def test_batch_insert_and_id_guard():
    """1. 批量增正常；含 id 行整批拒绝"""
    from core.data_ops import insert_rows
    rows = [{"code": "D1", "name": "甲", "price": 10.5, "amount": 3},
            {"code": "D2", "name": "乙", "price": 20.5, "amount": 2},
            {"code": "D3", "name": "丙", "price": 30.5, "amount": 6}]
    r = insert_rows("t_demo", rows)
    assert r["ok"] and r["count"] == 3, f"批量增 3 行: {r}"
    assert _count() == 3
    r2 = insert_rows("t_demo", [{"id": 99, "code": "D4", "name": "丁"}])
    assert not r2["ok"] and "主键" in r2["message"], f"含 id 行必须拒绝: {r2}"
    assert _count() == 3, "拒绝后不得有写入"
    print("OK - 批量增+id 主键保护")


def test_unique_conflict():
    """2. 唯一键冲突如实报不覆盖（行业外表走 AppError 翻译，同样不静默覆盖）"""
    from core.data_ops import insert_rows
    from core.exceptions import AppError
    try:
        r = insert_rows("t_demo", [{"code": "D1", "name": "甲改", "price": 99}])
        hit = r.get("conflict") or "已存在" in r.get("message", "")
    except AppError as e:
        hit = "已存在" in str(e) or "唯一" in str(e)
    assert hit, "重复 code 必须如实报冲突"
    row = _get_row("code='D1'")
    assert row["name"] == "甲", "冲突不得覆盖原行"
    print("OK - 唯一键冲突如实报不覆盖")


def _get_row(where):
    from core.data_ops import get_driver
    return get_driver().query(f"SELECT * FROM t_demo WHERE {where}")[0]


def test_type_validation_rejects():
    """3. 文本进 FLOAT 列契约拒收（SecurityError 指明字段/类型/行号）"""
    from core.data_ops import insert_rows
    from core.exceptions import SecurityError
    try:
        r = insert_rows("t_demo", [{"code": "D9", "name": "坏", "price": "abc"}])
        assert not r["ok"] and "非数值" in r.get("message", ""), f"必须拒收: {r}"
    except SecurityError as e:
        assert "非数值" in str(e) and "price" in str(e), f"错误须指明字段与原因: {e}"
    assert _count("code='D9'") == 0
    print("OK - 类型校验拒收（契约层指明字段+原因）")


def test_fullwidth_numeric_accepted():
    """4. 全角数字按数值列 NFKC 归一后可入"""
    from core.data_ops import insert_rows
    r = insert_rows("t_demo", [{"code": "D8", "name": "全角", "price": "１２３．４５"}])
    assert r["ok"], f"全角数字应归一后可入: {r}"
    assert abs(_get_row("code='D8'")["price"] - 123.45) < 1e-9
    print("OK - 全角数字归一可入")


def test_batch_update():
    """5. 改：主键条件改单条生效；非唯一条件批量改被安全契约拒绝"""
    from core.data_ops import update_rows
    from core.exceptions import SecurityError
    rid = _get_row("code='D1'")["id"]
    msg = update_rows("t_demo", "price = 9.9", f"id = {rid}")
    # 双轨契约：data 通道判结构（affected），text 通道判文案
    assert msg.data.get("affected") == 1, f"主键条件改 1 行: {msg}"
    assert "1" in str(msg), f"主键条件改 1 行文案: {msg}"
    assert _get_row("code='D1'")["price"] == 9.9
    try:
        update_rows("t_demo", "price = 0.1", "amount < 5")
        raise AssertionError("非唯一条件批量改必须被安全契约拒绝")
    except SecurityError as e:
        assert "主键" in str(e) or "唯一" in str(e), f"拒绝理由须说明: {e}"
    assert _get_row("code='D2'")["price"] == 20.5, "被拒的批量改不得有副作用"
    print("OK - 主键条件改生效+非唯一批量改安全拒绝")


def test_batch_delete():
    """6. 选择集批量删（产品级批量路径）：选中 2 行全删"""
    from core.context import get_context
    from agent.tools import delete_data
    rows = [_get_row("code='D1'"), _get_row("code='D2'")]
    sid = get_context().save_selection("t_demo", rows)
    msg = delete_data("t_demo", selection_id=sid)
    assert msg.data.get("affected") == 2 and "2" in str(msg), f"选择集 2 行应全删: {msg}"
    assert _count("code='D1'") == 0 and _count("code='D2'") == 0, "选中行必须删净"
    assert _count("code='D3'") == 1 and _count("code='D8'") == 1, "未选中行不得误删"
    print("OK - 选择集批量删（命中行全删）")


def test_zero_hit_honest():
    """7. 0 命中如实报不报错"""
    from core.data_ops import update_rows
    m1 = update_rows("t_demo", "price = 1.0", "id = 99999")
    assert m1.data.get("affected") == 0 and "0" in str(m1), f"主键条件 0 命中应如实报 0 条: {m1}"
    from core.context import get_context
    from agent.tools import delete_data
    sid = get_context().save_selection("t_demo", [{"id": 99999}])
    m2 = delete_data("t_demo", selection_id=sid)
    assert m2.data.get("affected") == 0 and "0" in str(m2), f"陈旧选择集 0 命中应如实报 0 条: {m2}"
    assert _count() == 2, "0 命中不得误删误改"
    print("OK - 0 命中如实报 0 条")


def test_delete_requires_selection():
    """8. 无选择集直接删除 → 拒绝（防全表清空）"""
    from agent.tools import delete_data
    from core.context import get_context
    get_context().clear_all()  # 确保无残留选择集
    msg = delete_data("t_demo")
    assert not msg.data.get("ok") and "请先查询" in str(msg), f"无选择集必须拒绝: {msg}"
    assert _count() == 2
    print("OK - 删除必须经选择集（防全表清空）")


if __name__ == "__main__":
    _setup()
    try:
        test_batch_insert_and_id_guard()
        test_unique_conflict()
        test_type_validation_rejects()
        test_fullwidth_numeric_accepted()
        test_batch_update()
        test_batch_delete()
        test_zero_hit_honest()
        test_delete_requires_selection()
    finally:
        _teardown()
    print("\n=== 层 19 全部通过：记录级 DML 行为已固化 ===")
