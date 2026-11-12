"""校验 agent：独立于 planner 检查可行性。

设计要点：planner 负责"排得好看"，critic 只负责"能不能落地"。
两者分离才能在演示时说清"为什么改了这一版"。

判定规则：
- blocker 触发回退重排（最多 2 轮）
- warning 只记录，不影响通过
"""

from __future__ import annotations

from datetime import time

from ..models import Critique, Issue, Itinerary, TripBrief
from .poi import PoiPlan
from .transport import TransportPlan
from .weather import WeatherPlan

MAX_DAY_MINUTES = 600
BUDGET_NEAR_RATIO = 0.9
AIRPORT_LEAD_MIN = 150

ELDER_UNFIT_TAGS = {"老人不宜", "老人慎选", "爬山", "体力消耗大"}


def _m(t: time) -> int:
    return t.hour * 60 + t.minute


def _active_minutes(day) -> int:
    return sum(i.duration_min for i in day.items if i.type != "rest")


def _timeline_issues(day, poi_map: dict, is_last: bool, transport: TransportPlan) -> list[Issue]:
    """时间轴本身是否成立。

    这里检查的是"这张表能不能照着走"：活动是否叠在一起、
    景点是不是在开放时间内、有没有错过赶飞机的时间。
    这些问题不该等到用户出门才被发现。
    """
    issues: list[Issue] = []
    acts = [i for i in day.items if i.type != "rest"]
    acts.sort(key=lambda i: (_m(i.start_time), _m(i.end_time)))

    # 1. 相邻活动不能重叠（航班也算：人在飞机上，不能同时安排别的）
    for a, b in zip(acts, acts[1:]):
        if _m(a.end_time) > _m(b.start_time):
            issues.append(
                Issue(
                    severity="blocker",
                    code="TIME_OVERLAP",
                    day=day.date,
                    message=(
                        f"{day.date} {a.title}（{a.start_time.strftime('%H:%M')}-"
                        f"{a.end_time.strftime('%H:%M')}）与 {b.title}"
                        f"（{b.start_time.strftime('%H:%M')}）时间重叠"
                    ),
                )
            )

    # 2. 景点必须落在开放时间内
    for item in acts:
        if item.type != "sight" or not item.ref_id:
            continue
        poi = poi_map.get(item.ref_id)
        if poi is None:
            continue
        if _m(item.start_time) < _m(poi.open_time) or _m(item.end_time) > _m(poi.close_time):
            issues.append(
                Issue(
                    severity="blocker",
                    code="OUTSIDE_OPEN_HOURS",
                    day=day.date,
                    message=(
                        f"{poi.name} 开放时间 {poi.open_time.strftime('%H:%M')}-"
                        f"{poi.close_time.strftime('%H:%M')}，"
                        f"却被排在 {item.start_time.strftime('%H:%M')}-{item.end_time.strftime('%H:%M')}"
                    ),
                )
            )

    # 3. 返程日不能错过赶飞机的时间
    if is_last and transport.chosen_inbound:
        deadline = _m(transport.chosen_inbound.dep_time) - AIRPORT_LEAD_MIN
        for item in acts:
            if item.type == "transport":
                continue
            if _m(item.start_time) >= deadline:
                issues.append(
                    Issue(
                        severity="blocker",
                        code="MISSED_AIRPORT_DEADLINE",
                        day=day.date,
                        message=(
                            f"{item.title} 排在 {item.start_time.strftime('%H:%M')}，"
                            f"但当天需在 {deadline // 60:02d}:{deadline % 60:02d} 前赶往机场"
                        ),
                    )
                )

    # 4. 一天里应该吃饭（长行程缺餐食说明排程把时间塞满了）
    if len(day.items) >= 3:
        titles = " ".join(i.title for i in day.items if i.type == "meal")
        if "午餐" not in titles:
            issues.append(
                Issue(
                    severity="warning",
                    code="MISSING_LUNCH",
                    day=day.date,
                    message=f"{day.date} 没有安排午餐",
                )
            )
        if "晚餐" not in titles:
            issues.append(
                Issue(
                    severity="warning",
                    code="MISSING_DINNER",
                    day=day.date,
                    message=f"{day.date} 没有安排晚餐",
                )
            )

    return issues


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
    is_last_day = itinerary.days[-1].date if itinerary.days else None
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

        # 时间轴本身是否立得住：重叠、开放时间、赶飞机、餐食
        issues.extend(
            _timeline_issues(day, poi_map, day.date == is_last_day, transport)
        )

        sights = [i for i in day.items if i.type == "sight"]
        # 最后一天以返程为主，没有景点属正常，不提示
        if not sights and len(itinerary.days) > 2 and day.date != is_last_day:
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
    if any(i.code in ("TIME_OVERLAP", "OUTSIDE_OPEN_HOURS") for i in blockers):
        suggestions.append("调整当天活动时段，避开开放时间边界并消除相互重叠")
    if any(i.code == "MISSED_AIRPORT_DEADLINE" for i in blockers):
        suggestions.append("压缩返程日安排，确保提前 150 分钟到机场")

    return Critique(passed=not blockers, issues=issues, suggestions=suggestions)
