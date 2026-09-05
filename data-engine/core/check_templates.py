"""CHECK 约束模板注册表——前后端共用真相源

设计原则：
  - expr_template 用 SQLite 语法为存储标准（YAML source of truth）
  - 由驱动层 translate_for_dialect() 翻译为各方言（MySQL 等）
  - 参数化渲染唯一出口 render_expr()，str_list 自动加引号防注入
  - 每个模板带稳定 key，存进 meta_columns.check_template_key 便于反向回显

模板结构:
  {
    "key": str,                 # 稳定标识
    "label": str,               # 中文展示名
    "expr_template": str,       # 含 {col} 和参数占位符 {param_name}
    "desc": str,                # 使用场景说明
    "params": [                 # 参数定义（空列表表示无参数）
      {"name": str, "kind": "int"|"float"|"str"|"int_list"|"str_list",
       "placeholder": str, "default": Any}
    ]
  }
"""
from __future__ import annotations

import re
from typing import Any, Optional


# ── 参数 kind 常量（便于前端类型推断）──
PARAM_INT = "int"
PARAM_FLOAT = "float"
PARAM_STR = "str"
PARAM_INT_LIST = "int_list"
PARAM_FLOAT_LIST = "float_list"
PARAM_STR_LIST = "str_list"


# ── 类型归一化映射 ──
# 前端 FIELD_TYPES 7 种 + 后端 VALID_TYPES 17 种 → 7 个标准分类
_TYPE_NORMALIZE: dict[str, str] = {
    "INTEGER": "INTEGER",
    "INT": "INTEGER",
    "BIGINT": "INTEGER",
    "SMALLINT": "INTEGER",
    "TINYINT": "INTEGER",
    "SERIAL": "INTEGER",
    "REAL": "REAL",
    "FLOAT": "REAL",
    "DOUBLE": "REAL",
    "NUMERIC": "NUMERIC",
    "DECIMAL": "NUMERIC",
    "TEXT": "TEXT",
    "VARCHAR": "TEXT",
    "CHAR": "TEXT",
    "CLOB": "TEXT",
    "DATE": "DATE",
    "DATETIME": "DATETIME",
    "TIMESTAMP": "DATETIME",
    "BLOB": "BLOB",
    "BOOLEAN": "INTEGER",   # 布尔当 0/1 整数处理
    "BOOL": "INTEGER",
}

# 7 个标准类型
STANDARD_TYPES = ["INTEGER", "REAL", "NUMERIC", "TEXT", "DATE", "DATETIME", "BLOB"]


# ── 自定义模板（所有类型共用）──
_CUSTOM_TEMPLATE = {
    "key": "custom",
    "label": "自定义",
    "expr_template": "",
    "desc": "手写 CHECK 表达式（需熟悉 SQL 语法）",
    "params": [],
}


def _tmpl(key: str, label: str, expr: str, desc: str,
          params: Optional[list[dict]] = None) -> dict:
    """构造模板 dict 的便捷函数"""
    return {
        "key": key,
        "label": label,
        "expr_template": expr,
        "desc": desc,
        "params": params or [],
    }


def _param(name: str, kind: str, placeholder: str, default: Any = None) -> dict:
    """构造参数定义"""
    return {"name": name, "kind": kind, "placeholder": placeholder, "default": default}


# ============================================================
# 模板注册表——按字段类型分组
# ============================================================

