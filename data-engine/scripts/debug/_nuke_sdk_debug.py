"""人审闸 SDK 级验收（带全事件打印，用于诊断 interrupt 未触发的原因）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_sdk import get_sync_client


def main():
    client = get_sync_client(url="http://localhost:2024")
    thread = client.threads.create()
    tid = thread["thread_id"]
    print(f"thread_id: {tid}")

    n = 0
    for chunk in client.runs.stream(
        tid, "agent",
        input={"messages": [{"role": "user", "content": "删除表 quota_items"}]},
        stream_mode="values",
    ):
        n += 1
        data = chunk.data
        if isinstance(data, dict):
            keys = list(data.keys())
            intr = data.get("__interrupt__")
            msgs = data.get("messages") or []
            last_msg = ""
            if msgs:
                m = msgs[-1]
                last_msg = (m.get("content") if isinstance(m, dict) else getattr(m, "content", ""))[:150]
            print(f"[{n}] event={chunk.event} keys={keys} interrupt={'YES' if intr else 'no'}")
            if intr:
                print(f"    INTERRUPT: {str(intr)[:500]}")
            if last_msg:
                print(f"    last_msg: {last_msg}")
        else:
            print(f"[{n}] event={chunk.event} data={str(data)[:200]}")
        if n > 60:
            print("...（截断）")
            break

    # run 结束后看线程状态
    state = client.threads.get_state(tid)
    print(f"\n最终状态: next={state.get('next')} tasks={len(state.get('tasks') or [])}")
    for t in (state.get("tasks") or []):
        print(f"  task: name={t.get('name')} error={str(t.get('error'))[:200]} interrupts={t.get('interrupts')}")


if __name__ == "__main__":
    main()
