"""Post-run analysis: newest run stats + team deliverable state + rework trace."""
import json
import sqlite3
import sys
import pathlib
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DB = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend\data\plans.db")
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=== 最近 runs ===")
for row in cur.execute("select rowid, run_id, status, created_at, payload from runs order by rowid desc limit 4"):
    payload = json.loads(row["payload"]) if row["payload"] else {}
    stats = payload.get("llm_stats") or {}
    print(f"rowid={row['rowid']} status={row['status']} at={row['created_at']} "
          f"calls={stats.get('calls')} tokens={stats.get('total_tokens')} "
          f"cost={payload.get('total_cost')} version={payload.get('version')}")

print("\n=== 最近 plans（含团队交付状态）===")
for row in cur.execute("select rowid, id, created_at, team, trace from plans order by rowid desc limit 3"):
    team = json.loads(row["team"]) if row["team"] else None
    trace = json.loads(row["trace"]) if row["trace"] else []
    phases = [item.get("phase") for item in trace if isinstance(item, dict)]
    print(f"plan={row['id']} at={row['created_at']}")
    print(f"  team_state={(team or {}).get('state')} approved={(team or {}).get('approved_version')} "
          f"ledger={len((team or {}).get('issue_ledger') or [])} "
          f"tasks={len((team or {}).get('tasks') or {})} "
          f"session_ref={bool((team or {}).get('session'))}")
    print(f"  trace_phases={phases}")

print("\n=== 版本快照 ===")
for row in cur.execute(
        "select plan_id, version, source, note, "
        "case when team is null then 0 else 1 end as has_team from plan_versions "
        "order by rowid desc limit 5"):
    print(f"  {row['plan_id']} v{row['version']} source={row['source']} team={row['has_team']} note={row['note'][:40]}")

print("\n=== 最近一次运行的事件概览 ===")
run_id = cur.execute("select run_id from run_events order by rowid desc limit 1").fetchone()
if run_id:
    rid = run_id["run_id"]
    events = []
    for row in cur.execute("select seq, occurred_at, payload from run_events where run_id=? order by seq", (rid,)):
        body = json.loads(row["payload"]) if row["payload"] else {}
        events.append((row["seq"], row["occurred_at"], body))
    print(f"run={rid} events={len(events)}")
    print("by_type:", dict(Counter(e[2].get("event_type") for e in events).most_common()))
    print("tools:", dict(Counter(e[2].get("payload", {}).get("tool_name") for e in events
                                 if e[2].get("event_type") == "tool_call").most_common()))
    bad = [(s, e.get("payload", {}).get("summary")) for s, _, e in events
           if e.get("event_type") == "tool_result" and "失败" in str(e.get("payload", {}).get("summary"))]
    print("failed tool results:", len(bad))
    for item in bad[:5]:
        print("   ", item)
    versions = {e[2].get("plan_version") for e in events}
    print("event plan_versions:", sorted(v for v in versions if isinstance(v, int)))
    for seq, _, body in events[-6:]:
        p = body.get("payload") or {}
        print(f"  {seq:>3} {body.get('event_type'):<16} {str(p.get('text') or p.get('message') or p.get('summary') or '')[:80]}")
con.close()
