"""Probe: team deliverable survives refine/rollback (temp DB, no repo mutation)."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\项目\agent_study\dsh_work2\backend")

from app.api import store  # noqa: E402

tmp = Path(tempfile.mkdtemp())
store.DB_PATH = tmp / "probe.db"

brief = {"origin": "上海", "destination": "成都", "start_date": "2026-11-20",
         "end_date": "2026-11-22", "travelers": 2, "budget_total": 8000}
itinerary = {"days": [{"date": "2026-11-20", "items": []}], "total_cost": 5000, "budget_total": 8000}
critique = {"passed": True, "issues": [], "suggestions": []}
trace = [{"phase": "team_deliverable", "itinerary": itinerary,
          "document": {"title": "旧计划书"}, "team_tasks": {"T1": {}},
          "issue_ledger": [{"code": "CLOSED_DAY", "status": "closed"}], "approved_version": 7}]
team = {"state": "approved", "document": {"title": "团队计划书"},
        "tasks": {"T1": {"owner": "logistics"}},
        "issue_ledger": [{"code": "CLOSED_DAY", "status": "closed"}],
        "approved_version": 7}

plan_id = "probeteam002"
store.save(plan_id, brief, itinerary, critique, trace, revision=0, team=team)

from app.api import main as api  # noqa: E402


def view(pid):
    record = asyncio.run(api.get_plan(pid))
    return (record.get("document") or {}).get("title"), len(record.get("issue_ledger") or []), \
        record.get("approved_version"), record.get("team_state")


print("approved      :", view(plan_id))

# 模拟 refine 的新语义：显式标注 detached，而不是静默丢字段。
refined = {"days": [{"date": "2026-11-20", "items": []}], "total_cost": 5200, "budget_total": 8000}
store.save(plan_id, brief, refined, critique,
           trace + [{"phase": "refine", "node": "planner", "target": "2026-11-20"}],
           revision=0, source="refine", note="重排",
           team={"state": "detached", "detached_reason": "单日重排未经过团队评审",
                 "previous_approved_version": 7, "document": None, "tasks": {},
                 "issue_ledger": [], "approved_version": None})
print("after refine  :", view(plan_id))

# 回滚到 v1（团队批准版本）：交付视图应随版本恢复。
target = store.load_version(plan_id, 1)
store.save(plan_id, brief, target["itinerary"], target["critique"],
           trace + [{"phase": "rollback", "to": 1}], revision=0,
           source="rollback", note="回滚", team=target.get("team"))
print("after rollback:", view(plan_id))
print("version count :", store.current_version(plan_id))
