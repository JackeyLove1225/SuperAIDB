"""公共助手——拆包前 agent/tools.py 顶部的私有工具函数。

域模块（query/records/ddl/files/templates/admin）共用，下沉于此，
域间不得交叉 import。
"""
from core.tool_result import ToolResult
from core.contract.security_contract import is_valid_identifier


# ============ 安全工具 ============

def _validate_table_name(table: str) -> str:
    """校验表名合法性，防止 SQL 注入

    1. 格式校验：只允许字母、数字、下划线
    2. 白名单校验：表名必须存在于数据库中（联邦数据库：跨所有数据源检查）

    Raises:
        ValueError: 表名非法或不存在
    """
    if not table:
        raise ValueError("表名不能为空")
    if not is_valid_identifier(table):
        raise ValueError(f"非法表名格式: {table}")
    # 联邦数据库：通过 FederatedDriver 聚合所有数据源的表
    from core.data_ops import _get_driver
    drv = _get_driver()
    valid_tables = set(drv.list_tables())
    if table not in valid_tables:
        raise ValueError(f"表 '{table}' 不存在")
    return table


# ============ wrapper 工厂 ============

def _msg_result(r):
    """核心函数 dict 结果 → ToolResult（text 与历史 message 完全一致）

    结构化规则：ok 取自 dict["ok"]；失败时 code=UNKNOWN（核心函数未分级），
    dict 显式带 need_force=True 时 reason=need_force（生产方自报，非文本反推）。
    dict 其余键原样进 data 负载（conflicts/count 等机器可用）。
    """
    if isinstance(r, ToolResult):
        return r
    if not isinstance(r, dict):
        return ToolResult.legacy(str(r))
    ok = bool(r.get("ok"))
    data = {k: v for k, v in r.items() if k != "message"}
    data["ok"] = ok
    data["code"] = "OK" if ok else "UNKNOWN"
    # need_force / confirm 两种自报标记同口径（confirm 为历史键名：
    # drop_column/drop_foreign_key 的"确认后带 force 重试"语义与 need_force 一致）
    if not ok and (r.get("need_force") or r.get("confirm")):
        data["code"] = "VALIDATION"
        data["reason"] = "need_force"
    return ToolResult(str(r.get("message", "")), data)


def _require_params(*names: str, msg: str):
    """生成必填参数校验器：任一参数为空则返回错误提示，否则返回 None"""
    def _validate(kwargs: dict):
        if any(not kwargs.get(n) for n in names):
            return msg
        return None
    return _validate


def _schema_tool(module_attr: str, params: dict, validate=None, transform=None):
    """工厂：生成「预校验 → 惰性调用核心函数 → 提取 message」的工具 wrapper

    表结构/外键/索引等工具共用同一模板（校验必填参数、调用核心函数、
    dict 结果提取 message），由此工厂统一生成，行为与原手写 wrapper 一致。

    Args:
        module_attr: "core.schema_manager.add_column" 形式的核心函数路径（惰性导入）
        params: {参数名: 默认值}，决定 wrapper 对外签名
                （execute_tool 按 inspect.signature 过滤参数，必须显式还原）
        validate: 可选，(kwargs) -> 错误提示 str 或 None
        transform: 可选，(kwargs) -> None，就地改写传给核心函数的参数
    """
    module_name, _, attr = module_attr.rpartition(".")

    def wrapper(**kwargs):
        import importlib
        kwargs.pop("database", None)
        if validate:
            err = validate(kwargs)
            if err:
                return ToolResult.fail(err, code="VALIDATION",
                                       reason="missing_params")
        if transform:
            transform(kwargs)
        fn = getattr(importlib.import_module(module_name), attr)
        return _msg_result(fn(**kwargs))

    # 注意：不能用模块级 import 的别名（test_02 以 exec 加载本文件，
    # 模块级名字不进函数 globals），必须函数内局部 import
    import inspect
    wrapper.__name__ = f"{attr}_tool"
    wrapper.__signature__ = inspect.Signature([
        inspect.Parameter(n, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=d)
        for n, d in params.items()
    ])
    return wrapper


# ============ 文件路径闸 ============

def _guard_file_path(filepath: str):
    """文件路径收容闸（20260822 安全修复；20260822 四轮加固）：process_file/upload_file
    只许读工作区内的文件——防 AI/提示注入借文件工具任意读盘外带（如 C:/Windows/…）。

    允许根：项目工作区根目录（data-engine 的上级）及其子树；
    之外的绝对路径一律拒绝（如实报错，不静默）。
    禁区（工作区内也不可读）：data-engine/config/ 整目录——初始管理员密码、
    daemon 令牌、人审待批 token、提权契约、数据源连接串都在那里
    （评审四轮 H-2：经 process_file 读入向量库即完成凭证出仓+二次扩散）。
    """
    from pathlib import Path as _P
    rp = _P(filepath).resolve()
    # 拆包后本文件在 agent/tools/ 下，parents[3] 仍为 data-engine 的上级（工作区根）
    ws_root = _P(__file__).resolve().parents[3]
    try:
        rp.relative_to(ws_root)
    except ValueError:
        return (f"⛔ 文件路径越界：只允许读取工作区（{ws_root}）内的文件，"
                f"已拒绝：{rp}")
    forbidden = ws_root / "data-engine" / "config"
    try:
        rp.relative_to(forbidden)
        return ("⛔ 该路径位于配置/凭证目录（config/），禁止经文件工具访问；"
                "敏感配置请在管理端查看")
    except ValueError:
        return None
