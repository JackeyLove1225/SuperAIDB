"""统一异常包装——所有模块用同一套异常"""
from core.logger import info as log_info


class AppError(Exception):
    """业务异常，前端应直接展示 message"""
    def __init__(self, message: str, detail: str = ""):
        self.message = message
        self.detail = detail
        super().__init__(message)


class ConfigError(AppError):
    """配置错误"""
    pass


class QueryError(AppError):
    """查询错误"""
    pass


class PipelineError(AppError):
    """流水线错误"""
    pass


class RiskError(AppError):
    """破坏性变更需确认——force=False 时抛出

    携带 report 字段（dict），包含风险评估详情：
    - risk: TypeChangeRisk 的序列化
    - data_scan: 数据采样扫描结果
    - changes: ChangeAnalyzer 的变更清单

    forceable（生产方自报，非文本反推）：
    - True（默认）：确认后可带 force=True 放行（类型风险/精度收紧/孤儿数据等），
      execute_tool 据此弹 force 确认卡
    - False：硬阻断（被引用禁删/重名冲突/行数超限等），确认也无济于事，
      不弹卡，直接把指引文案返回给用户
    """
    def __init__(self, message: str, report: dict = None, forceable: bool = True):
        self.report = report or {}
        self.forceable = forceable
        super().__init__(message)


class PrimaryKeyError(AppError):
    """主键操作被禁止——id 字段的类型/删除/重命名一律禁止

    项目硬约束：每张表都有且只有一个默认的主键 id。
    此异常不可被 force=True 绕过。
    """
    pass


class SecurityError(AppError):
    """安全校验失败——标识符非法、WHERE 子句注入风险、SQL 注入等"""
    pass


class PendingApproval(AppError):
    """MCP 通道高危操作待批准（20260807 人审桥接）

    MCP server 进程无 LangGraph runtime，interrupt 人审卡不可用——
    高危人审闸在此通道改为：登记挂起表 + 抛本异常（携带 token），
    execute_tool 映射为"待批准"ToolResult 返回给上层 AI；
    用户批准后由 confirm_action 工具结算（复用批量预批准通道放行）。

    安全语义不变：无确认不执行（fail-closed），只是确认的回执链路不同。
    """
    def __init__(self, message: str, token: str):
        self.token = token
        super().__init__(message)


def safe_call(fn, *args, **kwargs):
    """安全调用，捕获所有异常转为 AppError

    例外：GraphInterrupt（LangGraph HITL 挂起信号）不是错误，必须放行——
    吞掉它，核武人审闸的确认卡片就永远弹不出来（1a 修复 20260804）。
    """
    try:
        return fn(*args, **kwargs)
    except AppError:
        raise
    except Exception as e:
        from langgraph.errors import GraphInterrupt
        if isinstance(e, GraphInterrupt):
            raise
        log_info("异常捕获", fn=fn.__name__, error=str(e)[:100])
        raise AppError(f"操作失败: {str(e)[:200]}")
