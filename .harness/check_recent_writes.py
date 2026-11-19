"""Check whether the DB received writes today (2026-11-19) and what the newest rows are."""
import sqlite3
import pathlib
import datetime

DB = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend\data\plans.db")
print("db mtime:", datetime.datetime.fromtimestamp(DB.stat().st_mtime).isoformat(timespec="seconds"))
print("now     :", datetime.datetime.now().isoformat(timespec="seconds"))

con = sqlite3.connect(DB)
cur = con.cursor()
for table, col in (("plans", "created_at"), ("runs", "created_at"),
                   ("run_events", "occurred_at"), ("plan_versions", "created_at")):
    total = cur.execute(f"select count(*) from {table}").fetchone()[0]
    newest = cur.execute(f"select max({col}) from {table}").fetchone()[0]
    today = cur.execute(f"select count(*) from {table} where {col} like '2026-11-19%'").fetchone()[0]
    print(f"{table:<14} rows={total:<6} newest={newest} today={today}")

print("\nnewest 3 runs rows:")
for row in cur.execute("select rowid, run_id, status, created_at from runs order by rowid desc limit 3"):
    print("  ", row)
con.close()
