"""目标达成检测报告结构——一次写操作复查的结论载体"""
from dataclasses import dataclass, field


@dataclass
class VerifyReport:
    """写操作目标达成复查结论

    verified:
        True  —— 复查通过（数据库真实状态与声明一致）
        False —— 复查不符（mismatch_detail 说明差异）
        None  —— 无法复查（规则关闭/effects 信息不足/权限受限/复查器异常，
                  skipped_reason 说明原因）

    expected/actual：预期状态 vs 实际复查结果（结构化，供审计与前端呈现）
    """
    verified: bool | None
    table: str = ""
    action: str = ""
    expected: dict = field(default_factory=dict)
    actual: dict = field(default_factory=dict)
    mismatch_detail: str = ""
    skipped_reason: str = ""

    def render(self) -> str:
        """附在响应文本后的用户可见呈现"""
        target = f"{self.table}（{self.action}）" if self.table else self.action
        if self.verified is True:
            return f"✔ 已复查：{target} 结果与声明一致"
        if self.verified is False:
            return f"✘ 复查不符：{self.mismatch_detail}"
        return f"⚠ 未能复查：{target} {self.skipped_reason}".rstrip()

    def to_dict(self) -> dict:
        """机器通道：挂进 ToolResult.data["verify"]"""
        return {
            "verified": self.verified,
            "table": self.table,
            "action": self.action,
            "expected": self.expected,
            "actual": self.actual,
            "mismatch_detail": self.mismatch_detail,
            "skipped_reason": self.skipped_reason,
        }
