"""SQLite 元数据表——表结构高性能查询层

三张元数据表：
  meta_tables       — 表级元数据（表名、业务名、描述、数据源）
  meta_columns      — 字段级元数据（字段名、类型、约束、顺序）
  meta_foreign_keys — 外键关系元数据

设计原则：
  - 每张表有且只有一个自增 id 主键（系统约定）
  - table_name 作为业务唯一标识符，跨层关联
  - meta_columns / meta_foreign_keys 冗余存储 table_name（避免 JOIN，提升查询性能）
  - 外键只能指向 id（符合项目约定）
  - 索引覆盖高频查询路径，微秒级响应

性能预估（1000 表 / 10000 字段）：
  列出所有表  ~0.5ms (SELECT * FROM meta_tables)
  查单表字段  ~0.1ms (索引查询 table_name)
  查所有外键  ~0.3ms (索引查询 table_name / ref_table_name)
"""

import json
import sqlite3
from core.crypto.connection import open_db, compat_row_factory
import threading
from pathlib import Path
from typing import Optional

from config.settings import settings
from core.contract.security_contract import safe_column_sql


def _resolve_db_path() -> str:
    """解析 SQLite 数据库绝对路径

    settings.SQLITE_DB_PATH 是相对路径（./db/data_engine.db），
    需要相对于 data-engine 目录解析。
    """
    db_path = settings.SQLITE_DB_PATH
    if Path(db_path).is_absolute():
        return db_path
    # 相对于 data-engine 目录解析
    data_engine_root = Path(__file__).resolve().parent.parent.parent
    return str(data_engine_root / db_path)


def _resolve_meta_db_path() -> str:
    """按行业解析 MetaDB 文件路径（U-7/U-9：行业物理隔离）

    db/meta_{industry}.db —— 每个行业一份元数据库（表/字段/外键元数据），
    切换行业后 MetaDB 指向新行业自己的文件，互不污染。
    """
    data_engine_root = Path(__file__).resolve().parent.parent.parent
    return str(data_engine_root / "db" / f"meta_{settings.INDUSTRY}.db")


