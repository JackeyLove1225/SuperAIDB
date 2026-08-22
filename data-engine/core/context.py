"""
上下文记忆管理器——独立模块，不与任何功能耦合。

任何组件都可以存/读上下文，不依赖 Agent、路由、工具。

选择集（selections）文件化（20260822 四轮评审修复；五轮迁移到 JsonContract
公共实现）：落盘 config/runtime/selections.json + mtime 新鲜读 + 互斥锁——
MCP 进程挂起、管理端审批中心结算的跨进程链路不再断链，并发写不撞号
（与 pending_ops/escalation 同一"文件即契约"哲学）。
"""

import os
from pathlib import Path

from core.file_contract import JsonContract

_SELECTIONS_FILE = Path(
    os.environ.get("SUPERAIDB_SELECTIONS_FILE")  # 测试隔离：每层独立文件
    or (Path(__file__).resolve().parent.parent / "config" / "runtime" / "selections.json"))
_SELECTIONS_CAP = 50  # 容量帽：只留最新 50 个（防无界增长）


class ContextManager:
    """独立的上下文记忆"""

    def __init__(self):
        self._context = None
        self._trace_id = "??"
        self._selections = {}
        self._selections_mtime = -1.0
        self._nuke_batch = None  # 批量预批准（mutate_natural 多表合并卡，20260805）
        self._channel = "graph"  # 调用通道：graph=LangGraph 图内 / mcp=MCP server

    # ── 选择集（文件即契约，跨进程新鲜共享）──

    def _contract(self) -> JsonContract:
        """选择集文件契约（JsonContract 公共实现：mtime 新鲜读+原子写+互斥锁）"""
        c = getattr(self, "_sel_contract", None)
        if c is None or c.path != _SELECTIONS_FILE:
            c = JsonContract(_SELECTIONS_FILE)
            self._sel_contract = c
        return c

    def _load_selections(self) -> dict:
        """经文件契约新鲜读取（文件不存在=空；损坏=空）"""
        self._selections = self._contract().read()
        return self._selections

    def clear_selections(self) -> None:
        """清空选择集（测试隔离/会话重置用）"""
        self._selections = {}
        self._save_selections()

    def _save_selections(self) -> None:
        """原子落盘（经文件契约）"""
        self._contract().write(self._selections)

    def save(self, key: str, data: dict):
        """保存上下文"""
        self._context = {"key": key, "data": dict(data)}

    def get(self, key: str = ""):
        """读取上下文。指定 key 则只返回匹配的，否则返回最近一条"""
        if not self._context:
            return None
        if key and self._context["key"] != key:
            return None
        return self._context["data"]

    def match_table(self, name: str):
        """检查名字是否为已知表名"""
        if not name:
            return False
        name = name.strip().lower()
        try:
            from core.schema_matcher import _load_schemas
            for t in _load_schemas():
                if t["name"].lower() == name:
                    return True
        except Exception:
            pass
        try:
            from core.drivers import _get_driver as get_driver
            for row in get_driver().query("SELECT name FROM sqlite_master WHERE type='table'"):
                if row["name"].lower() == name:
                    return True
        except Exception:
            pass
        return False

    def consume(self, key: str = ""):
        """消费并清除上下文。只在命中时清除，未命中保留"""
        data = self.get(key)
        if data is not None:
            self._context = None
        return data

    def clear(self):
        self._context = None

    def clear_all(self):
        """清空全部会话态（暂存上下文+选择集+批量预批准）——行业切换时调用（U-9）。
        选择集是文件化共享态：必须落盘清空，否则其他进程仍读到旧行业选择集"""
        self._context = None
        self._selections = {}
        self._save_selections()
        self._nuke_batch = None

    def set_nuke_batch(self, tables: set, ops: set):
        """设置批量预批准——mutate_natural 多表合并卡已获用户批准，
        逐表 execute_tool 时核武闸免检（避免同一批准重复弹卡）"""
        self._nuke_batch = {"tables": set(tables), "ops": set(ops)}

    def get_nuke_batch(self) -> dict | None:
        """获取批量预批准（无则 None）"""
        return self._nuke_batch

    def clear_nuke_batch(self):
        """清除批量预批准（批量执行完毕后调用）"""
        self._nuke_batch = None

    def set_channel(self, channel: str):
        """设置调用通道：graph（默认）/ mcp。高危人审闸按通道选桥接方式——
        graph 走 interrupt 人审卡；mcp 无 graph runtime，走挂起表回执（confirm_action）"""
        self._channel = channel

    def get_channel(self) -> str:
        return self._channel

    def __bool__(self):
        return self._context is not None

    def set_trace_id(self) -> str:
        """生成并存储请求追踪 ID"""
        import uuid
        self._trace_id = f"req-{uuid.uuid4().hex[:8]}"
        return self._trace_id

    def get_trace_id(self) -> str:
        """获取当前请求的 trace ID（未设置时懒生成——任何路径都不会拿到 "??"）"""
        if self._trace_id == "??":
            self.set_trace_id()
        return self._trace_id

    def save_selection(self, table: str, rows: list, query: str = "", datasource: str = "") -> int:
        """保存查询结果为选择集（文件化跨进程共享；容量帽 50 只留最新）

        Args:
            table: 表名
            rows: 查询结果行列表
            query: 原始查询语句
            datasource: 数据源名（联邦数据库支持，空=默认数据源）
        """
        # 自动获取数据源名（如果未指定）
        if not datasource:
            try:
                from core.datasource_manager import DataSourceManager
                datasource = DataSourceManager().get_datasource_for_table(table)
            except Exception:
                datasource = ""
        # 整个读改写（取号→插入→截断→落盘）在互斥锁内——跨进程并发安全
        #（锁外取号会 sid 撞车/丢更新，评审五轮）
        with self._contract().lock():
            sels = self._load_selections()
            sid = max([int(k) for k in sels.keys()], default=0) + 1
            sels[str(sid)] = {
                "table": table,
                "datasource": datasource,
                "ids": [r["id"] for r in rows if "id" in r],
                "count": len(rows),
                "query": query,
                "sample": [{k: v for k, v in r.items()} for r in rows[:2]]
            }
            # 容量帽：只留最新 50 个
            while len(sels) > _SELECTIONS_CAP:
                sels.pop(min(sels.keys(), key=int))
            self._selections = sels
            self._save_selections()
        return sid

    def get_selection(self, sid) -> dict | None:
        """获取选择集，支持 sel_5 或 5 两种格式"""
        if isinstance(sid, str) and sid.startswith("sel_"):
            sid = sid[4:]
        return self._load_selections().get(str(sid))

    def get_last_selection_id(self):
        """获取最近创建的选择集编号，无选择集返回 None"""
        sels = self._load_selections()
        if not sels:
            return None
        return max(int(k) for k in sels.keys())

    def list_selections(self) -> list[dict]:
        """列出所有选择集摘要"""
        result = []
        for sid, sel in self._load_selections().items():
            result.append({
                "id": int(sid),
                "table": sel["table"],
                "datasource": sel.get("datasource", ""),
                "count": sel["count"],
                "query": sel["query"],
                "sample": sel.get("sample", [])[:2]
            })
        return result


# 全局单例
_context = None


def get_context():
    global _context
    if _context is None:
        _context = ContextManager()
    return _context
