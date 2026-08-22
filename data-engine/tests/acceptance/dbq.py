# 验收测试 DB 执行器：python dbq.py <sql>（自动提交，避免引号嵌套地狱）
import sqlite3
import sys
from core.crypto.connection import open_db

conn = open_db(r"D:\AIprojects\SuperAIOffice\data-engine\db\data_engine.db")
cur = conn.cursor()
sql = sys.argv[1]
rows = cur.execute(sql).fetchall()
conn.commit()
conn.close()
print(rows)
