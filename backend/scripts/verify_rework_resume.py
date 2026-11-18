"""Critic objection -> scoped repair -> patch delivery -> resume -> issue closure.

不调用真实模型，也不访问网络。
运行：
    .venv/Scripts/python.exe scripts/verify_rework_resume.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import verify_peer_success as base  # noqa: E402
from app.agents import hub  # noqa: E402
from app.llm import LLMError  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402

EXTRA_TITLE = "重复返程送机"


def records(messages):
    out = []
    for msg in messages:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out.append((fn.get("name"), args, call.get("id")))
    return out


def tasks_from_prompt(messages):
    text = base.prompt_text(messages)
    match = re.search(r"任务状态：(\{.*?\})\n当前任务：", text, re.S)
    return json.loads(match.group(1)) if match else {}


class RepairTeam(base.ScriptedTeam):
    """首轮制造重复送机，返工回合可模拟中断或执行受限修复。"""

    def __init__(self, *, interrupt_on_repair=False, remove_event_id=None):
        super().__init__()
        self.interrupt_on_repair = interrupt_on_repair
        self.remove_event_id = remove_event_id
        self.rework_tool_schemas: list[list[str]] = []
        self.saw_patch = False

    async def chat_tools(self, messages, tools):
        role = base.role_of(messages[0]["content"])
        text = base.prompt_text(messages)
        all_records = records(messages)

        if role == "orchestrator" and "请按 Issue Ledger 将问题打回" in text:
            evidence = base.last_evidence(messages)
            return base.say("request", evidence, to=["local_expert"], wake=["local_expert"], rework=[
                {"task_id": "T2", "reason": "删除重复返程送机并复核行程"},
            ])

        if role == "local_expert":
            task_state = tasks_from_prompt(messages).get("T2", {})
            if task_state.get("allowed_repair_tools"):
                self.rework_tool_schemas.append([t["function"]["name"] for t in tools])
                if self.interrupt_on_repair:
                    raise LLMError("脚本模拟：返工开始时中断")
                if not self.remove_event_id:
                    raise AssertionError("恢复用例必须提供真实 event_id")
                names = [r[0] for r in all_records]
                if "remove_fixed_event" not in names:
                    return base.tool("remove_fixed_event", event_id=self.remove_event_id)
                after = names[names.index("remove_fixed_event"):]
                if "materialize_plan" not in after:
                    return base.tool("materialize_plan")
                if "check_plan" not in after:
                    return base.tool("check_plan")
                base_version = (task_state.get("delivery_base") or {}).get("version")
                if type(base_version) is not int:
                    raise AssertionError("恢复后丢失差异交付基线版本")
                self.saw_patch = True
                return base.say("proposal", base.last_evidence(messages), to=["orchestrator"],
                                delivery_patch={
                                    "base_version": base_version,
                                    "rationale": "删除重复送机后复核，逐日景点未变",
                                })

            names = [r[0] for r in all_records]
            added = sum(1 for r in all_records if r[0] == "add_fixed_event")
            if added == 0:
                return base.tool("add_fixed_event", date="2026-11-24", title="返程送机",
                                 start_time="05:40", end_time="06:40", kind="airport_transfer")
            if added == 1:
                return base.tool("add_fixed_event", date="2026-11-24", title=EXTRA_TITLE,
                                 start_time="07:00", end_time="08:20", kind="airport_transfer")
            for step in ("materialize_plan", "check_plan"):
                if names.count(step) == 0:
                    return base.tool(step)

        if role == "critic_agent":
            packet_match = re.search(r"结构化 ReviewPacket（唯一事实来源）：(\{.*\})", text)
            packet = json.loads(packet_match.group(1)) if packet_match else {}
            issues = (packet.get("hard_check") or {}).get("issues", [])
            duplicate = next((i for i in issues if i.get("code") == "AIRPORT_TRANSFER_DUPLICATE"), None)
            if duplicate:
                return base.say("objection", None, to=["orchestrator"], changes=[{
                    "code": "AIRPORT_TRANSFER_DUPLICATE",
                    "day": duplicate.get("day"),
                    "owner": "local_expert",
                    "acceptance": "返程日只保留一条送机安排，硬校验通过",
                }])
            version_match = re.search(r"待审版本：(\d+)", text)
            version = int(version_match.group(1)) if version_match else 1
            return base.say("accept", None, to=["writer"], wake=["writer"],
                            approved_version=version, say="基于当前 ReviewPacket 完成复核")

        return await super().chat_tools(messages, tools)


async def main():
    first = RepairTeam(interrupt_on_repair=True)
    interrupted = await hub.run_session(
        brief=base.brief(), source=MockSource(), client=first,
        emit=lambda *a, **k: None, max_turns=30, max_rounds=2,
    )
    assert not interrupted.converged
    assert interrupted.escalation and interrupted.escalation.get("type") == "agent_turn_failed", \
        interrupted.escalation
    assert interrupted.session["pending_agent"] == "local_expert"
    issue = next(i for i in interrupted.session["pending_issue_codes"]
                 if i["code"] == "AIRPORT_TRANSFER_DUPLICATE")
    task = interrupted.session["tasks"]["T2"]
    assert task["delivery"] is None and task["delivery_base"]["version"] > 0
    assert set(task["allowed_repair_tools"]) >= {"remove_fixed_event", "materialize_plan", "check_plan"}
    events = interrupted.session["context"]["fixed_events"]["2026-11-24"]
    extra = next(e for e in events if e["title"] == EXTRA_TITLE and e["kind"] == "airport_transfer")
    # 返工模型必须能自己读到 event_id，否则"删除固定事件"在真实模型下不可执行。
    from app.agents.tools_spec import execute
    plan = execute(interrupted.ctx, "get_plan", {})
    visible = [e for e in (plan.get("fixed_events") or {}).get("2026-11-24", [])
               if e.get("title") == EXTRA_TITLE]
    assert visible and visible[0].get("event_id") == extra["event_id"], visible
    assert "get_plan" in set(task["allowed_repair_tools"])
    print("PASS repair scope exposes get_plan and its fixed_events[].event_id")
    print("PASS Critic objection creates issue-scoped repair task; interruption keeps baseline")

    resumed_client = RepairTeam(remove_event_id=extra["event_id"])
    resumed = await hub.run_session(
        brief=base.brief(), source=MockSource(), client=resumed_client,
        emit=lambda *a, **k: None, max_turns=20, max_rounds=2, resume=interrupted.session,
    )
    assert resumed.converged, resumed.escalation
    assert resumed.ctx.hard_check and resumed.ctx.hard_check["passed"]
    closed = next(i for i in resumed.session["pending_issue_codes"] if i["issue_id"] == issue["issue_id"])
    assert closed["status"] == "closed" and closed["fixed_in_version"] == resumed.ctx.plan_version
    assert resumed.session["tasks"]["T1"]["status"] == "delivered"
    assert resumed.session["tasks"]["T2"]["status"] == "delivered"
    assert "delivery_base" not in resumed.session["tasks"]["T2"]
    changes = resumed.session["tasks"]["T2"].get("changes")
    assert isinstance(changes, list) and changes, "返工交付应留下变更摘要"
    assert changes[-1]["plan_version"] == resumed.ctx.plan_version
    assert isinstance(changes[-1]["fields"], list)
    assert resumed.ctx.document
    assert resumed_client.saw_patch
    assert resumed_client.roles.get("logistics", 0) == 0
    allowed = set(resumed.session["tasks"]["T2"].get("allowed_repair_tools") or [])
    assert resumed_client.rework_tool_schemas, "返工回合应当有工具 schema"
    for names in resumed_client.rework_tool_schemas:
        assert set(names) <= allowed | {"remove_fixed_event", "replace_fixed_event"}
    print("PASS resume keeps scoped tools, submits patch delivery, closes issue, reaches Writer")
    print(f"PASS continuation used {resumed_client.calls} scripted requests and did not rerun logistics")


if __name__ == "__main__":
    asyncio.run(main())
