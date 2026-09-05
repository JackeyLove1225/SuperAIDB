"""数据库驱动基类——定义 29 个原子操作接口（27 抽象 + 2 共享默认实现）

接口分类：
- DML（4）：insert / query / update / delete
- DDL（15）：create_table / drop_table / rename_table / add_column / drop_column /
            modify_column / alter_precision / add_foreign_key / drop_foreign_key /
            create_index / drop_index / recreate_table / execute / delete_by_pk /
            _get_unique_key_column
- Utility（7）：list_tables / table_exists / column_exists / get_columns /
               get_referencing_tables / ping / close
- 事务（3）：begin / commit / rollback
- （原 _translate_error 空壳已删，错误翻译统一在契约层 ErrorTranslator）

合计 27 个抽象方法 + 2 个共享默认实现（commit / _get_unique_key_column——
方言无关，从 sqlite/mysql 驱动的逐字同构 override 下沉）= 29 个方法。
"""
from abc import ABC, abstractmethod

# 唯一业务键解析钩子（依赖倒置注册点，见 Driver._get_unique_key_column 注释）
_UNIQUE_KEY_RESOLVER = None

# 表级 schema 全量加载钩子（sqlite recreate_table 的 YAML 合并用——
# schema 知识在上层 schema_matcher，驱动面不向上感知，dsm 实例化时注入）
_TABLE_SCHEMA_LOADER = None


def register_unique_key_resolver(fn) -> None:
    """注入唯一业务键解析器（DataSourceManager.__init__ 调用）"""
    global _UNIQUE_KEY_RESOLVER
    _UNIQUE_KEY_RESOLVER = fn


def register_table_schema_loader(fn) -> None:
    """注入表级 schema 加载器（DataSourceManager.__init__ 调用）"""
    global _TABLE_SCHEMA_LOADER
    _TABLE_SCHEMA_LOADER = fn


class Driver(ABC):
    """数据库驱动，每个数据库实现一个子类。27 个抽象方法缺一不可。"""

    # ── DML：数据操作 ──
    @abstractmethod
    def insert(self, table: str, rows: list[dict], overwrite: bool = False) -> dict:
        """批量插入，返回 {"ok":bool,"count":int,"conflict":bool}"""
        ...

    @abstractmethod
    def query(self, sql: str) -> list[dict]:
        """执行 SELECT 查询"""
        ...

    @abstractmethod
    def update(self, table: str, set_clause: str, where: str = "") -> dict:
        """条件更新，返回 {"ok":bool,"count":int}"""
        ...

    @abstractmethod
    def delete(self, table: str, where: str) -> dict:
        """条件删除（必须带 WHERE），返回 {"ok":bool,"count":int}"""
        ...

    # ── DDL：表结构变更 ──
    @abstractmethod
    def create_table(self, table_config: dict) -> str:
        """根据配置建表"""
        ...

    @abstractmethod
    def delete_by_pk(self, table: str, pk_column: str, pk_value) -> dict:
        """按主键删除（参数化绑定，接口契约）"""
        ...

    def _get_unique_key_column(self, table: str) -> str:
        """表的唯一业务键列名（通用唯一键机制，接口契约）

        解析走注册钩子（依赖倒置）：唯一键知识在 schema 层（schema YAML），
        驱动面不向上感知——由 DataSourceManager 实例化时注入唯一实现
        （core.schema_matcher.get_unique_key_column）。未注入（独立构造
        驱动的测试场景）退化"无业务键声明"：返回空串，冲突检测留给
        DB 约束兜底（fail-safe，绝不臆造字段名）。
        """
        if _UNIQUE_KEY_RESOLVER is not None:
            return _UNIQUE_KEY_RESOLVER(table)
        return ""

    @abstractmethod
    def drop_table(self, table: str) -> str:
        """删除表"""
        ...

    @abstractmethod
    def rename_table(self, table: str, new_name: str) -> str:
        """重命名表"""
        ...

    @abstractmethod
    def add_column(self, table: str, column: str, col_type: str, precision=None, not_null=False) -> dict:
        """加字段，返回 {"ok":bool,"exists":bool,"message":str}

        签名五参（含 not_null）为接口契约——实现族（sqlite/mysql/contract/
        federated/daemon RPC）必须逐字对齐；test_02 有签名一致性断言守护。
        """
        ...

    @abstractmethod
    def drop_column(self, table: str, column: str) -> dict:
        """删字段"""
        ...

    @abstractmethod
    def modify_column(self, table: str, column: str, new_type: str) -> dict:
        """改字段类型"""
        ...

    @abstractmethod
    def alter_precision(self, table: str, column: str, new_precision: tuple) -> dict:
        """改字段精度。new_precision=(总长, 小数位)"""
        ...

    @abstractmethod
    def add_foreign_key(self, table: str, column: str, ref_table: str, ref_column: str = "id") -> dict:
        """给已有表添加外键约束"""
        ...

    @abstractmethod
    def drop_foreign_key(self, table: str, constraint_name: str) -> dict:
        """删除外键约束"""
        ...

    @abstractmethod
    def create_index(self, table: str, columns: str, unique: bool = False) -> str:
        """创建索引。unique=True 时创建唯一索引"""
        ...

    @abstractmethod
    def recreate_table(self, table_config: dict) -> dict:
        """重建表（RENAME→CREATE→COPY→DROP），保留共有列的数据"""
        ...

    @abstractmethod
    def execute(self, sql: str):
        """执行任意 SQL（DDL/DML），用于多步骤内部操作"""
        ...

    @abstractmethod
    def drop_index(self, name: str) -> str:
        """删除索引"""
        ...

    # ── Utility：元数据 ──
    @abstractmethod
    def list_tables(self) -> list[str]:
        """列出所有表名"""
        ...

    @abstractmethod
    def get_referencing_tables(self, table: str) -> list[dict]:
        """返回所有外键引用了本表的表及字段。每条含 {table, from_col, to_col}"""
        ...
    @abstractmethod
    def table_exists(self, table: str) -> bool:
        """检查表是否存在"""
        ...

    @abstractmethod
    def column_exists(self, table: str, column: str) -> bool:
        """检查字段是否存在"""
        ...

    @abstractmethod
    def get_columns(self, table: str) -> list[dict]:
        """获取表的字段列表 [{"name":"id","type":"INTEGER"}]"""
        ...

    @abstractmethod
    def ping(self) -> bool:
        """检查数据库连接"""
        ...

    @abstractmethod
    def close(self):
        """关闭连接"""
        ...

    # ── Utility：事务 ──
    @abstractmethod
    def begin(self, name: str = ""):
        """开启事务或 savepoint"""
        ...

    def commit(self):
        """提交事务（共享默认实现：sqlite/mysql 原 override 逐字相同，已删）"""
        self.conn.commit()

    @abstractmethod
    def rollback(self, name: str = ""):
        """回滚事务或 savepoint"""
        ...
