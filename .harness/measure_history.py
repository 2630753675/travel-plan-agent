"""Measure how many characters each request spends on history vs prompt."""
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


def size(message: dict) -> int:
    content = message.get("content") or ""
    calls = message.get("tool_calls") or []
    return len(content) + (len(json.dumps(calls, ensure_ascii=False)) if calls else 0)


class Measuring(ScriptedTeam):
    def __init__(self):
        super().__init__()
        self.rows = []

    async def chat_tools(self, messages, tools):
        history = sum(size(m) for m in messages[1:-1])
        prompt = size(messages[-1])
        self.rows.append((role_of(messages[0]["content"]), len(messages), history, prompt))
        return await super().chat_tools(messages, tools)


async def main():
    team = Measuring()
    await hub.run_session(brief=brief(), source=MockSource(), client=team,
                          emit=lambda *_a, **_k: None, max_turns=20, max_rounds=2)
    print("role".ljust(14) + "msgs".rjust(5) + "history".rjust(9) + "prompt".rjust(8))
    total_h = total_p = 0
    for role, msgs, history, prompt in team.rows:
        total_h += history
        total_p += prompt
        print(role.ljust(14) + str(msgs).rjust(5) + str(history).rjust(9) + str(prompt).rjust(8))
    print(f"TOTAL history={total_h} prompt={total_p} sum={total_h + total_p}")


asyncio.run(main())
