"""协商各方的评估工具。

关键设计：每个角色掌握**不同的计算能力**，而不是同一份数据换一句 prompt。
没有这层差异，协商就退化成"多跑几轮说辞"。

- 预算官能算出：这一天的钱花在哪、砍掉某处能省多少
- 体验官能算出：每个景点在本行程里有多难被替代
- 两者结论天然冲突，冲突才值得协商
"""

from __future__ import annotations

from collections import Counter

from ...models import DayPlan, Itinerary, POI

# 不可替代性的权重构成。category 唯一性占大头：
# 一个城市里只有它这一类，砍掉就没有替代品。
W_CATEGORY = 0.4
W_DURATION = 0.2
W_TICKET = 0.2
W_INDOOR = 0.2


def poi_irreplaceability(candidates: list[POI]) -> dict[str, float]:
    """给每个景点打 0-1 的"不可替代"分。

    纯规则，可解释：同类越稀缺、耗时越长、门票越贵、越不可替代。
    """
    if not candidates:
        return {}
    total = len(candidates)
    category_count = Counter(p.category for p in candidates)
    indoor_count = sum(1 for p in candidates if p.indoor)
    outdoor_count = total - indoor_count
    max_duration = max(p.duration_min for p in candidates) or 1
    max_ticket = max(p.ticket_price for p in candidates) or 1

    scores: dict[str, float] = {}
    for p in candidates:
        # 同类越少分越高
        cat = 1.0 / category_count[p.category]
        dur = p.duration_min / max_duration
        ticket = p.ticket_price / max_ticket
        # 室内/室外各只有一份时更不可替代
        same_kind = indoor_count if p.indoor else outdoor_count
        indoor = 1.0 / max(1, same_kind)
        scores[p.id] = round(
            W_CATEGORY * cat + W_DURATION * dur + W_TICKET * ticket + W_INDOOR * indoor, 3
        )
    return scores


def day_cost_breakdown(day: DayPlan) -> dict:
    """把一天的钱拆开，预算官据此决定从哪砍。"""
    buckets: dict[str, float] = {"transport": 0.0, "sight": 0.0, "meal": 0.0, "hotel": 0.0}
    for item in day.items:
        if item.type in buckets:
            buckets[item.type] += item.cost
        else:
            buckets["transport"] += item.cost  # rest 之类归入杂项
    buckets["meal"] += day.meal_estimate
    return {
        "date": str(day.date),
        "total": round(day.day_cost, 2),
        "by_type": {k: round(v, 2) for k, v in buckets.items()},
        "items": [
            {"title": i.title, "type": i.type, "cost": round(i.cost, 2), "ref_id": i.ref_id}
            for i in day.items
            if i.type != "rest" and i.cost > 0
        ],
    }


def day_experience(
    day: DayPlan, scores: dict[str, float], poi_by_id: dict[str, POI]
) -> dict:
    """体验官的一天评分：由当天景点的不可替代性加权。"""
    sights = [i for i in day.items if i.type == "sight" and i.ref_id]
    if not sights:
        return {"date": str(day.date), "score": 0.0, "sights": [], "note": "当天没有景点"}

    detail = []
    for item in sights:
        pid = item.ref_id or ""
        poi = poi_by_id.get(pid)
        detail.append(
            {
                "id": pid,
                "title": item.title,
                "irreplaceability": scores.get(pid, 0.0),
                "tags": poi.tags if poi else [],
            }
        )
    avg = sum(d["irreplaceability"] for d in detail) / len(detail)
    # 一天里最高那项的权重更高：一天的"值得程度"往往由最好的那个决定
    top = max(d["irreplaceability"] for d in detail)
    score = round(0.6 * top + 0.4 * avg, 3)
    return {"date": str(day.date), "score": score, "sights": detail}


def whole_trip_view(itinerary: Itinerary, scores: dict[str, float], poi_by_id: dict[str, POI]) -> dict:
    """给两个角色看的总览：每天花了多少、值多少。"""
    days = []
    for day in itinerary.days:
        cost = day_cost_breakdown(day)
        exp = day_experience(day, scores, poi_by_id)
        days.append(
            {
                "date": str(day.date),
                "cost": cost["total"],
                "cost_by_type": cost["by_type"],
                "experience": exp["score"],
                "sights": exp["sights"],
            }
        )
    return {
        "total_cost": round(itinerary.total_cost, 2),
        "budget": itinerary.budget_total,
        "over": round(itinerary.total_cost - itinerary.budget_total, 2),
        "days": days,
    }
