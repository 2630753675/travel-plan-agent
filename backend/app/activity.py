"""执行轨迹：把编排过程写成有层级的时间线。

层级约定（前端据此缩进与折叠）：
    depth=0  阶段标题        例：阶段: 并行检索
    depth=1  agent 的动作    例：交通 · 选中 MU5401
    depth=2  工具调用/证据   例：search_flights(上海→成都 11-20) → 4 条

group 决定归到哪个阶段块，前端按 group 分组、组内按 actor 分块，
这样"谁做了什么、调了什么工具"是一眼可读的，而不是一锅平铺。
"""

from __future__ import annotations

from datetime import datetime

from .models import LogEntry

ACTOR_LABEL: dict[str, str] = {
    "system": "系统",
    "clarify": "需求澄清",
    "transport": "交通",
    "lodging": "住宿",
    "poi": "景点",
    "weather": "天气",
    "planner": "行程规划",
    "critic": "可行性校验",
    "negotiation": "协商",
    "budget_officer": "预算官",
    "experience_officer": "体验官",
    "mediator": "调解者",
    "confirm": "等待确认",
    "ask": "等待确认",
    "user": "你",
}

GROUP_LABEL: dict[str, str] = {
    "clarify": "需求澄清",
    "retrieval": "并行检索",
    "plan": "行程规划",
    "check": "可行性校验",
    "negotiation": "预算与体验协商",
    "confirm": "等你决定",
}

# 节点 → 所属阶段块
NODE_GROUP: dict[str, str] = {
    "clarify": "clarify",
    "transport": "retrieval",
    "lodging": "retrieval",
    "poi": "retrieval",
    "weather": "retrieval",
    "planner": "plan",
    "critic": "check",
    "negotiation": "negotiation",
    "ask": "negotiation",
    "confirm": "confirm",
}

# 只有这些节点产生"阶段标题"行；并行检索的四路共用一个阶段块，不各发标题
STAGE_NODES = {"clarify", "planner", "critic", "negotiation", "ask", "confirm"}


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
    """一次工具调用的记录：函数名 + 入参摘要 → 返回摘要。"""
    return entry(
        actor,
        f"{tool}({args}) → {result}",
        kind="tool",
        group=group,
        depth=2,
        tool=tool,
    )
