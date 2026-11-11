"""校验 agent：独立于 planner 检查可行性。

设计要点：planner 负责"排得好看"，critic 只负责"能不能落地"。
两者分离才能在演示时说清"为什么改了这一版"。

判定规则：
- blocker 触发回退重排（最多 2 轮）
- warning 只记录，不影响通过
"""

from __future__ import annotations

from ..models import Critique, Issue, Itinerary, TripBrief
from .poi import PoiPlan
from .transport import TransportPlan
from .weather import WeatherPlan

MAX_DAY_MINUTES = 600
BUDGET_NEAR_RATIO = 0.9

ELDER_UNFIT_TAGS = {"老人不宜", "老人慎选", "爬山", "体力消耗大"}


def _active_minutes(day) -> int:
    return sum(i.duration_min for i in day.items if i.type != "rest")


def run(
    brief: TripBrief,
    itinerary: Itinerary,
    poi_plan: PoiPlan,
    transport: TransportPlan,
    weather: WeatherPlan,
) -> Critique:
    issues: list[Issue] = []
    poi_map = {p.id: p for p in poi_plan.candidates}
    rainy = weather.rainy_dates()

    # 1. 预算
    over = itinerary.total_cost - brief.budget_total
    if over > 0:
        issues.append(
            Issue(
                severity="blocker",
                code="BUDGET_EXCEEDED",
                message=f"总花费 {itinerary.total_cost:.0f} 超出预算 {over:.0f} 元"
                f"（{over / brief.budget_total:.0%}）",
            )
        )
    elif itinerary.total_cost > brief.budget_total * BUDGET_NEAR_RATIO:
        issues.append(
            Issue(
                severity="warning",
                code="BUDGET_NEAR",
                message=f"已使用预算 {itinerary.total_cost / brief.budget_total:.0%}，接近上限",
            )
        )

    # 2. 逐日检查
    for day in itinerary.days:
        active = _active_minutes(day)
        if active > MAX_DAY_MINUTES:
            issues.append(
                Issue(
                    severity="blocker",
                    code="DAY_TOO_LONG",
                    day=day.date,
                    message=f"{day.date} 活动 {active} 分钟，超过 {MAX_DAY_MINUTES} 分钟上限",
                )
            )

        sights = [i for i in day.items if i.type == "sight"]
        # 最后一天以返程为主，没有景点属正常，不提示
        is_last_day = day.date == itinerary.days[-1].date
        if not sights and len(itinerary.days) > 2 and not is_last_day:
            issues.append(
                Issue(
                    severity="warning",
                    code="DAY_EMPTY",
                    day=day.date,
                    message=f"{day.date} 没有任何景点安排",
                )
            )

        for item in sights:
            poi = poi_map.get(item.ref_id or "")
            if poi and not poi.is_open_on(day.date):
                issues.append(
                    Issue(
                        severity="blocker",
                        code="CLOSED_DAY",
                        day=day.date,
                        message=f"{poi.name} 在 {day.date}（周{'一二三四五六日'[day.date.weekday()]}）闭馆",
                    )
                )
            if poi and day.date in rainy and not poi.indoor:
                issues.append(
                    Issue(
                        severity="warning",
                        code="RAIN_OUTDOOR",
                        day=day.date,
                        message=f"{day.date} 有雨，{poi.name} 是户外项目",
                    )
                )
            if poi and brief.has_elder and set(poi.tags) & ELDER_UNFIT_TAGS:
                issues.append(
                    Issue(
                        severity="blocker",
                        code="ELDER_UNFIT",
                        day=day.date,
                        message=f"{poi.name} 强度对老人不友好（{'/'.join(poi.tags)}）",
                    )
                )

    # 3. 航班余座
    for f in (transport.chosen_outbound, transport.chosen_inbound):
        if f and f.seats_left < brief.travelers:
            issues.append(
                Issue(
                    severity="warning",
                    code="LOW_SEATS",
                    message=f"{f.flight_no} 仅剩 {f.seats_left} 座，不足 {brief.travelers} 人",
                )
            )

    blockers = [i for i in issues if i.severity == "blocker"]
    suggestions: list[str] = []
    if any(i.code == "BUDGET_EXCEEDED" for i in blockers):
        suggestions.append("改用更便宜的酒店，并优先免费景点")
    if any(i.code == "DAY_TOO_LONG" for i in blockers):
        suggestions.append("减少单日景点数量，或把长时项目拆到另一天")
    if any(i.code == "ELDER_UNFIT" for i in blockers):
        suggestions.append("替换为低强度景点或改为休整")

    return Critique(passed=not blockers, issues=issues, suggestions=suggestions)
