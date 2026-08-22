"""Ladybug 图数据库存储——表关系可视化图层

LadybugDB（Kuzu 继任者）是嵌入式图数据库：进程内运行、无独立服务、
零 JVM、Cypher 兼容。
  - 嵌入式：不占 200MB+ 常驻内存，无 JVM 冷启动
  - 零外部依赖：不需要 Java 环境，DLL 已 vendor（含 OpenSSL 补齐）

数据模型（Ladybug 是结构化 schema 图，需先建表）：
  NODE TABLE: GraphNode(
    node_id STRING PRIMARY KEY,   # "industry:name" 复合主键（多行业天然隔离）
    name STRING, business_name STRING, description STRING,
    datasource STRING, x DOUBLE, y DOUBLE, industry STRING
  )
  REL TABLE: GraphEdge(
    FROM GraphNode TO GraphNode,
    source STRING, target STRING, rel_col STRING, ref_col STRING, industry STRING
  )

设计原则：
  - 懒导入 ladybug 驱动（不用时不加载）
  - 不可用时降级为空操作（功能降级，不阻断主流程）——schema_graph_service
    的 SQLite 外键降级路径兜底
  - 所有方法在 Ladybug 不可用时静默返回空结果
  - 注入面收口：表名/列名等标识符经上游 is_valid_identifier 校验后内联
    （create_relationship 等多 MATCH 语句受驱动参数作用域限制无法参数化），
    其余查询一律参数化
"""

import os
import threading
from pathlib import Path
from typing import Optional

from config.settings import settings
from core.logger import info as log_info, warning as log_warning, error as log_error

# 写失败台账（与 Neo4jStore 对齐，供对账端点上报）
from collections import deque
_WRITE_FAILURES = deque(maxlen=100)


def _record_write_failure(op: str, detail: str, error: str):
    from datetime import datetime
    _WRITE_FAILURES.append({
        "op": op, "detail": detail, "error": str(error)[:200],
        "ts": datetime.now().isoformat(timespec="seconds"),
    })


def _vendor_dll_dir() -> Optional[str]:
    """Ladybug 运行时 DLL 目录（vendor/ladybug）——嵌入式 C++ 库
    需要 OpenSSL 3 / msvcp140 依赖，全部 vendor 在同目录下。"""
    p = Path(__file__).resolve().parent.parent.parent / "vendor" / "ladybug"
    return str(p) if (p / "lbug_shared.dll").exists() else None


def _ensure_ladybug_env():
    """把 vendor DLL 目录加入进程 DLL 搜索路径（ctypes 依赖查找必需）"""
    dll_dir = _vendor_dll_dir()
    if not dll_dir:
        return
    try:
        os.add_dll_directory(dll_dir)
    except Exception:
        pass
    os.environ["LBUG_C_API_LIB_PATH"] = os.path.join(dll_dir, "lbug_shared.dll")
    if dll_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")