TEMPLATES_BY_TYPE: dict[str, list[dict]] = {
    "INTEGER": [
        _tmpl("int_positive", "大于0", "{col} > 0",
              "正整数。适用于数量、金额、年龄、外键ID等正数字段"),
        _tmpl("int_non_negative", "大于等于0", "{col} >= 0",
              "非负整数。适用于余额、计数、重试次数等"),
        _tmpl("int_range", "范围 [min, max]", "{col} >= {min} AND {col} <= {max}",
              "整数区间。如年龄 0-150、分页大小 1-100",
              [_param("min", PARAM_INT, "最小值", 0),
               _param("max", PARAM_INT, "最大值", 100)]),
        _tmpl("int_greater", "大于 N", "{col} > {n}",
              "大于指定整数",
              [_param("n", PARAM_INT, "数值", 0)]),
        _tmpl("int_less", "小于 N", "{col} < {n}",
              "小于指定整数",
              [_param("n", PARAM_INT, "数值", 100)]),
        _tmpl("int_enum", "枚举值", "{col} IN ({values})",
              "限定离散整数集合。如状态码、类型码",
              [_param("values", PARAM_INT_LIST, "用逗号分隔，如 1,2,3", [1, 2, 3])]),
        _tmpl("int_bool_flag", "0或1（布尔标志）", "{col} IN (0, 1)",
              "布尔标志位。如是否删除、是否启用"),
        _tmpl("int_id_positive", "外键正整数", "{col} > 0",
              "外键字段必须为正整数（指向 id）"),
        _CUSTOM_TEMPLATE,
    ],

    "REAL": [
        _tmpl("real_positive", "大于0", "{col} > 0",
              "正浮点。适用于温度开尔文、速率等"),
        _tmpl("real_non_negative", "大于等于0", "{col} >= 0",
              "非负浮点。适用于评分下限、误差等"),
        _tmpl("real_range", "范围 [min, max]", "{col} >= {min} AND {col} <= {max}",
              "浮点区间",
              [_param("min", PARAM_FLOAT, "最小值", 0.0),
               _param("max", PARAM_FLOAT, "最大值", 1.0)]),
        _tmpl("real_ratio_0_1", "0-1之间（比例）", "{col} >= 0 AND {col} <= 1",
              "比例/概率/权重值"),
        _tmpl("real_percent_0_100", "0-100之间（百分比）", "{col} >= 0 AND {col} <= 100",
              "百分比/完成度"),
        _tmpl("real_greater", "大于 N", "{col} > {n}",
              "大于指定浮点数",
              [_param("n", PARAM_FLOAT, "数值", 0.0)]),
        _tmpl("real_less", "小于 N", "{col} < {n}",
              "小于指定浮点数",
              [_param("n", PARAM_FLOAT, "数值", 100.0)]),
        _tmpl("real_enum", "枚举值", "{col} IN ({values})",
              "离散浮点值集合",
              [_param("values", PARAM_FLOAT_LIST, "用逗号分隔，如 1.0,1.5,2.0", [1.0, 1.5])]),
        _CUSTOM_TEMPLATE,
    ],

    "NUMERIC": [
        _tmpl("num_positive", "大于0", "{col} > 0",
              "正金额。适用于单价、贷款额等（精度由 DECIMAL(p,s) 保证，CHECK 仅限符号）"),
        _tmpl("num_non_negative", "金额非负", "{col} >= 0",
              "金额/余额非负。注：CHECK 无法强制小数位数，那是 DECIMAL(p,s) 的职责"),
        _tmpl("num_range", "范围 [min, max]", "{col} >= {min} AND {col} <= {max}",
              "金额区间",
              [_param("min", PARAM_FLOAT, "最小值", 0.0),
               _param("max", PARAM_FLOAT, "最大值", 1000000.0)]),
        _tmpl("num_greater", "大于 N", "{col} > {n}",
              "大于指定金额",
              [_param("n", PARAM_FLOAT, "数值", 0.0)]),
        _tmpl("num_less", "小于 N", "{col} < {n}",
              "小于指定金额",
              [_param("n", PARAM_FLOAT, "数值", 1000000.0)]),
        _CUSTOM_TEMPLATE,
    ],

    "TEXT": [
        _tmpl("text_non_empty", "非空字符串", "length({col}) > 0",
              "不能为空串。适用于名称、标题等"),
        _tmpl("text_not_blank", "不等于空串", "{col} <> ''",
              "与“非空字符串”等价但更直白"),
        _tmpl("text_max_length", "最大长度 N", "length({col}) <= {max}",
              "短文本限长。如 ≤255",
              [_param("max", PARAM_INT, "最大长度", 255)]),
        _tmpl("text_min_length", "最小长度 N", "length({col}) >= {min}",
              "至少 N 字符",
              [_param("min", PARAM_INT, "最小长度", 1)]),
        _tmpl("text_length_range", "长度范围 [min, max]",
              "length({col}) >= {min} AND length({col}) <= {max}",
              "长度区间",
              [_param("min", PARAM_INT, "最小长度", 1),
               _param("max", PARAM_INT, "最大长度", 255)]),
        _tmpl("text_enum", "枚举值", "{col} IN ({values})",
              "限定字符串集合。如性别、级别",
              [_param("values", PARAM_STR_LIST, "用逗号分隔，如 active,inactive", ["active", "inactive"])]),
        _tmpl("text_email_like", "邮箱格式（弱）", "{col} LIKE '%_@_%._%'",
              "简单邮箱格式校验（含 @ 与 .）。注：仅弱校验，严格校验需应用层"),
        _tmpl("text_phone_like", "手机号格式（弱）", "{col} LIKE '1_________'",
              "11 位以 1 开头（SQLite LIKE 下划线匹配单字符）"),
        _tmpl("text_upper_no_space", "无空格", "{col} NOT LIKE '% %'",
              "编码类字段不允许空格"),
        _CUSTOM_TEMPLATE,
    ],

    "DATE": [
        _tmpl("date_not_future", "不晚于今天", "{col} <= date('now')",
              "生日、历史日期等不应晚于今天"),
        _tmpl("date_not_past", "不早于今天", "{col} >= date('now')",
              "截止日期、预约日期等不应早于今天"),
        _tmpl("date_after", "不早于指定日期", "{col} >= date('{date}')",
              "不早于指定日期",
              [_param("date", PARAM_STR, "YYYY-MM-DD", "2024-01-01")]),
        _tmpl("date_before", "不晚于指定日期", "{col} <= date('{date}')",
              "不晚于指定日期",
              [_param("date", PARAM_STR, "YYYY-MM-DD", "2099-12-31")]),
        _tmpl("date_range", "日期范围",
              "{col} >= date('{start}') AND {col} <= date('{end}')",
              "日期区间",
              [_param("start", PARAM_STR, "开始日期 YYYY-MM-DD", "2024-01-01"),
               _param("end", PARAM_STR, "结束日期 YYYY-MM-DD", "2099-12-31")]),
        _CUSTOM_TEMPLATE,
    ],

    "DATETIME": [
        _tmpl("dt_not_future", "不晚于现在", "{col} <= datetime('now')",
              "已发生事件时间不应晚于现在"),
        _tmpl("dt_not_past", "不早于现在", "{col} >= datetime('now')",
              "截止时刻不应早于现在"),
        _tmpl("dt_after", "不早于指定时刻", "{col} >= datetime('{dt}')",
              "不早于指定时刻",
              [_param("dt", PARAM_STR, "YYYY-MM-DD HH:MM:SS", "2024-01-01 00:00:00")]),
        _tmpl("dt_before", "不晚于指定时刻", "{col} <= datetime('{dt}')",
              "不晚于指定时刻",
              [_param("dt", PARAM_STR, "YYYY-MM-DD HH:MM:SS", "2099-12-31 23:59:59")]),
        _tmpl("dt_range", "时刻范围",
              "{col} >= datetime('{start}') AND {col} <= datetime('{end}')",
              "时刻区间",
              [_param("start", PARAM_STR, "开始 YYYY-MM-DD HH:MM:SS", "2024-01-01 00:00:00"),
               _param("end", PARAM_STR, "结束 YYYY-MM-DD HH:MM:SS", "2099-12-31 23:59:59")]),
        _CUSTOM_TEMPLATE,
    ],

    "BLOB": [
        _CUSTOM_TEMPLATE,
    ],
}


