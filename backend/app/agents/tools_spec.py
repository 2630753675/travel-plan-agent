"""Agent 可自主调用的工具注册表。

两条分工原则，写在这里是为了不让人误解：

1. 查询类工具（航班/酒店/景点/天气）只返回事实，不做判断。
2. 落定类工具（materialize_plan）把"Agent 决定的内容"变成"可执行的时间表"：
   Agent 决定每天去哪几个景点、住哪家酒店；
   航班衔接、午晚餐、入住时间、赶机截止由确定性代码计算。
   这样模型想得再自由，也不会排出 18:30 到闭馆景点的行程。

工具一律通过 JSON Schema 暴露给模型，参数由模型自己填。
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any, Callable
from uuid import uuid4

from ..models import FixedEvent, Itinerary, POI, TripBrief, VisitEstimate
from ..tools.base import SourceError, TravelSource
from . import critic, planner
from .lodging import LodgingPlan
from .poi import PoiPlan
from .transport import TransportPlan
from .weather import WeatherPlan
from .negotiators.tools import day_cost_breakdown, poi_irreplaceability

ELDER_UNFIT = {"老人不宜", "老人慎选", "爬山", "体力消耗大"}

# 每个 blocker 对应的可执行修复工具；Agent 不需要猜“该找谁”。
REPAIR_HINTS: dict[str, list[str]] = {
    "AIRPORT_TRANSFER_LATE": ["replace_fixed_event", "remove_fixed_event"],
    "AIRPORT_TRANSFER_DUPLICATE": ["replace_fixed_event", "remove_fixed_event"],
    "TIME_OVERLAP": ["replace_fixed_event", "assign_pois"],
    "MISSED_AIRPORT_DEADLINE": ["replace_fixed_event", "assign_pois"],
    "OUTSIDE_OPEN_HOURS": ["assign_pois", "replace_fixed_event"],
    "CLOSED_DAY": ["assign_pois"],
    "ELDER_UNFIT": ["assign_pois"],
    "DAY_TOO_LONG": ["assign_pois"],
    "DAY_EMPTY": ["assign_pois"],
    "RAIN_OUTDOOR": ["assign_pois"],
    "BUDGET_EXCEEDED": ["set_hotel", "assign_pois"],
    "MEAL_BEFORE_ARRIVAL": ["replace_fixed_event"],
    "MEAL_AFTER_FLIGHT": ["replace_fixed_event"],
    "MEAL_AFTER_AIRPORT_DEPARTURE": ["replace_fixed_event"],
}


@dataclass
class ToolContext:
    """一次协作会话内共享的工具上下文。"""

    brief: TripBrief
    source: TravelSource
    transport: TransportPlan = field(default_factory=lambda: TransportPlan())
    lodging: LodgingPlan = field(default_factory=lambda: LodgingPlan())
    poi: PoiPlan = field(default_factory=lambda: PoiPlan())
    weather: WeatherPlan = field(default_factory=lambda: WeatherPlan())
    assignment: dict[date, list[str]] = field(default_factory=dict)
    visit_estimates: dict[date, dict[str, VisitEstimate]] = field(default_factory=dict)
    fixed_events: dict[date, list[FixedEvent]] = field(default_factory=dict)
    hotel_id: str | None = None
    itinerary: Itinerary | None = None
    hard_check: dict | None = None
    plan_version: int = 0
    # 按域版本：并行专业任务只关心自己领域的版本是否被别人改过。
    # plan_version 仍是聚合版本，供硬校验/缓存/批准使用。
    transport_version: int = 0
    lodging_version: int = 0
    assignment_version: int = 0
    fixed_events_version: int = 0
    approved_version: int | None = None
    document: dict | None = None
    itinerary_version: int | None = None
    evidence: dict[str, dict] = field(default_factory=dict)
    # 工具调用账本：恢复时复用成功操作，避免重复副作用。
    operations: dict[str, dict] = field(default_factory=dict)
    # 只读结果缓存：同一次会话内相同参数的查询不重复打数据源。
    cache: dict[str, Any] = field(default_factory=dict)

    def poi_map(self) -> dict[str, POI]:
        return {p.id: p for p in self.poi.candidates}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    run: Callable[[ToolContext, dict], Any]


def _ok(**kwargs) -> dict:
    return {"ok": True, **kwargs}


def _err(message: str) -> dict:
    return {"ok": False, "error": message}


def _search_flights(ctx: ToolContext, args: dict) -> dict:
    origin = str(args.get("origin") or ctx.brief.origin)
    destination = str(args.get("destination") or ctx.brief.destination)
    try:
        out = ctx.source.search_flights(origin, destination, ctx.brief.start_date)
        back = ctx.source.search_flights(destination, origin, ctx.brief.end_date)
    except SourceError as e:
        return _err(str(e))
    ctx.transport.outbound = out
    ctx.transport.inbound = back
    def row(f):
        return {
            "flight_no": f.flight_no,
            "route": f"{f.dep_city}→{f.arr_city}",
            "dep_time": f.dep_time.strftime("%H:%M"),
            "arr_time": f.arr_time.strftime("%H:%M"),
            "price": f.price,
            "seats_left": f.seats_left,
        }
    return _ok(count=len(out) + len(back), outbound=[row(f) for f in out], inbound=[row(f) for f in back])


def _search_hotels(ctx: ToolContext, args: dict) -> dict:
    try:
        hotels = ctx.source.search_hotels(ctx.brief.destination, ctx.brief.start_date, ctx.brief.days - 1)
    except SourceError as e:
        return _err(str(e))
    chosen = next((h for h in hotels if h.name == ctx.hotel_id), None)
    ctx.lodging = LodgingPlan(candidates=hotels, chosen=chosen)
    rows = [
        {
            "id": h.name,
            "name": h.name,
            "district": h.district,
            "price_per_night": h.price_per_night,
            "rating": h.rating,
            "distance_to_center_km": h.distance_to_center_km,
            "tags": h.tags,
        }
        for h in hotels
    ]
    return _ok(count=len(rows), hotels=rows)


def _list_pois(ctx: ToolContext, args: dict) -> dict:
    """景点候选。走 poi.run 复用同一套人群过滤与偏好排序：
    工具可以给 Agent 自由度，但不能把"老人不宜"的项目递给它去排。"""
    from . import poi as poi_agent

    plan = poi_agent.run(ctx.brief, ctx.source)
    if plan.error:
        return _err(plan.error)
    ctx.poi = plan
    rows = [
        {
            "id": p.id,
            "name": p.name,
            "category": p.category,
            "duration_min": p.duration_min,
            "ticket_price": p.ticket_price,
            "open": f"{p.open_time.strftime('%H:%M')}-{p.close_time.strftime('%H:%M')}",
            "closed_days": p.closed_days,
            "tags": p.tags,
            "indoor": p.indoor,
        }
        for p in plan.candidates
    ]
    return _ok(count=len(rows), excluded=plan.excluded, pois=rows)


def _get_weather(ctx: ToolContext, args: dict) -> dict:
    try:
        days = ctx.source.get_weather(ctx.brief.destination, ctx.brief.start_date, ctx.brief.days)
    except SourceError as e:
        return _err(str(e))
    ctx.weather = WeatherPlan(days=days)
    return _ok(
        days=[
            {"date": str(w.date), "condition": w.condition, "rain_prob": w.rain_prob, "rainy": w.is_rainy}
            for w in days
        ]
    )


def _assign_pois(ctx: ToolContext, args: dict) -> dict:
    """Agent 决定某天去哪几个景点；只要 id 合法就收下，冲突交给 materialize 暴露。"""
    raw_date = str(args.get("date") or "")
    try:
        d = date.fromisoformat(raw_date)
    except ValueError:
        return _err(f"日期格式不对：{raw_date}")
    if d < ctx.brief.start_date or d > ctx.brief.end_date:
        return _err(f"{d} 不在行程区间内")
    ids = [str(i) for i in (args.get("poi_ids") or [])]
    valid = ctx.poi_map()
    unknown = [i for i in ids if i not in valid]
    if unknown:
        return _err(f"不存在的景点 id：{unknown}")
    if ctx.assignment.get(d) == ids:
        return _ok(date=str(d), assigned=[valid[i].name for i in ids], plan_version=ctx.plan_version, unchanged=True)
    ctx.assignment[d] = ids
    ctx.assignment_version += 1
    ctx.plan_version += 1
    ctx.hard_check = None
    ctx.approved_version = None
    ctx.document = None
    return _ok(date=str(d), assigned=[valid[i].name for i in ids], plan_version=ctx.plan_version)


def _add_fixed_event(ctx: ToolContext, args: dict) -> dict:
    """登记 Agent 明确要求的固定时段（如去机场），由排程器让景点与餐食避开。"""
    raw_date = str(args.get("date") or "")
    try:
        d = date.fromisoformat(raw_date)
    except ValueError:
        return _err(f"日期格式不对：{raw_date}")
    if d < ctx.brief.start_date or d > ctx.brief.end_date:
        return _err(f"{d} 不在行程区间内")
    title = str(args.get("title") or "").strip()
    if not title:
        return _err("固定事件必须有 title")
    try:
        start = time.fromisoformat(str(args.get("start_time") or ""))
        end = time.fromisoformat(str(args.get("end_time") or ""))
    except ValueError:
        return _err("时间格式必须是 HH:MM")
    start_m = start.hour * 60 + start.minute
    end_m = end.hour * 60 + end.minute
    if end_m <= start_m:
        return _err("结束时间必须晚于开始时间")
    kind = str(args.get("kind") or "activity")
    if kind not in {"activity", "airport_transfer"}:
        return _err("kind 只能是 activity 或 airport_transfer")
    for event in ctx.fixed_events.get(d, []):
        es = event.start_time.hour * 60 + event.start_time.minute
        ee = event.end_time.hour * 60 + event.end_time.minute
        if start_m < ee and es < end_m:
            return _err(f"与已有固定事件「{event.title}」时间重叠")
    event = FixedEvent(
        event_id=str(args.get("event_id") or f"fe-{uuid4().hex[:12]}"),
        date=d,
        title=title,
        start_time=start,
        end_time=end,
        kind=kind,
        note=str(args.get("note") or ""),
    )
    ctx.fixed_events.setdefault(d, []).append(event)
    ctx.fixed_events[d].sort(key=lambda e: (e.start_time.hour * 60 + e.start_time.minute))
    ctx.fixed_events_version += 1
    ctx.plan_version += 1
    ctx.hard_check = None
    ctx.approved_version = None
    ctx.document = None
    return _ok(event_id=event.event_id, date=str(d), title=title, start=start.strftime("%H:%M"), end=end.strftime("%H:%M"), plan_version=ctx.plan_version)


def _find_fixed_event(ctx: ToolContext, event_id: str) -> tuple[date, FixedEvent] | None:
    for d, events in ctx.fixed_events.items():
        for event in events:
            if event.event_id == event_id:
                return d, event
    return None


def _replace_fixed_event(ctx: ToolContext, args: dict) -> dict:
    event_id = str(args.get("event_id") or "")
    found = _find_fixed_event(ctx, event_id)
    if found is None:
        return _err(f"不存在固定事件：{event_id}")
    old_date, old = found
    patch = dict(args.get("event") or {})
    patch.setdefault("event_id", old.event_id)
    patch.setdefault("date", str(old.date))
    patch.setdefault("title", old.title)
    patch.setdefault("start_time", old.start_time.strftime("%H:%M"))
    patch.setdefault("end_time", old.end_time.strftime("%H:%M"))
    patch.setdefault("kind", old.kind)
    patch.setdefault("note", old.note)
    try:
        d = date.fromisoformat(str(patch["date"]))
        start = time.fromisoformat(str(patch["start_time"]))
        end = time.fromisoformat(str(patch["end_time"]))
    except (KeyError, TypeError, ValueError):
        return _err("替换固定事件需要有效的 date/start_time/end_time")
    if end <= start or d < ctx.brief.start_date or d > ctx.brief.end_date:
        return _err("替换固定事件的日期或时间范围无效")
    kind = str(patch.get("kind") or old.kind)
    if kind not in {"activity", "airport_transfer"}:
        return _err("kind 只能是 activity 或 airport_transfer")
    for event in ctx.fixed_events.get(d, []):
        if event.event_id == old.event_id:
            continue
        if start < event.end_time and event.start_time < end:
            return _err(f"与已有固定事件「{event.title}」时间重叠")
    replacement = FixedEvent(
        event_id=old.event_id, date=d, title=str(patch.get("title") or old.title),
        start_time=start, end_time=end, kind=kind, note=str(patch.get("note") or old.note),
    )
    ctx.fixed_events[old_date] = [e for e in ctx.fixed_events.get(old_date, []) if e.event_id != old.event_id]
    if not ctx.fixed_events[old_date]:
        ctx.fixed_events.pop(old_date, None)
    ctx.fixed_events.setdefault(d, []).append(replacement)
    ctx.fixed_events[d].sort(key=lambda e: (e.start_time.hour * 60 + e.start_time.minute))
    ctx.fixed_events_version += 1
    ctx.plan_version += 1
    ctx.hard_check = None
    ctx.approved_version = None
    ctx.document = None
    return _ok(event_id=old.event_id, replaced=True, plan_version=ctx.plan_version,
               previous=old.model_dump(mode="json"), current=replacement.model_dump(mode="json"))


def _remove_fixed_event(ctx: ToolContext, args: dict) -> dict:
    event_id = str(args.get("event_id") or "")
    found = _find_fixed_event(ctx, event_id)
    if found is None:
        return _err(f"不存在固定事件：{event_id}")
    d, old = found
    ctx.fixed_events[d] = [e for e in ctx.fixed_events[d] if e.event_id != event_id]
    if not ctx.fixed_events[d]:
        ctx.fixed_events.pop(d, None)
    ctx.fixed_events_version += 1
    ctx.plan_version += 1
    ctx.hard_check = None
    ctx.approved_version = None
    ctx.document = None
    return _ok(event_id=event_id, removed=True, previous=old.model_dump(mode="json"), plan_version=ctx.plan_version)


def _submit_day_plans(ctx: ToolContext, args: dict) -> dict:
    """声明式批量提交多日行程：先全量校验，再原子应用，只递增一次版本。"""
    items = args.get("days")
    if not isinstance(items, list) or not items:
        return _err("days 必须是非空数组，每项包含 date 与 poi_ids")
    expected = args.get("expected_version")
    if expected is not None and int(expected) != ctx.plan_version:
        return _err(f"方案版本冲突：期望 {expected}，当前 {ctx.plan_version}；请重新读取后再提交")
    valid = ctx.poi_map()
    staged: dict[date, list[str]] = {}
    staged_estimates: dict[date, dict[str, VisitEstimate]] = {}
    for item in items:
        if not isinstance(item, dict):
            return _err("days 每一项必须是对象")
        try:
            d = date.fromisoformat(str(item.get("date") or ""))
        except ValueError:
            return _err(f"日期格式不对：{item.get('date')}")
        if d < ctx.brief.start_date or d > ctx.brief.end_date:
            return _err(f"{d} 不在行程区间内")

        raw_stops = item.get("stops")
        estimates: dict[str, VisitEstimate] = {}
        if isinstance(raw_stops, list):
            ids = []
            for stop in raw_stops:
                if not isinstance(stop, dict):
                    return _err(f"{d} 的 stops 每一项必须是对象")
                pid = str(stop.get("poi_id") or "")
                if not pid:
                    return _err(f"{d} 的 stop 缺少 poi_id")
                try:
                    est = VisitEstimate(
                        low=stop.get("minutes_low", stop.get("minutes")),
                        high=stop.get("minutes_high", stop.get("minutes")),
                        confidence=stop.get("confidence", "中"),
                        reason=str(stop.get("reason") or ""),
                        source=stop.get("source", "model"),
                    )
                except (ValueError, TypeError) as exc:
                    return _err(f"{d} / {pid} 的游览时长估算不合法：{exc}")
                ids.append(pid)
                estimates[pid] = est
        else:
            ids = [str(i) for i in (item.get("poi_ids") or [])]

        if len(ids) != len(set(ids)):
            return _err(f"{d} 的景点 id 有重复")
        unknown = [i for i in ids if i not in valid]
        if unknown:
            return _err(f"不存在的景点 id：{unknown}")
        closed = [valid[i].name for i in ids if not valid[i].is_open_on(d)]
        if closed:
            return _err(f"{d} 闭馆，不能安排：" + "、".join(closed))
        staged[d] = ids
        staged_estimates[d] = estimates

    unchanged = all(ctx.assignment.get(d) == ids for d, ids in staged.items()) and all(
        {pid: e.model_dump(mode="json") for pid, e in (ctx.visit_estimates.get(d) or {}).items()}
        == {pid: e.model_dump(mode="json") for pid, e in estimates.items()}
        for d, estimates in staged_estimates.items()
    )
    if unchanged:
        return _ok(
            plan_version=ctx.plan_version,
            changed=[],
            assignment={str(d): list(v) for d, v in ctx.assignment.items()},
            unchanged=True,
        )
    before = {str(d): list(ctx.assignment.get(d, [])) for d in staged}
    ctx.assignment.update(staged)
    for d, estimates in staged_estimates.items():
        ctx.visit_estimates[d] = estimates
    ctx.assignment_version += 1
    ctx.plan_version += 1
    ctx.hard_check = None
    ctx.approved_version = None
    ctx.document = None
    return _ok(
        plan_version=ctx.plan_version,
        changed=[
            {"date": str(d), "before": before[str(d)], "after": list(ids)}
            for d, ids in staged.items()
        ],
        assignment={str(d): list(v) for d, v in ctx.assignment.items()},
    )


def _set_hotel(ctx: ToolContext, args: dict) -> dict:
    name = str(args.get("hotel_id") or "")
    names = {h.name for h in ctx.lodging.candidates}
    if name not in names:
        return _err(f"没有这家酒店：{name}")
    if ctx.hotel_id == name:
        return _ok(hotel=name, unchanged=True)
    ctx.hotel_id = name
    ctx.lodging_version += 1
    ctx.plan_version += 1
    ctx.hard_check = None
    ctx.approved_version = None
    ctx.document = None
    return _ok(hotel=name)


def _set_flights(ctx: ToolContext, args: dict) -> dict:
    """采用 Agent 挑选的去返程航班；不在查询结果里的航班不能进入方案。"""
    outbound_id = str(args.get("outbound_flight_no") or "")
    inbound_id = str(args.get("inbound_flight_no") or "")
    if not ctx.transport.outbound or not ctx.transport.inbound:
        result = _search_flights(ctx, {})
        if not result.get("ok"):
            return result
        # _search_flights 已经写入 ctx.transport，不再重复请求数据源。
    outbound = next((f for f in ctx.transport.outbound if f.flight_no == outbound_id), None)
    inbound = next((f for f in ctx.transport.inbound if f.flight_no == inbound_id), None)
    if outbound is None or inbound is None:
        return _err(
            "方向选择无效。outbound_flight_no 必须选去程（"
            + "、".join(f.flight_no for f in ctx.transport.outbound)
            + "）；inbound_flight_no 必须选返程（"
            + "、".join(f.flight_no for f in ctx.transport.inbound)
            + "）"
        )
    if (
        ctx.transport.chosen_outbound is not None
        and ctx.transport.chosen_inbound is not None
        and ctx.transport.chosen_outbound.flight_no == outbound.flight_no
        and ctx.transport.chosen_inbound.flight_no == inbound.flight_no
    ):
        return _ok(
            outbound={"flight_no": outbound.flight_no, "dep": outbound.dep_time.strftime("%H:%M"), "arr": outbound.arr_time.strftime("%H:%M"), "price": outbound.price},
            inbound={"flight_no": inbound.flight_no, "dep": inbound.dep_time.strftime("%H:%M"), "arr": inbound.arr_time.strftime("%H:%M"), "price": inbound.price},
            unchanged=True,
        )
    ctx.transport.chosen_outbound = outbound
    ctx.transport.chosen_inbound = inbound
    ctx.transport_version += 1
    ctx.plan_version += 1
    ctx.hard_check = None
    ctx.approved_version = None
    ctx.document = None
    return _ok(
        outbound={"flight_no": outbound.flight_no, "dep": outbound.dep_time.strftime("%H:%M"), "arr": outbound.arr_time.strftime("%H:%M"), "price": outbound.price},
        inbound={"flight_no": inbound.flight_no, "dep": inbound.dep_time.strftime("%H:%M"), "arr": inbound.arr_time.strftime("%H:%M"), "price": inbound.price},
    )


def _materialize(ctx: ToolContext, args: dict) -> dict:
    """把 Agent 的内容决策落成可执行时间表，并立刻指出硬冲突。"""
    expected_dates = {ctx.brief.start_date.fromordinal(ctx.brief.start_date.toordinal() + i) for i in range(ctx.brief.days)}
    missing_dates = sorted(expected_dates - ctx.assignment.keys())
    if missing_dates:
        return _err("规划前必须由 Agent 对每个日期提交景点安排（优先 submit_day_plans，也可逐日 assign_pois）：" + ", ".join(map(str, missing_dates)))
    missing_minutes = []
    poi_by_id = ctx.poi_map()
    for d, ids in ctx.assignment.items():
        for pid in ids:
            poi = poi_by_id.get(pid)
            if pid not in (ctx.visit_estimates.get(d) or {}) and poi is not None and poi.duration_min is None:
                missing_minutes.append(f"{d} / {poi.name}")
    if missing_minutes:
        return _err(
            "以下景点缺少本次游览时长估算，请用 submit_day_plans 的 stops 提交 minutes_low/minutes_high："
            + "、".join(missing_minutes)
        )

    if ctx.brief.days > 1 and not ctx.hotel_id:
        return _err("过夜行程必须由 Agent 通过 set_hotel 选定酒店")
    if not ctx.transport.outbound or not ctx.transport.inbound:
        result = _search_flights(ctx, {})
        if not result.get("ok"):
            return result
        # _search_flights 已经写入 ctx.transport，不再重复请求数据源。
    if ctx.transport.chosen_outbound is None or ctx.transport.chosen_inbound is None:
        return _err("规划前必须由 Agent 通过 set_flights 选出去程和返程航班")

    if ctx.hotel_id:
        chosen = next((h for h in ctx.lodging.candidates if h.name == ctx.hotel_id), None)
        if chosen is not None:
            ctx.lodging = LodgingPlan(candidates=ctx.lodging.candidates, chosen=chosen)

    itinerary = planner.run(
        brief=ctx.brief,
        transport=ctx.transport,
        lodging=ctx.lodging,
        poi_plan=ctx.poi,
        weather=ctx.weather,
        revision=0,
        llm_selection=ctx.assignment,
        visit_estimates=ctx.visit_estimates,
        require_explicit_assignments=True,
        fixed_events=ctx.fixed_events,
    )
    ctx.itinerary = itinerary
    ctx.itinerary_version = ctx.plan_version
    verdict = critic.run(ctx.brief, itinerary, ctx.poi, ctx.transport, ctx.weather)
    ctx.hard_check = {
        "version": ctx.plan_version,
        "passed": verdict.passed,
        "issues": [
            {
                "severity": i.severity,
                "code": i.code,
                "message": i.message,
                "repair_tools": REPAIR_HINTS.get(i.code, []),
            }
            for i in verdict.issues
        ],
        "suggestions": verdict.suggestions,
    }
    return _ok(
        total_cost=round(itinerary.total_cost, 1),
        budget=ctx.brief.budget_total,
        over=round(itinerary.total_cost - ctx.brief.budget_total, 1),
        hard_check=ctx.hard_check,
        days=[
            {
                "date": str(day.date),
                "cost": round(day.day_cost, 1),
                "items": [f"{i.start_time.strftime('%H:%M')} {i.title}" for i in day.items if i.type != "rest"],
            }
            for day in itinerary.days
        ],
    )


def _check_plan(ctx: ToolContext, args: dict) -> dict:
    if ctx.itinerary is None or ctx.itinerary_version != ctx.plan_version:
        return _err("当前草案版本尚未落定，请调用 materialize_plan")
    if ctx.hard_check is None or ctx.hard_check.get("version") != ctx.plan_version:
        verdict = critic.run(ctx.brief, ctx.itinerary, ctx.poi, ctx.transport, ctx.weather)
        ctx.hard_check = {
            "version": ctx.plan_version,
            "passed": verdict.passed,
            "issues": [
                {
                    "severity": i.severity,
                    "code": i.code,
                    "message": i.message,
                    "repair_tools": REPAIR_HINTS.get(i.code, []),
                }
                for i in verdict.issues
            ],
            "suggestions": verdict.suggestions,
        }
    return {"ok": True, **ctx.hard_check}


def _get_plan(ctx: ToolContext, args: dict) -> dict:
    if ctx.itinerary is None:
        return _err("还没有行程，先调用 materialize_plan")
    view = str(args.get("view") or "summary")
    if view not in {"summary", "full"}:
        return _err("view 只能是 summary 或 full")
    base = {
        "plan_version": ctx.plan_version,
        "itinerary_version": ctx.itinerary_version,
        "approved_version": ctx.approved_version,
        "document": ctx.document,
        "fixed_events": {str(d): [e.model_dump(mode="json") for e in events] for d, events in ctx.fixed_events.items()},
        "total_cost": ctx.itinerary.total_cost,
    }
    flights = {
        "outbound": ctx.transport.chosen_outbound.model_dump(mode="json") if ctx.transport.chosen_outbound else None,
        "inbound": ctx.transport.chosen_inbound.model_dump(mode="json") if ctx.transport.chosen_inbound else None,
    }
    hotel = next((h.model_dump(mode="json") for h in ctx.lodging.candidates if h.name == ctx.hotel_id), None)
    if view == "full":
        return _ok(
            **base,
            itinerary=ctx.itinerary.model_dump(mode="json"),
            flights=flights,
            hotel=hotel,
            assignment={str(d): list(ids) for d, ids in ctx.assignment.items()},
            hard_check=ctx.hard_check,
        )
    # 摘要视图：只给"哪天有什么、花多少"，不给原始模型字段与完整硬校验结构。
    days = [
        {
            "date": str(day.date),
            "day_cost": round(day.day_cost, 1),
            "meal_estimate": day.meal_estimate,
            "notes": list(day.notes or [])[:2],
            "items": [
                {
                    "start": i.start_time.strftime("%H:%M"),
                    "end": i.end_time.strftime("%H:%M"),
                    "title": i.title,
                    "type": i.type,
                    "cost": round(i.cost, 1),
                    "ref_id": i.ref_id,
                }
                for i in day.items if i.type != "rest"
            ],
        }
        for day in ctx.itinerary.days
    ]
    return _ok(**base, days=days, flights=flights, hotel=hotel)


def _route_distances(ctx: ToolContext, args: dict) -> dict:
    from math import asin, cos, radians, sin, sqrt

    poi_by_id = ctx.poi_map()
    hotel = next((h for h in ctx.lodging.candidates if h.name == ctx.hotel_id), None)
    if hotel is None or not hotel.lat:
        return _err("缺少已选酒店坐标，无法估算距离")
    ids = [str(pid) for pid in args.get("poi_ids", [])]
    unknown = [pid for pid in ids if pid not in poi_by_id]
    if unknown:
        return _err(f"不存在的景点 id：{unknown}")
    route_fn = getattr(ctx.source, "route", None)
    distances = []
    used_road = False
    for pid in ids:
        poi = poi_by_id[pid]
        if not poi.lat:
            distances.append({"poi_id": pid, "name": poi.name, "distance_km": None, "note": "缺少坐标"})
            continue
        if callable(route_fn) and hotel.lng and hotel.lat and poi.lng:
            try:
                road = route_fn((hotel.lng, hotel.lat), (poi.lng, poi.lat), "driving")
                distances.append({
                    "poi_id": pid,
                    "name": poi.name,
                    "distance_km": road["distance_km"],
                    "duration_min": road["duration_min"],
                    "note": "高德驾车路线距离，不含实时路况",
                })
                used_road = True
                continue
            except Exception as exc:  # noqa: BLE001
                fallback_note = f"高德路线不可用（{type(exc).__name__}），以下为直线估算"
        else:
            fallback_note = "坐标直线距离估算，不是道路/地图路线"
        lat1, lat2 = radians(hotel.lat), radians(poi.lat)
        dlat, dlng = lat2 - lat1, radians(poi.lng - hotel.lng)
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
        distances.append({
            "poi_id": pid,
            "name": poi.name,
            "distance_km": round(6371 * 2 * asin(sqrt(a)), 2),
            "note": fallback_note,
        })
    return _ok(
        hotel=hotel.name,
        distances=distances,
        mode="amap_driving" if used_road else "straight_line",
        road_data_available=used_road,
        limitation=(
            "高德驾车距离来自道路规划，不含实时路况；未成功查询的项仍为直线估算，"
            "不得声称步行可达或给出步行分钟数"
            if used_road
            else "仅为坐标直线距离估算，不含道路绕行、步行可行性、车程或车费；"
            "不得据此声称景点相邻、步行可达或给出步行分钟数"
        ),
    )


def _day_costs(ctx: ToolContext, args: dict) -> dict:
    if ctx.itinerary is None:
        return _err("还没有行程，先调用 materialize_plan")
    return _ok(days=[day_cost_breakdown(day) for day in ctx.itinerary.days])


def _experience_check(ctx: ToolContext, args: dict) -> dict:
    """返回终局校验中的体验风险，供体验评审基于事实提出接受/反对。"""
    if ctx.itinerary is None:
        return _err("还没有行程，先调用 materialize_plan")
    verdict = critic.run(ctx.brief, ctx.itinerary, ctx.poi, ctx.transport, ctx.weather)
    codes = {"RAIN_OUTDOOR", "ELDER_UNFIT", "DAY_EMPTY", "DAY_TOO_LONG"}
    issues = [i.model_dump(mode="json") for i in verdict.issues if i.code in codes]
    return _ok(issues=issues, passed=not any(i["severity"] == "blocker" for i in issues))


def _irreplaceability(ctx: ToolContext, args: dict) -> dict:
    scores = poi_irreplaceability(ctx.poi.candidates)
    names = {p.id: p.name for p in ctx.poi.candidates}
    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return _ok(scores=[{"id": pid, "name": names.get(pid, pid), "score": s} for pid, s in top])


def _elder_fit(ctx: ToolContext, args: dict) -> dict:
    risky = [
        {"id": p.id, "name": p.name, "tags": [t for t in p.tags if t in ELDER_UNFIT]}
        for p in ctx.poi.candidates
        if set(p.tags) & ELDER_UNFIT
    ]
    return _ok(elder=ctx.brief.has_elder, risky=risky)


def _hotel_prices(ctx: ToolContext, args: dict) -> dict:
    rows = sorted(
        (
            {"name": h.name, "price_per_night": h.price_per_night, "distance_to_center_km": h.distance_to_center_km}
            for h in ctx.lodging.candidates
        ),
        key=lambda r: r["price_per_night"],
    )
    return _ok(hotels=rows)


TOOLS: dict[str, ToolSpec] = {
    "search_flights": ToolSpec(
        "search_flights",
        "查询出发地与目的地之间的往返航班候选（含价格与余票）",
        {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "出发城市，默认用用户填写的"},
                "destination": {"type": "string", "description": "到达城市，默认用用户填写的"},
            },
        },
        _search_flights,
    ),
    "search_hotels": ToolSpec(
        "search_hotels",
        "查询目的地酒店候选（含价格、评分、距市中心距离）",
        {"type": "object", "properties": {}},
        _search_hotels,
    ),
    "list_pois": ToolSpec(
        "list_pois",
        "查询目的地景点候选（含类别、时长、票价、开放时间、闭馆日、标签）",
        {"type": "object", "properties": {}},
        _list_pois,
    ),
    "get_weather": ToolSpec(
        "get_weather",
        "查询行程期间逐日天气与降水概率",
        {"type": "object", "properties": {}},
        _get_weather,
    ),
    "assign_pois": ToolSpec(
        "assign_pois",
        "把你的行程安排写成数据：指定某一天去哪些景点。可多次调用覆盖同一天。",
        {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                "poi_ids": {"type": "array", "items": {"type": "string"}, "description": "当天的景点 id 列表"},
            },
            "required": ["date", "poi_ids"],
        },
        _assign_pois,
    ),
    "add_fixed_event": ToolSpec(
        "add_fixed_event",
        "登记必须固定时段的事件（如去机场、预约参观）；景点与餐食会自动避开该时段",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "可选稳定事件 ID；恢复或重试时复用"},
                "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                "title": {"type": "string", "description": "事件名称"},
                "start_time": {"type": "string", "description": "开始 HH:MM"},
                "end_time": {"type": "string", "description": "结束 HH:MM"},
                "kind": {"type": "string", "enum": ["activity", "airport_transfer"], "description": "普通活动或去机场接驳"},
                "note": {"type": "string", "description": "可选说明"},
            },
            "required": ["date", "title", "start_time", "end_time"],
        },
        _add_fixed_event,
    ),
    "replace_fixed_event": ToolSpec(        "replace_fixed_event",
        "按 event_id 原子替换已有固定事件，适用于修正错误时间；不会删除其他事件",
        {"type": "object", "properties": {"event_id": {"type": "string"}, "event": {"type": "object"}}, "required": ["event_id", "event"]},
        _replace_fixed_event,
    ),
    "remove_fixed_event": ToolSpec(
        "remove_fixed_event",
        "按 event_id 删除错误固定事件，返回删除前快照",
        {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]},
        _remove_fixed_event,
    ),
    "set_hotel": ToolSpec(
        "set_hotel",
        "选定全程入住的酒店（用酒店名称）",
        {
            "type": "object",
            "properties": {"hotel_id": {"type": "string", "description": "酒店名称"}},
            "required": ["hotel_id"],
        },
        _set_hotel,
    ),
    "set_flights": ToolSpec(
        "set_flights",
        "从已查到的去返程候选中选定航班，时间表将按这两班安排",
        {
            "type": "object",
            "properties": {
                "outbound_flight_no": {"type": "string"},
                "inbound_flight_no": {"type": "string"},
            },
            "required": ["outbound_flight_no", "inbound_flight_no"],
        },
        _set_flights,
    ),
    "submit_day_plans": ToolSpec(
        "submit_day_plans",
        "声明式批量提交多日景点安排：先校验全部日期与景点，再原子应用，只递增一次版本；可替代逐日 assign_pois",
        {
            "type": "object",
            "properties": {
                "days": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                            "poi_ids": {"type": "array", "items": {"type": "string"}},
                            "stops": {
                                "type": "array",
                                "description": "优先使用：每个景点附带本次行程的游览时长估算",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "poi_id": {"type": "string"},
                                        "minutes_low": {"type": "integer", "minimum": 30, "maximum": 240},
                                        "minutes_high": {"type": "integer", "minimum": 30, "maximum": 240},
                                        "confidence": {"type": "string", "enum": ["高", "中", "低"]},
                                        "reason": {"type": "string"},
                                        "source": {"type": "string", "enum": ["model", "curated", "feedback", "unknown"]},
                                    },
                                    "required": ["poi_id", "minutes_low", "minutes_high"],
                                },
                            },
                        },
                        "required": ["date"],
                    },
                },
                "expected_version": {"type": "integer", "description": "可选：要求当前方案版本一致，防止覆盖并发修改"},
            },
            "required": ["days"],
        },
        _submit_day_plans,
    ),
    "materialize_plan": ToolSpec(
        "materialize_plan",
        "把当前安排落成可执行时间表（必须先由 Agent 选出去返程航班；按实际起降时刻安排），返回总花费与每日安排",
        {"type": "object", "properties": {}},
        _materialize,
    ),
    "get_plan": ToolSpec(
        "get_plan",
        "只读获取当前方案。默认 view=summary 返回逐日摘要（时间、项目、费用、固定事件）；"
        "需要原始模型字段时传 view=full",
        {"type": "object", "properties": {
            "view": {"type": "string", "enum": ["summary", "full"], "description": "默认 summary"},
        }},
        _get_plan,
    ),
    "route_distances": ToolSpec(
        "route_distances",
        "按已有坐标估算酒店到景点的直线距离，不返回地图或道路路线",
        {"type": "object", "properties": {"poi_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["poi_ids"]},
        _route_distances,
    ),
    "check_plan": ToolSpec(
        "check_plan",
        "检查当前行程是否可行：返回通过与否、以及时间重叠/闭馆/赶机/预算等具体问题",
        {"type": "object", "properties": {}},
        _check_plan,
    ),
    "day_costs": ToolSpec(
        "day_costs",
        "按天拆解花费构成，用于判断钱花在哪里",
        {"type": "object", "properties": {}},
        _day_costs,
    ),
    "irreplaceability": ToolSpec(
        "irreplaceability",
        "计算每个景点在本城行程中的不可替代性（0-1，越高越不该砍）",
        {"type": "object", "properties": {}},
        _irreplaceability,
    ),
    "elder_fit": ToolSpec(
        "elder_fit",
        "列出对老人同行不适宜的景点（爬山、体力消耗大等）",
        {"type": "object", "properties": {}},
        _elder_fit,
    ),
    "experience_check": ToolSpec(
        "experience_check",
        "基于终局校验器列出雨天户外、老人不适、空白日、单日过长等体验问题",
        {"type": "object", "properties": {}},
        _experience_check,
    ),
    "hotel_prices": ToolSpec(
        "hotel_prices",
        "按价格排序列出酒店候选，用于判断换酒店能省多少",
        {"type": "object", "properties": {}},
        _hotel_prices,
    ),
}


def schemas_for(names: list[str]) -> list[dict]:
    """转成 OpenAI tools 格式，只暴露该 Agent 被允许的工具。"""
    out: list[dict] = []
    for name in names:
        spec = TOOLS.get(name)
        if spec is None:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
        )
    return out


def execute(ctx: ToolContext, name: str, args: dict) -> dict:
    spec = TOOLS.get(name)
    if spec is None:
        return _err(f"不存在这个工具：{name}")
    key = _cache_key(ctx, name, args)
    if key is not None and key in ctx.cache:
        return {**deepcopy(ctx.cache[key]), "cached": True}
    try:
        result = spec.run(ctx, args or {})
    except Exception as e:  # 工具异常不能让整场协作崩掉
        return _err(f"{type(e).__name__}: {e}")
    if key is not None and result.get("ok"):
        ctx.cache[key] = deepcopy(result)
    return result


# 只读且结果不随方案版本变化的查询：整场会话只打一次数据源。
_STATIC_READS = {"search_flights", "search_hotels", "list_pois", "get_weather"}
# 只读但结果绑定方案版本：版本变化后必须重新计算。
_STATEFUL_READS = {
    "get_plan", "check_plan", "day_costs", "hotel_prices",
    "route_distances", "elder_fit", "experience_check", "irreplaceability",
}


def _cache_key(ctx: ToolContext, name: str, args: dict) -> str | None:
    if name not in _STATIC_READS and name not in _STATEFUL_READS:
        return None
    payload = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
    version = ctx.plan_version if name in _STATEFUL_READS else "-"
    return f"{name}|{version}|{payload}"
