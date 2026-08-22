"""创建第二个数据源——用于跨库 JOIN 测试

创建一个独立的 SQLite 数据库 (db/secondary_test.db)，
包含 price_history 表，通过 quota_id 与主库的 quota_item 关联
"""
import sqlite3
import os
from pathlib import Path

# 数据库路径
db_path = Path(__file__).parent / "db" / "secondary_test.db"

# 如果已存在则删除
if db_path.exists():
    db_path.unlink()

print(f"创建第二数据源: {db_path}")
conn = sqlite3.connect(str(db_path))
c = conn.cursor()

# 创建价格历史表（与主库的 quota_item 通过 quota_id 关联）
c.execute("""
    CREATE TABLE price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quota_id INTEGER NOT NULL,
        price_date TEXT NOT NULL,
        price REAL NOT NULL,
        price_type TEXT DEFAULT 'market',
        notes TEXT
    )
""")

# 创建供应商表（独立于主库）
c.execute("""
    CREATE TABLE supplier (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_name TEXT NOT NULL,
        contact_person TEXT,
        phone TEXT,
        region TEXT,
        rating INTEGER DEFAULT 3
    )
""")

# 创建索引（与 YAML schema 配置保持一致）
c.execute("CREATE INDEX IF NOT EXISTS idx_price_history_quota_id ON price_history(quota_id)")
c.execute("CREATE INDEX IF NOT EXISTS idx_supplier_region ON supplier(region)")
print("  已创建索引: idx_price_history_quota_id, idx_supplier_region")

# 插入测试数据——价格历史
# 先获取主库中 quota_item 的前几条 ID
main_db_path = Path(__file__).parent / "db" / "data_engine.db"
main_conn = sqlite3.connect(str(main_db_path))
main_c = main_conn.cursor()

try:
    main_c.execute("SELECT id, quota_id FROM quota_item LIMIT 10")
    quota_items = main_c.fetchall()
    print(f"  从主库获取 {len(quota_items)} 条 quota_item")

    if quota_items:
        # 为每个定额创建 2-3 条价格历史
        price_data = []
        for idx, (qid, quota_code) in enumerate(quota_items):
            base_price = 100 + idx * 50
            price_data.append((qid, "2024-01-15", base_price, "market", f"{quota_code} 市场价"))
            price_data.append((qid, "2024-06-20", base_price * 1.05, "market", f"{quota_code} 更新价"))
            price_data.append((qid, "2024-01-15", base_price * 0.95, "budget", f"{quota_code} 预算价"))

        c.executemany(
            "INSERT INTO price_history (quota_id, price_date, price, price_type, notes) VALUES (?, ?, ?, ?, ?)",
            price_data
        )
        print(f"  插入 {len(price_data)} 条 price_history 数据")
except Exception as e:
    print(f"  获取主库数据失败: {e}")
    # 使用假数据
    price_data = [
        (1, "2024-01-15", 100.0, "market", "测试1"),
        (1, "2024-06-20", 105.0, "market", "测试2"),
        (2, "2024-01-15", 200.0, "market", "测试3"),
    ]
    c.executemany(
        "INSERT INTO price_history (quota_id, price_date, price, price_type, notes) VALUES (?, ?, ?, ?, ?)",
        price_data
    )
    print(f"  插入 {len(price_data)} 条 price_history 数据（假数据）")
finally:
    main_conn.close()

# 插入供应商数据
suppliers = [
    ("北京建材有限公司", "张三", "13800138001", "北京", 5),
    ("上海钢铁集团", "李四", "13800138002", "上海", 4),
    ("广州水泥厂", "王五", "13800138003", "广州", 4),
    ("深圳混凝土公司", "赵六", "13800138004", "深圳", 3),
    ("成都建材市场", "钱七", "13800138005", "成都", 5),
]
c.executemany(
    "INSERT INTO supplier (supplier_name, contact_person, phone, region, rating) VALUES (?, ?, ?, ?, ?)",
    suppliers
)
print(f"  插入 {len(suppliers)} 条 supplier 数据")

conn.commit()

# 验证
c.execute("SELECT COUNT(*) FROM price_history")
print(f"  price_history 总行数: {c.fetchone()[0]}")
c.execute("SELECT COUNT(*) FROM supplier")
print(f"  supplier 总行数: {c.fetchone()[0]}")

conn.close()
print(f"\n第二数据源创建完成: {db_path}")
print(f"文件大小: {db_path.stat().st_size / 1024:.1f} KB")
