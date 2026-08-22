"""ContractDriver 包装器——契约层的唯一入口

职责：
包装原始 Driver，所有 DML/DDL 操作都经过对应契约校验 + 异常翻译。
Utility/事务操作直接透传，无需契约。

设计原则：
- 上层通过 Steward._get_driver() 拿到 ContractDriver 实例，零感知
- 所有前置校验失败抛 RiskError/PrimaryKeyError/SecurityError（不被 ErrorTranslator 翻译）
- driver 抛出的其他异常经 ErrorTranslator.translate 翻译为 AppError
- 新增 driver 时，只需实现纯 Driver 接口，本类自动应用所有保护

27 个原子操作的包装映射：
- DML（4）：insert/update/delete 经 DataCrudContract；query 直接透传+翻译
- DDL（13）：create_table/drop_table/rename_table/add_column/drop_column/
            modify_column/alter_precision/add_foreign_key/drop_foreign_key/
            create_index/drop_index/recreate_table/execute
            → 经 SchemaChangeContract/SecurityContract/TypeContract
- Utility（7）：list_tables/table_exists/column_exists/get_columns/
               get_referencing_tables/ping/close → 直接透传
- 事务（3）：begin/commit/rollback → 直接透传
- 兼容方法（2）：_safe_where/_translate_error → 委托 SecurityContract/ErrorTranslator
合计覆盖 27 抽象 + 1 非抽象（_translate_error）+ 1 兼容（_safe_where）= 全部接口。
"""
import inspect
from core.logger import get_logger
import re
from typing import Optional

logger = get_logger(__name__)

from core.drivers.base import Driver
from .security_contract import SecurityContract, extract_tables_from_sql, safe_column_sql
from .type_contract import TypeContract
from .schema_change_contract import SchemaChangeContract
from .data_crud_contract import DataCrudContract
from .error_translator import ErrorTranslator
from core.exceptions import RiskError, PrimaryKeyError, SecurityError, AppError
from core.permission import Operation, PermissionPolicy


# 不被 ErrorTranslator 翻译的异常类型（契约层主动抛出的）
_CONTRACT_ERRORS = (RiskError, PrimaryKeyError, SecurityError, AppError)

# execute() 透传加固：仅放行的首关键字（合法调用方只用 DDL/DML/PRAGMA）
_EXECUTE_ALLOWED_KEYWORDS = frozenset({
    "CREATE", "DROP", "ALTER", "PRAGMA",
    "INSERT", "UPDATE", "DELETE", "SELECT",
})

# 匹配单/双引号包裹的字面量（含 '' 和 "" 转义）
# 先剥掉字面量再查分号/注释，避免合法 SQL 的字符串内容被误判
_QUOTED_LITERAL_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


