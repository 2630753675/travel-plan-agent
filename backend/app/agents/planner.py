"""行程规划 agent：把检索结果组装成按天日程。

本阶段用确定性排程（不调 LLM），保证：
- 闭馆日、开放时间、雨天、人群限制在生成时就已规避；
- 返程日留足赶飞机时间，不安排远距离或长时项目；
- 受 critique 反馈影响可做出可解释的降配（换便宜酒店、砍付费景点、减少单日景点数）。

时间统一按"当日分钟数"计算，避免 time 类型跨天比较的坑。
"""

from __future__ import annotations

import math
from datetime import date, time, timedelta

from ..models import Critique, DayPlan, FixedEvent, Hotel, Item, Itinerary, POI, TripBrief, WeatherDay
from .lodging import LodgingPlan
from .poi import PoiPlan
from .transport import TransportPlan
from .weather import WeatherPlan

MEAL_LUNCH = 60
MEAL_DINNER = 80
LOCAL_TRANSPORT_PER_PERSON_DAY = 50

DAY_START = 8 * 60 + 30
DAY_LATEST_POI_START = 17 * 60
LUNCH_START = 12 * 60
LUNCH_END = 13 * 60
DINNER_START = 18 * 60 + 30

MAX_POI_MINUTES_LAST_DAY = 120
# 超过这个时长算"半天级"项目：单日已经超时时，这类项目要先让路
HALF_DAY_MINUTES = 240
AIRPORT_LEAD_MIN = 150
AIRPORT_TRANSFER_MIN = 90
ARRIVAL_TRANSFER_MIN = 90
POI_PER_DAY = {"轻松": 2, "适中": 3, "紧凑": 4}


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _to_time(m: int) -> time:
    m = max(0, min(m, 23 * 60 + 59))
    return time(m // 60, m % 60)


def _slot_for(m: int) -> str:
    if m < LUNCH_START:
        return "上午"
    if m < DINNER_START:
        return "下午"
    return "晚上"


def _rooms(travelers: int) -> int:
    return max(1, math.ceil(travelers / 2))


def _pick_hotel(lodging: LodgingPlan, brief: TripBrief, budget_pressure: bool) -> Hotel | None:
    if lodging.chosen is None:
        return None
    if not budget_pressure:
        return lodging.chosen
    return lodging.cheaper_than(lodging.chosen) or lodging.chosen


def _preference_score(poi: POI, preferences: list[str]) -> int:
    """偏好命中打分：类别命中权重最高，标签命中累积。

    类别（人文/自然/美食/购物）是粗粒度主题，标签是细粒度特征；
    用户勾"人文"时，一个 category=人文 的景点比"碰巧有个相关标签"的更该选中。
    """
    if not preferences:
        return 0
    score = 2 if poi.category in preferences else 0
    score += len(set(poi.tags) & set(preferences))
    return score


def _select_for_day(
    pool: list[POI],
    used: set[str],
    d: date,
    rainy: bool,
    want: int,
    prefer_free: bool,
    is_last_day: bool,
    preferences: list[str] | None = None,
    duration_pressure: bool = False,
) -> list[POI]:
    preferences = preferences or []
    avail = [p for p in pool if p.id not in used and p.is_open_on(d)]
    # 夜间才开门的项目在本版不排（会和晚餐/入住冲突）
    avail = [p for p in avail if _minutes(p.open_time) < DAY_LATEST_POI_START]
    if is_last_day:
        # 返程日只排短时、非长途项目
        avail = [
            p
            for p in avail
            if p.duration_min <= MAX_POI_MINUTES_LAST_DAY and "需半天" not in p.tags
        ]
    if duration_pressure:
        # 上一轮被判单日超时：只减数量不够，半天级项目也要让路，
        # 否则 300 分钟的山加上两三个景点永远压不进 600 分钟上限。
        avail = [p for p in avail if p.duration_min <= HALF_DAY_MINUTES and "需半天" not in p.tags]
    if rainy:
        indoor = [p for p in avail if p.indoor]
        if indoor:
            avail = indoor + [p for p in avail if not p.indoor]

    # 排序决定选哪些：先看偏好命中，再（预算吃紧时）看票价。
    # 只用这两个键、不追加时长排序——sort 是稳定的，其余保持数据原序，
    # 这样"没有任何偏好"时选出来的景点和改动前完全一致。
    def sort_key(p: POI) -> tuple:
        pref = -_preference_score(p, preferences)
        price = p.ticket_price if prefer_free else 0
        return (pref, price)

    avail.sort(key=sort_key)
    picked: list[POI] = []
    for p in avail:
        if len(picked) >= want:
            break
        picked.append(p)
        used.add(p.id)
    return picked


def _picked_by_llm(pool: list[POI], used: set[str], d: date, ids: list[str], want: int) -> list[POI]:
    """按 LLM 给出的 id 取景点，但硬约束照样执行：闭馆、夜间、超量都过滤。"""
    by_id = {p.id: p for p in pool}
    out: list[POI] = []
    for pid in ids:
        poi = by_id.get(pid)
        if poi is None or poi.id in used:
            continue
        if not poi.is_open_on(d):
            continue
        if _minutes(poi.open_time) >= DAY_LATEST_POI_START:
            continue
        out.append(poi)
        used.add(poi.id)
        if len(out) >= want:
            break
    return out


def _avoid_busy(start: int, end: int, busy: list[tuple[int, int]]) -> tuple[int, int]:
    """把活动区间推到固定事件之后，直到不再重叠。"""
    length = end - start
    changed = True
    while changed:
        changed = False
        for b_start, b_end in busy:
            if start < b_end and b_start < end:
                start = b_end
                end = start + length
                changed = True
    return start, end


def _place_poi(start: int, poi: POI, busy: list[tuple[int, int]] | None = None) -> tuple[int | None, int | None]:
    """按开放时间放置一个景点；餐食与固定事件已由单独调度避让。"""
    s = max(start, _minutes(poi.open_time))
    e = s + poi.duration_min
    if busy:
        s, e = _avoid_busy(s, e, busy)
    if e > _minutes(poi.close_time):
        return None, None
    return s, e


def _meal_window(
    *, day_start: int, cursor: int, period_start: int, period_end: int,
    duration: int, deadline: int | None, arrival: int,
    busy: list[tuple[int, int]] | None = None,
) -> tuple[int, int] | None:
    """只在餐期窗口内排餐；无足够空档则不伪造、截短或排到午夜。"""
    start = max(day_start, cursor, period_start, arrival)
    end = start + duration
    if busy:
        start, end = _avoid_busy(start, end, busy)
    if start >= period_end or end > period_end or (deadline is not None and end > deadline):
        return None
    return start, end


def _unplaced_notes(requested: list[str], placed: set[str], pool: list[POI], d: date) -> list[str]:
    """把"Agent 点名但没排进去"的项目说清楚，不静默丢弃。"""
    by_id = {p.id: p for p in pool}
    notes: list[str] = []
    for pid in requested:
        if pid in placed:
            continue
        poi = by_id.get(pid)
        if poi is None:
            notes.append(f"未安排 {pid}：候选中不存在")
        elif not poi.is_open_on(d):
            notes.append(f"未安排 {poi.name}：当天闭馆")
        elif _minutes(poi.open_time) >= DAY_LATEST_POI_START:
            notes.append(f"未安排 {poi.name}：开放时间过晚")
        else:
            notes.append(f"未安排 {poi.name}：当天时间或航班节点放不下")
    return notes


def run(
    brief: TripBrief,
    transport: TransportPlan,
    lodging: LodgingPlan,
    poi_plan: PoiPlan,
    weather: WeatherPlan,
    critique: Critique | None = None,
    revision: int = 0,
    frozen: dict[date, DayPlan] | None = None,
    target_date: date | None = None,
    feedback: str = "",
    llm_selection: dict[date, list[str]] | None = None,
    forced_drops: set[str] | None = None,
    force_cheaper_hotel: bool = False,
    require_explicit_assignments: bool = False,
    fixed_events: dict[date, list[FixedEvent]] | None = None,
) -> Itinerary:
    budget_pressure = bool(
        force_cheaper_hotel
        or (critique and any(i.code == "BUDGET_EXCEEDED" for i in critique.issues))
    )
    duration_pressure = bool(critique and any(i.code == "DAY_TOO_LONG" for i in critique.issues))

    hotel = lodging.chosen if require_explicit_assignments else _pick_hotel(lodging, brief, budget_pressure)
    weather_by_date: dict[date, WeatherDay] = {w.date: w for w in weather.days}
    pool = list(poi_plan.candidates)
    used: set[str] = set()

    # 单日重排：其余各天原样保留，并把他们已占用的景点登记为已用
    if frozen and target_date:
        for d0, day in frozen.items():
            if d0 == target_date:
                continue
            for item in day.items:
                if item.type == "sight" and item.ref_id:
                    used.add(item.ref_id)

    per_day = max(1, POI_PER_DAY[brief.pace] - (1 if duration_pressure else 0))
    if feedback:
        if any(k in feedback for k in ("轻松", "少", "松一点", "太赶")):
            per_day = max(1, per_day - 1)
        elif any(k in feedback for k in ("多", "紧凑", "加一个")):
            per_day = min(4, per_day + 1)

    warnings: list[str] = []
    for err in (transport.error, lodging.error, weather.error, poi_plan.error):
        if err:
            warnings.append(err)
    if poi_plan.excluded:
        warnings.append("已按同行人群排除：" + "、".join(poi_plan.excluded))

    days: list[DayPlan] = []

    for i in range(brief.days):
        d = brief.start_date + timedelta(days=i)

        if frozen and target_date and d != target_date:
            days.append(frozen[d])
            continue

        is_first, is_last = i == 0, i == brief.days - 1
        wd = weather_by_date.get(d)
        rainy = bool(wd and wd.is_rainy)
        items: list[Item] = []
        cursor = DAY_START
        deadline: int | None = None

        # 去程航班
        if is_first and transport.chosen_outbound:
            f = transport.chosen_outbound
            items.append(
                Item(
                    slot=_slot_for(_minutes(f.dep_time)),
                    start_time=f.dep_time,
                    end_time=f.arr_time,
                    type="transport",
                    title=f"{f.flight_no} {f.dep_city}→{f.arr_city}",
                    ref_id=f.flight_no,
                    cost=f.price * brief.travelers,
                    duration_min=f.duration_min,
                    note="含往返机场时间约 90 分钟",
                )
            )
            cursor = _minutes(f.arr_time) + ARRIVAL_TRANSFER_MIN

        # 返程航班：必须提前到机场，因此给当天设硬截止时间
        if is_last and transport.chosen_inbound:
            deadline = _minutes(transport.chosen_inbound.dep_time) - AIRPORT_LEAD_MIN - AIRPORT_TRANSFER_MIN

        # Agent 明确指定的固定时段（接驳、预约等）：原样进入时间表，其余活动避让。
        fixed = sorted(
            (fixed_events or {}).get(d, []),
            key=lambda e: _minutes(e.start_time),
        )
        busy: list[tuple[int, int]] = []
        for event in fixed:
            start_m = _minutes(event.start_time)
            end_m = _minutes(event.end_time)
            if end_m <= start_m:
                continue
            items.append(
                Item(
                    slot=_slot_for(start_m),
                    start_time=event.start_time,
                    end_time=event.end_time,
                    type="fixed",
                    title=event.title,
                    ref_id=f"fixed:{event.kind}",
                    cost=0,
                    duration_min=end_m - start_m,
                    note=event.note,
                )
            )
            busy.append((start_m, end_m))

        if is_last:
            # 返程日不安排景点：航班、接驳与机场提前量优先，机场路程没有实时数据。
            want = 0
        elif is_first:
            want = min(1, per_day) if brief.days > 1 else 0
        else:
            want = per_day

        if llm_selection is not None and d in llm_selection:
            # LLM 明确给了这一天的选择：尊重其意图（哪怕是空列表），只做硬约束过滤
            picked = _picked_by_llm(pool, used, d, llm_selection[d], want)
        elif require_explicit_assignments:
            picked = []
        else:
            picked = _select_for_day(
                pool, used, d, rainy, want,
                prefer_free=budget_pressure,
                is_last_day=is_last,
                preferences=brief.preferences,
                duration_pressure=duration_pressure,
            )

        # 协商结果：被点名的景点一律不排
        if forced_drops:
            picked = [p for p in picked if p.id not in forced_drops]
            for pid in forced_drops:
                used.add(pid)

        requested = list(llm_selection[d]) if llm_selection is not None and d in llm_selection else []
        placed_ids: set[str] = set()

        arrival = _minutes(transport.chosen_outbound.arr_time) + ARRIVAL_TRANSFER_MIN if is_first and transport.chosen_outbound else DAY_START
        sightseeing_end = cursor
        lunch_reserved = False
        pre_lunch_end = DAY_START
        for poi in picked:
            # 景点若会压到午餐窗口，先把 12:00-13:00 完整留出来再排。
            guess_start = max(sightseeing_end, _minutes(poi.open_time))
            guess_end = guess_start + poi.duration_min
            if (
                not is_last
                and not lunch_reserved
                and guess_start < LUNCH_END
                and guess_end > LUNCH_START
                and not (is_first and arrival >= LUNCH_START)
            ):
                lunch_reserved = True
                sightseeing_end = max(sightseeing_end, LUNCH_END)
            s, e = _place_poi(sightseeing_end, poi, busy)
            if s is None or e is None:
                # 关门前来不及游览，换下一个
                continue
            if s >= DAY_LATEST_POI_START:
                continue
            if deadline is not None and e > deadline:
                continue
            slot = "上午" if s < LUNCH_START else "下午"
            items.append(
                Item(
                    slot=slot,  # type: ignore[arg-type]
                    start_time=_to_time(s),
                    end_time=_to_time(e),
                    type="sight",
                    title=poi.name,
                    ref_id=poi.id,
                    cost=poi.ticket_price * brief.travelers,
                    duration_min=poi.duration_min,
                    note="室内备选" if poi.indoor else "",
                )
            )
            placed_ids.add(poi.id)
            if e <= LUNCH_START:
                pre_lunch_end = max(pre_lunch_end, e)
            sightseeing_end = e + 30

        unplaced_notes = _unplaced_notes(requested, placed_ids, pool, d)

        lunch_blocked = any(s < LUNCH_END and LUNCH_START < e for s, e in busy)
        if lunch_reserved and not lunch_blocked:
            lunch = (LUNCH_START, LUNCH_END)
        else:
            lunch = _meal_window(
                day_start=DAY_START,
                cursor=max(DAY_START, pre_lunch_end),
                period_start=LUNCH_START,
                period_end=LUNCH_END,
                duration=MEAL_LUNCH,
                deadline=deadline,
                arrival=arrival,
                busy=busy,
            )
        dinner_arrival_limit = 21 * 60
        dinner = _meal_window(
            day_start=DAY_START,
            cursor=max(sightseeing_end, DINNER_START),
            period_start=DINNER_START,
            period_end=dinner_arrival_limit,
            duration=MEAL_DINNER,
            deadline=deadline,
            arrival=arrival,
            busy=busy,
        )
        meal_notes: list[str] = []
        meal_estimate = 0.0
        if lunch:
            meal_notes.append(f"午餐休息窗口 {_to_time(lunch[0]):%H:%M}-{_to_time(lunch[1]):%H:%M}；费用为估算")
            meal_estimate += MEAL_LUNCH * brief.travelers
        elif is_first and transport.chosen_outbound and arrival >= LUNCH_END:
            meal_notes.append("去程抵达较晚，午餐按实际到达与转场时间自行安排；费用未计入固定时间表")
        elif is_last and transport.chosen_inbound:
            meal_notes.append("返程日时间不足以安排市区午餐，请在机场自行安排；费用未计入固定时间表")
        else:
            meal_notes.append("当天没有足够午餐时间窗，建议自行安排；费用未计入固定时间表")
        if dinner:
            meal_notes.append(f"晚餐休息窗口 {_to_time(dinner[0]):%H:%M}-{_to_time(dinner[1]):%H:%M}；费用为估算")
            meal_estimate += MEAL_DINNER * brief.travelers
        elif is_first and arrival >= dinner_arrival_limit:
            meal_notes.append("航班晚到，请自行安排晚餐；费用未计入固定时间表")
        elif is_last and transport.chosen_inbound:
            meal_notes.append("返程日不安排市区晚餐，需按航班时间前往机场")

        meal_ends = [end for meal in (lunch, dinner) if meal for end in [meal[1]]]
        day_cursor = max([sightseeing_end, *meal_ends])

        # 返程航班（排在当天最后）
        if is_last and transport.chosen_inbound:
            f = transport.chosen_inbound
            items.append(
                Item(
                    slot=_slot_for(_minutes(f.dep_time)),
                    start_time=f.dep_time,
                    end_time=f.arr_time,
                    type="transport",
                    title=f"{f.flight_no} {f.dep_city}→{f.arr_city}",
                    ref_id=f.flight_no,
                    cost=f.price * brief.travelers,
                    duration_min=f.duration_min,
                    note=f"假设市区到机场地面交通预留 {AIRPORT_TRANSFER_MIN} 分钟，并提前 {AIRPORT_LEAD_MIN} 分钟到机场；请按实际交通核实，最后一晚不住店",
                )
            )

        # 住宿（最后一晚不住）
        # 入住时间不能早于晚餐结束，否则晚餐和入住会叠在一起
        if not is_last and hotel:
            hotel_at, hotel_end = _avoid_busy(
                max(21 * 60, day_cursor, arrival if is_first else 0),
                max(21 * 60, day_cursor, arrival if is_first else 0) + 30,
                busy,
            )
            late_arrival = bool(is_first and transport.chosen_outbound and hotel_at >= 23 * 60 + 30)
            if not late_arrival:
                items.append(
                    Item(
                        slot="晚上",
                        start_time=_to_time(hotel_at),
                        end_time=_to_time(hotel_end),
                        type="hotel",
                        title=f"入住 {hotel.name}",
                        ref_id=hotel.name,
                        cost=hotel.price_per_night * _rooms(brief.travelers),
                        duration_min=30,
                        note=f"{hotel.district} · 距市中心 {hotel.distance_to_center_km}km",
                    )
                )
        else:
            late_arrival = False

        items.sort(key=lambda x: (_minutes(x.start_time), _minutes(x.end_time)))
        # 极晚抵达时当日不建虚假的 23:59 入住时间项；房费照计，时间轴只留零时长标记。
        if late_arrival and hotel:
            items.append(
                Item(
                    slot="晚上",
                    start_time=_to_time(min(hotel_at, 23 * 60 + 59)),
                    end_time=_to_time(min(hotel_at, 23 * 60 + 59)),
                    type="hotel",
                    title=f"晚到入住 {hotel.name}",
                    ref_id=hotel.name,
                    cost=hotel.price_per_night * _rooms(brief.travelers),
                    duration_min=0,
                    note=(
                        f"航班 {transport.chosen_outbound.arr_time.strftime('%H:%M')} 抵达并转场后办理入住，"
                        "跨日不计入当天时间轴；房费按一晚计入"
                    ),
                )
            )
        day_cost = sum(x.cost for x in items) + meal_estimate + LOCAL_TRANSPORT_PER_PERSON_DAY * brief.travelers
        notes: list[str] = []
        notes.append(f"市内交通费用按 {LOCAL_TRANSPORT_PER_PERSON_DAY * brief.travelers:.0f} 元估算，未写入时间轴")
        if rainy and wd:
            notes.append(f"当天{wd.condition}，已优先安排室内景点")
        if late_arrival and hotel:
            notes.append(f"航班抵达较晚，请直接前往 {hotel.name} 办理入住")
        if is_last and deadline is not None:
            notes.append(
                f"估算最晚 {_to_time(deadline).strftime('%H:%M')} 从市区出发；"
                f"假设机场地面交通 {AIRPORT_TRANSFER_MIN} 分钟、提前 {AIRPORT_LEAD_MIN} 分钟到机场，需按实际交通核实；"
                "早班航班可能需要前一晚靠近机场住宿"
            )

        days.append(DayPlan(date=d, items=items, day_cost=day_cost, meal_estimate=meal_estimate, notes=notes + meal_notes + unplaced_notes))

    total = sum(x.day_cost for x in days)
    if revision > 0:
        warnings.append(f"已根据校验反馈重排 {revision} 次")

    return Itinerary(
        days=days,
        total_cost=total,
        budget_total=brief.budget_total,
        warnings=warnings,
    )
