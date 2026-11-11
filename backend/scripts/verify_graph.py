"""P3 验收：完整编排图。

断言：
A 正常行程能生成，天数正确，预算内
B 预算不足会触发回退，达到轮次上限后强制放行并保留未解决项
C 目的地无数据时整体降级，仍产出结果而非崩溃
D 数据源损坏时节点异常被收集，不抛穿
E 所有被安排的景点当天必须开放（闭馆日不得出现）
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.graph.build import MAX_REVISIONS, build
from app.models import TripBrief
from app.tools import mock as mock_mod
from app.tools.mock import MockSource

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def make(**kw) -> TripBrief:
    base = dict(
        origin="上海",
        destination="成都",
        start_date=date(2026, 11, 20),
        end_date=date(2026, 11, 24),
        travelers=2,
        budget_total=8000,
        companions=["老人"],
    )
    base.update(kw)
    return TripBrief(**base)


async def run(brief: TripBrief) -> dict:
    app = build(MockSource())
    final: dict = {}
    async for chunk in app.astream({"brief": brief, "revision": 0, "errors": []}, stream_mode="updates"):
        for node, upd in chunk.items():
            final.update({node: upd})
            if node == "critic":
                final["critique"] = upd["critique"]
                final["revision"] = upd["revision"]
            if node == "planner":
                final["draft"] = upd["draft"]
            if node == "brief":
                pass
    # 需要完整 state，重跑一次拿 values
    async for chunk in app.astream({"brief": brief, "revision": 0, "errors": []}, stream_mode="values"):
        final["state"] = chunk
    state = final["state"]
    return state


async def main() -> int:
    print("\n[A] 正常行程 上海→成都 5 天")
    st = await run(make())
    it = st["draft"]
    print(f"  天数 {len(it.days)}, 总花费 {it.total_cost:.0f}/{it.budget_total:.0f}")
    check("生成 5 天", len(it.days) == 5, f"实际 {len(it.days)}")
    check("预算内", it.total_cost <= it.budget_total, f"{it.total_cost:.0f}")
    check("校验通过", st["critique"].passed is True)
    check("无节点异常", not st.get("errors"), str(st.get("errors")))

    print("\n[B] 预算不足 5000")
    st_b = await run(make(budget_total=5000))
    print(f"  重排轮次 {st_b['revision']}, 总花费 {st_b['draft'].total_cost:.0f}, 通过={st_b['critique'].passed}")
    check("触发了回退", st_b["revision"] >= 1, f"revision={st_b['revision']}")
    check("达到轮次上限后放行", st_b["revision"] == MAX_REVISIONS, f"revision={st_b['revision']}")
    check("仍有行程产出", len(st_b["draft"].days) == 5)
    check("未解项被记录", st_b["critique"].passed is False)

    print("\n[C] 目的地无数据 拉萨")
    st_c = await run(make(destination="拉萨"))
    print(f"  warnings: {st_c['draft'].warnings[:2]} ...")
    check("仍有行程骨架", len(st_c["draft"].days) == 5, f"{len(st_c['draft'].days)}")
    check("给出了降级提示", len(st_c["draft"].warnings) >= 2, str(len(st_c["draft"].warnings)))

    print("\n[D] 数据源损坏")
    original = mock_mod.DATA_DIR
    mock_mod.DATA_DIR = pathlib.Path(BACKEND) / "data" / "__broken__"
    try:
        st_d = await run(make())
    finally:
        mock_mod.DATA_DIR = original
    print(f"  warnings: {st_d['draft'].warnings[:2]}")
    # SourceError 是预期内降级，走 warnings 而不是 errors；
    # errors 只承载 agent 未预期抛出的异常。
    check(
        "降级进入 warnings",
        any("不可用" in w for w in st_d["draft"].warnings),
        str(st_d["draft"].warnings[:2]),
    )
    check("图未崩溃", st_d.get("draft") is not None)
    check("未把降级误记为节点异常", not st_d.get("errors"), str(st_d.get("errors")))

    print("\n[E] 闭馆日校验")
    st_e = await run(make(travelers=2))
    poi_map = {p.id: p for p in st_e["poi"].candidates}
    bad: list[str] = []
    seen_dates: set[date] = set()
    for day in st_e["draft"].days:
        for item in day.items:
            if item.type != "sight":
                continue
            poi = poi_map.get(item.ref_id or "")
            if poi is None:
                continue
            seen_dates.add(day.date)
            if not poi.is_open_on(day.date):
                bad.append(f"{poi.name}@{day.date}")
    print(f"  检查了 {len(seen_dates)} 天共 {sum(1 for d in st_e['draft'].days for i in d.items if i.type == 'sight')} 个景点安排")
    check("无闭馆日安排", not bad, str(bad))

    print("\n[F] 时间轴自洽")
    overlaps: list[str] = []
    for day in st_e["draft"].days:
        acts = [i for i in day.items if i.type != "rest"]
        for a, b in zip(acts, acts[1:]):
            if a.end_time > b.start_time and a.type != "transport" and b.type != "transport":
                overlaps.append(f"{day.date} {a.title}({a.end_time}) 与 {b.title}({b.start_time})")
    check("相邻活动不重叠", not overlaps, str(overlaps[:3]))

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  P3 编排图全部验收项通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
