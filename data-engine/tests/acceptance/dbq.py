# 验收测试 DB 执行器：python dbq.py <sql>（自动提交，避免引号嵌套地狱）
# 路径一律读 settings（不写死个人机器绝对路径——避免个人痕迹漏进公开仓）
import sys
from config.settings import settings
from core.crypto.connection import open_db

conn = open_db(settings.SQLITE_DB_PATH)
cur = conn.cursor()
sql = sys.argv[1]
rows = cur.execute(sql).fetchall()
conn.commit()
conn.close()
print(rows)
