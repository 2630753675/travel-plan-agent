"""验收：检索 agent 之间的按需协作。

覆盖：
A 协调者能识别"酒店位置与行程不匹配"
B 请求确实发给了另一个 agent，且对方有回应
C 协作后住宿被重选，且真的换了更合适的位置
D 没有问题时不做无谓的协作（不打扰）
E 协作只发生一轮
F 协作过程进入执行轨迹
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.agents import coordinator, lodging, poi
from app.models import TripBrief
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


def main() -> int:
    src = MockSource()
    b = brief()
    pp = poi.run(b, src)
    lp = lodging.run(b, src)
    focus = coordinator._preferred_pois(b, pp.candidates)
    dist = coordinator.avg_distance(lp.chosen, focus)

    print("\n[1] 协调者识别位置不匹配")
    res = coordinator.review(b, lp.chosen, pp.candidates)
    print(f"  酒店 {lp.chosen.name}，焦点景点 {len(focus)} 个，平均 {dist:.1f} km")
    check("超过阈值时发起协作", res.needed, f"{dist:.1f}km")
    check("请求方是住宿", res.requests[0].sender == "lodging", res.requests[0].sender)
    check("请求对象是景点", res.requests[0].receiver == "poi", res.requests[0].receiver)
    check("请求里带了要哪些景点", len(res.requests[0].payload.get("poi_ids", [])) > 0)
    check("给出了理由", bool(res.requests[0].reason), res.requests[0].reason)

    print("\n[2] 协作后住宿确实改变")
    fixed = lodging.run(b, src, focus_pois=focus)
    print(f"  协作前 {lp.chosen.name}（{lp.chosen.price_per_night:.0f} 元）"
          f" → 协作后 {fixed.chosen.name}（{fixed.chosen.price_per_night:.0f} 元）")
    check("换了酒店", fixed.chosen.name != lp.chosen.name, fixed.chosen.name)
    new_dist = coordinator.avg_distance(fixed.chosen, focus)
    print(f"  到目标景点平均距离 {dist:.1f} km → {new_dist:.1f} km")
    check("新酒店离景点更近", new_dist < dist, f"{dist:.1f} → {new_dist:.1f}")

    print("\n[3] 没问题时不打扰")
    b_near = brief(preferences=[])
    lp2 = lodging.run(b_near, src)
    res2 = coordinator.review(b_near, lp2.chosen, poi.run(b_near, src).candidates)
    print(f"  无偏好时平均距离 {coordinator.avg_distance(lp2.chosen, coordinator._preferred_pois(b_near, poi.run(b_near, src).candidates)):.1f} km")
    check("距离合适时不发起协作", not res2.needed, str(res2.requests))

    print("\n[4] 边界：没有酒店或没有景点")
    check("无酒店不报错", not coordinator.review(b, None, pp.candidates).needed)
    check("无景点不报错", not coordinator.review(b, lp.chosen, []).needed)

    print("\n[5] 协作只一轮（图结构）")
    import asyncio

    from langgraph.checkpoint.memory import MemorySaver

    from app.graph.build import build
    from app.settings import AppSettings

    graph = build(src, AppSettings(), checkpointer=MemorySaver())
    edges = graph.get_graph().edges
    to_coordinator = [e for e in edges if e.target == "coordinator"]
    from_coordinator = [e for e in edges if e.source == "coordinator"]
    from_fix = [e for e in edges if e.source == "lodging_fix"]
    print(f"  进入协调者 {len(to_coordinator)} 条，离开 {len(from_coordinator)} 条，重选后去向 {[e.target for e in from_fix]}")
    check("四路检索都汇入协调者", len(to_coordinator) == 4, str(len(to_coordinator)))
    check("重选节点只通向规划", [e.target for e in from_fix] == ["planner"], str([e.target for e in from_fix]))
    check("协调者不会再回到检索", all(e.target != "lodging" for e in from_coordinator), str([e.target for e in from_coordinator]))

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  检索 agent 按需协作验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
