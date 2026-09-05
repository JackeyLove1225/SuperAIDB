"""SQLite 驱动——纯执行层

设计原则（契约模块接管后）：
- 本驱动只负责 SQL 执行和 SQLite 方言处理
- 所有业务校验（标识符/WHERE/主键/类型/CHECK）由 core.contract 模块负责
- 所有错误翻译由 core.contract.ErrorTranslator 负责
- 新增数据库 driver 时，只需实现 Driver 基类的 27 个原子操作

保留的兼容方法（供 FederatedDriver 等旧代码调用）：
- _safe_where(where) -> bool：委托 SecurityContract
- （原 _translate_error 透传空壳已删，错误翻译统一契约层 ErrorTranslator）

安全加固：所有 f-string SQL 中的标识符插值统一使用
core.sql_safe 中的 safe_table_sql/safe_column_sql/
safe_index_sql/safe_pragma_arg/safe_savepoint_name 进行校验。
"""
import re
import sqlite3
from core.crypto.connection import open_db
import threading
from pathlib import Path

from core.sql_safe import (
    safe_table_sql, safe_column_sql, safe_index_sql, safe_default_sql,
    safe_pragma_arg, safe_savepoint_name, validate_identifier,
)
from .base import Driver


# 类型串白名单：纯字母类型名，可带 (n) 或 (m,n) 精度，如 VARCHAR(255)、DECIMAL(20,4)
# 纵深防御：上层 schema_manager._normalize_type 已有白名单兜底，
# 驱动层独立再校验一次，拒绝任何其他字符（防 ALTER TABLE 类型串注入）。
_TYPE_RE = re.compile(r'^[A-Za-z]{1,20}(\(\d{1,5}(,\d{1,5})?\))?$')


def _validate_col_type(col_type: str) -> str:
    """校验 ALTER TABLE 用的类型串，非法即抛 ValueError"""
    ct = (col_type or "").strip()
    if not _TYPE_RE.match(ct):
        raise ValueError(f"非法字段类型: {col_type!r}")
    return ct


