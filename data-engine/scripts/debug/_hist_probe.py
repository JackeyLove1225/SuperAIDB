"""对比 get_state vs get_state_history 的 tasks[].interrupts 形态"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_sdk import get_sync_client

TID = sys.argv[1] if len(sys.argv) > 1 else "019fca5a-538a-7412-aa20-c3fd594b4355"

client = get_sync_client(url="http://localhost:2024")

print("=== get_state ===")
st = client.threads.get_state(TID)
print("next:", st.get("next"))
for t in (st.get("tasks") or []):
    print(f"  task {t.get('name')}: interrupts={len(t.get('interrupts') or [])}")

print("\n=== get_state_history (最近2个 snapshot) ===")
hist = client.threads.get_history(TID, limit=2)
for i, snap in enumerate(hist):
    print(f"snapshot[{i}] next={snap.get('next')} checkpoint={(snap.get('checkpoint') or {}).get('checkpoint_id', '?')[:13]}")
    for t in (snap.get("tasks") or []):
        intrs = t.get("interrupts") or []
        print(f"  task {t.get('name')}: interrupts={len(intrs)}")
        for it in intrs:
            print(f"    keys={list(it.keys()) if isinstance(it, dict) else type(it)}")
            print(f"    {json.dumps(it, ensure_ascii=False)[:250]}")
    vals = snap.get("values") or {}
    print(f"  values.__interrupt__: {str(vals.get('__interrupt__'))[:150]}")
