"""双轨契约——工具结果的协议单一定义

每个工具返回 ToolResult：
- text：给用户看的文本（与历史 str 返回完全一致，保兼容）
- data：给机器判断的结构化字段，消灭"文案即协议"

data 字段规约：
    ok: bool | None   — 操作是否成功；None = legacy 未迁移文本（迁移期过渡态，
                        全量迁移完成后生产路径不应再出现）
    code: str         — ResultCode 枚举值（OK/NOT_FOUND/VALIDATION/CONTRACT/
                        TRANSIENT/UNKNOWN），重试/纠错/失败判定只读此字段
    reason: str       — 业务子码：need_selection / need_force / pending_confirm /
                        nuke_rejected / unknown_tool / permission_denied / …
    error_kind: str   — 异常/错误分类（类名或语义名），友好文案映射用
    effects: dict     — 写操作效果：
                        {table, action, affected, affected_ids, changed_fields, values}
                        ——写操作结构化对账负载（审计/复查消费）
    其余键            — 工具私有负载（rows/row_count/selection_id/tables/…）

兼容设计：text 通道对存量文本语境完全等价——__str__/__contains__/__eq__(str)/
__getitem__/__len__/__bool__ 全部委托 text，存量 `"x" in result`、`result == "..."`、
`result[:120]`、f-string、拼接消费点零改动（下游结构化改造前的不变量）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.result_codes import ResultCode


@dataclass(eq=False)
class ToolResult:
    text: str
    data: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.text

    def __bool__(self) -> bool:
        # 与旧字符串语义一致：空文本 = falsy（"if not result" 判空调用点不变）
        return bool(self.text)

    # ── text 通道兼容面（委托 text，存量文本断言/切片/比较零改动）──

    def __contains__(self, item: str) -> bool:
        return item in self.text

    def __getitem__(self, key):
        return self.text[key]

    def __len__(self) -> int:
        return len(self.text)

    def __eq__(self, other) -> bool:
        if isinstance(other, str):
            return self.text == other
        if isinstance(other, ToolResult):
            return self.text == other.text and self.data == other.data
        return NotImplemented

    def __ne__(self, other) -> bool:
        eq = self.__eq__(other)
        return eq if eq is NotImplemented else not eq

    # ── 构造器 ──

    @classmethod
    def ok(cls, text: str, **data) -> "ToolResult":
        """成功结果。data 可带 rows/row_count/effects 等负载"""
        return cls(text, {"ok": True, "code": ResultCode.OK.value, **data})

    @classmethod
    def fail(cls, text: str, code: str = ResultCode.UNKNOWN.value,
             reason: str = "", **data) -> "ToolResult":
        """失败结果。code 取 ResultCode 枚举值；reason 为业务子码"""
        d = {"ok": False, "code": code}
        if reason:
            d["reason"] = reason
        return cls(text, {**d, **data})

    @classmethod
    def legacy(cls, text: str) -> "ToolResult":
        """未迁移工具的纯文本包装（ok=None 显式标记过渡态）"""
        return cls(text if text is not None else "执行完成",
                   {"ok": None, "code": None, "reason": "legacy_text"})

    # ── 机器通道便捷读取 ──

    @property
    def is_ok(self) -> bool:
        """结构化成功判定（legacy 结果返回 None——调用方对 None 自行决策）"""
        return self.data.get("ok")

    @property
    def code(self) -> str:
        return self.data.get("code") or ""

    @property
    def reason(self) -> str:
        return self.data.get("reason") or ""

    @property
    def effects(self) -> dict:
        return self.data.get("effects") or {}
