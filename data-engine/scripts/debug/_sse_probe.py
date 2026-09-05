"""模拟前端 SSE 请求：复刻 agent-chat-ui 的 submit 参数，观察 __interrupt__ 事件形态"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

BASE = "http://localhost:2024"


def main():
    with httpx.Client(timeout=180) as c:
        # 1. 建 thread（与前端一致）
        tid = c.post(f"{BASE}/threads", json={}).json()["thread_id"]
        print("thread:", tid)

        # 2. 复刻前端 submit：stream_mode 多模态 + stream_resumable
        payload = {
            "assistant_id": "agent",
            "input": {"messages": [{"role": "user", "content": "删除表 test_nuke_1"}]},
            "stream_mode": ["values", "messages-tuple", "custom"],
            "stream_resumable": True,
        }
        n = 0
        with c.stream("POST", f"{BASE}/threads/{tid}/runs/stream", json=payload) as r:
            event = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data = line.split(":", 1)[1].strip()
                    if event and "values" in event:
                        n += 1
                        try:
                            obj = json.loads(data)
                            keys = list(obj.keys()) if isinstance(obj, dict) else type(obj).__name__
                            has_intr = isinstance(obj, dict) and "__interrupt__" in obj
                            print(f"[values #{n}] keys={keys}")
                            if has_intr:
                                intr = obj["__interrupt__"]
                                print(f"  >>> __interrupt__ PRESENT: {json.dumps(intr, ensure_ascii=False)[:400]}")
                        except Exception as e:
                            print(f"[values #{n}] parse error: {e}; raw={data[:200]}")
        print(f"\n流结束，共 {n} 个 values 事件")

        # 3. 流结束后查 state
        st = c.get(f"{BASE}/threads/{tid}/state").json()
        print("state.next:", st.get("next"))
        for t in (st.get("tasks") or []):
            intrs = t.get("interrupts") or []
            print(f"  task {t.get('name')}: {len(intrs)} interrupts")
            for it in intrs:
                print(f"    {json.dumps(it, ensure_ascii=False)[:300]}")


if __name__ == "__main__":
    main()
