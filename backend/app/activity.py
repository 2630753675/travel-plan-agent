"""动作日志：把编排过程写成一条时间线。

设计原则：
- 每条都必须是具体动作（选中哪个航班、砍掉哪个景点），不写"执行中"这类空话
- 系统阶段单独成行（阶段: 检索），便于定位
- 时间戳精确到秒，按发生顺序排列
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
    "user": "你",
}


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def entry(actor: str, text: str, *, kind: str = "action", detail: str = "") -> LogEntry:
    return LogEntry(
        ts=now(),
        actor=ACTOR_LABEL.get(actor, actor),
        kind=kind,  # type: ignore[arg-type]
        text=text,
        detail=detail,
    )


def stage(name: str) -> LogEntry:
    return entry("system", f"阶段: {name}", kind="stage")


STAGE_LABEL: dict[str, str] = {
    "clarify": "需求澄清",
    "transport": "查航班",
    "lodging": "查酒店",
    "poi": "查景点",
    "weather": "查天气",
    "planner": "排行程",
    "critic": "可行性校验",
    "negotiation": "预算与体验协商",
    "confirm": "等待你决定",
}
