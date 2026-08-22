"""子任务执行器——开放式 AI 的每个数据库操作都走完整的 P1→树→P2

核心原则：开放式 AI 不直接操作数据库，只通过 Agent.execute_single() 公开入口访问。
失败判定/重试决策只读 ToolResult.data 的结构化 code（双轨契约 4.3），
不做文本关键词分类。
失败时按错误类型决定是否重试（最多 MAX_RETRIES 次），可重试错误带退避，
并在最终结果中附带友好错误说明。
"""

import sys
import time
from pathlib import Path

# 确保项目根目录在 sys.path 中
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 已知耦合点：模块级 import 拉起整个 legacy 栈（core.ai_runtime/工具注册/会话历史等），
# 且 Agent 单例在首次调用时连带创建 AIClient + load_history 磁盘 I/O。
# 本轮仅消除对私有方法 _execute_single 的依赖；模块级单例拉起是更大的重构，留待后续。
from agent import Agent
from core.result_codes import ResultCode
from core.tool_result import ToolResult

# 全局 Agent 实例（复用，避免每次创建新实例）
_agent_instance: Agent | None = None

# 最大重试次数（首次执行 + 重试次数 = 总尝试次数）
MAX_RETRIES = 2

# 可重试错误的退避间隔（秒）：第 1 次重试前等 1s，第 2 次重试前等 2s
RETRY_BACKOFF_SECONDS = (1, 2)


class SubTaskResult:
    """execute_sub_task 的结构化返回：执行文本 + 结果码（ResultCode）+ data 通道。

    graph 层判定失败只读 .code——code 传递替代文案解析；
    data 透传工具双轨负载（effects/affected/rows 等，供 goal_verify 钩子消费）；
    __str__ 返回文本，兼容存量字符串用法（results.append / 消息拼接）。
    """
    __slots__ = ("text", "code", "data")

    def __init__(self, text: str, code: "ResultCode", data: dict | None = None):
        self.text = text
        self.code = code
        self.data = data or {}

    def __str__(self):
        return self.text


def get_agent() -> Agent:
    """获取全局 Agent 实例（单例）"""
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = Agent()
    return _agent_instance


def _code_of(result) -> "ResultCode":
    """从 ToolResult 读结构化 code。

    code 缺失（legacy 文本直通，ok=None）按 OK 处理——文本原样呈现，
    机器侧不做失败结论（与迁移期 legacy 语义一致）。
    """
    if isinstance(result, ToolResult):
        c = result.data.get("code")
        if c:
            return ResultCode(c)
    return ResultCode.OK


def _is_error_result(result) -> bool:
    """判断执行结果是否为错误——读结构化 code，不做关键词匹配"""
    return _code_of(result) != ResultCode.OK


def _is_retryable(exc_or_result) -> bool:
    """判断错误是否可重试——结构化判定

    - ToolResult：仅 TRANSIENT（超时/网络/限流）可重试；
      NOT_FOUND/VALIDATION/CONTRACT/UNKNOWN 均为确定性结论，不重试
    - 异常对象：按临时故障可重试（与改造前行为一致——异常被抛出本身即失败，
      OK 分类不适用于它；GraphInterrupt 已在调用点之前放行，不会到达这里）
    """
    if isinstance(exc_or_result, ToolResult):
        return _code_of(exc_or_result) == ResultCode.TRANSIENT
    return True


def _make_friendly_error(sub_task: str, result, attempts: int) -> str:
    """生成用户友好的错误提示——按结构化 code/reason 映射（不解析文案）

    映射优先级与子码对齐工具层自报的 reason；
    无专用映射时经口语化翻译层转换（技术细节进日志，用户看人话）。
    """
    code = _code_of(result)
    reason = result.data.get("reason", "") if isinstance(result, ToolResult) else ""
    text = str(result)
    if code == ResultCode.NOT_FOUND:
        if reason == "table_not_found":
            return "操作失败：引用的表不存在。请先使用「查看数据库表结构」确认可用表。"
        if reason == "column_not_found":
            return "操作失败：引用的字段不存在。请先使用「查看表结构」确认可用字段。"
        if reason == "no_fk_relation":
            return "操作失败：表之间未建立外键关系，无法执行关联查询。请检查表结构配置。"
    elif code == ResultCode.CONTRACT:
        if reason == "unsafe_where":
            return "操作失败：查询条件格式不支持。请使用更明确的条件描述。"
        # 其他契约类错误如实转述（不一律冒充外键关系问题——S6 误导事故）
    elif code == ResultCode.VALIDATION and reason == "cannot_route":
        return f"操作失败：无法理解指令意图（{attempts}次尝试均失败）。请换一种方式描述您的需求。"
    # 默认：经口语化翻译层转换（技术细节进日志，用户看人话）
    from core.error_translate import translate_error
    friendly_text, detail = translate_error(text)
    from core.logger import get_logger
    get_logger(__name__).info("错误翻译: %s ← %s", friendly_text, detail[:120])
    if friendly_text.startswith("操作失败"):
        return f"{friendly_text}（已重试{attempts}次）"
    return friendly_text


