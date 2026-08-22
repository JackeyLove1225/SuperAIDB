"""跨库 JOIN 编排器——应用层实现跨数据源的关联查询

核心思路：
跨库 JOIN 无法下推到单个数据库执行，需要在应用层分步查询 + 内存合并。

流程：
1. 解析涉及的表，获取每张表的数据源
2. 如果全部在同一数据源 → 返回 None（交给原生 join_query）
3. 如果跨库 → 应用层编排：
   a. 分别从各自数据源查询主表和关联表的数据
   b. 通过外键关系在内存中做 JOIN（哈希索引加速）
   c. 应用 WHERE 过滤（跨表条件在内存中执行）
   d. 选择指定字段
   e. 限制结果集（默认 100 行，防止内存溢出）

限制：
- 跨库 JOIN 结果集限制 100 行
- 跨库 JOIN 不支持跨库事务
- WHERE 条件中的跨表比较需在内存中执行
"""

import re
from typing import Optional

from core.contract.security_contract import safe_table_sql

# 跨库 JOIN 最大返回行数（防止内存溢出）
FEDERATED_JOIN_LIMIT = 100


def _all_schema_tables() -> list[str]:
    """获取当前行业 schema 中定义的所有表名"""
    try:
        from core.schema_matcher import _load_schemas
        return [t["name"] for t in _load_schemas()]
    except Exception:
        return []


def _is_cross_datasource(tables: list[str]) -> bool:
    """判断给定的表是否分布在多个数据源"""
    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()
    datasources = set()
    for t in tables:
        ds = dsm.get_datasource_for_table(t)
        datasources.add(ds)
    return len(datasources) > 1


def _query_single_table(table: str, where: str = "") -> list[dict]:
    """从表所属的数据源查询数据（单表）

    Args:
        table: 表名
        where: 仅涉及该表的 WHERE 条件（已拆分）

    Returns:
        行列表（dict 格式）
    """
    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()
    drv = dsm.get_driver_for_table(table)

    sql = f'SELECT * FROM {safe_table_sql(table)}'
    if where:
        # 安全校验 WHERE
        if hasattr(drv, '_safe_where') and not drv._safe_where(where):
            raise ValueError(f"WHERE 条件不安全: {where}")
        sql += f" WHERE {where}"
    sql += f" LIMIT {FEDERATED_JOIN_LIMIT}"

    return drv.query(sql)


def _fetch_side(table: str, where: str, role: str = "关联表") -> tuple[Optional[list[dict]], Optional[str]]:
    """单侧取数：查询一侧表的数据，失败时返回错误消息而非抛异常

    Args:
        table: 表名
        where: 仅涉及该表的 WHERE 条件（已拆分）
        role: 表角色（主表/中间表/关联表），用于错误消息

    Returns:
        (行列表, None) 成功；(None, 错误消息) 失败
    """
    try:
        return _query_single_table(table, where), None
    except Exception as e:
        return None, f"跨库查询失败：查询{role} {table} 出错 - {e}"


def _memory_join(
    left_rows: list[dict],
    right_rows: list[dict],
    left_table: str,
    right_table: str,
    left_col: str,
    right_col: str,
    join_type: str = "INNER",
) -> list[dict]:
    """在内存中执行 JOIN 操作

    使用哈希索引加速：先对 right_rows 按 right_col 建立索引，
    再遍历 left_rows 进行匹配。

    Args:
        left_rows: 左表数据
        right_rows: 右表数据
        left_table: 左表名（用于字段前缀）
        right_table: 右表名（用于字段前缀）
        left_col: 左表的 JOIN 列
        right_col: 右表的 JOIN 列
        join_type: INNER 或 LEFT

    Returns:
        JOIN 后的行列表，字段格式为 {table.column: value}
    """
    # 对右表建立哈希索引：right_col 的值 → 行列表
    right_index = {}
    for row in right_rows:
        key = row.get(right_col)
        if key is not None:
            right_index.setdefault(key, []).append(row)

    result = []
    for left_row in left_rows:
        key = left_row.get(left_col)
        matches = right_index.get(key, []) if key is not None else []

        if matches:
            # 有匹配——合并左行和右行
            for right_row in matches:
                merged = {}
                # 左表字段加 table. 前缀
                for k, v in left_row.items():
                    merged[f"{left_table}.{k}"] = v
                # 右表字段加 table. 前缀
                for k, v in right_row.items():
                    merged[f"{right_table}.{k}"] = v
                result.append(merged)
        elif join_type == "LEFT":
            # LEFT JOIN：左表无匹配也保留，右表字段为 None
            merged = {}
            for k, v in left_row.items():
                merged[f"{left_table}.{k}"] = v
            # 右表字段填充 None
            if right_rows:
                for k in right_rows[0].keys():
                    merged[f"{right_table}.{k}"] = None
            result.append(merged)

    return result


