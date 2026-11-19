"""Analyse the most recent run: status, llm stats, event/tool breakdown, timeline."""
import json
import sqlite3
import pathlib
from collections import Counter

DB = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend\data\plans.db")
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

rows = list(cur.execute("select rowid, * from runs order by rowid desc limit 3"))
print("=== recent runs ===")
for r in rows:
    payload = r["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else (payload or {})
    stats = payload.get("llm_stats") or {}
    print(f"rowid={r['rowid']} at={r['created_at']} status={r['status']} plan_id={payload.get('plan_id')}")
    print("   stats:", {k: stats.get(k) for k in
                        ("calls", "max_calls", "input_chars", "output_chars",
                         "total_tokens", "prompt_tokens", "completion_tokens",
                         "latency_ms", "usage_available")})
    for key in ("message", "error_type", "recovery_status", "blockers", "issue_codes"):
        if payload.get(key):
            print(f"   {key}: {str(payload[key])[:300]}")

latest = rows[0]
payload = latest["payload"]
payload = json.loads(payload) if isinstance(payload, str) else (payload or {})
run_id = payload.get("run_id") or latest["run_id"]
print(f"\n=== events for run_id={run_id} ===")

events = []
for row in cur.execute("select seq, occurred_at, payload from run_events where run_id=? order by seq", (run_id,)):
    body = row["payload"]
    body = json.loads(body) if isinstance(body, str) else (body or {})
    events.append((row["seq"], row["occurred_at"], body))

print("event rows:", len(events))
if not events:
    # fall back: newest run_id present in the table
    newest = cur.execute("select run_id, count(*) c from run_events group by run_id order by max(rowid) desc limit 3")
    print("newest run_ids in events:", [(r['run_id'], r['c']) for r in newest])
else:
    types = Counter(e[2].get("event_type") for e in events)
    print("\nby type:", dict(types.most_common()))
    actors = Counter(e[2].get("actor") for e in events if e[2].get("event_type") == "agent_message")
    print("agent_message by actor:", dict(actors.most_common()))
    tools = Counter(e[2].get("payload", {}).get("tool_name") for e in events if e[2].get("event_type") == "tool_call")
    print("tool_call by tool:", dict(tools.most_common()))
    span = (events[-1][1] or "")[:19], (events[0][1] or "")[:19]
    print("time span:", span)
    print("\nfirst 12 events:")
    for seq, ts, body in events[:12]:
        p = body.get("payload") or {}
        text = str(p.get("text") or p.get("message") or p.get("summary") or "")[:110]
        print(f"  {seq:>3} {body.get('event_type'):<16} {str(body.get('actor'))[:16]:<16} {text}")
    print("\nlast 10 events:")
    for seq, ts, body in events[-10:]:
        p = body.get("payload") or {}
        text = str(p.get("text") or p.get("message") or p.get("summary") or "")[:110]
        print(f"  {seq:>3} {body.get('event_type'):<16} {str(body.get('actor'))[:16]:<16} {text}")
con.close()
