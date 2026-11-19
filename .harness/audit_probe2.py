"""Refined probes for P1 (traversal) and P4 (replay ignores arguments)."""
import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\项目\agent_study\dsh_work2\backend")

from app.agents import runtime  # noqa: E402
from app.agents.bus import Bus  # noqa: E402
from app.agents.tools_spec import ToolContext, execute  # noqa: E402
from app.artifacts import ArtifactStore  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402

print("=== P1 traversal with a crafted id (secret inside artifact root) ===")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "artifacts"
    root.mkdir(parents=True)
    (root / "secret.json").write_text(json.dumps({"leaked": True}), encoding="utf-8")
    store = ArtifactStore(root)
    for candidate in ("a/../../secret", "..\\..\\secret", "./../secret"):
        try:
            data = store.get({"artifact_id": candidate})
            print(f"  VULNERABLE id={candidate!r} -> {data}")
        except Exception as exc:
            print(f"  blocked id={candidate!r}: {type(exc).__name__}")

print("\n=== P4 replay correctness: same id, different arguments ===")
start = date(2026, 11, 20)


def brief():
    return TripBrief(origin="上海", destination="成都", start_date=start,
                     end_date=date(2026, 11, 22), travelers=2, budget_total=8000)


ctx = ToolContext(brief=brief(), source=MockSource())
first = execute(ctx, "get_weather", {})
ctx.operations["call_1"] = {"agent": "local_expert", "tool": "get_weather",
                            "status": "succeeded", "arguments": {},
                            "result": {**first, "marker": "OLD_RESULT"}}
print("  cached result marker:", ctx.operations["call_1"]["result"].get("marker"))


class Reuse:
    def __init__(self):
        self.done = False

    async def chat_tools(self, messages, tools):
        if not self.done:
            self.done = True
            return {"content": "", "tool_calls": [
                {"id": "call_1", "name": "get_weather", "arguments": {"destination": "重庆"}},
            ]}
        return {"content": "", "tool_calls": [
            {"id": "d1", "name": "submit_decision", "arguments": {"kind": "note", "say": "ok"}},
        ]}


turn = asyncio.run(runtime.run_turn(
    agent="local_expert", system="s", prompt="p", tools=["get_weather"], ctx=ctx, bus=Bus(),
    client=Reuse(), emit=lambda *a, **k: None, max_steps=2,
))
tool_msg = next(m for m in turn.history if m.get("role") == "tool")
print("  tool message returned to model:", tool_msg["content"][:160])
print("  replayed old result instead of running new args:", "OLD_RESULT" in tool_msg["content"])