# ============================================================
# 公共 API
# ============================================================

def normalize_type(col_type: str) -> str:
    """归一化字段类型为 7 个标准分类之一

    INTEGER / REAL / NUMERIC / TEXT / DATE / DATETIME / BLOB
    未知类型返回 "TEXT"（最安全的回退，CHECK 表达式通常对 TEXT 也适用）
    """
    if not col_type:
        return "TEXT"
    # 取大写、去括号、去空格
    raw = str(col_type).upper().strip().split("(")[0].split(" ")[0].strip()
    return _TYPE_NORMALIZE.get(raw, "TEXT")


def get_templates_by_type(col_type: str) -> list[dict]:
    """返回归一化类型对应的模板列表；未知类型返回 [custom]"""
    nt = normalize_type(col_type)
    return TEMPLATES_BY_TYPE.get(nt, [_CUSTOM_TEMPLATE])


def get_all_templates_flat() -> list[dict]:
    """返回所有模板的扁平列表（去重，custom 只出现一次）"""
    seen_keys = set()
    result = []
    for tmpls in TEMPLATES_BY_TYPE.values():
        for t in tmpls:
            if t["key"] not in seen_keys:
                seen_keys.add(t["key"])
                result.append(t)
    return result


def get_template_by_key(key: str) -> Optional[dict]:
    """跨类型查找模板（反向回显用）"""
    if key == "custom":
        return _CUSTOM_TEMPLATE
    for tmpls in TEMPLATES_BY_TYPE.values():
        for t in tmpls:
            if t["key"] == key:
                return t
    return None