class LadybugStore:
    """Ladybug 图数据库 CRUD——表关系可视化（与 Neo4jStore 接口对齐）

    单例 Database + Connection（线程安全），嵌入式进程内运行
    降级模式：Ladybug 不可用（DLL 缺失等）时写操作静默跳过，读返回空
    """

    _db = None
    _conn = None
    _initialized = False
    _lock = threading.Lock()
    _available: Optional[bool] = None

    # ── 连接管理 ──

    @classmethod
    def _db_path(cls) -> str:
        """Ladybug 数据库文件路径（默认 data-engine/db/schema_graph.lbdb）"""
        explicit = getattr(settings, "LADYBUG_DB_PATH", "") or ""
        if explicit:
            return explicit
        base = getattr(settings, "SQLITE_DB_PATH", None)
        if base:
            base = str(Path(base).parent)
        else:
            base = str(Path(__file__).resolve().parent.parent.parent / "db")
        return str(Path(base) / "schema_graph.lbdb")

    @classmethod
    def _get_conn(cls):
        """懒初始化 Ladybug 连接（线程安全单例）

        嵌入式：打开即用，无端口无服务。DLL 缺失时降级返回 None。
        """
        if not getattr(settings, "LADYBUG_ENABLED", True):
            return None
        if cls._conn is not None:
            return cls._conn
        with cls._lock:
            if cls._conn is not None:
                return cls._conn
            try:
                _ensure_ladybug_env()
                import ladybug as lb  # 懒导入
                db_path = cls._db_path()
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                cls._db = lb.Database(db_path)
                cls._conn = lb.Connection(cls._db)
                cls._ensure_schema_locked()
                cls._available = True
                log_info("Ladybug 图数据库连接成功", path=db_path)
            except Exception as e:
                cls._available = False
                cls._db = None
                cls._conn = None
                log_warning("Ladybug 不可用，降级为纯 SQLite 模式", error=str(e)[:100])
                return None
        return cls._conn

    @classmethod
    def is_available(cls, auto_start: bool = False) -> bool:
        """检查 Ladybug 是否可用（嵌入式无懒启动语义，auto_start 兼容保留）"""
        return cls._get_conn() is not None

    @classmethod
    def _ensure_schema_locked(cls):
        """建表（幂等）——调用方必须已持有 cls._lock 且 cls._conn 可用"""
        if cls._initialized:
            return
        try:
            cls._conn.execute(
                "CREATE NODE TABLE IF NOT EXISTS GraphNode("
                "node_id STRING PRIMARY KEY, name STRING, business_name STRING, "
                "description STRING, datasource STRING, "
                "x DOUBLE, y DOUBLE, industry STRING)"
            )
            cls._conn.execute(
                "CREATE REL TABLE IF NOT EXISTS GraphEdge("
                "FROM GraphNode TO GraphNode, "
                "source STRING, target STRING, rel_col STRING, "
                "ref_col STRING, industry STRING)"
            )
            cls._initialized = True
            log_info("Ladybug 图模型初始化完成")
        except Exception as e:
            log_error("Ladybug 初始化失败", error=str(e)[:100])

    @classmethod
    def init_schema(cls):
        """初始化 Ladybug 图模型（幂等）"""
        if cls._initialized:
            return
        conn = cls._get_conn()
        if conn is None:
            return
        with cls._lock:
            cls._ensure_schema_locked()

    # ── 工具 ──

    @classmethod
    def _cur_ind(cls) -> str:
        """当前行业（图命名空间）——节点/边按此隔离，防多行业串台"""
        return getattr(settings, "INDUSTRY", "") or ""

    @classmethod
    def _node_id(cls, name: str, industry: str = "") -> str:
        """复合主键：industry:name（行业隔离）"""
        return f"{industry or cls._cur_ind()}:{name}"

    @staticmethod
    def _bolt_reachable(timeout: float = 0.75) -> bool:
        """兼容接口：Ladybug 是嵌入式，无端口探测，恒 True（始终可用）

        schema_graph_service 用它判断"是否走图库快路径"——Ladybug 内置
        即用，走真实图操作（与 Neo4j 可用时一致）。
        """
        return LadybugStore.is_available()

    # ── 节点 CRUD ──

    @classmethod
    def upsert_table_node(cls, name: str, business_name: str = "",
                          description: str = "", datasource: str = "primary",
                          x: float = 0, y: float = 0, industry: str = ""):
        """创建或更新表节点（MERGE + ON CREATE/ON MATCH，带行业命名空间）"""
        conn = cls._get_conn()
        if conn is None:
            return
        ind = industry or cls._cur_ind()
        nid = cls._node_id(name, ind)
        try:
            conn.execute(
                """MERGE (n:GraphNode {node_id: $nid})
                   ON CREATE SET n.name = $name, n.industry = $ind,
                       n.business_name = $business_name, n.description = $description,
                       n.datasource = $datasource, n.x = $x, n.y = $y
                   ON MATCH SET n.business_name = $business_name,
                       n.description = $description, n.datasource = $datasource""",
                parameters={
                    "nid": nid, "name": name, "ind": ind,
                    "business_name": business_name, "description": description,
                    "datasource": datasource, "x": x, "y": y,
                }
            )
        except Exception as e:
            _record_write_failure("upsert_table_node", name, e)
            log_warning("Ladybug upsert_table_node 失败", table=name, error=str(e)[:60])

    @classmethod
    def delete_table_node(cls, name: str):
        """删除表节点及其所有关系（DETACH DELETE，限当前行业命名空间）"""
        conn = cls._get_conn()
        if conn is None:
            return
        try:
            conn.execute(
                "MATCH (t:GraphNode {node_id: $nid}) DETACH DELETE t",
                parameters={"nid": cls._node_id(name)}
            )
        except Exception as e:
            _record_write_failure("delete_table_node", name, e)
            log_warning("Ladybug delete_table_node 失败", table=name, error=str(e)[:60])

    @classmethod
    def update_position(cls, name: str, x: float, y: float):
        """更新表节点的画布坐标（仅图库，高频操作，限当前行业命名空间）"""
        conn = cls._get_conn()
        if conn is None:
            return
        try:
            conn.execute(
                "MATCH (t:GraphNode {node_id: $nid}) SET t.x = $x, t.y = $y",
                parameters={"nid": cls._node_id(name), "x": x, "y": y}
            )
        except Exception as e:
            _record_write_failure("update_position", name, e)
            log_warning("Ladybug update_position 失败", table=name, error=str(e)[:60])

    # ── 关系 CRUD ──

    @classmethod
    def create_relationship(cls, from_table: str, to_table: str,
                            column: str, ref_column: str = "id"):
        """创建外键关系边: (from_table)-[:GraphEdge]->(to_table)

        只允许在当前行业命名空间内连边（跨行业污染拦截）。
        Ladybug 强约束：两端节点必须已存在（先 upsert 占位）。
        """
        conn = cls._get_conn()
        if conn is None:
            return
        ind = cls._cur_ind()
        from_id = cls._node_id(from_table, ind)
        to_id = cls._node_id(to_table, ind)
        try:
            # 确保两端节点存在（Ladybug REL 强约束）
            conn.execute(
                "MERGE (a:GraphNode {node_id: $fid}) "
                "ON CREATE SET a.name = $from, a.industry = $ind, a.x = 0, a.y = 0",
                parameters={"fid": from_id, "from": from_table, "ind": ind}
            )
            conn.execute(
                "MERGE (b:GraphNode {node_id: $tid}) "
                "ON CREATE SET b.name = $to, b.industry = $ind, b.x = 0, b.y = 0",
                parameters={"tid": to_id, "to": to_table, "ind": ind}
            )
            # Ladybug 参数作用域限制：单条查询里多 MATCH 的 $param 不可靠，
            # 用受控来源的字面量内联（表名/列名来自 schema 校验后的真实数据，无注入面）
            conn.execute(
                "MATCH (a:GraphNode) WHERE a.node_id = '%s' "
                "WITH a MATCH (b:GraphNode) WHERE b.node_id = '%s' "
                "CREATE (a)-[:GraphEdge {source: '%s', target: '%s', "
                "rel_col: '%s', ref_col: '%s', industry: '%s'}]->(b)"
                % (from_id, to_id, from_table, to_table, column, ref_column, ind)
            )
        except Exception as e:
            _record_write_failure("create_relationship", f"{from_table}->{to_table}", e)
            log_warning("Ladybug create_relationship 失败",
                        from_table=from_table, error=str(e)[:60])

    @classmethod
    def delete_relationship(cls, from_table: str, column: str):
        """删除外键关系边"""
        conn = cls._get_conn()
        if conn is None:
            return
        try:
            # 内联字面量（表名/列名来自 schema 校验后的受控来源）
            conn.execute(
                "MATCH (a:GraphNode)-[r:GraphEdge]->() "
                "WHERE a.node_id = '%s' AND r.rel_col = '%s' DELETE r"
                % (cls._node_id(from_table), column)
            )
        except Exception as e:
            log_warning("Ladybug delete_relationship 失败",
                        from_table=from_table, error=str(e)[:60])

    @classmethod
    def delete_relationships_from(cls, from_table: str):
        """删除某表的所有出向外键边（保留节点本身及其坐标、入向边）"""
        conn = cls._get_conn()
        if conn is None:
            return
        try:
            conn.execute(
                "MATCH (a:GraphNode)-[r:GraphEdge]->() "
                "WHERE a.node_id = '%s' DELETE r" % cls._node_id(from_table)
            )
        except Exception as e:
            log_warning("Ladybug delete_relationships_from 失败",
                        from_table=from_table, error=str(e)[:60])

    # ── 批量读取 ──

    @classmethod
    def get_all_nodes(cls, industry: str = "") -> list[dict]:
        """获取表节点（含画布坐标）——按行业命名空间过滤（默认当前行业）"""
        conn = cls._get_conn()
        if conn is None:
            return []
        ind = industry or cls._cur_ind()
        try:
            r = conn.execute(
                "MATCH (t:GraphNode) WHERE t.industry = $ind RETURN t.*",
                parameters={"ind": ind}
            )
            nodes = []
            for d in r.rows_as_dict().get_all():
                # 键格式 't.name' → 去前缀取字段名
                g = {k.split('.', 1)[-1]: v for k, v in d.items()}
                nodes.append({
                    "name": g.get("name") or "",
                    "business_name": g.get("business_name") or "",
                    "description": g.get("description") or "",
                    "x": float(g.get("x") or 0),
                    "y": float(g.get("y") or 0),
                    "datasource": g.get("datasource") or "primary",
                })
            return nodes
        except Exception as e:
            log_warning("Ladybug get_all_nodes 失败", error=str(e)[:60])
            return []

    @classmethod
    def get_all_edges(cls, industry: str = "") -> list[dict]:
        """获取外键关系边——两端都属当前行业才返回（跨行业边不可见）"""
        conn = cls._get_conn()
        if conn is None:
            return []
        ind = industry or cls._cur_ind()
        try:
            r = conn.execute(
                "MATCH (a:GraphNode)-[e:GraphEdge]->(b:GraphNode) "
                "WHERE e.industry = $ind RETURN e.*",
                parameters={"ind": ind}
            )
            edges = []
            for d in r.rows_as_dict().get_all():
                g = {k.split('.', 1)[-1]: v for k, v in d.items()}
                edges.append({
                    "source": g.get("source") or "",
                    "target": g.get("target") or "",
                    "column": g.get("rel_col") or "",
                    "ref_column": g.get("ref_col") or "",
                })
            return edges
        except Exception as e:
            log_warning("Ladybug get_all_edges 失败", error=str(e)[:60])
            return []

    # ── 维护 ──

    @classmethod
    def recent_write_failures(cls) -> list[dict]:
        """最近的写失败记录（对账端点显式上报用）"""
        return list(_WRITE_FAILURES)

    @classmethod
    def close(cls):
        """关闭连接"""
        try:
            if cls._conn is not None:
                cls._conn.close()
        except Exception:
            pass
        cls._conn = None
        cls._db = None
        cls._available = None
        cls._initialized = False

    @classmethod
    def migrate_industry_labels(cls) -> dict:
        """兼容接口：Ladybug 用 node_id 天然隔离行业，无需标签迁移。

        返回空结果（无历史数据迁移需求）。
        """
        return {"stamped": 0, "legacy": 0}
