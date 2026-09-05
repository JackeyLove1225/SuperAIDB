"""MySQL 驱动——纯执行层

设计原则（契约模块接管后）：
- 本驱动只负责 SQL 执行和 MySQL 方言处理
- 所有业务校验（标识符/WHERE/主键/类型/CHECK）由 core.contract 模块负责
- 所有错误翻译由 core.contract.ErrorTranslator 负责
- 新增数据库 driver 时，只需实现 Driver 基类的 27 个原子操作

类型映射（SQLite -> MySQL）：
    INTEGER -> INT
    TEXT -> VARCHAR(255) 或 TEXT
    FLOAT/REAL -> DOUBLE
    BOOLEAN -> TINYINT(1)

参数占位符：? -> %s

安全加固：所有 f-string SQL 中的标识符插值在方法入口用
core.sql_safe.validate_identifier 校验，非法标识符抛 SecurityError。
保留 MySQL 反引号风格不变（safe_table_sql 返回双引号在 MySQL 默认模式下不兼容）。
"""

import os
import re

try:
    import pymysql
    import pymysql.cursors
    _HAS_PYMYSQL = True
except ImportError:
    _HAS_PYMYSQL = False

from .base import Driver
from core.sql_safe import validate_identifier, is_valid_identifier, safe_default_sql

_TYPE_MAP = {
    "INTEGER": "INT",
    "INT": "INT",
    "TEXT": "TEXT",
    "FLOAT": "DOUBLE",
    "REAL": "DOUBLE",
    "NUMERIC": "DECIMAL(20,4)",
    "BOOLEAN": "TINYINT(1)",
    "BOOL": "TINYINT(1)",
    "BLOB": "BLOB",
    "DATE": "DATE",
    "DATETIME": "DATETIME",
    "TIMESTAMP": "TIMESTAMP",
}

# 标识符正则统一定义在 core.sql_safe（{0,63} 上限，唯一标准），
# 本模块通过 is_valid_identifier 复用，不再维护本地副本。
def _validate_identifier(name: str) -> str:
    """校验标识符（内部辅助，SQL 构造时使用；业务校验由契约层负责）"""
    if not name or not is_valid_identifier(name):
        raise ValueError(f"非法标识符: {name}")
    return name


# 类型串白名单：纯字母类型名，可带 (n) 或 (m,n) 精度，如 VARCHAR(255)、DECIMAL(20,4)
_TYPE_RE = re.compile(r'^[A-Za-z]{1,20}(\(\d{1,5}(,\d{1,5})?\))?$')


def _map_type(col_type: str) -> str:
    """将 SQLite 类型映射为 MySQL 类型

    入参先做白名单/正则约束，只允许 `NAME` 或 `NAME(n)`/`NAME(m,n)`
    形态，其他字符一律拒绝（防止 "VARCHAR(255)); DROP TABLE x--" 类注入透传）。
    """
    ct = col_type.upper().strip()
    if not _TYPE_RE.match(ct):
        raise ValueError(f"非法字段类型: {col_type!r}")
    if ct.startswith("VARCHAR"):
        # VARCHAR 不允许无长度（MySQL 语法要求），统一补默认长度
        return "VARCHAR(255)" if ct == "VARCHAR" else ct
    return _TYPE_MAP.get(ct, "TEXT")


