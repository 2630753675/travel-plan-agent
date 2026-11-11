"""图状态定义。

约定：并行节点各写各的字段，只有 errors 需要 reducer 合并。
违反这个约定会在运行时直接冲突，不要图省事共用字段。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from ..agents.lodging import LodgingPlan
from ..agents.negotiators.protocol import NegotiationLog
from ..agents.poi import PoiPlan
from ..agents.transport import TransportPlan
from ..agents.weather import WeatherPlan
from ..models import Critique, Itinerary, TripBrief


class TripState(TypedDict, total=False):
    brief: TripBrief
    notes: list[str]
    transport: TransportPlan
    lodging: LodgingPlan
    poi: PoiPlan
    weather: WeatherPlan
    draft: Itinerary
    critique: Critique
    revision: int
    decision: str
    negotiation: NegotiationLog
    negotiated: bool
    forced_drops: list[str]
    force_cheaper_hotel: bool
    errors: Annotated[list[str], operator.add]
