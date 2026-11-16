"""交通查询工具封装：查往返航班并按明确的时间/价格规则排序。

此模块不是 Agent；它不自主决定下一步，只返回候选、规则排序结果和数据源错误。
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, Field

from ..models import Flight, TripBrief
from ..tools.base import SourceError, TravelSource

# 去程希望中午前落地（当天下午还能安排活动）；返程希望下午再走（最后半天不浪费）
IDEAL_ARRIVAL = time(12, 0)
EARLIEST_RETURN = time(15, 0)


class TransportPlan(BaseModel):
    outbound: list[Flight] = Field(default_factory=list)
    inbound: list[Flight] = Field(default_factory=list)
    chosen_outbound: Flight | None = None
    chosen_inbound: Flight | None = None
    error: str | None = None

    @property
    def round_trip_cost(self) -> float:
        total = 0.0
        if self.chosen_outbound:
            total += self.chosen_outbound.price
        if self.chosen_inbound:
            total += self.chosen_inbound.price
        return total


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _score_outbound(f: Flight) -> float:
    late = max(0, _minutes(f.arr_time) - _minutes(IDEAL_ARRIVAL))
    return f.price / 1000 * 0.4 + late / 60 * 0.6


def _score_inbound(f: Flight) -> float:
    early = max(0, _minutes(EARLIEST_RETURN) - _minutes(f.dep_time))
    return f.price / 1000 * 0.4 + early / 60 * 0.6


def run(brief: TripBrief, source: TravelSource) -> TransportPlan:
    plan = TransportPlan()
    try:
        plan.outbound = sorted(
            source.search_flights(brief.origin, brief.destination, brief.start_date),
            key=_score_outbound,
        )
        plan.inbound = sorted(
            source.search_flights(brief.destination, brief.origin, brief.end_date),
            key=_score_inbound,
        )
    except SourceError as e:
        # 源不可用必须与"查无结果"区分开
        plan.error = f"航班数据源不可用，请手动确认交通：{e}"
        return plan

    if not plan.outbound:
        plan.error = f"未查到 {brief.origin}→{brief.destination} 的航班，请手动确认交通"
    else:
        plan.chosen_outbound = plan.outbound[0]

    if not plan.inbound:
        plan.error = (plan.error + "；" if plan.error else "") + (
            f"未查到 {brief.destination}→{brief.origin} 的返程航班，请手动确认"
        )
    else:
        plan.chosen_inbound = plan.inbound[0]

    return plan