def render_expr(template_key: str, col_name: str, params: Optional[dict] = None) -> str:
    """渲染最终 CHECK 表达式

    参数:
        template_key: 模板 key（如 "int_range"）
        col_name: 字段名（替换 {col}）
        params: 参数字典（如 {"min": 0, "max": 150}）

    返回: 渲染后的表达式字符串；custom 或找不到模板返回空串

    安全策略:
        - str_list 参数：每项加单引号，内部单引号转义为 ''，杜绝引号逃逸
        - int_list/float_list：裸值（数字字面量天然安全）
        - int/float/str：原样插入（str 已在模板中用 date('...') 包裹）
    """
    if template_key == "custom" or not template_key:
        return ""
    tmpl = get_template_by_key(template_key)
    if tmpl is None or not tmpl["expr_template"]:
        return ""
    params = params or {}
    expr = tmpl["expr_template"]
    # 替换 {col}
    expr = expr.replace("{col}", col_name)
    # 替换参数
    for p_def in tmpl["params"]:
        p_name = p_def["name"]
        p_kind = p_def["kind"]
        p_val = params.get(p_name, p_def.get("default"))
        if p_val is None or p_val == "":
            # 参数缺失，用占位符保留（让 validate_check_expr 拦截）
            continue
        placeholder = "{" + p_name + "}"
        if placeholder not in expr:
            continue
        rendered_val = _render_param_value(p_val, p_kind)
        expr = expr.replace(placeholder, rendered_val)
    return expr


def _render_param_value(value: Any, kind: str) -> str:
    """渲染单个参数值为 SQL 字面量"""
    if kind == PARAM_STR:
        # 字符串参数：原样插入（DATE/DATETIME 模板已自带 date('...') 包裹）
        return str(value)
    if kind == PARAM_INT:
        # 整数：转 int 防注入（非数字会抛 ValueError，由上层捕获）
        try:
            return str(int(value))
        except (ValueError, TypeError):
            return "0"
    if kind == PARAM_FLOAT:
        # 浮点：转 float 防注入
        try:
            return str(float(value))
        except (ValueError, TypeError):
            return "0.0"
    if kind == PARAM_INT_LIST:
        # 整数列表：每项转 int，逗号分隔
        items = _to_list(value)
        rendered = []
        for v in items:
            try:
                rendered.append(str(int(v)))
            except (ValueError, TypeError):
                continue
        return ", ".join(rendered)
    if kind == PARAM_FLOAT_LIST:
        # 浮点列表：每项转 float，逗号分隔
        items = _to_list(value)
        rendered = []
        for v in items:
            try:
                rendered.append(str(float(v)))
            except (ValueError, TypeError):
                continue
        return ", ".join(rendered)
    if kind == PARAM_STR_LIST:
        # 字符串列表：每项加单引号，内部单引号转义为 ''（SQL 标准）
        items = _to_list(value)
        rendered = []
        for v in items:
            escaped = str(v).replace("'", "''")
            rendered.append(f"'{escaped}'")
        return ", ".join(rendered)
    return str(value)


def _to_list(value: Any) -> list:
    """将参数值转为 list（兼容 list/逗号字符串/单值）"""
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    if isinstance(value, str):
        # 逗号分隔的字符串
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


def translate_for_dialect(expr: str, dialect: str) -> str:
    """SQLite 表达式 → 目标方言

    dialect="sqlite": 原样返回
    dialect="mysql":
        datetime('now') → NOW()
        date('now')     → CURDATE()
        length(         → CHAR_LENGTH(   (大小写不敏感，词边界)
        其余保留（date('2024-01-01')、datetime('...')、LIKE、IN 均两库通用）

    注：只做受控替换，不做任意 SQL 解析。
    """
    if not expr or dialect == "sqlite":
        return expr
    if dialect == "mysql":
        result = expr
        # datetime('now') → NOW()  （必须先于 date('now')，避免部分匹配）
        result = re.sub(
            r"datetime\(\s*'now'\s*\)",
            "NOW()",
            result,
            flags=re.IGNORECASE,
        )
        # date('now') → CURDATE()
        result = re.sub(
            r"date\(\s*'now'\s*\)",
            "CURDATE()",
            result,
            flags=re.IGNORECASE,
        )
        # length( → CHAR_LENGTH(  （词边界，大小写不敏感）
        result = re.sub(
            r"\blength\(",
            "CHAR_LENGTH(",
            result,
            flags=re.IGNORECASE,
        )
        return result
    return expr