def _parse_json_field(value) -> dict:
    """解析 meta_columns.check_template_params 等 JSON 字段

    空值返回 {}，解析失败返回 {}，成功返回 dict。
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


class MetaDB:
    """SQLite 元数据表管理——CRUD + 初始化

    线程安全：每线程一个 sqlite3.Connection（WAL 模式，支持并发读）
    直接使用 sqlite3，不经过 Steward（元数据表是系统内部表，非业务数据）
    """

    _instance: "MetaDB" = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "MetaDB":
        """获取全局单例（避免重复创建连接池）

        行业切换自愈合：settings.INDUSTRY 已是热键（.env 新鲜读取），
        若绑定库与当前行业不符，自动换绑到新行业的 meta 库——免 reset 免重启。
        """
        want = _resolve_meta_db_path()
        inst = cls._instance
        if inst is not None and inst._db_path != want:
            with cls._lock:
                if cls._instance is not None and cls._instance._db_path != want:
                    try:
                        cls._instance.close()
                    except Exception:
                        pass
                    cls._instance = None
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or _resolve_meta_db_path()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conns: dict[int, sqlite3.Connection] = {}
        self._init_lock = threading.Lock()
        self._initialized = False
        self._init_schema()
        self._migrate_from_legacy_if_empty()

    @property
    def conn(self) -> sqlite3.Connection:
        """获取当前线程的 SQLite 连接（线程安全）"""
        tid = threading.get_ident()
        if tid not in self._conns:
            conn = open_db(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = compat_row_factory()  # 支持按列名访问
            self._conns[tid] = conn
        return self._conns[tid]

    @classmethod
    def reset_instance(cls):
        """单例重置的公开入口（行业切换/测试用，U-9/P2-4）"""
        cls._instance = None

    def _migrate_from_legacy_if_empty(self):
        """一次性迁移：共享 data_engine.db 中的 meta_* 数据迁入按行业文件（U-7）。

        触发条件：新行业文件 meta_tables 为空 且 旧共享库存在 meta_ 数据，
        **且遗产表名集与当前行业 schemas YAML 完全一致**——否则遗产属于
        别的行业，迁入即串台（历史上 school 表混入 logistics 的教训）。
        保留原 id（meta_columns/meta_foreign_keys 的 table_id 引用随之保持）。
        """
        legacy = _resolve_db_path()
        if legacy == self._db_path or not Path(legacy).exists():
            return
        try:
            src = open_db(legacy)
            src.row_factory = compat_row_factory()
            try:
                try:
                    tables = src.execute("SELECT * FROM meta_tables").fetchall()
                except Exception:
                    # 遗产库连 meta_tables 都不存在（如共享 meta 时代已翻篇、
                    # 主库只剩业务表）——无遗产可迁，直接跳过（20260822 修复：
                    # 此前裸读抛错，把"无遗产"误判成故障）
                    return
                if not tables:
                    return
                # 串台闸门：遗产表名集必须与当前行业 schemas 完全一致
                legacy_names = {t["name"] for t in tables}
                data_engine_root = Path(__file__).resolve().parent.parent.parent
                schemas_dir = data_engine_root / "industries" / settings.INDUSTRY / "schemas"
                yaml_names = {
                    f.stem
                    for f in list(schemas_dir.glob("*.yaml")) + list(schemas_dir.glob("*.yml"))
                } if schemas_dir.is_dir() else set()
                if legacy_names != yaml_names:
                    from core.logger import get_logger
                    get_logger(__name__).info(
                        "遗产 meta 表 %s 与行业 '%s' schemas %s 不一致，跳过迁移（防串台）",
                        sorted(legacy_names), settings.INDUSTRY, sorted(yaml_names))
                    return
                conn = self.conn
                existing = conn.execute("SELECT COUNT(*) FROM meta_tables").fetchone()[0]
                if existing > 0:
                    return
                for t in tables:
                    conn.execute(
                        "INSERT INTO meta_tables (id, name, business_name, description, datasource, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (t["id"], t["name"], t["business_name"], t["description"],
                         t["datasource"], t["created_at"], t["updated_at"]))
                for col in src.execute("SELECT * FROM meta_columns").fetchall():
                    keys = col.keys()
                    conn.execute(
                        f"INSERT INTO meta_columns ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                        tuple(col[k] for k in keys))
                for fk in src.execute("SELECT * FROM meta_foreign_keys").fetchall():
                    keys = fk.keys()
                    conn.execute(
                        f"INSERT INTO meta_foreign_keys ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                        tuple(fk[k] for k in keys))
                conn.commit()
            finally:
                src.close()
        except sqlite3.OperationalError:
            pass  # 旧库无 meta_ 表（全新部署）——无需迁移

    def _init_schema(self):
        """初始化三张元数据表（幂等，重复调用安全）

        包含迁移逻辑：对已存在的 meta_columns 表添加新字段（is_unique/is_indexed/check_constraint）
        """
        with self._init_lock:
            if self._initialized:
                return
            conn = self.conn
            conn.executescript("""
                -- 表级元数据
                CREATE TABLE IF NOT EXISTS meta_tables (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    business_name TEXT,
                    description TEXT,
                    datasource TEXT DEFAULT 'primary',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_tables_name ON meta_tables(name);

                -- 字段级元数据
                CREATE TABLE IF NOT EXISTS meta_columns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_id INTEGER NOT NULL,
                    table_name TEXT NOT NULL,
                    column_name TEXT NOT NULL,
                    column_type TEXT NOT NULL,
                    not_null INTEGER DEFAULT 0,
                    is_pk INTEGER DEFAULT 0,
                    is_autoincrement INTEGER DEFAULT 0,
                    is_unique INTEGER DEFAULT 0,
                    is_indexed INTEGER DEFAULT 0,
                    check_constraint TEXT,
                    description TEXT,
                    position INTEGER DEFAULT 0,
                    FOREIGN KEY (table_id) REFERENCES meta_tables(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_meta_columns_table_id ON meta_columns(table_id);
                CREATE INDEX IF NOT EXISTS idx_meta_columns_table_name ON meta_columns(table_name);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_columns_table_col ON meta_columns(table_name, column_name);

                -- 外键关系元数据
                CREATE TABLE IF NOT EXISTS meta_foreign_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_id INTEGER NOT NULL,
                    table_name TEXT NOT NULL,
                    column_name TEXT NOT NULL,
                    ref_table_id INTEGER,
                    ref_table_name TEXT NOT NULL,
                    ref_column_name TEXT NOT NULL,
                    constraint_name TEXT,
                    FOREIGN KEY (table_id) REFERENCES meta_tables(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_meta_fks_table_name ON meta_foreign_keys(table_name);
                CREATE INDEX IF NOT EXISTS idx_meta_fks_ref_table ON meta_foreign_keys(ref_table_name);
            """)
            # 迁移逻辑：对已存在的旧表追加新字段（CREATE TABLE IF NOT EXISTS 不会修改已有表结构）
            self._migrate_meta_columns(conn)
            conn.commit()
            self._initialized = True

    def _migrate_meta_columns(self, conn):
        """迁移 meta_columns 表——为旧版本追加新字段

        支持需求5（唯一约束/普通索引）、需求6（CHECK 约束）、CHECK 模板分类的元数据存储。
        幂等：通过 PRAGMA 检查字段是否已存在，避免重复 ALTER 报错。
        """
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(meta_columns)").fetchall()}
        migrations = [
            ("is_unique", "INTEGER DEFAULT 0"),
            ("is_indexed", "INTEGER DEFAULT 0"),
            ("check_constraint", "TEXT"),
            # CHECK 模板分类（需求1：CHECK 约束按字段类型分类）
            # check_template_key: 模板 key（如 int_range），自定义时存 "custom"
            # check_template_params: JSON 字符串，如 {"min":0,"max":150}
            ("check_template_key", "TEXT"),
            ("check_template_params", "TEXT"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                conn.execute(f'ALTER TABLE meta_columns ADD COLUMN {safe_column_sql(col_name)} {col_def}')

    # ========== meta_tables CRUD ==========

    def upsert_table(self, name: str, business_name: str = "",
                     description: str = "", datasource: str = "primary") -> int:
        """插入或更新表元数据，返回 table_id"""
        conn = self.conn
        # 先查是否已存在
        row = conn.execute(
            "SELECT id FROM meta_tables WHERE name = ?", (name,)
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE meta_tables SET business_name=?, description=?, datasource=?,
                   updated_at=datetime('now') WHERE id=?""",
                (business_name, description, datasource, row["id"])
            )
            conn.commit()
            return row["id"]
        cur = conn.execute(
            """INSERT INTO meta_tables (name, business_name, description, datasource)
               VALUES (?, ?, ?, ?)""",
            (name, business_name, description, datasource)
        )
        conn.commit()
        return cur.lastrowid

    def get_table_id(self, name: str) -> Optional[int]:
        """根据表名查 table_id"""
        row = self.conn.execute(
            "SELECT id FROM meta_tables WHERE name = ?", (name,)
        ).fetchone()
        return row["id"] if row else None

    def get_table(self, name: str) -> Optional[dict]:
        """获取单表元数据"""
        row = self.conn.execute(
            "SELECT * FROM meta_tables WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def list_tables(self) -> list[dict]:
        """列出所有表元数据"""
        rows = self.conn.execute(
            "SELECT * FROM meta_tables ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_table(self, name: str) -> bool:
        """删除表元数据（CASCADE 自动删除关联的 columns 和 foreign_keys）"""
        conn = self.conn
        cur = conn.execute("DELETE FROM meta_tables WHERE name = ?", (name,))
        conn.commit()
        return cur.rowcount > 0

    # ========== meta_columns CRUD ==========

    def replace_columns(self, table_name: str, columns: list[dict]):
        """替换某表的所有字段（先删后插，用于全量更新）

        支持字段约束元数据：not_null / is_unique / is_indexed / check_constraint
        以及 CHECK 模板分类：check_template_key / check_template_params
        """
        table_id = self.get_table_id(table_name)
        if table_id is None:
            raise ValueError(f"表 '{table_name}' 不存在于 meta_tables 中")
        conn = self.conn
        conn.execute("DELETE FROM meta_columns WHERE table_id = ?", (table_id,))
        for i, col in enumerate(columns):
            # check_template_params: dict → JSON 字符串
            params_val = col.get("check_template_params")
            if isinstance(params_val, (dict, list)):
                params_json = json.dumps(params_val, ensure_ascii=False)
            elif params_val is None:
                params_json = ""
            else:
                params_json = str(params_val)
            # check_template_key: 有 check_constraint 但无 key 时默认 "custom"
            tmpl_key = col.get("check_template_key", "")
            if not tmpl_key and col.get("check_constraint"):
                tmpl_key = "custom"
            conn.execute(
                """INSERT INTO meta_columns
                   (table_id, table_name, column_name, column_type, not_null, is_pk,
                    is_autoincrement, is_unique, is_indexed, check_constraint,
                    check_template_key, check_template_params,
                    description, position)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (table_id, table_name, col["name"], col.get("type", "TEXT"),
                 1 if col.get("not_null") else 0,
                 1 if col.get("is_pk") else 0,
                 1 if col.get("autoincrement") else 0,
                 1 if col.get("is_unique") else 0,
                 1 if col.get("is_indexed") else 0,
                 col.get("check_constraint", ""),
                 tmpl_key,
                 params_json,
                 col.get("description", ""),
                 col.get("position", i))
            )
        conn.commit()

    def add_column_if_missing(self, table_name: str, column_name: str,
                              column_type: str = "TEXT", description: str = "") -> bool:
        """缺则补一个字段（元数据增量同步用，如外键列补登）——返回是否真插入

        service 层不得再直接摸 self.conn 手写 SQL（封装纪律，评审四轮）：
        元数据写只经本类方法。
        """
        existing = self.get_columns(table_name)
        if any(c["name"] == column_name for c in existing):
            return False
        table_id = self.get_table_id(table_name)
        if table_id is None:
            return False
        max_pos = max((c.get("position", 0) for c in existing), default=-1)
        conn = self.conn
        conn.execute(
            """INSERT INTO meta_columns
               (table_id, table_name, column_name, column_type, not_null, is_pk,
                is_autoincrement, is_unique, is_indexed, check_constraint,
                check_template_key, check_template_params, description, position)
               VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0, '', '', '', ?, ?)""",
            (table_id, table_name, column_name, column_type,
             description, max_pos + 1)
        )
        conn.commit()
        return True
        """获取某表的所有字段（按 position 排序）"""
        rows = self.conn.execute(
            """SELECT column_name, column_type, not_null, is_pk, is_autoincrement,
                      is_unique, is_indexed, check_constraint,
                      check_template_key, check_template_params,
                      description, position
               FROM meta_columns WHERE table_name = ? ORDER BY position""",
            (table_name,)
        ).fetchall()
        return [{
            "name": r["column_name"],
            "type": r["column_type"],
            "not_null": bool(r["not_null"]),
            "is_pk": bool(r["is_pk"]),
            "autoincrement": bool(r["is_autoincrement"]),
            "is_unique": bool(r["is_unique"]),
            "is_indexed": bool(r["is_indexed"]),
            "check_constraint": r["check_constraint"] or "",
            "check_template_key": r["check_template_key"] or "",
            "check_template_params": _parse_json_field(r["check_template_params"]),
            "description": r["description"],
            "position": r["position"],
        } for r in rows]

    def get_all_columns(self) -> dict[str, list[dict]]:
        """获取所有表的字段（按表名分组，用于一次加载完整图）"""
        rows = self.conn.execute(
            """SELECT table_name, column_name, column_type, not_null, is_pk,
                      is_autoincrement, is_unique, is_indexed, check_constraint,
                      check_template_key, check_template_params,
                      description, position
               FROM meta_columns ORDER BY table_name, position"""
        ).fetchall()
        result: dict[str, list[dict]] = {}
        for r in rows:
            result.setdefault(r["table_name"], []).append({
                "name": r["column_name"],
                "type": r["column_type"],
                "not_null": bool(r["not_null"]),
                "is_pk": bool(r["is_pk"]),
                "autoincrement": bool(r["is_autoincrement"]),
                "is_unique": bool(r["is_unique"]),
                "is_indexed": bool(r["is_indexed"]),
                "check_constraint": r["check_constraint"] or "",
                "check_template_key": r["check_template_key"] or "",
                "check_template_params": _parse_json_field(r["check_template_params"]),
                "description": r["description"],
                "position": r["position"],
            })
        return result

    # ========== meta_foreign_keys CRUD ==========

    def replace_foreign_keys(self, table_name: str, foreign_keys: list[dict]):
        """替换某表的所有外键关系（先删后插）"""
        table_id = self.get_table_id(table_name)
        if table_id is None:
            raise ValueError(f"表 '{table_name}' 不存在于 meta_tables 中")
        conn = self.conn
        conn.execute("DELETE FROM meta_foreign_keys WHERE table_id = ?", (table_id,))
        for fk in foreign_keys:
            ref_table_id = self.get_table_id(fk.get("references", ""))
            for col in fk.get("columns", []):
                for ref_col in fk.get("ref_columns", ["id"]):
                    conn.execute(
                        """INSERT INTO meta_foreign_keys
                           (table_id, table_name, column_name, ref_table_id,
                            ref_table_name, ref_column_name, constraint_name)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (table_id, table_name, col,
                         ref_table_id, fk.get("references", ""),
                         ref_col, f"fk_{table_name}_{col}")
                    )
        conn.commit()

    def get_foreign_keys(self, table_name: str) -> list[dict]:
        """获取某表的外键关系"""
        rows = self.conn.execute(
            """SELECT column_name, ref_table_name, ref_column_name, constraint_name
               FROM meta_foreign_keys WHERE table_name = ?""",
            (table_name,)
        ).fetchall()
        return [{
            "column": r["column_name"],
            "references": r["ref_table_name"],
            "ref_column": r["ref_column_name"],
            "constraint_name": r["constraint_name"],
        } for r in rows]

    def get_all_foreign_keys(self) -> list[dict]:
        """获取所有外键关系（用于一次加载完整图）"""
        rows = self.conn.execute(
            """SELECT table_name, column_name, ref_table_name, ref_column_name
               FROM meta_foreign_keys"""
        ).fetchall()
        return [{
            "table_name": r["table_name"],
            "column": r["column_name"],
            "ref_table": r["ref_table_name"],
            "ref_column": r["ref_column_name"],
        } for r in rows]

    def add_foreign_key(self, table_name: str, column_name: str,
                        ref_table_name: str, ref_column_name: str = "id") -> bool:
        """添加单条外键关系"""
        table_id = self.get_table_id(table_name)
        if table_id is None:
            return False
        ref_table_id = self.get_table_id(ref_table_name)
        conn = self.conn
        try:
            conn.execute(
                """INSERT INTO meta_foreign_keys
                   (table_id, table_name, column_name, ref_table_id,
                    ref_table_name, ref_column_name, constraint_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (table_id, table_name, column_name, ref_table_id,
                 ref_table_name, ref_column_name, f"fk_{table_name}_{column_name}")
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_foreign_key(self, table_name: str, column_name: str) -> bool:
        """删除单条外键关系"""
        conn = self.conn
        cur = conn.execute(
            "DELETE FROM meta_foreign_keys WHERE table_name = ? AND column_name = ?",
            (table_name, column_name)
        )
        conn.commit()
        return cur.rowcount > 0

    # ========== 统计与搜索 ==========

    def get_stats(self) -> dict:
        """获取统计信息"""
        conn = self.conn
        table_count = conn.execute("SELECT COUNT(*) FROM meta_tables").fetchone()[0]
        column_count = conn.execute("SELECT COUNT(*) FROM meta_columns").fetchone()[0]
        fk_count = conn.execute("SELECT COUNT(*) FROM meta_foreign_keys").fetchone()[0]
        return {
            "table_count": table_count,
            "column_count": column_count,
            "relationship_count": fk_count,
        }

    def search(self, query: str) -> dict:
        """搜索表名和字段名（利用索引，微秒级响应）"""
        conn = self.conn
        pattern = f"%{query}%"
        tables = [dict(r) for r in conn.execute(
            """SELECT name, business_name, description FROM meta_tables
               WHERE name LIKE ? OR business_name LIKE ? OR description LIKE ?
               ORDER BY name""",
            (pattern, pattern, pattern)
        ).fetchall()]
        columns = [dict(r) for r in conn.execute(
            """SELECT table_name, column_name, column_type, description FROM meta_columns
               WHERE column_name LIKE ? OR description LIKE ?
               ORDER BY table_name, position""",
            (pattern, pattern)
        ).fetchall()]
        return {"tables": tables, "columns": columns}

    def close(self):
        """关闭所有连接"""
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()
