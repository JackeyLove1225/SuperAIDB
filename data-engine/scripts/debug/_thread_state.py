"""查看指定 thread 的 LangGraph 状态（诊断 interrupt 悬停形态）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_sdk import get_sync_client

TID = sys.argv[1] if len(sys.argv) > 1 else "019fca35-3e9f-7900-a08a-2bc808c038b3"

client = get_sync_client(url="http://localhost:2024")
state = client.threads.get_state(TID)

print("thread:", TID)
print("next:", state.get("next"))
print("checkpoint_id:", (state.get("checkpoint") or {}).get("checkpoint_id"))
tasks = state.get("tasks") or []
print(f"tasks: {len(tasks)}")
for t in tasks:
    print(f"  task name={t.get('name')} error={str(t.get('error'))[:150]}")
    for it in (t.get("interrupts") or []):
        print(f"    interrupt: {str(it)[:400]}")
vals = state.get("values") or {}
print("values.__interrupt__:", str(vals.get("__interrupt__"))[:400])
msgs = vals.get("messages") or []
print(f"messages: {len(msgs)}")
for m in msgs[-4:]:
    c = m.get("content") if isinstance(m, dict) else ""
    t = m.get("type") if isinstance(m, dict) else "?"
    print(f"  [{t}] {str(c)[:250]}")
print("results:", str(vals.get("results"))[:300])
print("failed_tasks:", str(vals.get("failed_tasks"))[:300])
