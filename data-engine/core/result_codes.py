"""执行结果码——全项目唯一的结果分类枚举（双轨契约 4.3 定稿）

原则：重试/纠错/失败判定只读 ToolResult.data 的 code 字段，不在各处做文本关键词匹配。
工具自报 code/reason，下游（executor/graph/OODA 信号）全部读结构化字段。

code 语义：
- OK          成功
- NOT_FOUND   表/字段/记录/文件不存在（确定性，不重试）
- VALIDATION  参数/校验类错误：缺参/选择集失效/意图不明/需 force 确认（确定性，不重试）
- CONTRACT    安全契约拒绝：WHERE 不安全/权限拒绝/标识符非法（确定性，不重试）
- TRANSIENT   临时故障：超时/网络/限流（可重试）
- UNKNOWN     未归类错误（确定性，不重试）
"""
from enum import Enum


class ResultCode(str, Enum):
    OK = "OK"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION = "VALIDATION"
    CONTRACT = "CONTRACT"
    TRANSIENT = "TRANSIENT"
    UNKNOWN = "UNKNOWN"
