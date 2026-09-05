"""数据 CRUD 契约——所有 DML 操作的前置校验

职责：
1. 插入前校验（validate_insert）：标识符、字段存在性、类型匹配、批量上限
2. 更新前校验（validate_update）：WHERE 必须含主键、类型匹配、影响行数上限
3. 删除前校验（validate_delete）：WHERE 必填、必须含主键、影响行数上限
4. 单值类型校验 + 数值清洗（_validate_and_clean_value）

设计原则：
- 静态方法，无状态
- 不执行 SQL，只做校验（影响行数预估除外，那是只读 SELECT COUNT）
- 校验失败抛 SecurityError（标识符/类型）或 RiskError（影响行数超限）
- 数值清洗规则与 TypeContract.INVALID_NUMERIC 保持一致

从 sqlite_driver / mysql_driver 提取，统一管理。新增 driver 时无需重复实现。
"""
import re

from .security_contract import safe_table_sql, SecurityContract, split_set_pairs
from .type_contract import TypeContract
from core.exceptions import SecurityError, RiskError


class DataCrudContract:
    """数据 CRUD 契约——所有 DML 操作的前置校验"""

    # 批量操作上限（防止误操作导致大规模数据变更）
    MAX_INSERT_BATCH = 1000
    MAX_UPDATE_DELETE_AFFECTED = 10000

    # ── 插入校验 ──

    @staticmethod
    def validate_insert(drv, table: str, rows: list) -> list:
        """插入前校验：标识符、字段存在性、类型匹配、批量上限

        Args:
            drv: Driver 实例（用于查询列类型）
            table: 表名
            rows: 待插入的行列表

        Returns:
            清洗后的行列表（移除 id 字段、数值清洗后的值）

        Raises:
            SecurityError: 标识符非法 / 字段不存在 / 类型不匹配 / 批量超限
        """
        SecurityContract.validate_identifier(table, "表名")
        if not isinstance(rows, list):
            raise SecurityError("插入数据必须为列表")
        if not rows:
            return []
        if len(rows) > DataCrudContract.MAX_INSERT_BATCH:
            raise SecurityError(
                f"单次插入上限 {DataCrudContract.MAX_INSERT_BATCH} 行，当前 {len(rows)} 行"
            )

        # 获取列类型映射（列名小写 → 类型大写）
        try:
            col_types = {
                c["name"].lower(): (c.get("type") or "").upper()
                for c in drv.get_columns(table)
            }
        except Exception as e:
            raise SecurityError(f"获取表 '{table}' 列信息失败: {str(e)[:100]}")

        if not col_types:
            raise SecurityError(f"表 '{table}' 无字段信息")

        cleaned_rows = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                raise SecurityError(f"第 {idx + 1} 行数据不是 dict")
            cleaned = {}
            for k, v in row.items():
                # 移除系统列 id（让自增）
                if k.lower() == "id":
                    continue
                if k.lower() not in col_types:
                    raise SecurityError(
                        f"字段 '{k}' 不存在于表 '{table}'（第 {idx + 1} 行）"
                    )
                # 类型校验 + 数值清洗
                cleaned[k] = DataCrudContract._validate_and_clean_value(
                    k, v, col_types[k.lower()], row_idx=idx + 1
                )
            cleaned_rows.append(cleaned)
        return cleaned_rows

    # ── 更新校验 ──

    @staticmethod
    def validate_update(drv, table: str, set_clause: str, where: str) -> tuple:
        """更新前校验：WHERE 必须含主键、类型匹配、影响行数上限

        Args:
            drv: Driver 实例
            table: 表名
            set_clause: SET 子句（如 "name='张三', age=30"）
            where: WHERE 子句

        Returns:
            (set_clause, where) 校验通过的值

        Raises:
            SecurityError: 标识符非法 / WHERE 不含主键 / 类型不匹配
            RiskError: 影响行数超限
        """
        SecurityContract.validate_identifier(table, "表名")
        if not set_clause or not set_clause.strip():
            raise SecurityError("SET 子句不能为空")
        if not where or not where.strip():
            raise SecurityError("更新操作必须指定 WHERE 条件，禁止全表更新")

        SecurityContract.validate_where(where)

        # WHERE 必须包含主键或唯一索引列（防误更新全表）
        if not DataCrudContract._where_has_primary_key(where, drv, table):
            raise SecurityError(
                "更新操作必须指定主键（id）或唯一索引条件，防止误更新大批数据"
            )

        # 预估影响行数——COUNT 失败必须 fail-closed（
        # 曾 except 吞掉按 0 行放行，上限安全闸的 fail 方向被取反）
        try:
            count_sql = f'SELECT COUNT(*) AS c FROM {safe_table_sql(table)} WHERE {where}'
            rows = drv.query(count_sql)
            affected = rows[0].get("c", 0) if rows else 0
        except Exception as e:
            raise SecurityError(
                f"影响行数预估失败（{e}）——上限闸守不住时宁可不执行") from e
        if affected > DataCrudContract.MAX_UPDATE_DELETE_AFFECTED:
            raise RiskError(
                f"更新影响 {affected} 行，超过上限 "
                f"{DataCrudContract.MAX_UPDATE_DELETE_AFFECTED}，请缩小范围",
                report={"affected": affected, "limit": DataCrudContract.MAX_UPDATE_DELETE_AFFECTED},
                forceable=False,
            )

        # 解析 SET 子句并校验类型（基础校验，不解析复杂表达式）
        DataCrudContract._validate_set_clause(drv, table, set_clause)

        return set_clause, where

    # ── 删除校验 ──

    @staticmethod
    def validate_delete(drv, table: str, where: str) -> str:
        """删除前校验：WHERE 必填、必须含主键、影响行数上限

        Args:
            drv: Driver 实例
            table: 表名
            where: WHERE 子句

        Returns:
            校验通过的 WHERE 子句

        Raises:
            SecurityError: WHERE 为空 / 不含主键
            RiskError: 影响行数超限
        """
        SecurityContract.validate_identifier(table, "表名")
        if not where or not where.strip():
            raise SecurityError("删除操作必须指定 WHERE 条件，禁止全表删除")

        SecurityContract.validate_where(where)

        if not DataCrudContract._where_has_primary_key(where, drv, table):
            raise SecurityError(
                "删除操作必须指定主键（id）或唯一索引条件，防止误删除大批数据"
            )

        # 预估影响行数——COUNT 失败 fail-closed（同 validate_update）
        try:
            count_sql = f'SELECT COUNT(*) AS c FROM {safe_table_sql(table)} WHERE {where}'
            rows = drv.query(count_sql)
            affected = rows[0].get("c", 0) if rows else 0
        except Exception as e:
            raise SecurityError(
                f"影响行数预估失败（{e}）——上限闸守不住时宁可不执行") from e
        if affected > DataCrudContract.MAX_UPDATE_DELETE_AFFECTED:
            raise RiskError(
                f"删除影响 {affected} 行，超过上限 "
                f"{DataCrudContract.MAX_UPDATE_DELETE_AFFECTED}，请缩小范围",
                report={"affected": affected, "limit": DataCrudContract.MAX_UPDATE_DELETE_AFFECTED},
                forceable=False,
            )
        return where

    # ── 内部辅助 ──

    @staticmethod
    def _validate_and_clean_value(field: str, value, col_type: str, row_idx: int = 0):
        """单个值的类型校验 + 数值清洗

        从 sqlite_driver.insert/update 提取，统一规则。

        Args:
            field: 字段名
            value: 原始值
            col_type: 字段类型（大写）
            row_idx: 行号（用于错误信息）

        Returns:
            清洗后的值

        Raises:
            SecurityError: 类型不匹配
        """
        if value is None:
            return None
        if not col_type:
            return value

        # 数值类型清洗
        if TypeContract.is_numeric_type(col_type):
            cleaned = TypeContract.clean_numeric_value(value)
            if cleaned is None:
                return None
            if cleaned is not value:
                value = cleaned

            # INT / BOOL 类型校验
            if "INT" in col_type or "BOOL" in col_type:
                if isinstance(value, str):
                    try:
                        int(value)
                    except ValueError:
                        raise SecurityError(
                            f"字段 '{field}' 类型为 {col_type}，"
                            f"但收到了非整数值 '{value}'（第 {row_idx} 行）"
                        )
            # FLOAT / REAL / DOUBLE / DECIMAL 类型校验
            elif any(t in col_type for t in ("FLOAT", "REAL", "DOUBLE", "DECIMAL", "NUMERIC")):
                if isinstance(value, str):
                    try:
                        float(value)
                    except ValueError:
                        raise SecurityError(
                            f"字段 '{field}' 类型为 {col_type}，"
                            f"但收到了非数值 '{value}'（第 {row_idx} 行）"
                        )
            return value

        # 文本类型：直接放行（任何值都可转字符串）
        if TypeContract.get_type_family(col_type) == "text":
            return value

        # 日期/时间类型：基础格式校验（不严格）
        # 让 driver 自己处理转换，契约层不阻断
        return value

    @staticmethod
    def _where_has_primary_key(where: str, drv, table: str) -> bool:
        """检查 WHERE 是否包含主键列或唯一索引列

        简单规则：WHERE 子句中是否出现 "id" 或唯一索引列名。
        不做完整 SQL 解析，只做关键字检测（够用且高效）。

        Args:
            where: WHERE 子句
            drv: Driver 实例
            table: 表名

        Returns:
            True 如果 WHERE 包含主键或唯一索引列
        """
        if not where:
            return False
        w = where.lower()
        # 顶层 OR 即不收敛（"id=1 OR id>0" 让主键条件形同虚设——
        # 启发式只在"条件收敛到主键/唯一键"语义下成立；字面值剥离后仍有 OR 即否决）
        w_nostr = re.sub(r"'([^']|'')*'", "''", w)          # 单引号字面值
        w_nostr = re.sub(r'"([^"]|"")*"', '""', w_nostr)    # 双引号标识符
        if re.search(r'\bor\b', w_nostr):
            return False
        # 主键 id 检测
        if re.search(r'\bid\b\s*(=|in|between)', w):
            return True
        # 唯一列检测（三路并查，覆盖最严）：
        # 1) get_columns 上报的 unique 标记  2) 物理唯一索引（PRAGMA，sqlite/mysql 各有实现）
        # 3) 行业 YAML 声明的唯一业务键（insert 冲突检测同口径 _get_unique_key_column）
        uniq_cols = set()
        try:
            for c in drv.get_columns(table):
                if c.get("unique") or c.get("is_unique"):
                    uniq_cols.add(c.get("name", "").lower())
        except Exception:
            pass  # 列级唯一标记读取失败则由索引/主键通道补判
        try:
            if hasattr(drv, "get_indexes"):
                for idx in drv.get_indexes(table):
                    if idx.get("unique"):
                        for col in idx.get("columns", []):
                            uniq_cols.add(str(col).lower())
        except Exception:
            pass  # 索引唯一性读取失败则由其余通道补判
        try:
            if hasattr(drv, "_get_unique_key_column"):
                kc = drv._get_unique_key_column(table)
                if kc:
                    uniq_cols.add(str(kc).lower())
        except Exception:
            pass  # 主键列补判失败则按无唯一列（要求显式选择集）
        for col_name in uniq_cols:
            if col_name and re.search(rf'\b{re.escape(col_name)}\b\s*(=|in|between)', w):
                return True
        return False

    @staticmethod
    def _validate_set_clause(drv, table: str, set_clause: str) -> None:
        """校验 SET 子句中的字段名和类型

        基础校验：解析 "field=value, field2=value2" 格式，
        检查字段存在性 + 类型匹配。不支持复杂表达式（如 field=field+1）。

        Args:
            drv: Driver 实例
            table: 表名
            set_clause: SET 子句

        Raises:
            SecurityError: 字段不存在或类型不匹配
        """
        # 获取列类型映射——获取失败必须 fail-closed（
        # 曾静默 return 让"字段存在性+主键禁改+类型校验"整闸关闭）
        try:
            col_types = {
                c["name"].lower(): (c.get("type") or "").upper()
                for c in drv.get_columns(table)
            }
        except Exception as e:
            raise SecurityError(f"列信息获取失败（{e}）——SET 校验无法进行，不执行") from e

        # 全仓唯一 SET 解析器（split_set_pairs：引号感知，值内逗号/等号不误切；
        # 带引号的 "id"=5 也能识别主键——朴素 split+正则曾被二者击穿）
        for field, value_str in split_set_pairs(set_clause):
            if not field or not value_str:
                continue
            field_l = field.lower()
            if field_l not in col_types:
                raise SecurityError(f"SET 子句中字段 '{field}' 不存在于表 '{table}'")
            # 主键保护：禁止修改 id
            if field_l == "id":
                raise SecurityError("主键 id 字段不允许通过 UPDATE 修改")
            # 类型校验（仅对字面值，非字段引用）
            col_type = col_types[field_l]
            DataCrudContract._validate_set_value(field_l, value_str, col_type)

    @staticmethod
    def _validate_set_value(field: str, value_str: str, col_type: str) -> None:
        """校验 SET 子句中的字面值类型

        Args:
            field: 字段名
            value_str: 值字符串（如 "'张三'" / "30" / "NULL"）
            col_type: 字段类型（大写）

        Raises:
            SecurityError: 类型不匹配
        """
        v = value_str.strip()
        # NULL / DEFAULT 放行
        if v.upper() in ("NULL", "DEFAULT", "?"):
            return
        # 字段引用（如 field=other_field）放行，让 driver 处理
        if re.match(r'^[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)?$', v):
            return
        # 函数调用放行
        if re.match(r'^[a-zA-Z_]+\s*\(', v):
            return

        # 提取字面值
        if v.startswith("'") and v.endswith("'"):
            literal = v[1:-1]
            is_str = True
        elif v.startswith('"') and v.endswith('"'):
            literal = v[1:-1]
            is_str = True
        else:
            literal = v
            is_str = False

        # 数值类型校验
        if TypeContract.is_numeric_type(col_type):
            if "INT" in col_type or "BOOL" in col_type:
                if is_str:
                    try:
                        int(literal)
                    except ValueError:
                        raise SecurityError(
                            f"字段 '{field}' 类型为 {col_type}，但 SET 值 '{literal}' 非整数"
                        )
                # 非字符串的数字字面量放行
            elif any(t in col_type for t in ("FLOAT", "REAL", "DOUBLE", "DECIMAL", "NUMERIC")):
                if is_str:
                    try:
                        float(literal)
                    except ValueError:
                        raise SecurityError(
                            f"字段 '{field}' 类型为 {col_type}，但 SET 值 '{literal}' 非数值"
                        )
