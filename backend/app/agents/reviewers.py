"""独立评审 Agent。

评审 Agent 不负责修改行程，也不直接调用旅行数据源；它们围绕 Planner 的同一份
提案，使用已有的确定性计算工具形成独立意见。最终是否可执行仍由 critic workflow
检查硬约束。
"""

from __future__ import annotations

from ..models import Itinerary, POI, TripBrief
from .negotiators.tools import day_cost_breakdown, day_experience, poi_irreplaceability


def budget_review(brief: TripBrief, itinerary: Itinerary) -> tuple[str, str, list[dict]]:
    over = itinerary.total_cost - brief.budget_total
    costs = [day_cost_breakdown(day) for day in itinerary.days]
    if over > 0:
        suggestion = "保留每天至少一个景点，优先换更便宜酒店并削减可替代付费项"
        content = f"这份提案超预算 {over:.0f} 元。建议：{suggestion}。"
        reason = "预算评审对比总预算与逐日费用构成"
    else:
        suggestion = "无需为预算削减项目"
        content = f"这份提案在预算内，剩余 {abs(over):.0f} 元；{suggestion}。"
        reason = "预算评审确认总额没有超过用户上限"
    evidence = [{"kind": "budget", "total": itinerary.total_cost, "budget": brief.budget_total, "over": over, "days": costs}]
    return content, reason, evidence + [{"kind": "recommendation", "action": "downgrade" if over > 0 else "accept", "instruction": suggestion}]


def experience_review(brief: TripBrief, itinerary: Itinerary, candidates: list[POI]) -> tuple[str, str, list[dict]]:
    by_id = {p.id: p for p in candidates}
    scores = poi_irreplaceability(candidates)
    concerns: list[str] = []
    evidence: list[dict] = []
    if brief.has_elder:
        for day in itinerary.days:
            for item in day.items:
                poi = by_id.get(item.ref_id or "")
                if poi and set(poi.tags) & {"老人不宜", "老人慎选", "爬山", "体力消耗大"}:
                    concerns.append(f"{poi.name} 对老人同行风险较高")
    daily_sights = {
        day.date.isoformat(): [item for item in day.items if item.type == "sight"]
        for day in itinerary.days[:-1]
    }
    empty_days = [day for day, sights in daily_sights.items() if not sights]
    if empty_days:
        concerns.append(f"这些日期没有景点安排：{'、'.join(empty_days)}")
    for day in itinerary.days:
        for item in day.items:
            if item.type == "sight" and item.ref_id in scores:
                evidence.append({"poi_id": item.ref_id, "title": item.title, "irreplaceability": scores[item.ref_id]})
    if concerns:
        content = "我发现体验风险：" + "；".join(concerns[:3]) + "，请规划 Agent 调整。"
        recommendation = {"action": "revise", "concerns": concerns}
    else:
        content = f"当前安排符合同行人群与节奏，已评估 {len(evidence)} 个景点的体验价值。"
        recommendation = {"action": "accept"}
    return content, "体验评审依据同行人群、每日安排与景点不可替代性提出意见", [{"kind": "experience", "concerns": concerns, "scores": evidence}, recommendation]