def _split_where_by_table(where: str, tables: list[str]) -> dict[str, str]:
    """将 WHERE 条件按表拆分

    解析 WHERE 中的 AND/OR 条件，根据条件中引用的 table.column 前缀
    分配到对应的表。不带表前缀的条件归入主表。

    Returns:
        {table_name: where_clause} 映射
    """
    if not where:
        return {t: "" for t in tables}

    # 简化处理：按 AND 拆分（不支持复杂 OR 逻辑的拆分）
    conditions = re.split(r'\s+AND\s+', where, flags=re.IGNORECASE)

    result = {t: [] for t in tables}
    cross_table_conditions = []

    for cond in conditions:
        cond = cond.strip()
        if not cond:
            continue

        # 检查条件中引用了哪些表
        referenced_tables = set()
        for t in tables:
            if re.search(r'\b' + re.escape(t) + r'\.', cond):
                referenced_tables.add(t)

        if len(referenced_tables) == 1:
            # 单表条件——分配到对应表
            t = referenced_tables.pop()
            result[t].append(cond)
        elif len(referenced_tables) > 1:
            # 跨表条件——需要内存执行
            cross_table_conditions.append(cond)
        else:
            # 无表前缀——归入主表（第一个表）
            result[tables[0]].append(cond)

    # 跨表条件记录到主表（稍后在内存中过滤）
    if cross_table_conditions:
        result["__cross__"] = " AND ".join(cross_table_conditions)

    # 合并各表的条件
    for t in result:
        if t == "__cross__":
            continue
        if isinstance(result[t], list):
            result[t] = " AND ".join(result[t])

    return result


def _apply_cross_filter(rows: list[dict], cross_condition: str, tables: list[str]) -> list[dict]:
    """在内存中应用跨表 WHERE 条件

    简化实现：只支持简单的 table.column OP value 格式
    """
    if not cross_condition or not rows:
        return rows

    filtered = []
    conditions = re.split(r'\s+AND\s+', cross_condition, flags=re.IGNORECASE)

    for row in rows:
        match_all = True
        for cond in conditions:
            cond = cond.strip()
            if not _evaluate_condition(row, cond):
                match_all = False
                break
        if match_all:
            filtered.append(row)

    return filtered


def _evaluate_condition(row: dict, cond: str) -> bool:
    """评估单个条件是否满足

    支持：table.column = value, table.column > value 等简单比较
    """
    # 解析条件：table.column OP value
    m = re.match(
        r'(["`]?(\w+)["`]?\.["`]?(\w+)["`]?)\s*(=|!=|<>|>=|<=|>|<|LIKE)\s*(.+)',
        cond.strip()
    )
    if not m:
        return True  # 无法解析的条件默认通过

    field_key = f"{m.group(2)}.{m.group(3)}"
    op = m.group(4)
    value_str = m.group(5).strip().strip('"').strip("'")

    if field_key not in row:
        return False

    row_value = row[field_key]
    if row_value is None:
        return False

    # 类型转换
    try:
        if isinstance(row_value, (int, float)):
            value = float(value_str)
        else:
            value = value_str
    except ValueError:
        value = value_str

    if op == "=":
        try:
            return float(row_value) == float(value)
        except (ValueError, TypeError):
            return str(row_value) == str(value)
    elif op in ("!=", "<>"):
        try:
            return float(row_value) != float(value)
        except (ValueError, TypeError):
            return str(row_value) != str(value)
    elif op == ">":
        try:
            return float(row_value) > float(value)
        except (ValueError, TypeError):
            return str(row_value) > str(value)
    elif op == "<":
        try:
            return float(row_value) < float(value)
        except (ValueError, TypeError):
            return str(row_value) < str(value)
    elif op == ">=":
        try:
            return float(row_value) >= float(value)
        except (ValueError, TypeError):
            return str(row_value) >= str(value)
    elif op == "<=":
        try:
            return float(row_value) <= float(value)
        except (ValueError, TypeError):
            return str(row_value) <= str(value)
    elif op.upper() == "LIKE":
        pattern = value_str.replace("%", ".*").replace("_", ".")
        return bool(re.search(pattern, str(row_value)))

    return True


