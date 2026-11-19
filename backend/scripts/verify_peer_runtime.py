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
    local_rework = {key: dict(value) for key, value in valid.items()}
    affected_local = hub._request_rework(local_rework, [{"task_id": "T2", "reason": "替换闭馆景点", "propagate": False}])
    check("日期级问题只重开被点名任务", affected_local == {"T2"}
          and local_rework["T1"]["status"] == "pending" and local_rework["T2"]["status"] == "pending")
    upstream_rework = {key: dict(value) for key, value in valid.items()}
    affected_upstream = hub._request_rework(upstream_rework, [{"task_id": "T1", "reason": "更换航班酒店", "propagate": True}])
    check("上游交通住宿变更传播到依赖行程", affected_upstream == {"T1", "T2"})
    independent = hub._validate_tasks([
        {"id": "T1", "owner": "logistics", "goal": "确认交通住宿"},
        {"id": "T2", "owner": "local_expert", "goal": "编排行程", "depends_on": ["T1"]},
        {"id": "T3", "owner": "logistics", "goal": "独立核对酒店发票政策"},
        {"id": "T4", "owner": "local_expert", "goal": "检查老人步行强度", "depends_on": ["T1"]},
    ])
    independent["T1"]["status"] = "delivered"
    independent["T2"]["status"] = "delivered"
    independent["T3"]["status"] = "delivered"
    independent["T4"]["status"] = "delivered"
    affected_local_group = hub._request_rework(independent, [{
        "task_id": "T2", "reason": "替换闭馆景点", "propagate": False,
    }])
    check("同一责任角色的无关任务保持交付", affected_local_group == {"T2"}
          and independent["T4"]["status"] == "delivered")
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

    print("[3] 只读工具并发")
    parallel_ctx = hub.ToolContext(brief=brief(), source=MockSource())

    class TwoReads:
        def __init__(self):
            self.calls = 0

        async def chat_tools(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {"content": "", "tool_calls": [
                    {"id": "c1", "name": "list_pois", "arguments": {}},
                    {"id": "c2", "name": "get_weather", "arguments": {}},
                ]}
            return reply(kind="note", say="已并行查询景点与天气")

    two = TwoReads()
    turn = asyncio.run(runtime.run_turn(
        agent="local_expert", system="s", prompt="p",
        tools=["list_pois", "get_weather"], ctx=parallel_ctx, bus=Bus(),
        client=two, emit=lambda *_a, **_k: None, max_steps=2,
    ))
    check("两个只读调用都被执行", turn.ok and turn.tool_calls == ["list_pois", "get_weather"], str(turn.tool_calls))
    check("并发结果按原顺序回填", parallel_ctx.poi.candidates and parallel_ctx.weather.days)
    check("只读并发不推进版本", parallel_ctx.plan_version == 0, str(parallel_ctx.plan_version))

    print("[4] 证据边界")
    ctx = hub.ToolContext(brief=brief(), source=MockSource())
    bad = asyncio.run(runtime.run_turn(
        agent="critic_agent", system="s", prompt="p", tools=[], ctx=ctx, bus=Bus(),
        client=Client([reply(kind="proposal", evidence=[{"id": "fake"}])]),
        emit=lambda *_a, **_k: None, max_steps=1,
    ))
    check("伪造 evidence 被拒绝", not bad.ok)

    print("[5] 结构化决策工具")
    decision_ctx = hub.ToolContext(brief=brief(), source=MockSource())

    class Structured:
        def __init__(self, first: dict):
            self.first = first
            self.calls = 0

        async def chat_tools(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {"content": "", "tool_calls": [
                    {"id": "d1", "name": "submit_decision", "arguments": self.first},
                ]}
            return {"content": "", "tool_calls": [
                {"id": "d2", "name": "submit_decision", "arguments": {
                    "kind": "note", "say": "补齐字段后重新提交",
                    "to": [], "wake": [], "done": True, "changes": [], "evidence": [], "escalate": None,
                }},
            ]}

    good = asyncio.run(runtime.run_turn(
        agent="orchestrator", system="s", prompt="p", tools=[], ctx=decision_ctx, bus=Bus(),
        client=Structured({"kind": "note", "say": "直接提交",
                           "to": [], "wake": [], "done": True, "changes": [], "evidence": [], "escalate": None}),
        emit=lambda *_a, **_k: None, max_steps=2,
    ))
    check("结构化决策可直接提交", good.ok and good.decision.get("kind") == "note", str(good.decision)[:120])
    check("决策工具出现在工具清单", "submit_decision" in good.tool_calls, str(good.tool_calls))

    repaired = Structured({"say": "缺少 kind 字段"})
    fixed = asyncio.run(runtime.run_turn(
        agent="orchestrator", system="s", prompt="p", tools=[], ctx=decision_ctx, bus=Bus(),
        client=repaired, emit=lambda *_a, **_k: None, max_steps=3,
    ))
    check("缺字段时用工具错误并要求同回合修正", fixed.ok and repaired.calls == 2, f"{fixed.ok}/{repaired.calls}")
    check("修正后决策生效", fixed.decision.get("say") == "补齐字段后重新提交", str(fixed.decision)[:120])

    class MinimalOnly:
        async def chat_tools(self, messages, tools):
            return {"content": "", "tool_calls": [
                {"id": "m1", "name": "submit_decision", "arguments": {"kind": "note", "say": "只给必填字段"}},
            ]}

    minimal = asyncio.run(runtime.run_turn(
        agent="orchestrator", system="s", prompt="p", tools=[], ctx=decision_ctx, bus=Bus(),
        client=MinimalOnly(), emit=lambda *_a, **_k: None, max_steps=2,
    ))
    check("只填 kind/say 也能通过（其余取默认）", minimal.ok, str(minimal.error))
    check("默认值已补齐", minimal.decision.get("to") == [] and minimal.decision.get("wake") == []
          and minimal.decision.get("done") is True and minimal.decision.get("escalate") is None,
          str(minimal.decision)[:160])

    critic_ctx = hub.ToolContext(brief=brief(), source=MockSource())

    class CriticFakeEvidence:
        async def chat_tools(self, messages, tools):
            return {"content": "", "tool_calls": [
                {"id": "c1", "name": "submit_decision", "arguments": {
                    "kind": "accept", "say": "基于 ReviewPacket 通过",
                    "approved_version": 9, "evidence": [{"id": "made-up"}],
                }},
            ]}

    critic_turn = asyncio.run(runtime.run_turn(
        agent="critic_agent", system="s", prompt="p", tools=[], ctx=critic_ctx, bus=Bus(),
        client=CriticFakeEvidence(), emit=lambda *_a, **_k: None, max_steps=2,
    ))
    check("Critic 自造 evidence 不再导致协议失败", critic_turn.ok, str(critic_turn.error))
    check("Critic 证据被清空", critic_turn.decision.get("evidence") == [], str(critic_turn.decision.get("evidence")))

    print("[6] stop 终态")
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