class SqliteDriver(Driver):
    """SQLite 纯执行驱动

    所有校验由 ContractDriver 包装层处理，本类只负责：
    1. 连接管理
    2. SQL 构造（SQLite 方言）
    3. SQL 执行
    4. 事务管理
    """

    def __init__(self, db_path: str = "./db/data_engine.db"):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conns: dict[int, sqlite3.Connection] = {}

    @property
    def conn(self):
        tid = threading.get_ident()
        # 死线程连接清扫：线程死了而连接未清，ident 复用后新线程
        # 会拿到死线程创建的连接（check_same_thread → ProgrammingError）
        if any(k != tid for k in self._conns):
            live = {t.ident for t in threading.enumerate()}
            for old_tid, old_conn in list(self._conns.items()):
                if old_tid != tid and old_tid not in live:
                    try:
                        old_conn.close()
                    except Exception:
                        pass  # 死连接关闭失败无碍——OS 进程退出时回收
                    self._conns.pop(old_tid, None)
        if tid not in self._conns:
            self._conns[tid] = open_db(self._db_path)
            self._conns[tid].execute("PRAGMA journal_mode=WAL")
            self._conns[tid].execute("PRAGMA foreign_keys=ON")
        return self._conns[tid]

    def connect(self):
        return self.conn

    def _safe_where(self, where: str) -> bool:
        if not where:
            return False
        try:
            from core.checks import validate_where
            return validate_where(where)
        except Exception:
            return False


    def _get_fk_config(self, table: str) -> dict:
        refs = self.conn.execute(f"PRAGMA foreign_key_list({safe_pragma_arg(table)})").fetchall()
        if refs:
            entries = []
            for row in refs:
                if len(row) >= 5:
                    entries.append({"columns": [row[3]], "references": row[2], "ref_columns": [row[4]]})
            return {"foreign_keys": entries}
        return {}

    def query(self, sql: str) -> list[dict]:
        cur = self.conn.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def insert(self, table: str, rows: list[dict], overwrite: bool = False) -> dict:
        if not rows:
            return {"ok": True, "count": 0, "conflict": False}  # 键集合与 sqlite 侧一致
        # 入口校验 table（防御性，确保 f-string SQL 安全）
        validate_identifier(table, "表名")
        # 唯一业务键冲突检测（通用机制，字段名来自行业 YAML 声明）
        key_col = self._get_unique_key_column(table)
        keys = {r.get(key_col) for r in rows if r.get(key_col) is not None} if key_col else set()
        if keys:
            existing = 0
            for k in keys:
                existing += self.conn.execute(
                    f'SELECT COUNT(*) FROM {safe_table_sql(table)} WHERE {safe_column_sql(key_col)} = ?', (k,)
                ).fetchone()[0]
            if existing > 0 and not overwrite:
                return {"ok": False, "conflict": True, "message": f"数据已存在（{existing}条）"}
            if overwrite and existing > 0:
                for k in keys:
                    self.conn.execute(
                        f'DELETE FROM {safe_table_sql(table)} WHERE {safe_column_sql(key_col)} = ?', (k,))
        cols = list(rows[0].keys())
        cols_no_id = [c for c in cols if c.lower() != "id"]
        # 校验所有字段名
        for c in cols_no_id:
            validate_identifier(c, "字段名")
        vals = [[r.get(c) for c in cols_no_id] for r in rows]
        placeholders = ", ".join(["?" for _ in cols_no_id])
        col_names = ", ".join(safe_column_sql(c) for c in cols_no_id)
        sql = f'INSERT INTO {safe_table_sql(table)} ({col_names}) VALUES ({placeholders})'
        self.conn.executemany(sql, vals)
        return {"ok": True, "count": len(rows), "conflict": False}

    def update(self, table: str, set_clause: str, where: str = "") -> dict:
        # 入口校验 table
        validate_identifier(table, "表名")
        if "=" not in set_clause:
            raise ValueError(f"SET 格式错误: {set_clause}")
        # 全仓唯一 SET 解析器（split_set_pairs/dec​​ode_sql_literal，
        # 单双引号+doubling 全感知）——权限判定与执行同源
        from core.sql_safe import split_set_pairs, decode_sql_literal
        pairs = split_set_pairs(set_clause)
        if not pairs:
            raise ValueError(f"SET 格式错误: {set_clause}")
        set_parts = []
        params = []
        for field, raw in pairs:
            value = decode_sql_literal(raw)
            # 校验 SET 子句中的字段名
            validate_identifier(field, "字段名")
            set_parts.append(f'{safe_column_sql(field)} = ?')
            params.append(value)
        sql = f'UPDATE {safe_table_sql(table)} SET ' + ', '.join(set_parts)
        if where:
            sql += f" WHERE {where}"
        cur = self.conn.execute(sql, params)
        return {"ok": True, "count": cur.rowcount}

    def delete(self, table: str, where: str) -> dict:
        # 入口校验 table
        validate_identifier(table, "表名")
        sql = f'DELETE FROM {safe_table_sql(table)}'
        if where:
            sql += f" WHERE {where}"
        cur = self.conn.execute(sql)
        return {"ok": True, "count": cur.rowcount}

    def delete_by_pk(self, table: str, pk_column: str, pk_value) -> dict:
        """按主键删除——参数化绑定，调用方（前端/工具）不再手拼 WHERE"""
        validate_identifier(table, "表名")
        validate_identifier(pk_column, "主键列名")
        cur = self.conn.execute(
            f'DELETE FROM {safe_table_sql(table)} WHERE {safe_column_sql(pk_column)} = ?',
            (pk_value,))
        return {"ok": True, "count": cur.rowcount}


    def add_column(self, table: str, column: str, col_type: str, precision=None, not_null=False) -> dict:
        # 驱动层独立校验类型串（纵深防御，不依赖上层白名单兜底）
        col_type = _validate_col_type(col_type)
        nn = " NOT NULL" if not_null else ""
        try:
            self.conn.execute(f'ALTER TABLE {safe_table_sql(table)} ADD COLUMN {safe_column_sql(column)} {col_type}{nn}')
            self.conn.commit()
            return {"ok": True, "exists": False, "message": f"已加入新字段：{column} ({col_type})"}
            # exists 键与契约/ mysql 侧一致（曾漏键造成返回形状漂移）
        except Exception as e:
            if not_null and "NOT NULL" in str(e).upper():
                self.conn.rollback()
                raise RuntimeError(
                    f"表 {table} 中已有数据，无法直接添加非空列 {column}。"
                    f"请先添加允许为空的列，手动填入有效数据后，再将该列设为非空"
                ) from e
            raise

    def drop_column(self, table: str, column: str) -> dict:
        self.conn.execute(f'ALTER TABLE {safe_table_sql(table)} DROP COLUMN {safe_column_sql(column)}')
        self.conn.commit()
        return {"ok": True, "message": f"已删除字段 {column}"}

    def modify_column(self, table: str, column: str, new_type: str) -> dict:
        map_sqlite = {"VARCHAR": "TEXT", "INTEGER": "INTEGER", "FLOAT": "REAL", "REAL": "REAL", "TEXT": "TEXT"}
        nt = map_sqlite.get(new_type.upper(), "TEXT")
        cols = self.get_columns(table)
        for col in cols:
            if col["name"].lower() == column.lower():
                col["type"] = nt
                break
        config = {"name": table, "columns": cols}
        config.update(self._get_fk_config(table))
        return self.recreate_table(config)


    def _build_create_sql(self, table: str, table_config: dict) -> str:
        """从配置生成 CREATE TABLE SQL（SQLite 方言）"""
        TYPE_MAP = {"VARCHAR": "TEXT", "INTEGER": "INTEGER", "INT": "INTEGER", "FLOAT": "REAL", "REAL": "REAL", "TEXT": "TEXT", "SERIAL": "INTEGER"}
        # 入口校验 table
        validate_identifier(table, "表名")
        col_defs = []
        pk_fields = []
        has_composite = len([c for c in table_config.get("columns", []) if c.get("is_pk")]) > 1
        for _fk in table_config.get("foreign_keys", []):
            if _fk.get("references") and _fk.get("ref_columns"):
                try:
                    # 校验外键引用表名
                    _ri = self.conn.execute(f"PRAGMA table_info({safe_pragma_arg(_fk['references'])})").fetchall()
                    for _rc in _fk.get("ref_columns", []):
                        validate_identifier(_rc, "外键引用字段")
                        for _r in _ri:
                            if _r[1].lower() == _rc.lower():
                                for _c2 in table_config.get("columns", []):
                                    if _c2["name"].lower() in [x.lower() for x in _fk.get("columns", [])]:
                                        _c2["type"] = _r[2]
                                        _c2.pop("precision", None)
                                break
                except Exception:
                    pass  # 外键引用类型回填失败按无精度信息处理（尽力而为）
        for col in table_config.get("columns", []):
            if col.get("is_pk"): col["pk"] = True
            # 校验字段名
            validate_identifier(col["name"], "字段名")
            raw_type = str(col["type"]).upper().split("(")[0].strip()
            ct = TYPE_MAP.get(raw_type, "TEXT")
            if col.get("precision"):
                prec_str = ",".join(str(p) for p in col["precision"])
                ct = f"{ct}({prec_str})"
            constraints = []
            if col.get("pk") or col.get("pk"):
                if has_composite and col["name"].lower() == "id":
                    pk_fields.append(safe_column_sql(col["name"]))
                    ct_upper = ct.upper().split("(")[0].strip()
                    if ct_upper in ("INTEGER", "INT", "BIGINT", "SMALLINT"):
                        constraints.append("PRIMARY KEY AUTOINCREMENT")
                elif has_composite and col["name"].lower() != "id":
                    constraints.append("NOT NULL")
                    constraints.append("UNIQUE")
                else:
                    pk_fields.append(safe_column_sql(col["name"]))
                    ct_upper = ct.upper().split("(")[0].strip()
                    if ct_upper in ("INTEGER", "INT", "BIGINT", "SMALLINT"):
                        constraints.append("PRIMARY KEY AUTOINCREMENT")
            if col.get("not_null"): constraints.append("NOT NULL")
            if col.get("unique"): constraints.append("UNIQUE")
            if col.get("check"):
                from core.check_templates import translate_for_dialect
                constraints.append(f"CHECK ({translate_for_dialect(col['check'], 'sqlite')})")
            if col.get("default") is not None: constraints.append(f"DEFAULT {safe_default_sql(col['default'])}")
            col_defs.append(f'  {safe_column_sql(col["name"])} {ct} {" ".join(constraints)}'.rstrip())
        if pk_fields and not has_composite:
            only_int_pk = (
                len(pk_fields) == 1 and
                not has_composite and
                any((c.get("is_pk") or c.get("pk")) and c["name"].lower() == pk_fields[0].strip('"').lower() and c["type"].upper() in ("INTEGER","INT","BIGINT","SMALLINT") for c in table_config.get("columns", []))
            )
            if not only_int_pk:
                col_defs.append(f"  PRIMARY KEY ({', '.join(pk_fields)})")
        for cu in table_config.get("compound_uniques", []):
            for c in cu:
                validate_identifier(c, "复合唯一字段")
            cols = ", ".join(safe_column_sql(c) for c in cu)
            col_defs.append(f"  UNIQUE ({cols})")
        for fk in table_config.get("foreign_keys", []):
            ref_table = fk.get("references", "")
            ref_fk_cols = fk.get("ref_columns", ["id"])
            if ref_table and ref_fk_cols:
                try:
                    # 校验引用表名
                    ref_info = self.conn.execute(f"PRAGMA table_info({safe_pragma_arg(ref_table)})").fetchall()
                    for fkc in fk.get("columns", []):
                        validate_identifier(fkc, "外键字段")
                        for ri in ref_info:
                            if ri[1].lower() == ref_fk_cols[0].lower():
                                for col in table_config.get("columns", []):
                                    if col["name"].lower() == fkc.lower():
                                        col["type"] = ri[2]
                                        col.pop("precision", None)
                                        break
                                break
                except Exception:
                    pass  # 外键引用类型回填失败按无精度信息处理（尽力而为）
            for c in fk.get("columns", []):
                validate_identifier(c, "外键字段")
            for c in fk.get("ref_columns", ["id"]):
                validate_identifier(c, "外键引用字段")
            cols = ", ".join(safe_column_sql(c) for c in fk.get("columns", []))
            ref_cols = ", ".join(safe_column_sql(c) for c in fk.get("ref_columns", ["id"]))
            if cols and ref_table and ref_cols:
                col_defs.append(f'  FOREIGN KEY ({cols}) REFERENCES {safe_table_sql(ref_table)} ({ref_cols}) ON UPDATE CASCADE ON DELETE RESTRICT')
        return f'CREATE TABLE IF NOT EXISTS {safe_table_sql(table)} (\n' + ",\n".join(col_defs) + "\n);"


    def create_table(self, table_config: dict) -> str:
        table = table_config["name"]
        sql = self._build_create_sql(table, table_config)
        self.conn.execute(sql)
        self.conn.commit()
        return f"表 {table} 已创建"

    def alter_precision(self, table: str, column: str, new_precision: tuple) -> dict:
        cols = [c for c in self.get_columns(table)]
        for col in cols:
            if col["name"].lower() == column.lower():
                col["precision"] = new_precision
                break
        config = {"name": table, "columns": cols}
        config.update(self._get_fk_config(table))
        return self.recreate_table(config)

    def rename_table(self, table: str, new_name: str) -> str:
        refs_data = []
        for r in self.get_referencing_tables(table):
            ref_name = r["table"]
            cols = self.get_columns(ref_name)
            refs_data.append({"name": ref_name, "columns": cols, "fk": r.get("from_col", "")})
        self.conn.execute(f'ALTER TABLE {safe_table_sql(table)} RENAME TO {safe_table_sql(new_name)}')
        for rd in refs_data:
            config = {"name": rd["name"], "columns": rd["columns"],
                      "references": new_name, "fk": rd["fk"]}
            self.recreate_table(config)
        self.conn.commit()
        return f"已重命名 {table} -> {new_name}"  # 接口契约 -> str（曾返回 dict 与声明漂移）

    def create_index(self, table: str, columns: str, unique: bool = False) -> str:
        # 校验 table 和 columns（columns 是逗号分隔的字段名列表）
        validate_identifier(table, "表名")
        col_list = [c.strip() for c in columns.split(",") if c.strip()]
        for c in col_list:
            validate_identifier(c, "索引字段")
        name = f"idx_{table}_{columns.replace(',','_').replace(' ','')}"
        validate_identifier(name, "索引名")
        unique_clause = "UNIQUE " if unique else ""
        cols_sql = ", ".join(safe_column_sql(c) for c in col_list)
        self.conn.execute(f'CREATE {unique_clause}INDEX IF NOT EXISTS {safe_index_sql(name)} ON {safe_table_sql(table)} ({cols_sql})')
        self.conn.commit()
        return f"已创建索引 {name}"  # 接口契约 -> str（曾返回 dict 与声明漂移，与 rename_table 同型）

    def drop_index(self, name: str) -> str:
        self.conn.execute(f'DROP INDEX IF EXISTS {safe_index_sql(name)}')
        self.conn.commit()
        return f"已删除索引 {name}"  # 接口契约 -> str（同上）

    def execute(self, sql: str):
        self.conn.execute(sql)

    def add_foreign_key(self, table: str, column: str, ref_table: str, ref_column: str = "id") -> dict:
        cols = self.get_columns(table)
        if not any(c['name'].lower() == column.lower() for c in cols):
            cols.append({'name': column, 'type': 'INTEGER'})
        ref_type = 'INTEGER'
        # 校验引用表名和字段名
        validate_identifier(ref_table, "外键引用表")
        validate_identifier(ref_column, "外键引用字段")
        for r in self.conn.execute(f"PRAGMA table_info({safe_pragma_arg(ref_table)})").fetchall():
            if r[1].lower() == ref_column.lower() and r[5] > 0:
                ref_type = r[2]
                break
        for c in cols:
            if c['name'].lower() == column.lower():
                c['type'] = ref_type
                break
        return self.recreate_table({'name': table, 'columns': cols,
            'foreign_keys': [{'columns': [column], 'references': ref_table, 'ref_columns': [ref_column]}]})

    def drop_foreign_key(self, table: str, constraint_name: str) -> dict:
        fk_config = self._get_fk_config(table)
        all_fks = fk_config.get("foreign_keys", [])
        keep_fks = [fk for fk in all_fks
                    if constraint_name.lower() not in [c.lower() for c in fk.get("columns", [])]]
        table_config = {"name": table, "columns": self.get_columns(table), "foreign_keys": keep_fks}
        return self.recreate_table(table_config)

    def list_tables(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]

    def get_referencing_tables(self, table: str) -> list[dict]:
        refs = []
        for t in self.list_tables():
            fks = self.conn.execute(f"PRAGMA foreign_key_list({safe_pragma_arg(t)})").fetchall()
            for fk in fks:
                if fk[2].lower() == table.lower():
                    refs.append({"table": t, "from_col": fk[3], "to_col": fk[4]})
        return refs

    def table_exists(self, table: str) -> bool:
        r = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return r is not None

    def column_exists(self, table: str, column: str) -> bool:
        return any(r[1] == column for r in self.conn.execute(f"PRAGMA table_info({safe_pragma_arg(table)})").fetchall())

    def get_columns(self, table: str) -> list[dict]:
        return [{"name": r[1], "type": r[2], "not_null": r[3], "pk": r[5]} for r in self.conn.execute(f"PRAGMA table_info({safe_pragma_arg(table)})").fetchall()]

    def ping(self) -> bool:
        try:
            self.conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def close(self):
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass  # 清理/关闭失败不影响主流程（OS 兜底回收）
        self._conns.clear()

    def begin(self, name: str = ""):
        if name:
            self.conn.execute(f"SAVEPOINT {safe_savepoint_name(name)}")
        else:
            self.conn.execute("BEGIN")

    def rollback(self, name: str = ""):
        if name:
            # ROLLBACK TO 后必须 RELEASE：只 ROLLBACK 不 RELEASE 时 savepoint
            # 起的事务悬置（in_transaction 残留），写锁滞留到连接关闭——
            # 且该连接后续 begin/commit 语义全部错位（悬挂事务）
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {safe_savepoint_name(name)}")
            self.conn.execute(f"RELEASE SAVEPOINT {safe_savepoint_name(name)}")
        else:
            self.conn.rollback()

    def recreate_table(self, table_config: dict) -> dict:
        """重建表（SQLite 序：CREATE(temp)→COPY→DROP→RENAME；MySQL 序相反），保留共有列的数据

        FK 处理：重建被引用的父表时，子表的外键引用会让 DROP 原表失败
        （FOREIGN KEY constraint failed）——重建期间必须临时关闭外键强制
        （PRAGMA 在事务内无效，须先提交再切换）
        """
        table = table_config["name"]
        temp = f"{table}_recreate"
        self.conn.commit()  # PRAGMA foreign_keys 在事务内无效，先收尾
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("SAVEPOINT sp_recreate")
        try:
            # 表级 schema 合并走注册钩子（依赖倒置——schema 知识在上层，
            # 驱动面不向上感知）；未注入（独立构造场景）按传入 config 原样重建
            from core.drivers import base as _driver_base
            full_yaml = (_driver_base._TABLE_SCHEMA_LOADER(table)
                         if _driver_base._TABLE_SCHEMA_LOADER else None)
            if full_yaml:
                for k, v in full_yaml.items():
                    if k not in table_config:
                        table_config[k] = v
            sql = self._build_create_sql(temp, table_config)
            self.conn.execute(sql)
            old_cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({safe_pragma_arg(table)})").fetchall()]
            new_cols = [c["name"] for c in table_config.get("columns", [])]
            common = [c for c in old_cols if c in new_cols]
            if common:
                for c in common:
                    validate_identifier(c, "重建表共有字段")
                cols = ", ".join(safe_column_sql(c) for c in common)
                self.conn.execute(f'INSERT INTO {safe_table_sql(temp)} ({cols}) SELECT {cols} FROM {safe_table_sql(table)}')
            self.conn.execute(f'DROP TABLE IF EXISTS {safe_table_sql(table)}')
            self.conn.execute(f'ALTER TABLE {safe_table_sql(temp)} RENAME TO {safe_table_sql(table)}')
            for idx in table_config.get("indexes", []):
                unique_str = "UNIQUE " if idx.get("unique") else ""
                for c in idx.get("columns", []):
                    validate_identifier(c, "重建表索引字段")
                validate_identifier(idx["name"], "重建表索引名")
                cols_str = ", ".join(safe_column_sql(c) for c in idx.get("columns", []))
                self.conn.execute(f'CREATE {unique_str}INDEX IF NOT EXISTS {safe_index_sql(idx["name"])} ON {safe_table_sql(table)} ({cols_str})')
            self.conn.execute("RELEASE SAVEPOINT sp_recreate")
            self.conn.execute("PRAGMA foreign_keys=ON")
            return {"ok": True, "message": f"已重建表 {table}"}
        except Exception as e:
            self.conn.execute("ROLLBACK TO SAVEPOINT sp_recreate")
            # ROLLBACK TO 不结束事务——必须 RELEASE 收尾后 PRAGMA 才生效，
            # 否则外键强制在该连接上永久关闭（与 FK 强制声明相悖）
            self.conn.execute("RELEASE SAVEPOINT sp_recreate")
            self.conn.execute("PRAGMA foreign_keys=ON")
            try: self.conn.execute(f'DROP TABLE IF EXISTS {safe_table_sql(temp)}')
            except Exception:
                pass  # 回滚后清临时表失败——临时表残留无碍正确性（IF EXISTS 幂等）
            raise RuntimeError(f"重建失败: {e}") from e

    def drop_table(self, table: str) -> str:
        self.conn.execute(f'DROP TABLE IF EXISTS {safe_table_sql(table)}')
        self.conn.commit()
        return f"表 {table} 已删除"
