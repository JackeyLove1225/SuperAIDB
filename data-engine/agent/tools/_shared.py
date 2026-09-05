"""公共助手——拆包前 agent/tools.py 顶部的私有工具函数。

域模块（query/records/ddl/files/templates/admin）共用，下沉于此，
域间不得交叉 import。
"""
import inspect
from pathlib import Path as _P
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
    from core.data_ops import get_driver
    drv = get_driver()
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
    wrapper.__name__ = f"{attr}_tool"
    wrapper.__signature__ = inspect.Signature([
        inspect.Parameter(n, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=d)
        for n, d in params.items()
    ])
    return wrapper


# ============ 文件路径闸 ============

def _is_sensitive_file(rp) -> bool:
    """文件名级敏感文件判定（无论扩展名，统一小写 fnmatch）——
    防 API 密钥/私钥/凭据经文件工具读入向量库/数据库完成出仓+二次扩散。
    模式集与历史 file_tools 黑名单同口径（图路径下线后由本闸继承）。"""
    import fnmatch
    name = rp.name.lower()
    patterns = (
        ".env", ".env.*",          # 环境变量/密钥配置
        "*.pem", "*.key",          # 证书/私钥
        "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",  # SSH 私钥
        "*.p12", "*.pfx",          # PKCS#12 密钥库
        ".netrc", ".npmrc",        # 明文凭据
        "datasources.yml", "datasources.yaml",  # 数据源连接串（MySQL 等凭据所在）
        "master.key",              # 隔离模式密钥库文件
        "initial_admin.txt",       # 初始管理员密码（首轮登录凭证）
        "pending_approvals.json",  # 人审待批 token（"token 不出管理通道"的文件面）
        "settlement_results.json",  # 结算回执（token 明文 key，与挂起表同标准）
        "escalation.json",         # 提权契约
        "daemon.json",             # daemon IPC 令牌+端口
        "daemon.spawn.lock", "keygen.lock", "isolated.flag",  # 运行时内部态
        "*.log",                   # 审计/运行日志（含 SQL 与值，防入库二次扩散）
        "saga_*.json",             # 联邦补偿 journal（含整行明文快照）
    )
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _guard_file_path(filepath: str):
    """文件路径收容闸（20260822 安全加固）：process_file/upload_file
    只许读工作区内的文件——防 AI/提示注入借文件工具任意读盘外带（如 C:/Windows/…）。

    允许根：项目工作区根目录（data-engine 的上级）及其子树；
    之外的绝对路径一律拒绝（如实报错，不静默）。
    禁区（工作区内也不可读）：data-engine/config/ 整目录——初始管理员密码、
    daemon 令牌、人审待批 token、提权契约、数据源连接串都在那里
    （经 process_file 读入向量库即完成凭证出仓+二次扩散）。
    敏感文件名黑名单：工作区内任意位置的 .env/私钥/凭据类文件一律拒读
    （图路径 file_tools 黑名单的活路径继承，20260824）。
    """
    rp = _P(filepath).resolve()
    # 拆包后本文件在 agent/tools/ 下，parents[3] 仍为 data-engine 的上级（工作区根）
    ws_root = _P(__file__).resolve().parents[3]
    try:
        rp.relative_to(ws_root)
    except ValueError:
        return (f"⛔ 文件路径越界：只允许读取工作区（{ws_root}）内的文件，"
                f"已拒绝：{rp}")
    # 禁区锚定包位置（parents[2]=data-engine 包根，任何布局不变）——
    # 锚定 ws_root/"data-engine"/"config" 时，裸仓检出下 parents[3] 不再
    # 指向工作区根，禁区路径不存在导致检查静默失效（CI 裸检出场景）
    forbidden = _P(__file__).resolve().parents[2] / "config"
    try:
        rp.relative_to(forbidden)
        return ("⛔ 该路径位于配置/凭证目录（config/），禁止经文件工具访问；"
                "敏感配置请在管理端查看")
    except ValueError:
        pass
    if _is_sensitive_file(rp):
        return ("⛔ 敏感文件不可访问（命中敏感文件黑名单，可能含密钥/凭据）："
                f"{rp.name}")
    return None
