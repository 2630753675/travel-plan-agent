"""Analyse one run by id: stats, retries, tool/turn breakdown, timeline."""
import json
import sqlite3
import pathlib
import sys
import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DB = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend\data\plans.db")
RUN = sys.argv[1] if len(sys.argv) > 1 else ""

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

if not RUN:
    row = cur.execute("select run_id from run_events order by rowid desc limit 1").fetchone()
    RUN = row["run_id"]

record = cur.execute("select status, created_at, payload from runs where run_id=?", (RUN,)).fetchone()
print("run:", RUN)
if record:
    payload = json.loads(record["payload"]) if isinstance(record["payload"], str) else (record["payload"] or {})
    stats = payload.get("llm_stats") or {}
    print("status:", record["status"], "version:", payload.get("version"),
          "cost:", payload.get("total_cost"), "passed:", payload.get("passed"))
    print("stats:", {k: stats.get(k) for k in
                     ("calls", "max_calls", "input_chars", "output_chars", "total_tokens",
                      "prompt_tokens", "completion_tokens", "latency_ms", "usage_available")})

events = []
for row in cur.execute("select seq, occurred_at, payload from run_events where run_id=? order by seq", (RUN,)):
    body = row["payload"]
    body = json.loads(body) if isinstance(body, str) else (body or {})
    events.append((row["seq"], row["occurred_at"], body))

from collections import Counter
print("events:", len(events), dict(Counter(e[2].get("event_type") for e in events).most_common()))

retries = []
for seq, occurred, body in events:
    if body.get("event_type") != "tool_result":
        continue
    p = body.get("payload") or {}
    summary = str(p.get("summary") or "")
    if "失败" in summary or p.get("ok") is False:
        retries.append((seq, p.get("tool_name"), summary[:70]))
print("failed tool results:", len(retries))
for item in retries:
    print("   ", item)

repairs = [(s, b.get("payload")) for s, _, b in events if b.get("event_type") == "agent_message"
           and "history_repaired" in str((b.get("payload") or {}).get("text") or "")]
print("history_repaired markers:", len(repairs))

print("\n--- timeline ---")
prev = None
for seq, occurred, body in events:
    etype = str(body.get("event_type"))
    actor = str(body.get("actor"))
    p = body.get("payload") or {}
    if etype == "tool_call":
        text = "tool=" + str(p.get("tool_name")) + " " + str(p.get("arguments"))[:60]
    elif etype == "tool_result":
        text = "tool=" + str(p.get("tool_name")) + " " + str(p.get("summary"))[:60]
    else:
        text = str(p.get("text") or p.get("message") or p.get("summary") or "")[:70]
    stamp = ""
    if prev:
        a = datetime.datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        b = datetime.datetime.fromisoformat(prev.replace("Z", "+00:00"))
        stamp = f"+{(a - b).total_seconds():5.1f}s"
    prev = occurred
    print(f"{seq:>4} {stamp:>7} {etype:<15} {actor[:13]:<13} {text}")
con.close()