def _validate_execute_sql(sql):
    """execute() 透传 SQL 的纵深校验，不合规抛 SecurityError。

    规则（保守策略，不依赖 sqlparse）：
    - 强制单语句：剥掉引号字面量后仍含分号 → 视为语句堆叠，拒绝
    - 拒绝 SQL 注释（--、/*）
    - 仅放行 CREATE/DROP/ALTER/PRAGMA/INSERT/UPDATE/DELETE/SELECT 开头
    - 系统表豁免：DROP/ALTER TABLE <系统表> 拒绝（防 execute 绕过 drop_table 豁免）
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SecurityError("execute() 拒绝空 SQL")
    stripped = _QUOTED_LITERAL_RE.sub("", sql)
    if ";" in stripped:
        raise SecurityError("execute() 禁止语句堆叠（字符串字面量之外不得含分号）")
    if "--" in stripped or "/*" in stripped:
        raise SecurityError("execute() 禁止 SQL 注释（-- 或 /*）")
    first = sql.lstrip().split(None, 1)[0].upper()
    if first not in _EXECUTE_ALLOWED_KEYWORDS:
        raise SecurityError(
            f"execute() 仅放行 CREATE/DROP/ALTER/PRAGMA/INSERT/UPDATE/DELETE/SELECT "
            f"开头的语句，已拒绝: {first} ..."
        )
    # 系统表豁免（大小写归一化，与 drop_table/ContractDriver.drop_table 一致）：
    # 识别 DROP TABLE / ALTER TABLE <name> 目标，系统表一律拒绝
    # 支持：裸标识符 / 引号包裹("name"、'name'、`name`) / SQLite 方括号([name]) /
    # schema 前缀(main.users、[main].[users]) —— 取最后一个捕获段（末段表名）比对
    if first in ("DROP", "ALTER"):
        m = re.search(
            r"\b(?:DROP|ALTER)\s+TABLE(?:\s+IF\s+EXISTS)?\s+"
            r"(?:[\"'`\[]([a-zA-Z0-9_]+)[\]\"'`]|([a-zA-Z0-9_]+))"
            r"(?:\s*\.\s*(?:[\"'`\[]([a-zA-Z0-9_]+)[\]\"'`]|([a-zA-Z0-9_]+)))?"
            r"(?:\s*\.\s*(?:[\"'`\[]([a-zA-Z0-9_]+)[\]\"'`]|([a-zA-Z0-9_]+)))?",
            sql, re.IGNORECASE)
        if m:
            _parts = [g for g in m.groups() if g]
            _tbl = (_parts[-1] if _parts else "").lower()
            _SYS = {"users", "roles", "permissions", "role_permissions", "sessions"}
            if _tbl in _SYS or _tbl.startswith(("sqlite_", "meta_")):
                raise SecurityError(f"execute() 禁止操作系统表: {_tbl}")


class ContractDriver(Driver):
    """契约驱动——包装原始 Driver，强制经过所有契约校验

    Steward._get_driver() 透明返回本类的实例，上层无感知。

    新增 driver 时，只需实现纯 Driver 接口，本类自动应用所有保护：
    - 标识符/WHERE/主键/CHECK 安全校验（SecurityContract）
    - 类型变更风险评估（TypeContract）
    - 表结构变更差异分析（SchemaChangeContract + ChangeAnalyzer）
    - 数据 CRUD 类型校验 + 批量上限 + WHERE 主键要求（DataCrudContract）
    - 异常翻译为统一中文（ErrorTranslator）

    force_passthrough（类级开关，默认关）：force 确认闸属于"意图边界"语义
    （最外层契约，靠近用户/AI 意图处评估一次）。daemon 进程内的契约层是
    数据面实现细节——force 已在调用方契约评估过，内层重评会把已批准的
    操作再次拦死（daemon 模式下 force 操作必败，评审三轮发现）。
    daemon/server.py 启动时置真。
    """
    force_passthrough: bool = False

    def __init__(self, driver: Driver, driver_type: str = ""):
        """
        Args:
            driver: 原始 Driver 实例
            driver_type: driver 类型名（"sqlite"/"mysql"/...）
                         留空时自动从 driver 类名推断
        """
        self._driver = driver
        self._driver_type = driver_type or ErrorTranslator.get_driver_type(driver)

    # ── 透传属性 ──

    @property
    def raw_driver(self) -> Driver:
        """获取被包装的原始 Driver（仅供内部需要直接操作时使用）"""
        return self._driver

    @property
    def driver_type(self) -> str:
        """获取 driver 类型名"""
        return self._driver_type

    # ── Utility：元数据查询（直接透传，无需契约）──

    def list_tables(self):
        return self._driver.list_tables()

    def table_exists(self, table: str) -> bool:
        return self._driver.table_exists(table)

    def column_exists(self, table: str, column: str) -> bool:
        return self._driver.column_exists(table, column)

    def get_columns(self, table: str):
        return self._driver.get_columns(table)

    def get_referencing_tables(self, table: str):
        return self._driver.get_referencing_tables(table)

    def ping(self) -> bool:
        return self._driver.ping()

    def close(self):
        return self._driver.close()

    # ── 事务（直接透传）──

    def begin(self, name: str = ""):
        return self._driver.begin(name)

    def commit(self):
        return self._driver.commit()

    def rollback(self, name: str = ""):
        return self._driver.rollback(name)

    # ── 兼容方法（供 FederatedDriver 等旧代码调用）──

    def _safe_where(self, where: str) -> bool:
        """WHERE 条件安全校验——委托给 SecurityContract（兼容旧接口）

        返回 True 表示安全，False 表示不安全。
        新代码应直接用 SecurityContract.validate_where（抛异常而非返回 bool）。
        """
        try:
            SecurityContract.validate_where(where)
            return True
        except SecurityError:
            return False

    def _translate_error(self, error_msg: str) -> str:
        """错误翻译——委托给 ErrorTranslator（兼容旧接口）

        新代码应直接用 ErrorTranslator.translate(driver_type, exception)。
        """
        result = ErrorTranslator.translate(self._driver_type, Exception(error_msg))
        return result.message

    # ── 权限矩阵（core/permission 独立层，20260804 并入契约栈）──
    # 数据安全双栈收口：聊天 DML 栈在 FederatedDriver 内建同款检查；
    # 本层覆盖 schema/Steward 栈（全部 DDL 工具经此）。两处读同一份
    # config/permissions.yml，默认 full 时所有检查为 no-op（向后兼容）。

    def _perm(self, table: str, op, method: str):
        """表级权限：禁止时抛 PermissionDenied（AppError 子类，不被翻译）"""
        from core.permission.sql_guard import resolve_datasource
        PermissionPolicy.get_instance().check(
            resolve_datasource(table), op, method, table=table)

    def _perm_columns(self, table: str, columns, op):
        """列级权限：op 如实传（insert/update 各自判定——"写列一律按 update"的旧约定
        会让 deny:[insert] 类规则漏判，评审四轮 sql_guard 张冠李戴同源收口）"""
        from core.permission.sql_guard import resolve_datasource
        ds = resolve_datasource(table)
        policy = PermissionPolicy.get_instance()
        for col in columns:
            policy.check_column(ds, table, col, op)

    def _check_where_denied(self, table: str, where: str, op):
        """写路径 WHERE 的禁列扫描（与 query 读屏蔽同规则）——
        只罩投影会把 WHERE password_hash LIKE 'a%' 留成布尔预言机（评审四轮 H-1）"""
        if not where:
            return
        from core.permission.sql_guard import resolve_datasource
        ds = resolve_datasource(table)
        policy = PermissionPolicy.get_instance()
        denied = policy.denied_columns(ds, table, op)
        for col in denied:
            if re.search(rf'(?<![\w]){re.escape(col)}(?![\w])', where, flags=re.I):
                from core.permission import PermissionDenied
                raise PermissionDenied(
                    f"列 {ds}.{table}.{col} 权限不足：{op.value} 的 WHERE 不得引用禁列")

    # ── DML：数据操作（经 DataCrudContract）──

    def insert(self, table: str, rows: list, overwrite: bool = False):
        """批量插入——经 DataCrudContract.validate_insert"""
        try:
            cleaned = DataCrudContract.validate_insert(self._driver, table, rows)
            self._perm(table, Operation.INSERT, "insert")
            if cleaned:
                self._perm_columns(table, cleaned[0].keys(), Operation.INSERT)
            return self._driver.insert(table, cleaned, overwrite)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def update(self, table: str, set_clause: str, where: str = ""):
        """条件更新——经 DataCrudContract.validate_update"""
        try:
            DataCrudContract.validate_update(self._driver, table, set_clause, where)
            self._perm(table, Operation.UPDATE, "update")
            # 全仓唯一 SET 解析器提取列（权限判定与驱动执行同源，评审四轮 S-2）
            from .security_contract import split_set_pairs
            self._perm_columns(
                table, [col for col, _ in split_set_pairs(set_clause)],
                Operation.UPDATE)
            self._check_where_denied(table, where, Operation.UPDATE)
            return self._driver.update(table, set_clause, where)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def delete(self, table: str, where: str):
        """条件删除——经 DataCrudContract.validate_delete"""
        try:
            DataCrudContract.validate_delete(self._driver, table, where)
            self._perm(table, Operation.DELETE, "delete")
            self._check_where_denied(table, where, Operation.DELETE)
            return self._driver.delete(table, where)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def delete_by_pk(self, table: str, pk_column: str, pk_value):
        """按主键删除（P1-10 接口契约）——经 DataCrudContract.validate_delete 等价校验"""
        try:
            self._perm(table, Operation.DELETE, "delete_by_pk")
            # 与 delete(where="pk=?") 等价的契约保护：主键列标识符校验
            from core.contract.security_contract import SecurityContract
            SecurityContract.validate_identifier(table, "表名")
            SecurityContract.validate_identifier(pk_column, "主键列名")
            return self._driver.delete_by_pk(table, pk_column, pk_value)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def _get_unique_key_column(self, table: str) -> str:
        """表的唯一业务键列名（P1-2 接口契约）——直接透传"""
        return self._driver._get_unique_key_column(table)

    def query(self, sql: str):
        """SELECT 查询——读权限单栈收口 + 异常翻译 + SELECT 强制（security_review MEDIUM：
        防止未来调用方把用户 SQL 传入 query 绕过角色限制；query 只应执行读语句）"""
        # 首关键字必须为 SELECT/EXPLAIN（只读）；其余（UPDATE/DELETE/DROP 等）
        # 一律拒绝——写路径必须走 insert/update/delete 等命名方法过权限层
        import re as _qre
        _head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if _head not in ("SELECT", "EXPLAIN"):
            from core.exceptions import SecurityError
            raise SecurityError(
                f"query() 仅放行 SELECT/EXPLAIN 读语句，已拒绝: {_head or '(空)'} ...")
        # 读权限唯一栈（自 FederatedDriver 下沉 20260822）：表级 QUERY 校验 +
        # 列级读屏蔽。一切 query 路径（聊天/管理端/工具）过同一实现，不再双栈
        sql = self._check_read_perm_and_mask(sql)
        try:
            return self._driver.query(sql)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def _check_read_perm_and_mask(self, sql: str) -> str:
        """读权限校验 + 列级屏蔽（唯一实现，原 FederatedDriver._mask_columns_query 下沉）

        - 表级：policy.check(ds, QUERY)——read_only 放行、custom 空白名单/deny[query] 拦截
        - 列级：显式引用禁列 → PermissionDenied（列名大小写不敏感，re.I——
          SECret 与 secret 同列，不带 I 时大写显式引用可绕过，矩阵复盘发现）；
          SELECT */t.* → 展开为白名单列（全量替换：同表自引用子查询里的第二个
          SELECT * 也要展开，否则子查询星号原样执行导致禁列泄露，矩阵复盘发现。
          取舍：多表各自带星号的嵌套 SQL 可能列错位报错——宁可错拦，不可泄露）
        """
        from core.permission import PermissionDenied
        from core.permission.sql_guard import resolve_datasource
        tables = extract_tables_from_sql(sql)
        if not tables:
            return sql
        policy = PermissionPolicy.get_instance()
        for t in tables:
            ds_name = resolve_datasource(t)
            policy.check(ds_name, Operation.QUERY, "query", table=t)
            denied = policy.denied_columns(ds_name, t, Operation.QUERY)  # 规范形（小写）
            if not denied:
                continue
            # 列访问控制语义（非仅投影屏蔽）：禁列出现在 SQL 任何位置
            #（投影/WHERE/ORDER BY/子查询）一律拒绝——只罩投影会让
            # WHERE password_hash LIKE 'a%' 成为逐位拖库的布尔预言机（评审三轮 B）
            for col in denied:
                if re.search(rf'(?<![\w]){re.escape(col)}(?![\w])', sql, flags=re.I):
                    raise PermissionDenied(
                        f"列 {ds_name}.{t}.{col} 权限不足：禁止 query 访问该列（列级黑名单）")
            # 星号命中判定：SELECT *（任意空白）或 该表的任意别名星号（u.* / USERS.*
            # 全形态）——此前定单子空格检测对 SELECT\n*/SELECT\t* 落空泄露，
            # 与替换正则的 \s+ 不同源（评审五轮安全复核）
            from .security_contract import extract_table_aliases
            aliases = [a for a, tt in extract_table_aliases(sql).items() if tt == t] or [t]
            star_hit = bool(re.search(r'(?i)\bSELECT\s+\*', sql)) or any(
                re.search(rf'(?<![\w]){re.escape(a)}\.\*', sql, flags=re.I)
                for a in aliases)
            if star_hit:
                cols = [c["name"] for c in self._driver.get_columns(t)
                        if c["name"].casefold() not in denied]
                if not cols:
                    raise PermissionDenied(
                        f"表 {ds_name}.{t} 的所有列均被列级规则禁止 query")
                white = ", ".join(safe_column_sql(c) for c in cols)
                sql = re.sub(r'(?i)SELECT\s+\*', f"SELECT {white}", sql)
                for a in aliases:
                    sql = re.sub(rf'(?i)(?<![\w]){re.escape(a)}\.\*', white, sql)
        return sql

    # ── DDL：表结构变更 ──

    def create_table(self, table_config: dict):
        """建表——完整前置校验

        校验内容：
        1. 表名标识符合法性
        2. 主键存在性（必须有 id 字段）
        3. 无重复主键标记（不允许联合主键）
        4. 每个字段名标识符合法性
        5. 每个字段的 CHECK 表达式安全性
        """
        try:
            SecurityContract.validate_identifier(
                table_config.get("name", ""), "表名"
            )
            columns = table_config.get("columns", [])
            SecurityContract.assert_primary_key_exists(columns)
            SecurityContract.assert_no_duplicate_primary_key(columns)
            # 逐字段校验：标识符 + CHECK 表达式
            col_names = [c.get("name", "") for c in columns if isinstance(c, dict)]
            for col in columns:
                if not isinstance(col, dict):
                    continue
                col_name = col.get("name", "")
                if col_name:
                    SecurityContract.validate_identifier(col_name, "字段名")
                check_expr = col.get("check")
                if check_expr:
                    ok, msg = SecurityContract.validate_check_expr(
                        check_expr, col_name, col.get("type", ""), col_names
                    )
                    if not ok:
                        raise SecurityError(
                            f"字段 '{col_name}' 的 CHECK 约束非法: {msg}"
                        )
            self._perm(table_config.get("name", ""), Operation.DDL, "create_table")
            return self._driver.create_table(table_config)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def drop_table(self, table: str):
        """删表——外键引用检查 + 系统表豁免（最底层防线，覆盖工具层/画布 API 所有删除路径）"""
        # 系统表/内部表豁免（大小写归一化，SQLite 表名大小写不敏感）：
        # 防止绕过 schema_manager 直接删认证/基础设施表（security_review HIGH）
        _SYS_TABLES = {"users", "roles", "permissions", "role_permissions", "sessions"}
        _tbl = table.lower()
        if _tbl in _SYS_TABLES or _tbl.startswith(("sqlite_", "meta_")):
            from core.exceptions import SecurityError
            raise SecurityError(f"表 {table} 是系统表，不允许删除")
        try:
            self._perm(table, Operation.DROP, "drop_table")
            SchemaChangeContract.assert_can_drop_table(table, self._driver)
            return self._driver.drop_table(table)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def rename_table(self, table: str, new_name: str):
        """重命名表——标识符校验 + 新表名冲突检查"""
        try:
            self._perm(table, Operation.DDL, "rename_table")
            SecurityContract.validate_identifier(table, "表名")
            SecurityContract.validate_identifier(new_name, "新表名")
            SchemaChangeContract.assert_can_rename_table(table, new_name, self._driver)
            return self._driver.rename_table(table, new_name)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def add_column(
        self,
        table: str,
        column: str,
        col_type: str,
        precision=None,
        not_null: bool = False,
    ):
        """加字段——标识符校验 + 主键保护（禁止加名为 id 的字段）"""
        try:
            SecurityContract.validate_table_and_column(table, column)
            # 禁止通过 add_column 添加名为 id 的字段（id 由建表时自动创建）
            if column.lower() == "id":
                raise PrimaryKeyError(
                    "主键 id 字段由建表时自动创建，不能通过 add_column 添加"
                )
            self._perm(table, Operation.DDL, "add_column")
            return self._driver.add_column(table, column, col_type, precision, not_null)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def drop_column(self, table: str, column: str):
        """删字段——主键保护 + 外键引用检查"""
        try:
            self._perm(table, Operation.DROP, "drop_column")
            self._perm_columns(table, [column], Operation.DROP)
            SchemaChangeContract.assert_can_drop_column(table, column, self._driver)
            return self._driver.drop_column(table, column)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def modify_column(self, table: str, column: str, new_type: str, force: bool = False):
        """改字段类型——主键保护 + 类型变更风险评估 + 数据兼容性扫描

        扩展了 Driver.modify_column 的签名，新增 force 参数。
        调用底层 driver 时不传 force（driver 层无需感知契约）。

        Args:
            table: 表名
            column: 字段名
            new_type: 新类型
            force: 是否强制执行高危变更（默认 False）

        Raises:
            PrimaryKeyError: 字段是主键 id
            SecurityError: 标识符非法
            RiskError: 高危变更且 force=False
        """
        try:
            self._perm(table, Operation.DDL, "modify_column")
            self._perm_columns(table, [column], Operation.DDL)
            # 1. 主键保护
            SecurityContract.assert_not_primary_key(table, column, "修改类型")
            SecurityContract.validate_table_and_column(table, column)

            # 2. 查旧类型
            old_type = self._get_column_type(table, column)

            # 3. 类型变更风险评估
            risk = TypeContract.classify_change_risk(old_type, new_type)
            if risk.requires_force and not force and not self.force_passthrough:
                raise RiskError(
                    risk.message or f"将 {old_type} 改为 {new_type} 存在风险",
                    report={"risk": _risk_to_dict(risk)},
                )

            # 4. 数据兼容性扫描（高危时）
            if risk.requires_data_scan:
                scan = TypeContract.validate_data_compatibility(
                    self._driver, table, column, new_type
                )
                if scan["fail_count"] > 0 and not force and not self.force_passthrough:
                    samples = scan.get("fail_samples", [])[:3]
                    raise RiskError(
                        f"{risk.message}（采样 {scan['scanned']} 行，"
                        f"{scan['fail_count']} 行无法转换：{samples}）",
                        report={"risk": _risk_to_dict(risk), "data_scan": scan},
                    )

            # 5. 执行（底层 driver 无需感知 force）
            return self._driver.modify_column(table, column, new_type)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def alter_precision(
        self,
        table: str,
        column: str,
        new_precision: tuple,
        force: bool = False,
    ):
        """改字段精度——主键保护 + 精度收紧风险提示

        扩展了 Driver.alter_precision 的签名，新增 force 参数。

        Args:
            table: 表名
            column: 字段名
            new_precision: (总长, 小数位)
            force: 是否强制执行精度收紧

        Raises:
            PrimaryKeyError: 字段是主键 id
            SecurityError: 标识符非法
            RiskError: 精度收紧且 force=False
        """
        try:
            self._perm(table, Operation.DDL, "alter_precision")
            self._perm_columns(table, [column], Operation.DDL)
            SecurityContract.assert_not_primary_key(table, column, "修改精度")
            SecurityContract.validate_table_and_column(table, column)

            # 精度收紧风险提示
            old_prec = self._get_column_precision(table, column)
            if old_prec and _is_precision_tightened(old_prec, new_precision) \
                    and not force and not self.force_passthrough:
                raise RiskError(
                    f"精度从 {old_prec} 收紧到 {new_precision}，可能丢失精度数据，需 force=True",
                    report={"old_precision": list(old_prec), "new_precision": list(new_precision)},
                )

            return self._driver.alter_precision(table, column, new_precision)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def add_foreign_key(
        self,
        table: str,
        column: str,
        ref_table: str,
        ref_column: str = "id",
        force: bool = False,
    ):
        """加外键——类型一致性 + 数据采样扫描

        扩展了 Driver.add_foreign_key 的签名，新增 force 参数。

        Args:
            table: 本表名
            column: 本表外键列名
            ref_table: 被引用表名
            ref_column: 被引用列名（默认 id）
            force: 是否强制执行（类型不一致或数据不满足时）

        Raises:
            SecurityError: 标识符非法
            RiskError: 类型不一致或数据不满足约束且 force=False
        """
        try:
            self._perm(table, Operation.DDL, "add_foreign_key")
            SchemaChangeContract.assert_can_add_foreign_key(
                table, column, ref_table, self._driver, ref_column,
                force or self.force_passthrough  # 内层（daemon 侧）不再重复人审闸
            )
            return self._driver.add_foreign_key(table, column, ref_table, ref_column)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def drop_foreign_key(self, table: str, constraint_name: str):
        """删外键——标识符校验"""
        try:
            self._perm(table, Operation.DROP, "drop_foreign_key")
            SecurityContract.validate_identifier(table, "表名")
            SecurityContract.validate_identifier(constraint_name, "外键约束名")
            return self._driver.drop_foreign_key(table, constraint_name)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def create_index(self, table: str, columns: str, unique: bool = False):
        """创建索引——标识符校验"""
        try:
            self._perm(table, Operation.DDL, "create_index")
            SecurityContract.validate_identifier(table, "表名")
            # columns 可能是 "col1,col2" 联合索引
            for col in str(columns).split(","):
                col = col.strip()
                if col:
                    SecurityContract.validate_identifier(col, "索引列名")
            return self._driver.create_index(table, columns, unique)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def drop_index(self, name: str):
        """删索引——标识符校验"""
        try:
            self._perm("", Operation.DROP, "drop_index")  # 索引名全局唯一无表名，按默认数据源判定
            SecurityContract.validate_identifier(name, "索引名")
            return self._driver.drop_index(name)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def recreate_table(self, table_config: dict):
        """重建表——主键存在性校验

        recreate_table 是高危操作（数据迁移），调用方应自行确认。
        契约层只做基础主键校验，不强制 force（因为通常是内部调用）。
        """
        try:
            self._perm(table_config.get("name", ""), Operation.DDL, "recreate_table")
            SecurityContract.assert_primary_key_exists(
                table_config.get("columns", [])
            )
            SecurityContract.validate_identifier(
                table_config.get("name", ""), "表名"
            )
            return self._driver.recreate_table(table_config)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    def execute(self, sql: str):
        """执行任意 SQL——透传 + 纵深加固 + 审计日志

        警告：此方法绕过常规契约校验，仅供内部多步骤操作使用。
        加固（防滥用为注入后门）：
        - 强制单语句 / 禁止 SQL 注释 / 首关键字白名单（见 _validate_execute_sql）
        - 每次调用写 logger.warning 审计日志（调用位置 + SQL 前 100 字符）
        上层不应直接调用，应使用具体的 DML/DDL 方法。
        """
        # 审计：谁调了 execute（上一层调用帧）
        frame = inspect.currentframe()
        caller = "unknown"
        if frame is not None and frame.f_back is not None:
            caller = f"{frame.f_back.f_code.co_filename}:{frame.f_back.f_lineno}"
        logger.warning("ContractDriver.execute 审计: caller=%s sql=%.100s",
                       caller, str(sql))
        _validate_execute_sql(sql)
        # 权限护栏：裸 SQL 写/DDL 同样受 permissions.yml 约束（识别不了的读/管理语句放行）
        from core.permission.sql_guard import guard_write_sql
        guard_write_sql(sql)
        try:
            return self._driver.execute(sql)
        except _CONTRACT_ERRORS:
            raise
        except Exception as e:
            raise ErrorTranslator.translate(self._driver_type, e)

    # ── 内部辅助 ──

    def _get_column_type(self, table: str, column: str) -> str:
        """获取字段当前类型

        Args:
            table: 表名
            column: 字段名

        Returns:
            类型字符串（大写），未找到则返回 "TEXT"
        """
        try:
            for c in self._driver.get_columns(table):
                if c.get("name", "").lower() == column.lower():
                    return (c.get("type") or "TEXT").upper()
        except Exception:
            pass
        return "TEXT"

    def _get_column_precision(self, table: str, column: str) -> Optional[tuple]:
        """从字段类型字符串解析精度

        如 DECIMAL(10,2) → (10, 2)，INTEGER → None

        Args:
            table: 表名
            column: 字段名

        Returns:
            (总长, 小数位) 或 None
        """
        col_type = self._get_column_type(table, column)
        m = re.search(r'\((\d+)\s*,\s*(\d+)\)', col_type)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        m = re.search(r'\((\d+)\)', col_type)
        if m:
            return (int(m.group(1)), 0)
        return None


# ── 模块级辅助函数 ──

def _risk_to_dict(risk) -> dict:
    """把 TypeChangeRisk 序列化为 dict（用于 RiskError.report）"""
    return {
        "level": risk.level,
        "message": risk.message,
        "requires_force": risk.requires_force,
        "requires_data_scan": risk.requires_data_scan,
        "old_family": risk.old_family,
        "new_family": risk.new_family,
    }


def _is_precision_tightened(old_prec, new_prec) -> bool:
    """判断精度是否收紧（总长或小数位变小）

    Args:
        old_prec: 旧精度 (总长, 小数位)
        new_prec: 新精度 (总长, 小数位)

    Returns:
        True 如果新精度比旧精度小（收紧）
    """
    try:
        if isinstance(old_prec, (list, tuple)) and isinstance(new_prec, (list, tuple)):
            if len(old_prec) >= 1 and len(new_prec) >= 1:
                if new_prec[0] < old_prec[0]:
                    return True
            if len(old_prec) >= 2 and len(new_prec) >= 2:
                if new_prec[1] < old_prec[1]:
                    return True
    except Exception:
        pass
    return False
