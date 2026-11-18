"""Measure every turn prompt's labelled sections (no model calls, read-only)."""
from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.agents import hub  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402
from verify_peer_success import ScriptedTeam, brief, role_of  # noqa: E402

MARKERS = [
    ("团队已投递消息：", "bus"),
    ("你的职责：", "instr"),
    ("任务状态：", "tasks"),
    ("当前任务：", "current"),
    ("待返工问题", "rework"),
    ("交通住宿依赖交付事实：", "deps"),
    ("本回合收件：", "inbox"),
    ("结构化 ReviewPacket", "packet"),
    ("当前 Issue Ledger：", "ledger"),
]


class Measuring(ScriptedTeam):
    def __init__(self):
        super().__init__()
        self.rows = []

    async def chat_tools(self, messages, tools):
        if messages[-1].get("role") == "user":
            prompt = messages[-1].get("content") or ""
            hits = sorted((prompt.find(m), name) for m, name in MARKERS if prompt.find(m) >= 0)
            sizes = {}
            for i, (idx, name) in enumerate(hits):
                end = hits[i + 1][0] if i + 1 < len(hits) else len(prompt)
                sizes[name] = end - idx
            sizes["head"] = hits[0][0] if hits else len(prompt)
            self.rows.append({"who": role_of(messages[0]["content"]), "total": len(prompt), **sizes})
        return await super().chat_tools(messages, tools)


async def main():
    team = Measuring()
    await hub.run_session(brief=brief(), source=MockSource(), client=team,
                          emit=lambda *_a, **_k: None, max_turns=20, max_rounds=2)
    keys = ["head", "bus", "instr", "tasks", "current", "rework", "deps", "inbox", "packet", "ledger"]
    print("role".ljust(13) + "total".rjust(8) + "".join(k.rjust(10) for k in keys))
    totals = {k: 0 for k in keys}
    grand = 0
    for row in team.rows:
        grand += row["total"]
        for k in keys:
            totals[k] += row.get(k, 0)
        print(row["who"].ljust(13) + str(row["total"]).rjust(8)
              + "".join(str(row.get(k, 0)).rjust(10) for k in keys))
    print("TOTAL".ljust(13) + str(grand).rjust(8) + "".join(str(totals[k]).rjust(10) for k in keys))
    print("turn_prompts", len(team.rows), "avg", round(grand / max(1, len(team.rows))))


asyncio.run(main())