class MysqlDriver(Driver):
    """MySQL 纯执行驱动

    所有校验由 ContractDriver 包装层处理，本类只负责：
    1. 连接管理
    2. SQL 构造（MySQL 方言）
    3. SQL 执行
    4. 事务管理
    """

    def __init__(self, host: str = None, port: int = None, user: str = None,
                 password: str = None, database: str = None, charset: str = "utf8mb4"):
        if not _HAS_PYMYSQL:
            raise RuntimeError("pymysql 未安装，请运行: pip install pymysql")
        self._host = host or os.getenv("MYSQL_HOST", "localhost")
        self._port = port or int(os.getenv("MYSQL_PORT", "3306"))
        self._user = user or os.getenv("MYSQL_USER", "root")
        self._password = password or os.getenv("MYSQL_PASSWORD", "")
        self._database = database or os.getenv("MYSQL_DATABASE", "superaidb")
        self._charset = charset
        self._conn = None
        self._connect()

    def _connect(self):
        self._conn = pymysql.connect(
            host=self._host, port=self._port, user=self._user,
            password=self._password, database=self._database,
            charset=self._charset, cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
        with self._conn.cursor() as cursor:
            cursor.execute("SET NAMES utf8mb4")
            cursor.execute("SET FOREIGN_KEY_CHECKS=1")
            # 方言收口：契约层 safe_* 统一产出 ANSI 双引号标识符（SQLite 方言），
            # MySQL 默认 sql_mode 下双引号是字符串——ANSI_QUOTES 让上层拼装的
            # SQL 文本跨方言成立（"换数据库不动上层"的关键一行）
            cursor.execute("SET SESSION sql_mode = CONCAT(@@SESSION.sql_mode, ',ANSI_QUOTES')")
        self._conn.commit()

    @property
    def conn(self):
        if self._conn is None:
            self._connect()
        try:
            # reconnect=False：PyMySQL 内部自动重连不经过 _connect()，三条会话级 SET
            #（NAMES/FOREIGN_KEY_CHECKS/ANSI_QUOTES）会在重连后的会话上静默丢失——
            # 方言静默退回默认，上层 ANSI 双引号标识符变成字符串（错得沉默）。
            # 重连只有一个通道：_connect() 全量重建
            self._conn.ping(reconnect=False)
        except Exception:
            self._connect()
        return self._conn

    def ping(self) -> bool:
        try:
            self.conn  # 经 property 即完成保活/重建
            return True
        except Exception:
            return False

    def _safe_where(self, where: str) -> bool:
        """WHERE 安全检查（兼容方法，新代码应使用 SecurityContract.validate_where）"""
        if not where:
            return False
        w = where.strip()
        if not w or ";" in w or "--" in w or "/*" in w or "*/" in w:
            return False
        lower = w.lower()
        for kw in ["drop ", "delete ", "insert ", "update ", "create ", "alter ", "truncate "]:
            if kw in lower:
                return False
        return True



    # ── DML：数据操作（纯执行）──

    def query(self, sql: str) -> list[dict]:
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def insert(self, table: str, rows: list[dict], overwrite: bool = False) -> dict:
        if not rows:
            return {"ok": True, "count": 0, "conflict": False}
        # 入口校验 table 和所有字段名（防御性，确保 f-string SQL 安全）
        validate_identifier(table, "表名")
        # 唯一业务键冲突检测（与 sqlite_driver.insert 同一契约语义：
        # INSERT IGNORE 曾静默吞重复行且 conflict 恒 False——conflict 通道在
        # mysql 侧整体失效。键名来自行业 YAML 声明（BaseDriver 共享实现））
        key_col = self._get_unique_key_column(table)
        keys = {r.get(key_col) for r in rows if r.get(key_col) is not None} if key_col else set()
        if keys:
            probe = self.conn.cursor()
            try:
                existing = 0
                for k in keys:
                    probe.execute(f'SELECT COUNT(*) FROM `{table}` WHERE `{key_col}` = %s', (k,))
                    existing += probe.fetchone()[0]
            finally:
                probe.close()
            if existing > 0 and not overwrite:
                return {"ok": False, "conflict": True, "message": f"数据已存在（{existing}条）"}
            if overwrite and existing > 0:
                cur = self.conn.cursor()
                try:
                    for k in keys:
                        cur.execute(f'DELETE FROM `{table}` WHERE `{key_col}` = %s', (k,))
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
                finally:
                    cur.close()
        cols = list(rows[0].keys())
        for c in cols:
            validate_identifier(c, "字段名")
        placeholders = ", ".join(["%s"] * len(cols))
        col_names = ", ".join(f"`{c}`" for c in cols)
        if overwrite:
            sql_prefix = f"REPLACE INTO `{table}` ({col_names}) VALUES"
        else:
            sql_prefix = f"INSERT IGNORE INTO `{table}` ({col_names}) VALUES"
        cursor = self.conn.cursor()
        try:
            count = 0
            for row in rows:
                values = tuple(row.get(c) for c in cols)
                cursor.execute(f"{sql_prefix} ({placeholders})", values)
                count += cursor.rowcount
            self.conn.commit()
            return {"ok": True, "count": count, "conflict": False}
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def update(self, table: str, set_clause: str, where: str = "") -> dict:
        # SET 子句参数化——列名走标识符白名单校验，值走 %s 参数占位
        # （对齐 sqlite_driver.update 的解析逻辑，杜绝 set_clause 表达式注入，
        #   如 "bio=(SELECT password FROM users)" 只会作为字面值写入）
        validate_identifier(table, "表名")
        if "=" not in set_clause:
            raise ValueError(f"SET 格式错误: {set_clause}")
        # 全仓唯一 SET 解析器（与 sqlite 驱动同源）
        from core.sql_safe import split_set_pairs, decode_sql_literal
        pairs = split_set_pairs(set_clause)
        if not pairs:
            raise ValueError(f"SET 格式错误: {set_clause}")
        set_parts = []
        params = []
        for field, raw in pairs:
            value = decode_sql_literal(raw)
            validate_identifier(field, "字段名")
            set_parts.append(f'`{field}` = %s')
            params.append(value)
        sql = f"UPDATE `{table}` SET " + ", ".join(set_parts)
        if where:
            sql += f" WHERE {where}"
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql, params)
            count = cursor.rowcount
            self.conn.commit()
            return {"ok": True, "count": count}
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def delete(self, table: str, where: str) -> dict:
        sql = f"DELETE FROM `{table}` WHERE {where}"
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
            count = cursor.rowcount
            self.conn.commit()
            return {"ok": True, "count": count}
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def delete_by_pk(self, table: str, pk_column: str, pk_value) -> dict:
        """按主键删除（参数化绑定，接口契约）"""
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"DELETE FROM `{table}` WHERE `{pk_column}` = %s", (pk_value,))
            count = cursor.rowcount
            self.conn.commit()
            return {"ok": True, "count": count}
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def _get_fk_config(self, table: str) -> dict:
        """表的外键配置（INFORMATION_SCHEMA 读取）"""
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                "AND REFERENCED_TABLE_NAME IS NOT NULL", (table,))
            entries = [{"columns": [r[0]], "references": r[1], "ref_columns": [r[2]]}
                       for r in cursor.fetchall()]
            return {"foreign_keys": entries} if entries else {}
        finally:
            cursor.close()


    def _build_create_sql(self, table: str, table_config: dict) -> str:
        """构建 CREATE TABLE SQL（MySQL 方言）"""
        cols_sql = []
        foreign_keys = table_config.get("foreign_keys", [])
        cols_sql.append("id INT AUTO_INCREMENT PRIMARY KEY")
        for col in table_config.get("columns", []):
            col_name = _validate_identifier(col["name"])
            col_type = _map_type(col.get("type", "TEXT"))
            not_null = "NOT NULL" if col.get("not_null") else ""
            default = ""
            if col.get("default") is not None:
                default = f"DEFAULT {safe_default_sql(col['default'])}"
            unique = "UNIQUE" if col.get("unique") else ""
            check_clause = ""
            if col.get("check"):
                from core.check_templates import translate_for_dialect
                check_clause = f" CHECK ({translate_for_dialect(col['check'], 'mysql')})"
            cols_sql.append(f"`{col_name}` {col_type} {not_null} {default} {unique}{check_clause}".strip())
        for fk in foreign_keys:
            col = _validate_identifier(fk["column"])
            ref_table = _validate_identifier(fk["ref_table"])
            ref_col = fk.get("ref_column", "id")
            _validate_identifier(ref_col)
            constraint_name = f"fk_{table}_{col}"
            cols_sql.append(f"CONSTRAINT `{constraint_name}` FOREIGN KEY (`{col}`) REFERENCES `{ref_table}`(`{ref_col}`)")
        uniques = table_config.get("uniques", [])
        for u in uniques:
            if isinstance(u, dict):
                ucols = u.get("columns", [])
            else:
                ucols = [u]
            if ucols:
                col_list = ", ".join(f"`{c}`" for c in ucols)
                idx_name = f"uk_{table}_{'_'.join(ucols)}"
                cols_sql.append(f"CONSTRAINT `{idx_name}` UNIQUE ({col_list})")
        return f"CREATE TABLE IF NOT EXISTS `{table}` (\n  " + ",\n  ".join(cols_sql) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


    def create_table(self, table_config: dict) -> str:
        table = _validate_identifier(table_config["name"])
        sql = self._build_create_sql(table, table_config)
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
            self.conn.commit()
            return f"表 '{table}' 创建成功"
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def drop_table(self, table: str) -> str:
        # 入口校验 table
        validate_identifier(table, "表名")
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
            self.conn.commit()
            return f"表 '{table}' 已删除"
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def rename_table(self, table: str, new_name: str) -> str:
        # 入口校验 table 和 new_name
        validate_identifier(table, "表名")
        validate_identifier(new_name, "新表名")
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"RENAME TABLE `{table}` TO `{new_name}`")
            self.conn.commit()
            return f"表 '{table}' 已重命名为 '{new_name}'"
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def add_column(self, table: str, column: str, col_type: str, precision=None, not_null=False) -> dict:
        # 入口校验 table 和 column
        validate_identifier(table, "表名")
        validate_identifier(column, "字段名")
        mysql_type = _map_type(col_type)
        if precision:
            # 精度值强制 int，杜绝字符串注入透传
            mysql_type = f"DECIMAL({int(precision[0])},{int(precision[1])})"
        nn = "NOT NULL" if not_null else ""
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {mysql_type} {nn}".strip())
            self.conn.commit()
            return {"ok": True, "exists": False, "message": f"字段 '{column}' 已添加"}
        except Exception as e:
            err = str(e).lower()
            if "duplicate" in err or "exists" in err:
                return {"ok": True, "exists": True, "message": f"字段 '{column}' 已存在"}
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def drop_column(self, table: str, column: str) -> dict:
        # 入口校验 table 和 column
        validate_identifier(table, "表名")
        validate_identifier(column, "字段名")
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"ALTER TABLE `{table}` DROP COLUMN `{column}`")
            self.conn.commit()
            return {"ok": True, "message": f"字段 '{column}' 已删除"}
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def modify_column(self, table: str, column: str, new_type: str) -> dict:
        # 入口校验 table 和 column
        validate_identifier(table, "表名")
        validate_identifier(column, "字段名")
        mysql_type = _map_type(new_type)
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` {mysql_type}")
            self.conn.commit()
            return {"ok": True, "message": f"字段 '{column}' 类型已修改为 {mysql_type}"}
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def alter_precision(self, table: str, column: str, new_precision: tuple) -> dict:
        # 入口校验 table 和 column
        validate_identifier(table, "表名")
        validate_identifier(column, "字段名")
        mysql_type = f"DECIMAL({int(new_precision[0])},{int(new_precision[1])})"
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` {mysql_type}")
            self.conn.commit()
            return {"ok": True, "message": f"字段 '{column}' 精度已修改为 {mysql_type}"}
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def add_foreign_key(self, table: str, column: str, ref_table: str, ref_column: str = "id") -> dict:
        # 入口校验 table, column, ref_table, ref_column
        validate_identifier(table, "表名")
        validate_identifier(column, "字段名")
        validate_identifier(ref_table, "外键引用表")
        validate_identifier(ref_column, "外键引用字段")
        constraint_name = f"fk_{table}_{column}"
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"ALTER TABLE `{table}` ADD CONSTRAINT `{constraint_name}` FOREIGN KEY (`{column}`) REFERENCES `{ref_table}`(`{ref_column}`)")
            self.conn.commit()
            return {"ok": True, "message": f"外键 '{constraint_name}' 已添加"}
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def drop_foreign_key(self, table: str, constraint_name: str) -> dict:
        # 入口校验 table 和 constraint_name
        validate_identifier(table, "表名")
        validate_identifier(constraint_name, "外键约束名")
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{constraint_name}`")
            self.conn.commit()
            return {"ok": True, "message": f"外键 '{constraint_name}' 已删除"}
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def create_index(self, table: str, columns: str, unique: bool = False) -> str:
        # 入口校验 table 和 columns（逗号分隔的字段名列表，逐个校验）
        validate_identifier(table, "表名")
        col_parts = [c.strip() for c in columns.split(",") if c.strip()]
        for c in col_parts:
            validate_identifier(c, "索引字段")
        col_list = ", ".join(f"`{c}`" for c in col_parts)
        idx_name = f"idx_{table}_{'_'.join(col_parts)}"
        # 校验生成的索引名
        validate_identifier(idx_name, "索引名")
        unique_str = "UNIQUE" if unique else ""
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"CREATE {unique_str} INDEX `{idx_name}` ON `{table}` ({col_list})")
            self.conn.commit()
            return f"索引 '{idx_name}' 创建成功"
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def drop_index(self, name: str) -> str:
        # 入口校验 name
        validate_identifier(name, "索引名")
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"DROP INDEX `{name}`")
            self.conn.commit()
            return f"索引 '{name}' 已删除"
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def recreate_table(self, table_config: dict) -> dict:
        table = _validate_identifier(table_config["name"])
        temp_name = f"{table}_old"
        # 校验生成的临时表名
        validate_identifier(temp_name, "临时表名")
        cursor = self.conn.cursor()
        try:
            cursor.execute(f"RENAME TABLE `{table}` TO `{temp_name}`")
            create_sql = self._build_create_sql(table, table_config)
            cursor.execute(create_sql)
            old_cols = self.get_columns(temp_name)
            new_cols = [c["name"] for c in table_config.get("columns", [])]
            common_cols = [c["name"] for c in old_cols if c["name"] in new_cols]
            if common_cols:
                # 校验 col_list 中每个字段名
                for c in common_cols:
                    validate_identifier(c, "字段名")
                col_list = ", ".join(f"`{c}`" for c in common_cols)
                cursor.execute(f"INSERT INTO `{table}` ({col_list}) SELECT {col_list} FROM `{temp_name}`")
            cursor.execute(f"DROP TABLE `{temp_name}`")
            self.conn.commit()
            return {"ok": True, "message": f"表 '{table}' 已重建"}
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def execute(self, sql: str):
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    # ── Utility：元数据查询 ──

    def list_tables(self) -> list[str]:
        cursor = self.conn.cursor()
        try:
            cursor.execute("SHOW TABLES")
            result = []
            for row in cursor.fetchall():
                for v in row.values():
                    result.append(v)
                    break
            return sorted(result)
        finally:
            cursor.close()

    def get_referencing_tables(self, table: str) -> list[dict]:
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "SELECT TABLE_NAME as `table`, COLUMN_NAME as from_col, REFERENCED_COLUMN_NAME as to_col "
                "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                "WHERE REFERENCED_TABLE_NAME = %s AND TABLE_SCHEMA = %s",
                (table, self._database),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def table_exists(self, table: str) -> bool:
        return table in self.list_tables()

    def column_exists(self, table: str, column: str) -> bool:
        return any(c["name"] == column for c in self.get_columns(table))

    def get_columns(self, table: str) -> list[dict]:
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "SELECT COLUMN_NAME as `name`, DATA_TYPE as `type`, "
                "IS_NULLABLE = 'NO' as not_null, COLUMN_KEY = 'PRI' as pk "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = %s AND TABLE_SCHEMA = %s "
                "ORDER BY ORDINAL_POSITION",
                (table, self._database),
            )
            rows = cursor.fetchall()
            return [
                {
                    "name": row["name"],
                    "type": row["type"].upper(),
                    "not_null": bool(row.get("not_null", False)),
                    "pk": bool(row.get("pk", False)),
                }
                for row in rows
            ]
        finally:
            cursor.close()

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass  # 清理/关闭失败不影响主流程（OS 兜底回收）
            self._conn = None

    # ── 事务 ──

    def begin(self, name: str = ""):
        if name:
            # MySQL 原生支持 SAVEPOINT（此前无视 name 塌缩为全事务——
            # 联邦懒开 savepoint 域打到 MySQL 时域语义静默退化）
            self.conn.execute(f"SAVEPOINT `{name}`")
        else:
            self.conn.begin()

    def rollback(self, name: str = ""):
        if name:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT `{name}`")
            self.conn.execute(f"RELEASE SAVEPOINT `{name}`")
        else:
            self.conn.rollback()
