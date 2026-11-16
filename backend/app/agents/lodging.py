"""住宿查询工具封装：拉取候选酒店并按人群与性价比规则排序。

人群影响排序而不是硬过滤——老人同行时优先"老人友好"，儿童同行时优先"亲子"，
但不会直接剔除不合格的，以免候选为空时无解。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import Hotel, POI, TripBrief
from ..tools.base import SourceError, TravelSource

# 市区游的可接受住宿半径（公里）。mock 里都江堰民宿 55km，不应进入城市游候选。
CITY_RADIUS_KM = 12.0

# 协作后重选时，"离想去的景点近"所占的权重
LOCATION_WEIGHT = 0.35


class LodgingPlan(BaseModel):
    candidates: list[Hotel] = Field(default_factory=list)
    chosen: Hotel | None = None
    error: str | None = None

    def cheaper_than(self, h: Hotel) -> Hotel | None:
        """返回比 h 便宜一档的市内酒店。

        必须限制在市区范围内：只按价格降档会选到 55km 外的民宿，
        对城市游来说这不是"省钱"，是换了另一个目的地。
        """
        options = [
            c
            for c in self.candidates
            if c.price_per_night < h.price_per_night and c.distance_to_center_km <= CITY_RADIUS_KM
        ]
        if not options:
            return None
        return max(options, key=lambda c: c.price_per_night)


def _score(h: Hotel, brief: TripBrief, focus_pois: list[POI] | None = None) -> float:
    price_score = 1 - min(h.price_per_night, 2000) / 2000
    dist_score = 1 - min(h.distance_to_center_km, 20) / 20
    score = h.rating / 5 * 0.4 + price_score * 0.35 + dist_score * 0.25
    if brief.has_elder and "老人友好" in h.tags:
        score += 0.15
    if brief.has_child and "亲子" in h.tags:
        score += 0.15
    if focus_pois and h.lat:
        from .coordinator import avg_distance

        # 和用户真正要去的景点近，比"离市中心近"更有意义
        d = avg_distance(h, focus_pois)
        score += (1 - min(d, 15) / 15) * LOCATION_WEIGHT
    return score


def run(
    brief: TripBrief,
    source: TravelSource,
    *,
    budget_pressure: bool = False,
    focus_pois: list[POI] | None = None,
) -> LodgingPlan:
    nights = max(brief.days - 1, 0)
    try:
        hotels = source.search_hotels(brief.destination, brief.start_date, nights)
    except SourceError as e:
        return LodgingPlan(error=f"酒店数据源不可用，请手动确认住宿：{e}")

    if not hotels:
        return LodgingPlan(error=f"未查到 {brief.destination} 的酒店，请手动确认住宿")

    # 城市游只在市区范围内选；市内无候选时才退回全量，避免无解
    in_city = [h for h in hotels if h.distance_to_center_km <= CITY_RADIUS_KM]
    pool = in_city or hotels

    ranked = sorted(pool, key=lambda h: _score(h, brief, focus_pois), reverse=True)
    if focus_pois:
        # 协作重选时位置优先：先圈出离目标景点最近的几家，再在里面挑性价比最高的。
        # 只微调权重是不够的——权重差几百分之一，结果根本不会变，协作就成了走过场。
        from .coordinator import avg_distance

        nearest = sorted(pool, key=lambda h: avg_distance(h, focus_pois) if h.lat else 1e9)[:3]
        ranked = sorted(nearest, key=lambda h: _score(h, brief, focus_pois), reverse=True)
    if budget_pressure:
        # 预算压力下优先低价，评分只作次级排序
        ranked = sorted(ranked, key=lambda h: (h.price_per_night, -h.rating))

    return LodgingPlan(candidates=ranked, chosen=ranked[0])
