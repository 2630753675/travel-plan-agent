"""Full robust timeline for today's run (read-only)."""
import json
import sqlite3
import pathlib
import datetime
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend\data\plans.db")
RUN = "6f0d0d4cf107"

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()
events = []
for row in cur.execute("select seq, occurred_at, payload from run_events where run_id=? order by seq", (RUN,)):
    body = row["payload"]
    body = json.loads(body) if isinstance(body, str) else (body or {})
    events.append((row["seq"], row["occurred_at"], body))


def ts(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


prev = None
failures = 0
for seq, occurred, body in events:
    etype = str(body.get("event_type"))
    actor = str(body.get("actor"))
    p = body.get("payload") or {}
    if etype == "tool_call":
        text = "tool=" + str(p.get("tool_name")) + " args=" + str(p.get("arguments"))[:70]
    elif etype == "tool_result":
        ok = p.get("ok")
        if ok is False:
            failures += 1
        text = "tool=" + str(p.get("tool_name")) + " ok=" + str(ok) + " " + str(p.get("summary"))[:60]
    elif etype == "agent_message":
        text = str(p.get("text") or p.get("message") or p.get("summary") or "")[:80]
    else:
        text = json.dumps(p, ensure_ascii=False)[:80]
    delta = ""
    if prev is not None:
        delta = f"+{(ts(occurred) - ts(prev)).total_seconds():5.1f}s"
    prev = occurred
    print(f"{seq:>3} {delta:>8} {etype:<15} {actor[:14]:<14} {text}")

print("\nfailed tool_results:", failures)
con.close()
