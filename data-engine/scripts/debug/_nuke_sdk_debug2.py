"""核武闸批准路径深度诊断：打印 resume 流全部 messages 演变"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_sdk import get_sync_client


def dump_msgs(tag, data):
    msgs = (data or {}).get("messages") or []
    print(f"--- {tag} ({len(msgs)} msgs) ---")
    for m in msgs[-3:]:
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        t = m.get("type") if isinstance(m, dict) else getattr(m, "type", "")
        print(f"  [{t}] {str(c)[:300]}")
    ft = (data or {}).get("failed_tasks") or []
    if ft:
        print(f"  failed_tasks: {str(ft)[:400]}")
    res = (data or {}).get("results") or []
    if res:
        print(f"  results: {str(res)[:400]}")


def main():
    client = get_sync_client(url="http://localhost:2024")
    thread = client.threads.create()
    tid = thread["thread_id"]
    print(f"thread_id: {tid}\n== 阶段1: 发起指令，等 interrupt ==")

    interrupted = None
    for chunk in client.runs.stream(
        tid, "agent",
        input={"messages": [{"role": "user", "content": "删除表 test_nuke_1"}]},
        stream_mode="values",
    ):
        if chunk.event == "values" and isinstance(chunk.data, dict):
            intr = chunk.data.get("__interrupt__")
            if intr:
                interrupted = intr[0]
                break
    if not interrupted:
        print("❌ 未等到 interrupt")
        return
    payload = interrupted.get("value") if isinstance(interrupted, dict) else getattr(interrupted, "value", None)
    print(f"✅ interrupt: {str(payload)[:300]}")

    print("\n== 阶段2: resume approve ==")
    i = 0
    for chunk in client.runs.stream(
        tid, "agent",
        command={"resume": {"decisions": [{"type": "approve"}]}},
        stream_mode="values",
    ):
        if chunk.event == "values" and isinstance(chunk.data, dict):
            i += 1
            dump_msgs(f"resume values #{i}", chunk.data)

    state = client.threads.get_state(tid)
    print(f"\n最终: next={state.get('next')}")
    vals = state.get("values") or {}
    dump_msgs("checkpoint values", vals)


if __name__ == "__main__":
    main()