def execute_sub_task(
    sub_task: str,
    behavior_key: str = "",
    db_category_key: str = "",
    constraint: str = "",
    structured_args: dict | None = None,
) -> SubTaskResult:
    """执行单个子任务——走完整的 P1→树→P2 流程（按错误类型重试 + 退避）

    这是开放式 AI 访问数据库的唯一入口。
    子任务是一条自然语言指令（如"查一下A1-6的价格"），
    会被传递给 Agent.execute_single()，走完整的语义解析→决策树→工具执行流程。

    重试策略（读 ToolResult.data.code，不解析文案）：
    - 可重试错误（TRANSIENT：超时/网络/LLM 限流等临时故障）：最多重试 MAX_RETRIES 次，
      重试前按 RETRY_BACKOFF_SECONDS 退避（1s、2s）
    - 不可重试错误（NOT_FOUND/VALIDATION/CONTRACT 等确定性失败）：直接失败，
      不浪费后续 LLM 调用

    Args:
        sub_task: 自然语言指令（如"查一下A1-6的价格"）
        behavior_key: LangGraph 提供的结构化标签（7种行为之一），跳过 P1 的 AI 解析
        db_category_key: LangGraph 提供的结构化标签（15种对象之一）
        constraint: 约束值（如"非空"/"主键"/"单条"等）
        structured_args: LangGraph 输出的工具参数 JSON（如 table/data/conditions 等），
                         非空时跳过 FC AI 调用，直接构造工具参数

    Returns:
        SubTaskResult（.text 为结果文本，.code 为 ResultCode，.data 为工具双轨负载；
        graph 层判定失败只读 .code）
    """
    agent = get_agent()
    last_result = ToolResult.fail("未执行", code=ResultCode.UNKNOWN.value,
                                  reason="not_executed")
    attempts = 0

    for attempt in range(1 + MAX_RETRIES):
        attempts = attempt + 1
        try:
            result = agent.execute_single(
                sub_task,
                behavior_key=behavior_key,
                db_category_key=db_category_key,
                constraint=constraint,
                structured_args=structured_args or {},
            )
            # 契约兜底：execute_single 承诺返回 ToolResult；意外拿到 str 时按 legacy 包装
            if not isinstance(result, ToolResult):
                result = ToolResult.legacy(str(result))
            if not _is_error_result(result):
                # 目标达成检测钩子（4.5）：写操作后独立复查，报告附加到 text/data。
                # 单向依赖——工具层零感知；复查异常不阻断主流程
                from core.goal_verify.hooks import attach as _gv_attach
                _gv_attach(result)
                return SubTaskResult(result.text, ResultCode.OK, result.data)
            last_result = result
            # 确定性失败（表不存在/校验失败等）：重试无意义，直接退出
            if not _is_retryable(result):
                break
        except Exception as e:
            # GraphInterrupt（HITL 人审闸挂起信号）不是错误——放行进重试循环会
            # 被当临时故障重试 MAX_RETRIES 次且永远传不到 LangGraph runtime（1a 修复 20260804）
            from langgraph.errors import GraphInterrupt
            if isinstance(e, GraphInterrupt):
                raise
            last_result = ToolResult.fail(f"执行失败: {e}", code=ResultCode.UNKNOWN.value,
                                          reason="exception")
            # 异常对象按临时故障处理（重试至上限）

        # 最后一次尝试不再等待；可重试错误按 1s、2s 退避
        if attempt < MAX_RETRIES:
            backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            time.sleep(backoff)

    # 所有尝试均失败，返回友好错误提示（code 透传最后一次失败的结构化结论）
    friendly = _make_friendly_error(sub_task, last_result, attempts)
    return SubTaskResult(friendly, _code_of(last_result), last_result.data)