def _select_fields(rows: list[dict], select_fields: str, tables: list[str]) -> list[dict]:
    """从 JOIN 结果中选择指定字段

    Args:
        rows: JOIN 后的行（字段格式为 table.column）
        select_fields: 字段列表（如 "t1.code, t2.name" 或 "*"）
        tables: 涉及的表名列表
    """
    if not rows or select_fields.strip() == "*":
        return rows

    # 解析字段列表
    fields = [f.strip() for f in select_fields.split(",") if f.strip()]
    # 规范化为 table.column 格式
    normalized_fields = []
    for f in fields:
        if "." in f:
            # 带 table.* 格式 → 跳过（已在外层处理）
            if f.endswith(".*"):
                continue
            normalized_fields.append(f)
        else:
            # 无表前缀——尝试从所有表中查找
            for t in tables:
                key = f"{t}.{f}"
                if key in rows[0]:
                    normalized_fields.append(key)
                    break

    # 如果规范化后没有匹配字段（AI 传了不存在的字段名），回退为返回全部字段
    if not normalized_fields:
        return rows

    result = []
    for row in rows:
        selected = {}
        for f in normalized_fields:
            if f in row:
                # 去掉表前缀，只保留列名
                col_name = f.split(".", 1)[1]
                selected[col_name] = row[f]
        result.append(selected)

    return result


def _find_direct_relation(jt: str, joined_tables: set) -> tuple[Optional[tuple], Optional[str]]:
    """在已 JOIN 的表中查找与新表 jt 的直接外键关系

    Returns:
        (relation, rel_with)：relation 为 (from_table, from_col, to_table, to_col)，
        rel_with 为与 jt 关联的已 JOIN 表名；未找到返回 (None, None)
    """
    from core.data_ops import _find_fk_relation
    for joined_table in joined_tables:
        relation = _find_fk_relation(joined_table, jt)
        if relation:
            return relation, joined_table
    return None, None


def _iter_transitive_paths(jt: str, joined_tables: set):
    """惰性枚举传递外键路径候选：已JOIN的表 → 中间表 → jt

    例如 patient 和 diagnosis 无直接外键，但通过 visit 关联。

    Yields:
        (mid_table, mid_rel_to_joined, mid_joined)：中间表名、
        已JOIN表→中间表的外键关系、对应的已JOIN表名
    """
    from core.data_ops import _find_fk_relation
    for mid_table in _all_schema_tables():
        if mid_table in joined_tables or mid_table == jt:
            continue
        if not _find_fk_relation(mid_table, jt):
            continue
        for jt2 in joined_tables:
            r = _find_fk_relation(jt2, mid_table)
            if r:
                yield mid_table, r, jt2
                break


def _resolve_join_keys(relation: tuple, right_table: str, left_table: str) -> tuple[str, str]:
    """根据外键关系确定 JOIN 方向与左右键

    Args:
        relation: (from_table, from_col, to_table, to_col)，from_table 有 FK 指向 to_table
        right_table: 新表名（右侧，原始字段名）
        left_table: 已 JOIN 侧的表名（左侧，字段带 table. 前缀）

    Returns:
        (left_key, right_key)：left_key 带表前缀，right_key 为原始列名
    """
    from_table, from_col, to_table, to_col = relation
    if from_table == right_table:
        # 新表有 FK 指向已 JOIN 表
        # 左键 = left_table.to_col，右键 = right_table.from_col
        return f"{left_table}.{to_col}", from_col
    # 已 JOIN 表有 FK 指向新表
    # 左键 = left_table.from_col，右键 = right_table.to_col
    return f"{left_table}.{from_col}", to_col


