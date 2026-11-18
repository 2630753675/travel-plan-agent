"""Break each request down by message role: system / user(history) / user(turn prompt) / assistant / tool."""
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.agents import hub  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402
from verify_peer_success import ScriptedTeam, brief  # noqa: E402


def size(message: dict) -> int:
    content = message.get("content") or ""
    calls = message.get("tool_calls") or []
    return len(content) + (len(json.dumps(calls, ensure_ascii=False)) if calls else 0)


class Measuring(ScriptedTeam):
    def __init__(self):
        super().__init__()
        self.rows = []

    async def chat_tools(self, messages, tools):
        users = [m for m in messages if m.get("role") == "user"]
        prompt = users[-1] if users else {}
        older_user = sum(size(m) for m in users[:-1])
        tools_chars = sum(size(m) for m in messages if m.get("role") == "tool")
        assistant = sum(size(m) for m in messages if m.get("role") == "assistant")
        system = sum(size(m) for m in messages if m.get("role") == "system")
        self.rows.append({
            "system": system, "old_user": older_user, "prompt": size(prompt),
            "assistant": assistant, "tool": tools_chars,
        })
        return await super().chat_tools(messages, tools)


async def main():
    team = Measuring()
    await hub.run_session(brief=brief(), source=MockSource(), client=team,
                          emit=lambda *_a, **_k: None, max_turns=20, max_rounds=2)
    keys = ["system", "old_user", "prompt", "assistant", "tool"]
    print("n".rjust(3) + "".join(k.rjust(11) for k in keys) + "total".rjust(9))
    totals = {k: 0 for k in keys}
    for i, row in enumerate(team.rows, 1):
        for k in keys:
            totals[k] += row[k]
        print(str(i).rjust(3) + "".join(str(row[k]).rjust(11) for k in keys) + str(sum(row.values())).rjust(9))
    print("TOTAL".rjust(3) + "".join(str(totals[k]).rjust(11) for k in keys) + str(sum(totals.values())).rjust(9))


asyncio.run(main())
