"""Read-only inspection of the local plans DB: tables, row counts, latest runs."""
import sqlite3
import pathlib
import json

DB = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend\data\plans.db")
print("exists", DB.exists(), "size KB", round(DB.stat().st_size / 1024) if DB.exists() else 0)
if not DB.exists():
    raise SystemExit(0)

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()
tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'")]
for name in tables:
    cols = [r[1] for r in cur.execute(f"pragma table_info({name})")]
    count = cur.execute(f"select count(*) from {name}").fetchone()[0]
    print(f"{name}: rows={count} cols={cols}")

if "runs" in tables:
    print("\n--- latest runs ---")
    for row in cur.execute("select * from runs order by rowid desc limit 5"):
        data = dict(row)
        payload = data.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        payload = payload or {}
        stats = payload.get("llm_stats") or {}
        print(
            f"{data.get('created_at')} status={data.get('status')} "
            f"plan_id={payload.get('plan_id')} calls={stats.get('calls')}/{stats.get('max_calls')} "
            f"in={stats.get('input_chars')} out={stats.get('output_chars')} "
            f"tokens={stats.get('total_tokens')} latency_ms={stats.get('latency_ms')}"
        )
con.close()
