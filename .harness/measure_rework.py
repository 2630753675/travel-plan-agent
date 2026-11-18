"""Measure one forced rework round (scripted, no real model)."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.agents import hub  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402
from verify_peer_success import ScriptedTeam, brief, role_of  # noqa: E402


class MeasureReject(ScriptedTeam):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.rejected = False

    async def chat_tools(self, messages, tools):
        role = role_of(messages[0]["content"])
        total = sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or [], ensure_ascii=False)) for m in messages)
        self.rows.append({"role": role, "chars": total})
        resp = await super().chat_tools(messages, tools)
        if role == "critic_agent" and not resp.get("tool_calls") and not self.rejected:
            data = json.loads(resp["content"])
            if data.get("kind") == "accept":
                self.rejected = True
                data["kind"] = "objection"
                data["approved_version"] = None
                data["changes"] = [{
                    "owner": "local_expert",
                    "problem": "11-21 晚上没有安排夜景活动，用户偏好未落实",
                    "required_change": "增加一个夜间活动并重新提交",
                }]
                data["say"] = "硬约束通过，但夜景偏好没有落实，需要返工。"
                resp = {"content": json.dumps(data, ensure_ascii=False), "tool_calls": []}
        return resp


async def main():
    team = MeasureReject()
    result = await hub.run_session(
        brief=brief(), source=MockSource(), client=team,
        emit=lambda *_a, **_k: None, max_turns=20, max_rounds=2,
    )
    print("converged", result.converged, "turns", result.turns, "reviews", result.rounds)
    total = sum(r["chars"] for r in team.rows)
    by = {}
    for r in team.rows:
        by[r["role"]] = by.get(r["role"], 0) + 1
    print("requests", len(team.rows), "input_chars", total, "by_role", by)
    print("avg", round(total / max(1, len(team.rows))))


asyncio.run(main())
