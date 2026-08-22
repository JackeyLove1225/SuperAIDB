"""核武闸 SDK 级验收：interrupt 是否冒泡 / 批准与拒绝路径是否正确执行

用法：
  python scripts\_nuke_sdk_check.py          # 全流程：拒绝 quota_items + 批准 test_nuke_1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_sdk import get_sync_client


def _find_interrupt(run_chunk):
    """从 run 结果中提取 interrupt 对象（兼容 dict / 属性访问）"""
    interrupts = run_chunk.get("__interrupt__") or []
    return interrupts[0] if interrupts else None


def run_case(client, thread, instruction, decision_type):
    """发起一条核武指令，等待 interrupt，按 decision_type 决策，返回最终消息文本"""
    print(f"\n{'='*60}\n指令: {instruction}\n决策: {decision_type}\n{'='*60}")

    # 1. 发起 run（流式，直到 interrupt）
    interrupted = None
    for chunk in client.runs.stream(
        thread["thread_id"], "agent",
        input={"messages": [{"role": "user", "content": instruction}]},
        stream_mode="values",
    ):
        if chunk.event == "values" and isinstance(chunk.data, dict):
            intr = _find_interrupt(chunk.data)
            if intr:
                interrupted = intr
                break

    if not interrupted:
        print("❌ 未等到 interrupt——核武闸未触发！")
        return None

    # interrupt.value 是 payload
    payload = interrupted.get("value") if isinstance(interrupted, dict) else getattr(interrupted, "value", None)
    ar = (payload or {}).get("action_requests", [{}])[0]
    print(f"✅ interrupt 触发: name={ar.get('name')} args={ar.get('args')}")
    print(f"描述:\n{ar.get('description', '')[:400]}")

    # 2. 恢复 run（提交用户决策）
    resume_value = {"decisions": [{"type": decision_type}]}
    final_msgs = []
    for chunk in client.runs.stream(
        thread["thread_id"], "agent",
        command={"resume": resume_value},
        stream_mode="values",
    ):
        if chunk.event == "values" and isinstance(chunk.data, dict):
            msgs = chunk.data.get("messages") or []
            if msgs:
                final_msgs = msgs

    # 取最后一条 AI 消息
    last = ""
    for m in reversed(final_msgs):
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        mtype = m.get("type") if isinstance(m, dict) else getattr(m, "type", "")
        if content and mtype in ("ai", "assistant"):
            last = content
            break
    print(f"最终AI消息:\n{last[:600]}")
    return last


def main():
    client = get_sync_client(url="http://localhost:2024")

    # 用例1：拒绝路径（quota_items 必须存活）
    thread1 = client.threads.create()
    run_case(client, thread1, "删除表 quota_items", "reject")

    # 用例2：批准路径（test_nuke_1 必须被删）
    thread2 = client.threads.create()
    run_case(client, thread2, "删除表 test_nuke_1", "approve")


if __name__ == "__main__":
    main()
