"""执行轨迹：展示 Workflow 步骤、工具调用和 Agent 对话。

查询工具的调用是工具事实，不代表天气/交通/住宿/景点是 Agent；
Agent 对话由结构化消息协议单独流式投影。
"""

from __future__ import annotations

from datetime import datetime

from .models import LogEntry

ACTOR_LABEL: dict[str, str] = {
    "system": "系统",
    "clarify": "需求澄清 Workflow",
    "transport": "航班查询工具",
    "lodging": "酒店查询工具",
    "poi": "景点查询工具",
    "weather": "天气查询工具",
    "planner": "规划 Agent",
    "critic": "硬约束校验 Workflow",
    "budget_review": "预算评审 Agent",
    "experience_review": "体验评审 Agent",
    "negotiation": "预算与体验协商 Workflow",
    "budget_officer": "预算评审 Agent",
    "experience_officer": "体验评审 Agent",
    "mediator": "规则裁决",
    "confirm": "等待确认 Workflow",
    "ask": "等待确认 Workflow",
    "user": "你",
    "orchestrator": "团队统筹 Agent",
    "logistics": "交通住宿 Agent",
    "local_expert": "当地体验 Agent",
    "critic_agent": "方案评审 Agent",
    "writer": "行程撰稿 Agent",
    "team_protocol": "协作协议校验",
}

GROUP_LABEL: dict[str, str] = {
    "clarify": "需求澄清 Workflow",
    "retrieval": "查询工具",
    "collab": "工具协作 Workflow",
    "plan": "规划 Agent",
    "review": "Agent 评审",
    "check": "硬约束校验 Workflow",
    "negotiation": "预算与体验协商",
    "confirm": "等你决定",
}

NODE_GROUP: dict[str, str] = {
    "clarify": "clarify",
    "transport": "retrieval",
    "lodging": "retrieval",
    "poi": "retrieval",
    "weather": "retrieval",
    "coordinator": "collab",
    "lodging_fix": "collab",
    "planner": "plan",
    "critic": "check",
    "budget_review": "review",
    "experience_review": "review",
    "negotiation": "negotiation",
    "ask": "negotiation",
    "confirm": "confirm",
}

STAGE_NODES = {"clarify", "coordinator", "planner", "budget_review", "experience_review", "critic", "negotiation", "ask", "confirm"}


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def entry(
    actor: str,
    text: str,
    *,
    kind: str = "action",
    detail: str = "",
    group: str = "",
    depth: int = 1,
    tool: str = "",
) -> LogEntry:
    return LogEntry(
        ts=now(),
        actor=ACTOR_LABEL.get(actor, actor),
        kind=kind,  # type: ignore[arg-type]
        text=text,
        detail=detail,
        group=group,
        depth=depth,
        tool=tool,
    )


def stage(group: str) -> LogEntry:
    return entry("system", f"阶段: {GROUP_LABEL.get(group, group)}", kind="stage", group=group, depth=0)


def tool_call(actor: str, tool: str, args: str, result: str, *, group: str) -> LogEntry:
    return entry(
        actor,
        f"{tool}({args}) → {result}",
        kind="tool",
        group=group,
        depth=2,
        tool=tool,
    )
