"""核武闸验收辅助：查看测试表状态 / 重建测试表 / 清理验收环境"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import settings

ACTION = sys.argv[1] if len(sys.argv) > 1 else "status"

# drop_table 会连带删除该 yaml——所以批准路径回归前必须用 setup 重建两者
TEST_YAML = (Path(__file__).resolve().parent.parent
             / "industries" / "engineering" / "schemas" / "test_nuke_1.yaml")
TEST_YAML_CONTENT = """name: test_nuke_1
business_name: 核武闸验收测试表
description: 临时表，仅用于核武人审闸端到端验收（批准路径），验收后即删
columns:
- name: id
  type: INTEGER
  pk: true
  not_null: true
  business_name: 主键
- name: name
  type: TEXT
  business_name: 名称
foreign_keys: []
"""

conn = sqlite3.connect(settings.SQLITE_DB_PATH)
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
print("TABLES:", tables)

for t in ("test_nuke_1", "quota_items"):
    if t in tables:
        c = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"  {t}: EXISTS, {c} rows")
    else:
        print(f"  {t}: NOT EXISTS")

if ACTION == "fix_index":
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_quota_items_quota_code ON quota_items(quota_code)")
    conn.commit()
    print("FIX DONE: idx_quota_items_quota_code created")
elif ACTION == "indexes":
    rows = conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index'").fetchall()
    for r in rows:
        print("IDX:", r[0], "on", r[1], "|", (r[2] or "(auto)")[:120])
elif ACTION == "setup":
    # 重建 test_nuke_1（3 行）+ schema yaml，供批准路径验收
    conn.execute("DROP TABLE IF EXISTS test_nuke_1")
    conn.execute("CREATE TABLE test_nuke_1 (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO test_nuke_1 (name) VALUES (?)",
                     [("alpha",), ("beta",), ("gamma",)])
    conn.commit()
    TEST_YAML.write_text(TEST_YAML_CONTENT, encoding="utf-8")
    print("SETUP DONE: test_nuke_1 recreated with 3 rows + schema yaml")
elif ACTION == "teardown":
    # 验收后清理：删表 + 删 yaml（yaml 残留会让 AI 误以为表存在）
    conn.execute("DROP TABLE IF EXISTS test_nuke_1")
    conn.commit()
    if TEST_YAML.exists():
        TEST_YAML.unlink()
    print("TEARDOWN DONE: test_nuke_1 table + yaml removed")
conn.close()