def federated_join(
    main_table: str,
    join_tables: str,
    select_fields: str = "*",
    where: str = "",
    join_type: str = "INNER",
) -> Optional[str]:
    """跨库 JOIN 编排器

    Args:
        main_table: 主表名
        join_tables: 关联表名（逗号分隔）
        select_fields: 查询字段
        where: WHERE 条件
        join_type: INNER 或 LEFT

    Returns:
        查询结果字符串，或 None（表示应走原生 join_query）
    """
    join_list = [t.strip() for t in join_tables.split(",") if t.strip()] if join_tables else []
    all_tables = [main_table] + join_list

    # 检查是否跨库
    if not _is_cross_datasource(all_tables):
        return None  # 同库——交给原生 join_query

    from core.datasource_manager import DataSourceManager
    dsm = DataSourceManager()

    # 1. 拆分 WHERE 条件
    where_parts = _split_where_by_table(where, all_tables)
    cross_condition = where_parts.pop("__cross__", "")

    # 2. 查询主表数据（带主表的 WHERE 条件）
    main_where = where_parts.get(main_table, "")
    main_rows, err = _fetch_side(main_table, main_where, "主表")
    if err:
        return err

    if not main_rows:
        return f"主表 {main_table} 无匹配数据"

    # 3. 逐个关联表进行内存 JOIN
    current_rows = []
    # 初始化：主表数据加 table. 前缀
    for row in main_rows:
        prefixed = {f"{main_table}.{k}": v for k, v in row.items()}
        current_rows.append(prefixed)

    # 已 JOIN 的表集合（用于查找传递外键关系，如 c→b→a）
    joined_tables = {main_table}

    for jt in join_list:
        # 查找外键关系：在所有已 JOIN 的表中查找与新表 jt 的直接外键关系
        # 支持传递关系：c 关联 b（而非 main_table a）
        relation, rel_with = _find_direct_relation(jt, joined_tables)

        if not relation:
            # 尝试查找传递路径：jt → 中间表 → 已JOIN的表
            # 例如 patient 和 diagnosis 无直接外键，但通过 visit 关联
            mid_found = False
            for mid_table, mid_rel_to_joined, mid_joined in _iter_transitive_paths(jt, joined_tables):
                # 找到传递路径：joined → mid_table → jt
                # 先 JOIN mid_table，再 JOIN jt
                # 插入 mid_table 到 join_list 前面（通过递归处理）
                join_list.insert(join_list.index(jt), mid_table)
                # 更新 where_parts
                if mid_table not in where_parts:
                    where_parts[mid_table] = ""
                # 重新处理 mid_table
                mid_jt_where = where_parts.get(mid_table, "")
                mid_rows, err = _fetch_side(mid_table, mid_jt_where, "中间表")
                if err:
                    return err
                # JOIN mid_table
                m_left_key, m_right_key = _resolve_join_keys(mid_rel_to_joined, mid_table, mid_joined)
                current_rows = _memory_join_with_prefixed(
                    current_rows, mid_rows,
                    mid_joined, mid_table,
                    m_left_key, m_right_key, join_type)
                joined_tables.add(mid_table)
                # 现在重新查找 jt 与已 JOIN 表的关系
                relation, rel_with = _find_direct_relation(jt, joined_tables)
                if relation:
                    mid_found = True
                    break
            if not mid_found:
                return (f"未找到 {jt} 与已关联表({', '.join(sorted(joined_tables))})之间的外键关系，"
                        f"无法自动推断跨库 JOIN 条件")

        # 查询关联表数据（带该表的 WHERE 条件）
        jt_where = where_parts.get(jt, "")
        jt_rows, err = _fetch_side(jt, jt_where, "关联表")
        if err:
            return err

        # 确定 JOIN 方向：
        # current_rows（已 JOIN，字段带 table. 前缀）在左，jt_rows（新表，原始字段）在右
        left_key, right_key = _resolve_join_keys(relation, jt, rel_with)

        current_rows = _memory_join_with_prefixed(
            current_rows, jt_rows,
            rel_with, jt,
            left_key, right_key,
            join_type
        )
        joined_tables.add(jt)

    # 4. 应用跨表 WHERE 过滤
    if cross_condition:
        current_rows = _apply_cross_filter(current_rows, cross_condition, all_tables)

    # 5. 限制结果集
    total_count = len(current_rows)
    if total_count > FEDERATED_JOIN_LIMIT:
        current_rows = current_rows[:FEDERATED_JOIN_LIMIT]

    # 6. 选择字段
    if select_fields.strip() != "*":
        selected_rows = _select_fields(current_rows, select_fields, all_tables)
        # 列完整性保护：检查选择后的字段是否覆盖所有涉及表
        # _select_fields 会去掉 table. 前缀，所以 selected_rows 的列名是无前缀的
        # 如果某个表的所有字段都被丢弃，回退为返回全部字段
        if selected_rows and selected_rows[0] and current_rows and current_rows[0]:
            selected_col_names = set(selected_rows[0].keys())
            missing_tables = []
            for t in all_tables:
                # current_rows 中该表的所有字段（去掉 table. 前缀）
                table_cols = {k.split(".", 1)[1] for k in current_rows[0].keys() if k.startswith(f"{t}.")}
                # 检查是否有至少一个字段被选中
                if table_cols and not (table_cols & selected_col_names):
                    missing_tables.append(t)
            if missing_tables:
                selected_rows = current_rows  # 回退为全部字段
        current_rows = selected_rows

    # 7. 格式化结果
    from core.db_chat import DBChat
    chat = DBChat()
    ds_info = dsm.get_datasource_for_table(main_table)
    for jt in join_list:
        jt_ds = dsm.get_datasource_for_table(jt)
        if jt_ds != ds_info:
            ds_info += f" + {jt_ds}"

    header = f"跨库 JOIN 结果（数据源: {ds_info}）"
    if total_count > FEDERATED_JOIN_LIMIT:
        header += f" — 共 {total_count} 条，已截断为前 {FEDERATED_JOIN_LIMIT} 条"

    formatted = chat._format_multi_table({"跨库JOIN结果": current_rows})
    return f"{header}\n{formatted}"


