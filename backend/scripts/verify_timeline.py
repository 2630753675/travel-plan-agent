"""P0 验收：时间轴与硬约束。

正向：真实生成的行程不能有时间轴问题，长行程也不能出现"闭馆后游览"。
负向：人为构造的违规行程，critic 必须能抓住——校验器本身不能是摆设。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from datetime import date, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.agents import critic
from app.agents.poi import PoiPlan
from app.agents.transport import TransportPlan
from app.agents.weather import WeatherPlan
from app.graph.build import build
from app.models import DayPlan, Item, Itinerary, POI, TripBrief
from app.tools.mock import MockSource

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def brief(**kw) -> TripBrief:
    base = dict(
        origin="上海",
        destination="成都",
        start_date=date(2026, 11, 20),
        end_date=date(2026, 11, 24),
        travelers=2,
        budget_total=9000,
        companions=["老人"],
        preferences=["人文"],
    )
    base.update(kw)
    return TripBrief(**base)


async def run_graph(b: TripBrief) -> dict:
    app = build(MockSource())
    state: dict = {}
    async for chunk in app.astream(
        {"brief": b, "revision": 0, "errors": []}, stream_mode="values"
    ):
        state = chunk
    return state


def _m(t: time) -> int:
    return t.hour * 60 + t.minute


def overlaps(itinerary: Itinerary) -> list[str]:
    bad = []
    for day in itinerary.days:
        acts = [i for i in day.items if i.type != "rest"]
        acts.sort(key=lambda i: (_m(i.start_time), _m(i.end_time)))
        for a, b in zip(acts, acts[1:]):
            if _m(a.end_time) > _m(b.start_time):
                bad.append(f"{day.date} {a.title}→{b.title}")
    return bad


def outside_hours(itinerary: Itinerary, poi_map: dict) -> list[str]:
    bad = []
    for day in itinerary.days:
        for item in day.items:
            if item.type != "sight" or not item.ref_id:
                continue
            poi = poi_map.get(item.ref_id)
            if poi is None:
                continue
            if _m(item.start_time) < _m(poi.open_time) or _m(item.end_time) > _m(poi.close_time):
                bad.append(f"{poi.name}@{day.date} {item.start_time}-{item.end_time}")
    return bad


async def main() -> int:
    print("\n[1] 真实生成的行程：时间轴必须自洽")
    cases = [
        ("标准 5 天", brief()),
        ("紧凑节奏", brief(pace="紧凑", budget_total=12000)),
        ("14 天", brief(start_date=date(2026, 11, 16), end_date=date(2026, 11, 29), budget_total=30000)),
        ("一天往返", brief(start_date=date(2026, 11, 21), end_date=date(2026, 11, 21), budget_total=5000)),
    ]
    for name, b in cases:
        st = await run_graph(b)
        it = st["draft"]
        poi_map = {p.id: p for p in st["poi"].candidates}
        ov = overlaps(it)
        oh = outside_hours(it, poi_map)
        print(f"  {name}: 天={len(it.days)} 重叠={len(ov)} 超时={len(oh)}")
        check(f"[{name}] 无活动重叠", not ov, str(ov[:2]))
        check(f"[{name}] 无闭馆后游览", not oh, str(oh[:2]))
        check(f"[{name}] 天数正确", len(it.days) == b.days, str(len(it.days)))

    print("\n[2] 负向：人为构造的违规，critic 必须能抓到")
    poi = POI(
        id="p99", name="测试景点", category="人文",
        open_time=time(9, 0), close_time=time(12, 0), closed_days=[],
        duration_min=180, ticket_price=50, recommended_slot=["上午"],
        tags=[], lat=30.0, lng=104.0,
    )
    poi_plan = PoiPlan(candidates=[poi])
    weather = WeatherPlan(days=[])
    transport = TransportPlan()

    overlap_day = DayPlan(
        date=date(2026, 11, 20),
        items=[
            Item(slot="上午", start_time=time(9, 0), end_time=time(11, 0), type="sight",
                 title="测试景点", ref_id="p99", cost=100, duration_min=120),
            Item(slot="中午", start_time=time(10, 30), end_time=time(11, 30), type="meal",
                 title="午餐", cost=100, duration_min=60),
        ],
        day_cost=200,
    )
    it_overlap = Itinerary(days=[overlap_day], total_cost=200, budget_total=9000)
    crit = critic.run(brief(), it_overlap, poi_plan, transport, weather)
    codes = [i.code for i in crit.issues]
    print(f"  重叠场景 → {codes}")
    check("抓到 TIME_OVERLAP", "TIME_OVERLAP" in codes, str(codes))
    check("判为不通过", not crit.passed, str(crit.passed))

    late_day = DayPlan(
        date=date(2026, 11, 20),
        items=[
            Item(slot="下午", start_time=time(13, 0), end_time=time(16, 0), type="sight",
                 title="测试景点", ref_id="p99", cost=100, duration_min=180),
        ],
        day_cost=100,
    )
    it_late = Itinerary(days=[late_day], total_cost=100, budget_total=9000)
    crit2 = critic.run(brief(), it_late, poi_plan, transport, weather)
    codes2 = [i.code for i in crit2.issues]
    print(f"  超时场景 → {codes2}")
    check("抓到 OUTSIDE_OPEN_HOURS", "OUTSIDE_OPEN_HOURS" in codes2, str(codes2))

    print("\n[3] 负向：返程日错过机场截止")
    from app.models import Flight

    flight = Flight(
        flight_no="MU5402", dep_city="成都", arr_city="上海",
        dep_date=date(2026, 11, 24), dep_time=time(17, 40), arr_time=time(20, 45),
        duration_min=185, price=960, seats_left=8,
    )
    tp = TransportPlan(chosen_inbound=flight)
    last_day = DayPlan(
        date=date(2026, 11, 24),
        items=[
            Item(slot="下午", start_time=time(16, 0), end_time=time(17, 0), type="sight",
                 title="成都博物馆", ref_id="p08", cost=0, duration_min=60),
        ],
        day_cost=0,
    )
    it_late2 = Itinerary(days=[last_day], total_cost=100, budget_total=9000)
    crit3 = critic.run(brief(), it_late2, PoiPlan(candidates=[poi]), tp, weather)
    codes3 = [i.code for i in crit3.issues]
    print(f"  赶机场景 → {codes3}")
    check("抓到 MISSED_AIRPORT_DEADLINE", "MISSED_AIRPORT_DEADLINE" in codes3, str(codes3))

    print("\n[4] 正向：合规行程不应被误报")
    good_day = DayPlan(
        date=date(2026, 11, 20),
        items=[
            Item(slot="上午", start_time=time(9, 0), end_time=time(11, 30), type="sight",
                 title="测试景点", ref_id="p99", cost=100, duration_min=150),
            Item(slot="中午", start_time=time(12, 0), end_time=time(13, 0), type="meal",
                 title="午餐", cost=120, duration_min=60),
        ],
        day_cost=220,
    )
    it_good = Itinerary(days=[good_day], total_cost=220, budget_total=9000)
    crit4 = critic.run(brief(), it_good, poi_plan, transport, weather)
    bad_codes = [i.code for i in crit4.issues if i.severity == "blocker"]
    print(f"  合规场景 → blockers={bad_codes}")
    check("合规行程通过", crit4.passed, str(bad_codes))

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  时间轴与硬约束验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
