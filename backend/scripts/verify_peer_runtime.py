"""当前 peer Agent 运行边界验收，不调用真实模型。"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.agents import hub, runtime  # noqa: E402
from app.agents.bus import Bus  # noqa: E402
from app.llm import LLMError  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'} {name}{(': ' + detail) if detail else ''}")
    if not cond:
        raise SystemExit(1)


def brief() -> TripBrief:
    return TripBrief(
        origin="上海", destination="成都", start_date=date(2026, 11, 20), end_date=date(2026, 11, 24),
        travelers=2, budget_total=8000, companions=["老人"], preferences=["人文"],
        pace="适中", planning_mode="peer",
    )


class Client:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def chat_tools(self, messages, tools):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(kind="note", **extra):
    value = {"say": "数据已核对", "kind": kind, "to": [], "wake": [], "done": True,
             "changes": [], "evidence": [], "escalate": None}
    value.update(extra)
    return {"content": json.dumps(value, ensure_ascii=False), "tool_calls": []}


def main() -> int:
    print("[1] 任务 DAG 与角色边界")
    try:
        hub._validate_tasks([{"id": "T1", "owner": "critic_agent", "goal": "评审"}])
    except ValueError as exc:
        check("critic 不能伪装成专业交付负责人", "critic_agent" in str(exc))
    else:
        check("critic 不能伪装成专业交付负责人", False)
    valid = hub._validate_tasks([
        {"id": "T1", "owner": "logistics", "goal": "确认交通住宿"},
        {"id": "T2", "owner": "local_expert", "goal": "编排行程", "depends_on": ["T1"]},
    ])
    check("合法 DAG 通过", set(valid) == {"T1", "T2"})
    try:
        hub._validate_tasks([
            {"id": "T1", "owner": "logistics", "goal": "确认交通住宿", "depends_on": ["T9"]},
            {"id": "T2", "owner": "local_expert", "goal": "编排行程", "depends_on": ["T1"]},
        ])
    except ValueError as exc:
        check("未知依赖被准确指出", "T9" in str(exc))
    else:
        check("未知依赖被准确指出", False)

    print("[2] 空响应回合升级")
    client = Client([LLMError("模型未返回内容或工具调用（finish_reason=None）")])
    result = asyncio.run(hub.run_session(
        brief=brief(), source=MockSource(), client=client, emit=lambda *_a, **_k: None,
        max_turns=2, max_rounds=1,
    ))
    check("空响应不会伪造收敛", not result.converged)
    check("空响应形成用户升级", bool(result.escalation))
    check("升级原因包含回合失败", "回合失败" in (result.escalation or {}).get("question", ""))

    print("[3] 证据边界")
    ctx = hub.ToolContext(brief=brief(), source=MockSource())
    bad = asyncio.run(runtime.run_turn(
        agent="critic_agent", system="s", prompt="p", tools=[], ctx=ctx, bus=Bus(),
        client=Client([reply(kind="proposal", evidence=[{"id": "fake"}])]),
        emit=lambda *_a, **_k: None, max_steps=1,
    ))
    check("伪造 evidence 被拒绝", not bad.ok)

    print("[4] stop 终态")
    resume = {
        "architecture": "professional_team", "bus": {}, "context": {}, "tasks": {},
        "histories": {}, "trace": [], "turns": 1, "reviews": 0,
        "proposal_id": None, "review_version": None, "pending_rejections": [],
        "pending_issue_codes": [], "pending_agent": "logistics", "pending_task": None,
        "warnings": [],
    }
    stopped = asyncio.run(hub.run_session(
        brief=brief(), source=MockSource(), client=Client([]), emit=lambda *_a, **_k: None,
        max_turns=2, max_rounds=1, resume=resume, user_answer=json.dumps({"decision": "stop"}),
    ))
    check("stop 标记为终态", stopped.stopped)
    check("stop 不再生成下一次问题", stopped.escalation is None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
