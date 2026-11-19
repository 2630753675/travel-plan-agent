"""Analyse today's real run from run_events + plans (read-only)."""
import json
import sqlite3
import pathlib
from collections import Counter

DB = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend\data\plans.db")
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

runs_today = list(cur.execute(
    "select run_id, count(*) c, min(occurred_at) a, max(occurred_at) b from run_events "
    "where occurred_at like '2026-11-19%' group by run_id order by b desc"))
print("today run_ids:", [(r["run_id"], r["c"], r["a"], r["b"]) for r in runs_today])
if not runs_today:
    raise SystemExit("no events today")

run_id = runs_today[0]["run_id"]
events = []
for row in cur.execute("select seq, occurred_at, payload from run_events where run_id=? order by seq", (run_id,)):
    body = row["payload"]
    body = json.loads(body) if isinstance(body, str) else (body or {})
    events.append((row["seq"], row["occurred_at"], body))

print(f"\nrun {run_id}: {len(events)} events, span {events[0][1]} → {events[-1][1]}")
print("by type:", dict(Counter(e[2].get("event_type") for e in events).most_common()))
print("agent_message by actor:", dict(Counter(
    e[2].get("actor") for e in events if e[2].get("event_type") == "agent_message").most_common()))
print("tool_call by tool:", dict(Counter(
    e[2].get("payload", {}).get("tool_name") for e in events
    if e[2].get("event_type") == "tool_call").most_common()))

for seq, ts, body in events:
    if body.get("event_type") in {"run_completed", "run_needs_input", "run_failed", "run_interrupted"}:
        print(f"\nterminal event {body.get('event_type')} payload keys:",
              sorted((body.get("payload") or {}).keys()))
        payload = body.get("payload") or {}
        stats = payload.get("llm_stats") or {}
        print("  llm_stats:", {k: stats.get(k) for k in
              ("calls", "max_calls", "input_chars", "output_chars", "total_tokens",
               "prompt_tokens", "completion_tokens", "latency_ms", "usage_available")})
        for key in ("message", "error_type", "blockers", "issue_codes"):
            if payload.get(key):
                print(f"  {key}: {str(payload[key])[:300]}")

print("\n--- timeline ---")
for seq, ts, body in events:
    etype = body.get("event_type")
    p = body.get("payload") or {}
    if etype == "tool_call":
        text = f"tool={p.get('tool_name')} args={str(p.get('arguments'))[:60]}"
    elif etype == "tool_result":
        text = f"tool={p.get('tool_name')} ok={p.get('ok')} {str(p.get('summary'))[:50]}"
    else:
        text = str(p.get("text") or p.get("message") or p.get("summary") or "")[:90]
    print(f"{seq:>4} {etype:<16} {str(body.get('actor'))[:14]:<14} {text}")
con.close()
