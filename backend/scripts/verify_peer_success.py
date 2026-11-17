"""脚本化五角色成功路径回归：跑通完整的拆解→交付→评审→整合，不调用真实模型。

模型按“本回合已调用过哪些工具”决定下一步，因此协议重试不会打乱脚本。
运行：
    .venv/Scripts/python.exe scripts/verify_peer_success.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys
from datetime import date
from uuid import uuid4

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents import hub  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402

DAYS = ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23", "2026-11-24"]
ASSIGNMENT = {
    "2026-11-20": [],
    "2026-11-21": ["p02", "p03", "p09"],
    "2026-11-22": ["p08", "p12"],
    "2026-11-23": ["p07", "p01"],
    "2026-11-24": [],
}
OUTBOUND, INBOUND, HOTEL = "3U8890", "3U8891", "春熙里和颐"


def brief() -> TripBrief:
    return TripBrief(
        origin="上海", destination="成都", start_date=date(2026, 11, 20), end_date=date(2026, 11, 24),
        travelers=2, budget_total=8000, companions=["老人"], preferences=["人文"],
        pace="适中", planning_mode="peer",
    )


def tool(name: str, **args) -> dict:
    return {"content": "", "tool_calls": [{"id": f"{name}-{uuid4().hex[:8]}", "name": name, "arguments": args}]}


def say(kind: str, evidence: str | None, **extra) -> dict:
    value = {"say": "基于已查询事实完成本回合", "kind": kind, "to": [], "wake": [], "done": True,
             "changes": [], "escalate": None, "evidence": [{"id": evidence}] if evidence else []}
    value.update(extra)
    return {"content": json.dumps(value, ensure_ascii=False), "tool_calls": []}


def called(messages: list[dict]) -> list[str]:
    names = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name")
            if name:
                names.append(name)
    return names


def called_args(messages: list[dict], name: str) -> list[dict]:
    out = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name") == name:
                try:
                    out.append(json.loads(fn.get("arguments") or "{}"))
                except json.JSONDecodeError:
                    out.append({})
    return out


def last_evidence(messages: list[dict]) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        try:
            data = json.loads(message.get("content") or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("evidence_id"):
            return data["evidence_id"]
    return None


def prompt_text(messages: list[dict]) -> str:
    """只看本回合最新提示，避免历史轮次里的旧任务/旧驳回污染判断。"""
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def role_of(system: str) -> str:
    if "客户经理" in system:
        return "orchestrator"
    if "交通住宿专员" in system:
        return "logistics"
    if "本地体验与每日活动专员" in system:
        return "local_expert"
    if "独立财务与风控评审" in system:
        return "critic_agent"
    if "行程整合与文案专员" in system:
        return "writer"
    raise AssertionError("未知角色系统提示")


class ScriptedTeam:
    """按已发生的工具调用决定下一步；重试时保持同一逻辑位置。"""

    def __init__(self):
        self.calls = 0
        self.roles: dict[str, int] = {}

    async def chat_tools(self, messages, tools):
        self.calls += 1
        role = role_of(messages[0]["content"])
        self.roles[role] = self.roles.get(role, 0) + 1
        used = called(messages)
        evidence = last_evidence(messages)

        if role == "orchestrator":
            tasks_block = re.search(r"任务状态：(\{.*\})", prompt_text(messages))
            delivered = False
            if tasks_block:
                try:
                    tasks = json.loads(tasks_block.group(1))
                    delivered = bool(tasks) and all(t.get("status") == "delivered" for t in tasks.values())
                except json.JSONDecodeError:
                    delivered = False
            if "请主控将这些问题打回" in prompt_text(messages) or "待返工问题" in prompt_text(messages):
                return say("request", evidence, to=["logistics"], wake=["logistics"], rework=[
                    {"task_id": "T1", "reason": "换更便宜的酒店与航班，把总价压回预算内"},
                ])
            if not delivered and self.roles[role] == 1:
                return say("request", evidence, to=["logistics"], wake=["logistics"], tasks=[
                    {"id": "T1", "owner": "logistics", "goal": "选定往返航班与酒店，兼顾老人与预算"},
                    {"id": "T2", "owner": "local_expert", "goal": "按天气与体力排每日行程", "depends_on": ["T1"]},
                ])
            # 主控没有工具，只能引用专业交付转交的 evidence_ids。
            forwarded = re.findall(r'"evidence_ids":\s*\["([0-9a-f]{32})"', prompt_text(messages))
            if not forwarded:
                forwarded = re.findall(r'"evidence_ids":\s*\["([0-9a-f]{32})"', json.dumps(messages, ensure_ascii=False))
            return say("proposal", forwarded[-1] if forwarded else evidence,
                       to=["critic_agent"], wake=["critic_agent"])

        if role == "logistics":
            if "search_flights" not in used:
                return tool("search_flights")
            if "search_hotels" not in used:
                return tool("search_hotels")
            if "set_flights" not in used:
                return tool("set_flights", outbound_flight_no=OUTBOUND, inbound_flight_no=INBOUND)
            if "set_hotel" not in used:
                return tool("set_hotel", hotel_id=HOTEL)
            return say("proposal", evidence, to=["orchestrator"], delivery={
                "outbound_flight_no": OUTBOUND, "inbound_flight_no": INBOUND,
                "hotel_id": HOTEL, "rationale": "下午航班避免早起，经济酒店靠近地铁，老人友好",
            })

        if role == "local_expert":
            if "list_pois" not in used:
                return tool("list_pois")
            if "get_weather" not in used:
                return tool("get_weather")
            assigned = {a.get("date") for a in called_args(messages, "assign_pois")}
            for day in DAYS:
                if day not in assigned:
                    return tool("assign_pois", date=day, poi_ids=ASSIGNMENT[day])
            if "materialize_plan" not in used:
                return tool("materialize_plan")
            if "check_plan" not in used:
                return tool("check_plan")
            return say("proposal", evidence, to=["orchestrator"], delivery={
                "days": [{"date": day, "poi_ids": ASSIGNMENT[day]} for day in DAYS],
                "rationale": "雨天安排室内博物馆，抵达与返程日留白",
            })

        if role == "critic_agent":
            for name in ("get_plan", "check_plan", "day_costs", "experience_check"):
                if name not in used:
                    return tool(name)
            match = re.search(r"待审版本：(\d+)", prompt_text(messages))
            version = int(match.group(1)) if match else 1
            return say("accept", evidence, to=["writer"], wake=["writer"], approved_version=version)

        if role == "writer":
            if "get_plan" not in used:
                return tool("get_plan")
            return say("final", evidence, to=["orchestrator"], document={
                "title": "成都五日人文慢行（含老人）",
                "overview": "下午抵达、上午返程，中间三天以人文与室内博物馆为主，雨天不安排户外长时段。",
                "days": [
                    {"date": "2026-11-20", "summary": "下午航班抵达，入住后休息，不安排景点。"},
                    {"date": "2026-11-21", "summary": "武侯祠与锦里相邻，傍晚人民公园喝茶。"},
                    {"date": "2026-11-22", "summary": "小雨，安排成都博物馆与四川博物院两处室内场馆。"},
                    {"date": "2026-11-23", "summary": "杜甫草堂与宽窄巷子，步行强度低。"},
                    {"date": "2026-11-24", "summary": "上午航班返程，早餐后直接去机场。"},
                ],
                "practical_notes": ["酒店房型与早餐未核实，需向酒店确认", "博物馆需提前预约的政策未核实"],
            })

        raise AssertionError(role)


async def unresolved_conflict_reaches_user(session: dict) -> tuple[bool, dict | None]:
    """评审已到上限且仍有未解决问题时，必须升级给用户而不是静默失败。"""
    resumed = json.loads(json.dumps(session, ensure_ascii=False))
    resumed["reviews"] = 1
    resumed["pending_rejections"] = [{
        "owner": "logistics", "problem": "总价超出预算", "required_change": "换更便宜酒店",
    }]
    team = ScriptedTeam()
    result = await hub.run_session(
        brief=brief(), source=MockSource(), client=team, emit=lambda *_a, **_k: None,
        max_turns=6, max_rounds=1, resume=resumed,
    )
    escalation = result.escalation or {}
    ok = (
        escalation.get("type") == "review_rounds_exhausted"
        and {o["value"] for o in escalation.get("options", [])} == {"restart", "stop"}
        and not result.converged
    )
    return ok, escalation


async def main() -> int:
    team = ScriptedTeam()
    events: list[tuple[str, dict]] = []
    result = await hub.run_session(
        brief=brief(), source=MockSource(), client=team,
        emit=lambda kind, /, **payload: events.append((kind, payload)),
        max_turns=20, max_rounds=2,
    )
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"{'PASS' if cond else 'FAIL'} {name}{(': ' + detail) if detail and not cond else ''}")
        if not cond:
            failures.append(name)

    check("会话收敛", result.converged)
    check("没有升级为待确认", result.escalation is None, str(result.escalation)[:160])
    check("产出行程", result.itinerary is not None)
    check("产出文档", bool(result.ctx.document))
    check("批准版本与方案版本一致", result.ctx.approved_version == result.ctx.plan_version,
          f"{result.ctx.approved_version} vs {result.ctx.plan_version}")
    check("文档覆盖全部日期", [d["date"] for d in (result.ctx.document or {}).get("days", [])] == DAYS)
    check("调用数在默认预算内", team.calls <= 40, str(team.calls))
    check("五个角色都参与了", set(team.roles) == {"orchestrator", "logistics", "local_expert", "critic_agent", "writer"},
          str(team.roles))
    converged = [p for k, p in events if k == "converged"]
    check("发出收敛事件", bool(converged))
    print(f"调用次数 {team.calls}，各角色回合 {team.roles}，总花费 {result.itinerary.total_cost if result.itinerary else '-'}")

    print("\n[2] 预算类冲突到达用户升级")
    ok, escalation = await unresolved_conflict_reaches_user(result.session)
    check("评审上限后升级给用户", ok, str(escalation)[:240])
    check("升级说明评审已到上限", "评审" in str(escalation.get("question", "")), str(escalation)[:160])
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
