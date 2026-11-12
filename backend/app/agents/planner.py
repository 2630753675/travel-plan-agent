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

from ..models import Critique, DayPlan, Hotel, Item, Itinerary, POI, TripBrief, WeatherDay
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
AIRPORT_LEAD_MIN = 150
POI_PER_DAY = {"轻松": 2, "适中": 3, "紧凑": 4}


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _to_time(m: int) -> time:
    m = max(0, min(m, 23 * 60 + 59))
    return time(m // 60, m % 60)


def _rooms(travelers: int) -> int:
    return max(1, math.ceil(travelers / 2))


def _pick_hotel(lodging: LodgingPlan, brief: TripBrief, budget_pressure: bool) -> Hotel | None:
    if lodging.chosen is None:
        return None
    if not budget_pressure:
        return lodging.chosen
    return lodging.cheaper_than(lodging.chosen) or lodging.chosen


def _select_for_day(
    pool: list[POI],
    used: set[str],
    d: date,
    rainy: bool,
    want: int,
    prefer_free: bool,
    is_last_day: bool,
) -> list[POI]:
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
    if rainy:
        indoor = [p for p in avail if p.indoor]
        if indoor:
            avail = indoor + [p for p in avail if not p.indoor]
    if prefer_free:
        avail.sort(key=lambda p: (p.ticket_price, -p.duration_min))
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


def _place_poi(
    start: int, poi: POI, lunch_at: int | None
) -> tuple[int | None, int | None, int | None]:
    """返回实际可用的 (start, end, lunch_at)。

    放不下时返回 (None, None, lunch_at)，由调用方跳过这个景点。
    两处需要午饭让路：已过午饭时间还没吃，或本次景点会横跨午饭时段。
    结束时间必须早于闭馆时间，否则就是把游客排到关门后的景点。
    """
    if lunch_at is None and start >= LUNCH_START:
        lunch_at = start
        start += MEAL_LUNCH
    s = max(start, _minutes(poi.open_time))
    e = s + poi.duration_min
    if lunch_at is None and s < LUNCH_START < e:
        lunch_at = LUNCH_START
        s = max(LUNCH_END, _minutes(poi.open_time))
        e = s + poi.duration_min
    # 关门时间检查：排不进去就放弃这个景点，不能排出"闭馆后游览"
    if e > _minutes(poi.close_time):
        return None, None, lunch_at
    return s, e, lunch_at


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
) -> Itinerary:
    budget_pressure = bool(
        force_cheaper_hotel
        or (critique and any(i.code == "BUDGET_EXCEEDED" for i in critique.issues))
    )
    duration_pressure = bool(critique and any(i.code == "DAY_TOO_LONG" for i in critique.issues))

    hotel = _pick_hotel(lodging, brief, budget_pressure)
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
                    slot="上午",
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
            cursor = _minutes(f.arr_time) + 90

        # 返程航班：必须提前到机场，因此给当天设硬截止时间
        if is_last and transport.chosen_inbound:
            deadline = _minutes(transport.chosen_inbound.dep_time) - AIRPORT_LEAD_MIN

        if is_first or is_last:
            want = min(1, per_day) if brief.days > 1 else 0
        else:
            want = per_day

        if llm_selection is not None and d in llm_selection:
            # LLM 明确给了这一天的选择：尊重其意图（哪怕是空列表），只做硬约束过滤
            picked = _picked_by_llm(pool, used, d, llm_selection[d], want)
        else:
            picked = _select_for_day(
                pool, used, d, rainy, want, prefer_free=budget_pressure, is_last_day=is_last
            )

        # 协商结果：被点名的景点一律不排
        if forced_drops:
            picked = [p for p in picked if p.id not in forced_drops]
            for pid in forced_drops:
                used.add(pid)

        lunch_at: int | None = None
        for poi in picked:
            s, e, lunch_at = _place_poi(cursor, poi, lunch_at)
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
            cursor = e + 30

        if lunch_at is None:
            lunch_at = max(cursor, LUNCH_START)
        items.append(
            Item(
                slot="中午",
                start_time=_to_time(lunch_at),
                end_time=_to_time(lunch_at + MEAL_LUNCH),
                type="meal",
                title="午餐",
                cost=MEAL_LUNCH * brief.travelers,
                duration_min=MEAL_LUNCH,
            )
        )

        # 返程日若晚餐时间晚于机场截止时间，说明人在机场，不再单列晚餐
        dinner_allowed = deadline is None or deadline > DINNER_START + MEAL_DINNER
        dinner_end = cursor
        if dinner_allowed:
            dinner_at = max(cursor, DINNER_START)
            dinner_end = dinner_at + MEAL_DINNER
            items.append(
                Item(
                    slot="晚上",
                    start_time=_to_time(dinner_at),
                    end_time=_to_time(dinner_end),
                    type="meal",
                    title="晚餐",
                    cost=MEAL_DINNER * brief.travelers,
                    duration_min=MEAL_DINNER,
                )
            )

        # 返程航班（排在当天最后）
        if is_last and transport.chosen_inbound:
            f = transport.chosen_inbound
            items.append(
                Item(
                    slot="晚上",
                    start_time=f.dep_time,
                    end_time=f.arr_time,
                    type="transport",
                    title=f"{f.flight_no} {f.dep_city}→{f.arr_city}",
                    ref_id=f.flight_no,
                    cost=f.price * brief.travelers,
                    duration_min=f.duration_min,
                    note="需提前 90 分钟到机场，最后一晚不住店",
                )
            )

        # 住宿（最后一晚不住）
        # 入住时间不能早于晚餐结束，否则晚餐和入住会叠在一起
        if not is_last and hotel:
            hotel_at = max(21 * 60, dinner_end)
            items.append(
                Item(
                    slot="晚上",
                    start_time=_to_time(hotel_at),
                    end_time=_to_time(hotel_at + 30),
                    type="hotel",
                    title=f"入住 {hotel.name}",
                    ref_id=hotel.name,
                    cost=hotel.price_per_night * _rooms(brief.travelers),
                    duration_min=30,
                    note=f"{hotel.district} · 距市中心 {hotel.distance_to_center_km}km",
                )
            )
            rest_at = hotel_at + 30
        else:
            rest_at = max(21 * 60 + 30, dinner_end)

        items.append(
            Item(
                slot="晚上",
                start_time=_to_time(rest_at),
                end_time=_to_time(rest_at),
                type="rest",
                title="市内交通预留",
                cost=LOCAL_TRANSPORT_PER_PERSON_DAY * brief.travelers,
                duration_min=0,
            )
        )

        items.sort(key=lambda x: (_minutes(x.start_time), _minutes(x.end_time)))
        day_cost = sum(x.cost for x in items)
        notes: list[str] = []
        if rainy and wd:
            notes.append(f"当天{wd.condition}，已优先安排室内景点")
        if is_last and deadline is not None:
            notes.append(f"当天需在 {_to_time(deadline).strftime('%H:%M')} 前赶往机场")

        days.append(DayPlan(date=d, items=items, day_cost=day_cost, notes=notes))

    total = sum(x.day_cost for x in days)
    if revision > 0:
        warnings.append(f"已根据校验反馈重排 {revision} 次")

    return Itinerary(
        days=days,
        total_cost=total,
        budget_total=brief.budget_total,
        warnings=warnings,
    )