def _match_rows(
    left_rows: list[dict],
    right_rows: list[dict],
    left_key: str,
    right_key: str,
    join_type: str = "INNER",
) -> list[tuple[dict, Optional[dict]]]:
    """按键匹配左右行（哈希索引加速）

    先对 right_rows 按 right_key 建立索引，再遍历 left_rows 配对。

    Args:
        left_rows: 左表数据
        right_rows: 右表数据
        left_key: 左行中的 JOIN 键
        right_key: 右行中的 JOIN 键
        join_type: INNER 或 LEFT

    Returns:
        (左行, 右行) 配对列表；LEFT JOIN 下左孤行配对为 (左行, None)。
        键为 None 的行不参与匹配（与 SQL 语义一致）。
    """
    # 对右表建立哈希索引：right_key 的值 → 行列表
    right_index = {}
    for row in right_rows:
        key = row.get(right_key)
        if key is not None:
            right_index.setdefault(key, []).append(row)

    pairs = []
    for left_row in left_rows:
        key = left_row.get(left_key)
        matches = right_index.get(key, []) if key is not None else []

        if matches:
            for right_row in matches:
                pairs.append((left_row, right_row))
        elif join_type == "LEFT":
            pairs.append((left_row, None))

    return pairs


def _merge_row(
    left_row: dict,
    right_row: Optional[dict],
    right_table: str,
    right_columns: list[str],
) -> dict:
    """字段合并：复制左行（已带 table. 前缀），并入右行字段

    右行字段统一加 "{right_table}." 前缀，与左行已有前缀隔离，
    因此左右两侧同名列不会互相覆盖（字段名冲突由前缀化解）。
    右行为 None（LEFT JOIN 左孤行）时，右表字段填 None。

    Args:
        left_row: 左行（字段已带 table. 前缀）
        right_row: 右行（原始列名），None 表示无匹配
        right_table: 右表名（用于字段前缀）
        right_columns: 右表列名列表（right_row 为 None 时用于填充 None）

    Returns:
        合并后的新行（不修改入参）
    """
    merged = dict(left_row)  # 复制左表所有字段（已带前缀）
    if right_row is not None:
        for k, v in right_row.items():
            merged[f"{right_table}.{k}"] = v
    else:
        for k in right_columns:
            merged[f"{right_table}.{k}"] = None
    return merged


def _memory_join_with_prefixed(
    left_rows: list[dict],
    right_rows: list[dict],
    left_table: str,
    right_table: str,
    left_key: str,
    right_key: str,
    join_type: str = "INNER",
) -> list[dict]:
    """内存 JOIN（支持已带前缀的左表数据）

    left_rows 的字段可能已经是 table.column 格式（多表 JOIN 累积结果）
    right_rows 的字段是原始列名（单表查询结果）
    """
    right_columns = list(right_rows[0].keys()) if right_rows else []
    result = []
    for left_row, right_row in _match_rows(left_rows, right_rows, left_key, right_key, join_type):
        result.append(_merge_row(left_row, right_row, right_table, right_columns))
    return result
