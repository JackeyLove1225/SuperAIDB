"""记录级写操作人审闸 SDK 验收（1b，20260804）

验证 edit_data/delete_data 已入核武闸：interrupt 弹出、决策解析、影响面含选择集预览。

前置：python scripts\\_nuke_accept_check.py setup（重建 test_nuke_1 3 行）
用法：
  python scripts\\_mutate_gate_check.py reject   # 拒绝路径：test_nuke_1 必须保留 3 行
  python scripts\\_mutate_gate_check.py approve  # 批准路径：记录被删（0 行）
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_sdk import get_sync_client
from config.settings import settings

DECISION = sys.argv[1] if len(sys.argv) > 1 else "reject"
INSTRUCTION = "查询 test_nuke_1 表的全部记录，然后用选择集把查到的这些记录删除"


def count_rows() -> int:
    conn = sqlite3.connect(settings.SQLITE_DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM test_nuke_1").fetchone()[0]
    finally:
        conn.close()


def main():
    print(f"决策: {DECISION} | 删前行数: {count_rows()}")
    client = get_sync_client(url="http://localhost:2024")
    thread = client.threads.create()

    # 1. 发起 run，等 interrupt
    interrupted = None
    last_msgs = []
    for chunk in client.runs.stream(
        thread["thread_id"], "agent",
        input={"messages": [{"role": "user", "content": INSTRUCTION}]},
        stream_mode="values",
    ):
        if chunk.event == "values" and isinstance(chunk.data, dict):
            msgs = chunk.data.get("messages") or []
            if msgs:
                last_msgs = msgs
            intr = (chunk.data.get("__interrupt__") or [])
            if intr:
                interrupted = intr[0]
                break

    if not interrupted:
        print(f"❌ 未等到 interrupt（thread_id={thread['thread_id']}）——AI 未调记录级工具，最后消息：")
        for m in reversed(last_msgs):
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            mtype = m.get("type") if isinstance(m, dict) else getattr(m, "type", "")
            if content and mtype in ("ai", "assistant"):
                print(str(content)[:500])
                break
        return

    payload = interrupted.get("value") if isinstance(interrupted, dict) else getattr(interrupted, "value", None)
    ar = (payload or {}).get("action_requests", [{}])[0]
    print(f"✅ interrupt 触发: name={ar.get('name')} args={ar.get('args')}")
    print(f"描述:\n{ar.get('description', '')[:400]}")
    if ar.get("name") not in ("delete_data", "edit_data"):
        print(f"⚠️ 触发的是 {ar.get('name')} 而非记录级工具——AI 路径与预期不符")

    # 2. resume 提交决策
    final_msgs = []
    for chunk in client.runs.stream(
        thread["thread_id"], "agent",
        command={"resume": {"decisions": [{"type": DECISION}]}},
        stream_mode="values",
    ):
        if chunk.event == "values" and isinstance(chunk.data, dict):
            msgs = chunk.data.get("messages") or []
            if msgs:
                final_msgs = msgs

    last = ""
    for m in reversed(final_msgs):
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        mtype = m.get("type") if isinstance(m, dict) else getattr(m, "type", "")
        if content and mtype in ("ai", "assistant"):
            last = content
            break
    print(f"最终AI消息:\n{last[:500]}")

    # 3. 地面真值：直接查库
    n = count_rows()
    print(f"\n删后行数: {n}")
    if DECISION == "reject":
        print("判定:", "✅ PASS（记录保留）" if n == 3 else "❌ FAIL（记录被删！）")
    else:
        print("判定:", "✅ PASS（记录已删）" if n == 0 else "❌ FAIL（记录仍在！）")


if __name__ == "__main__":
    main()
