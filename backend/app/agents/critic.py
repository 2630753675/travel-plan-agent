"""硬约束校验 Workflow：独立于规划逻辑检查方案能否执行。

这是确定性验证器，不是 Agent。它检查预算、时间轴、开放时间、返程截止和同行人群，
blocker 由 LangGraph 路由回 Planner，warning 只记录不阻断。
"""

from __future__ import annotations

from datetime import time

from ..models import Critique, Issue, Itinerary, TripBrief
from .planner import AIRPORT_TRANSFER_MIN, ARRIVAL_TRANSFER_MIN
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


def _timeline_issues(day, poi_map: dict, is_first: bool, is_last: bool, transport: TransportPlan) -> list[Issue]:
    """时间轴本身是否成立。

    这里检查的是"这张表能不能照着走"：活动是否叠在一起、
    景点是不是在开放时间内、有没有错过赶飞机的时间。
    这些问题不该等到用户出门才被发现。
    """
    issues: list[Issue] = []
    acts = [i for i in day.items if i.type != "rest"]
    acts.sort(key=lambda i: (_m(i.start_time), _m(i.end_time)))

    for index, item in enumerate(acts):
        for other in acts[index + 1:]:
            if _m(item.start_time) < _m(other.end_time) and _m(other.start_time) < _m(item.end_time):
                issues.append(
                    Issue(
                        severity="blocker",
                        code="TIME_OVERLAP",
                        day=day.date,
                        message=(
                            f"{day.date} {item.title}（{item.start_time.strftime('%H:%M')}-"
                            f"{item.end_time.strftime('%H:%M')}）与 {other.title}"
                            f"（{other.start_time.strftime('%H:%M')}-{other.end_time.strftime('%H:%M')}）时间重叠"
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
        deadline = _m(transport.chosen_inbound.dep_time) - AIRPORT_LEAD_MIN - AIRPORT_TRANSFER_MIN
        transfers = [i for i in acts if i.type == "fixed" and i.ref_id == "fixed:airport_transfer"]
        if len(transfers) > 1:
            detail = "、".join(
                f"{t.title}（{t.start_time.strftime('%H:%M')}-{t.end_time.strftime('%H:%M')}）"
                for t in transfers
            )
            issues.append(
                Issue(
                    severity="blocker",
                    code="AIRPORT_TRANSFER_DUPLICATE",
                    day=day.date,
                    message=f"返程日存在多条互斥的送机安排：{detail}；必须只保留一条，并满足提前量要求",
                )
            )
        for item in acts:
            if item.type == "transport":
                continue
            if item.type == "fixed" and item.ref_id == "fixed:airport_transfer":
                # 接驳本身发生在离城之后，单独按“到达机场不晚于提前量”检查。
                if _m(item.end_time) > _m(transport.chosen_inbound.dep_time) - AIRPORT_LEAD_MIN:
                    issues.append(
                        Issue(
                            severity="blocker",
                            code="AIRPORT_TRANSFER_LATE",
                            day=day.date,
                            message=(
                                f"{item.title} 到达 {item.end_time.strftime('%H:%M')}，"
                                f"晚于航班起飞前 {AIRPORT_LEAD_MIN} 分钟到机场的要求"
                            ),
                        )
                    )
                continue
            if _m(item.start_time) >= deadline or _m(item.end_time) > deadline:
                issues.append(
                    Issue(
                        severity="blocker",
                        code="MISSED_AIRPORT_DEADLINE",
                        day=day.date,
                        message=(
                            f"{item.title} 排在 {item.start_time.strftime('%H:%M')}，"
                            f"但估算最晚 {deadline // 60:02d}:{deadline % 60:02d} 需离开市区"
                            f"（含地面交通与提前到机场），请按实际交通核实"
                        ),
                    )
                )

    # 飞行占用整段时间；返程航班之后的餐食不是当日地面活动，不得计为固定行程。
    if is_first and transport.chosen_outbound:
        arrival_ready = _m(transport.chosen_outbound.arr_time) + ARRIVAL_TRANSFER_MIN
        for item in day.items:
            if item.type == "meal" and _m(item.start_time) < arrival_ready:
                issues.append(Issue(severity="blocker", code="MEAL_BEFORE_ARRIVAL", day=day.date,
                                    message=f"{item.title} 安排在抵达并完成转场前"))
    if is_last and transport.chosen_inbound:
        departure = _m(transport.chosen_inbound.dep_time)
        airport_departure = departure - AIRPORT_LEAD_MIN - AIRPORT_TRANSFER_MIN
        for item in day.items:
            if item.type == "meal" and _m(item.start_time) >= departure:
                issues.append(Issue(severity="blocker", code="MEAL_AFTER_FLIGHT", day=day.date,
                                    message=f"{item.title} 安排在返程航班起飞之后"))
            elif item.type == "meal" and _m(item.end_time) > airport_departure:
                issues.append(Issue(severity="blocker", code="MEAL_AFTER_AIRPORT_DEPARTURE", day=day.date,
                                    message=f"{item.title} 与估算的最晚离开市区时间冲突；机场用餐应标为未定时活动"))

    # 餐食只在有合理时间窗时要求；晚班抵达日不因跳过已错过的餐次报错。
    arrival_late = is_first and transport.chosen_outbound and _m(transport.chosen_outbound.arr_time) + ARRIVAL_TRANSFER_MIN >= 21 * 60
    if len(day.items) >= 3 and not arrival_late and not (is_last and transport.chosen_inbound):
        meal_notes = " ".join(day.notes)
        if day.meal_estimate <= 0 and "自行安排" not in meal_notes and "没有足够午餐时间窗" not in meal_notes:
            issues.append(
                Issue(
                    severity="warning",
                    code="MISSING_LUNCH",
                    day=day.date,
                    message=f"{day.date} 没有安排午餐",
                )
            )
        if "晚餐休息窗口" not in meal_notes and "自行安排晚餐" not in meal_notes:
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
            _timeline_issues(
                day,
                poi_map,
                day.date == itinerary.days[0].date,
                day.date == is_last_day,
                transport,
            )
        )

        sights = [i for i in day.items if i.type == "sight"]
        arrival_day_late = (
            transport.chosen_outbound is not None
            and day.date == itinerary.days[0].date
            and _m(transport.chosen_outbound.arr_time) + ARRIVAL_TRANSFER_MIN >= 18 * 60
        )
        # 最后一天以返程为主、晚班抵达日以入住为主，没有景点属正常
        if not sights and len(itinerary.days) > 2 and day.date != is_last_day and not arrival_day_late:
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
