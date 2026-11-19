"""Audit probes: verify suspected defects with executable evidence (no repo mutation)."""
import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\项目\agent_study\dsh_work2\backend")

from app.agents import hub, runtime  # noqa: E402
from app.agents.bus import Bus  # noqa: E402
from app.agents.tools_spec import ToolContext, execute  # noqa: E402
from app.artifacts import ArtifactStore  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402


def brief(days: int = 3) -> TripBrief:
    start = date(2026, 11, 20)
    end = date.fromordinal(start.toordinal() + days - 1)
    return TripBrief(origin="上海", destination="成都", start_date=start, end_date=end,
                     travelers=2, budget_total=8000, companions=["老人"])


print("=== P1 artifact path traversal ===")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "artifacts"
    store = ArtifactStore(root)
    secret = Path(tmp) / "secret.json"
    secret.write_text(json.dumps({"leaked": True}), encoding="utf-8")
    ref = {"artifact_id": "../secret"}
    try:
        data = store.get(ref)
        print("VULNERABLE: read outside root ->", data)
    except Exception as exc:
        print("blocked:", type(exc).__name__, exc)

print("\n=== P2 stale allowed_repair_tools after delivery ===")
print("scan hub for pop('allowed_repair_tools') occurrences:")
import inspect
src = inspect.getsource(hub)
print("  set:", src.count('["allowed_repair_tools"] ='), " pop:", src.count("pop(\"allowed_repair_tools\""),
      " pop2:", src.count("pop('allowed_repair_tools'"))

print("\n=== P3 submit_decision first in a multi-call response ===")


class MultiCall:
    def __init__(self):
        self.n = 0

    async def chat_tools(self, messages, tools):
        self.n += 1
        if self.n == 1:
            return {"content": "", "tool_calls": [
                {"id": "d1", "name": "submit_decision", "arguments": {"kind": "note", "say": "先交决策"}},
                {"id": "t1", "name": "list_pois", "arguments": {}},
            ]}
        return {"content": "", "tool_calls": [
            {"id": "d2", "name": "submit_decision", "arguments": {"kind": "note", "say": "结束"}},
        ]}


ctx = ToolContext(brief=brief(), source=MockSource())
turn = asyncio.run(runtime.run_turn(
    agent="local_expert", system="s", prompt="p", tools=["list_pois"], ctx=ctx, bus=Bus(),
    client=MultiCall(), emit=lambda *a, **k: None, max_steps=2,
))
print("turn ok:", turn.ok, "| tool_calls:", turn.tool_calls)
print("history roles:", [m.get("role") for m in turn.history])
try:
    runtime._validate_history_messages(turn.history[1:])
    print("history valid: True")
except ValueError as exc:
    print("HISTORY CORRUPT:", exc)

print("\n=== P4 replay by reused tool-call id with different args ===")
ctx2 = ToolContext(brief=brief(), source=MockSource())
r1 = execute(ctx2, "list_pois", {})
print("first list_pois count:", r1.get("count"))
ctx2.operations["call_1"] = {"agent": "local_expert", "tool": "list_pois", "status": "succeeded",
                             "arguments": {}, "result": r1}


class ReuseId:
    async def chat_tools(self, messages, tools):
        # 同一个 id，但语义上是一次新查询
        return {"content": "", "tool_calls": [
            {"id": "call_1", "name": "list_pois", "arguments": {"_fresh": True}},
            {"id": "d9", "name": "submit_decision", "arguments": {"kind": "note", "say": "ok"}},
        ]}


turn2 = asyncio.run(runtime.run_turn(
    agent="local_expert", system="s", prompt="p", tools=["list_pois"], ctx=ctx2, bus=Bus(),
    client=ReuseId(), emit=lambda *a, **k: None, max_steps=2,
))
print("turn ok:", turn2.ok)
print("note: replay is keyed on id+agent+tool only; arguments are not compared")
print("operations keys:", list(ctx2.operations)[:3])

print("\n=== P5 stop-session snapshot fields ===")
src_session = inspect.getsource(hub.run_session)
early = src_session.split('if user_answer and decision in {"stop", "restart"}:')[1].split("return PeerResult")[0]
for field in ("pending_agent", "pending_task", "last_review_packet", "warnings"):
    print(f"  {field} in early-stop session:", field in early)
