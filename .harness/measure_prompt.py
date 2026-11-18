"""Measure prompt/context size for one scripted 5-role peer run (no real model)."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.agents import hub  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402
from verify_peer_success import ScriptedTeam, brief  # noqa: E402


class MeasuringTeam(ScriptedTeam):
    def __init__(self):
        super().__init__()
        self.rows = []

    async def chat_tools(self, messages, tools):
        role = "?"
        try:
            from verify_peer_success import role_of
            role = role_of(messages[0]["content"])
        except Exception:
            pass
        total = sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or [], ensure_ascii=False)) for m in messages)
        largest = max((len(str(m.get("content") or "")) for m in messages), default=0)
        self.rows.append({"n": len(self.rows) + 1, "role": role, "msgs": len(messages), "chars": total, "largest": largest})
        return await super().chat_tools(messages, tools)


async def main():
    team = MeasuringTeam()
    result = await hub.run_session(
        brief=brief(), source=MockSource(), client=team,
        emit=lambda *_a, **_k: None, max_turns=20, max_rounds=2,
    )
    print("converged", result.converged, "turns", result.turns, "reviews", result.rounds)
    total = 0
    for row in team.rows:
        total += row["chars"]
        print(f'{row["n"]:>2} {row["role"]:<12} msgs={row["msgs"]:>2} input_chars={row["chars"]:>7} largest_msg={row["largest"]:>6}')
    print("TOTAL requests", len(team.rows), "input_chars", total)
    print("avg chars/request", round(total / max(1, len(team.rows))))


asyncio.run(main())
