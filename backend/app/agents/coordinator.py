"""检索 agent 之间的按需协作。

不是让它们全程互相聊天——那只是把成本翻倍。只在**发现真问题时**发起一次请求：

   住宿 agent 选完酒店后，协调者算了一下：这家酒店离用户想去的景点平均 6.2km。
   于是它去问景点 agent 要坐标，住宿 agent 带着坐标重选一次。

这解决的是一个真实缺陷：住宿 agent 只看"距市中心多近"，
看不到用户实际要去哪，很可能把酒店订在城市另一头。

协作有上限：一轮。发现不了问题就不打扰，发现了也只补一轮。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..models import Hotel, POI, TripBrief

# 平均距离超过这个值，就认为酒店位置和行程不匹配。
# 3km 是"走路不方便、每天必须打车"的量级，对带老人的行程已经不划算。
TOO_FAR_KM = 3.0
# 至少要有这么多个景点参与判断，否则样本太少不做结论
MIN_POIS = 3


@dataclass
class CollabRequest:
    """一次 agent 间的请求：谁问谁、问什么、为什么。"""

    sender: str
    receiver: str
    question: str
    reason: str
    payload: dict = field(default_factory=dict)


@dataclass
class CollabResult:
    requests: list[CollabRequest] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def needed(self) -> bool:
        return bool(self.requests)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def avg_distance(hotel: Hotel, pois: list[POI]) -> float:
    if not pois or hotel.lat == 0 or hotel.lng == 0:
        return 0.0
    dists = [haversine_km(hotel.lat, hotel.lng, p.lat, p.lng) for p in pois]
    return sum(dists) / len(dists)


def _preferred_pois(brief: TripBrief, pois: list[POI]) -> list[POI]:
    """只拿用户偏好相关的景点做判断，避免被无关景点拉偏。"""
    if not brief.preferences:
        return pois[:MIN_POIS + 2] if len(pois) >= MIN_POIS else pois
    from .poi import PREFERENCE_HINTS

    want: set[str] = set()
    for p in brief.preferences:
        want |= PREFERENCE_HINTS.get(p, set())
    matched = [p for p in pois if set(p.tags) & want or p.category in want]
    return matched if len(matched) >= MIN_POIS else pois


def review(
    brief: TripBrief,
    chosen_hotel: Hotel | None,
    pois: list[POI],
) -> CollabResult:
    """协调者：检查四路结果之间有没有对不上的地方。"""
    result = CollabResult()
    if chosen_hotel is None or not pois:
        return result

    focus = _preferred_pois(brief, pois)
    dist = avg_distance(chosen_hotel, focus)
    if dist > TOO_FAR_KM:
        result.requests.append(
            CollabRequest(
                sender="lodging",
                receiver="poi",
                question=f"我选的 {chosen_hotel.name} 到这些景点平均 {dist:.1f} km，太远了。请给出它们的坐标，我重选酒店",
                reason=f"平均距离 {dist:.1f} km 超过 {TOO_FAR_KM} km 阈值",
                payload={
                    "hotel": chosen_hotel.name,
                    "avg_km": round(dist, 2),
                    "poi_ids": [p.id for p in focus],
                },
            )
        )
        result.notes.append(
            f"住宿的位置和行程对不上：{chosen_hotel.name} 离你常去的几个景点平均 {dist:.1f} km"
        )

    return result
