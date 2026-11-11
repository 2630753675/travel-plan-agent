"""P5 验收：边界用例。

每条用例都跑完整图，然后校验一组硬不变量：
1. 天数与请求一致
2. 景点当天必须开放
3. 相邻活动不重叠（航班除外）
4. 超预算必须被标为 blocker，不能静默通过
5. 图不崩溃
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from datetime import date, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.graph.build import build
from app.models import TripBrief
from app.tools.mock import MockSource

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"    PASS  {name}")
    else:
        print(f"    FAIL  {name} {detail}")
        failures.append(name)


async def run_graph(brief: TripBrief) -> dict:
    app = build(MockSource())
    state: dict = {}
    async for chunk in app.astream({"brief": brief, "revision": 0, "errors": []}, stream_mode="values"):
        state = chunk
    return state


def invariants(case: str, brief: TripBrief, state: dict) -> None:
    it = state["draft"]
    crit = state["critique"]
    poi_map = {p.id: p for p in state["poi"].candidates}

    check(f"[{case}] 天数 = {brief.days}", len(it.days) == brief.days, f"实际 {len(it.days)}")

    closed = []
    for day in it.days:
        for item in day.items:
            if item.type == "sight" and item.ref_id in poi_map:
                if not poi_map[item.ref_id].is_open_on(day.date):
                    closed.append(f"{item.title}@{day.date}")
    check(f"[{case}] 无闭馆日安排", not closed, str(closed[:3]))

    overlaps = []
    for day in it.days:
        acts = [i for i in day.items if i.type != "rest" and i.type != "transport"]
        for a, b in zip(acts, acts[1:]):
            if a.end_time > b.start_time:
                overlaps.append(f"{day.date} {a.title}->{b.title}")
    check(f"[{case}] 活动不重叠", not overlaps, str(overlaps[:2]))

    if it.total_cost > brief.budget_total:
        has_blocker = any(i.code == "BUDGET_EXCEEDED" and i.severity == "blocker" for i in crit.issues)
        check(f"[{case}] 超预算被标记", has_blocker, f"{it.total_cost:.0f}/{brief.budget_total:.0f}")
        check(f"[{case}] 超预算不算通过", crit.passed is False)

    check(f"[{case}] 图未崩溃", it is not None)


def brief(**kw) -> TripBrief:
    base = dict(
        origin="上海",
        destination="成都",
        start_date=date(2026, 11, 20),
        end_date=date(2026, 11, 24),
        travelers=2,
        budget_total=8000,
        companions=["无"],
    )
    base.update(kw)
    return TripBrief(**base)


CASES: list[tuple[str, TripBrief]] = [
    ("预算极低", brief(budget_total=3500)),
    ("单人出行", brief(travelers=1, budget_total=6000)),
    ("重复周一覆盖", brief(start_date=date(2026, 11, 23), end_date=date(2026, 11, 27))),
    ("一天往返", brief(start_date=date(2026, 11, 21), end_date=date(2026, 11, 21), budget_total=5000)),
    ("最长 14 天", brief(start_date=date(2026, 11, 16), end_date=date(2026, 11, 29), budget_total=30000)),
    ("无偏好", brief(preferences=[])),
    ("仅儿童", brief(companions=["儿童"])),
    ("老人加儿童", brief(companions=["老人", "儿童"])),
    ("紧凑节奏", brief(pace="紧凑")),
    ("轻松节奏", brief(pace="轻松", budget_total=12000)),
]


async def main() -> int:
    for name, b in CASES:
        print(f"\n[{name}] {b.start_date}~{b.end_date} {b.travelers}人 {b.budget_total:.0f}元 同行={b.companions}")
        try:
            state = await run_graph(b)
        except Exception as e:
            print(f"    FAIL  图执行抛异常: {e}")
            failures.append(f"{name} 抛异常")
            continue
        invariants(name, b, state)
        it = state["draft"]
        print(f"    花费 {it.total_cost:.0f}/{b.budget_total:.0f}, 重排 {state.get('revision')}, 通过={state['critique'].passed}")

    print("\n[目的地无数据 拉萨]")
    st = await run_graph(brief(destination="拉萨"))
    check("[拉萨] 仍有 5 天骨架", len(st["draft"].days) == 5)
    check("[拉萨] 有降级提示", len(st["draft"].warnings) >= 2)

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS  {len(CASES) + 1} 条边界用例全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
