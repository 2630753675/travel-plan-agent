"""景点 agent：拉取候选并做人群/偏好适配排序。

硬过滤只用两类：明确不适合该人群的（老人不宜、夜生活对儿童），
其余靠排序，避免候选池被清空后 planner 无解。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import POI, TripBrief
from ..tools.base import SourceError, TravelSource

ELDER_BLOCK = {"老人不宜", "老人慎选", "爬山", "体力消耗大"}
CHILD_BLOCK = {"不适合儿童老人"}

PREFERENCE_HINTS = {
    "美食": {"小吃", "美食"},
    "自然": {"自然", "自然人文", "爬山"},
    "人文": {"人文", "三国", "园林"},
    "购物": {"购物"},
    "慢节奏": {"慢节奏", "喝茶", "休闲"},
    "夜景": {"夜景", "夜生活"},
}


class PoiPlan(BaseModel):
    candidates: list[POI] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    error: str | None = None


def _blocked(poi: POI, brief: TripBrief) -> str | None:
    tags = set(poi.tags)
    if brief.has_elder and tags & ELDER_BLOCK:
        return "老人同行不适宜"
    if brief.has_child and tags & CHILD_BLOCK:
        return "儿童同行不适宜"
    return None


def _preference_score(poi: POI, brief: TripBrief) -> int:
    if not brief.preferences:
        return 0
    want: set[str] = set()
    for p in brief.preferences:
        want |= PREFERENCE_HINTS.get(p, set())
    return len(set(poi.tags) & want) + (1 if poi.category in want else 0)


def run(brief: TripBrief, source: TravelSource) -> PoiPlan:
    try:
        pois = source.list_pois(brief.destination)
    except SourceError as e:
        return PoiPlan(error=f"景点数据源不可用，请手动确认景点：{e}")

    if not pois:
        return PoiPlan(error=f"未查到 {brief.destination} 的景点数据")

    kept: list[POI] = []
    excluded: list[str] = []
    for poi in pois:
        reason = _blocked(poi, brief)
        if reason:
            excluded.append(f"{poi.name}（{reason}）")
        else:
            kept.append(poi)

    kept.sort(key=lambda p: (_preference_score(p, brief), -p.ticket_price), reverse=True)
    return PoiPlan(candidates=kept, excluded=excluded)
