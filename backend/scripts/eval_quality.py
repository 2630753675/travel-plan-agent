"""质量评测集：用一批需求跑确定性链路，量化"规划得好不好"。

功能验收只回答"能不能跑通"，这里回答"跑出来的东西有多好"：
改 planner 规则、换 prompt、换模型之后，用同一批用例对比分数。

评测走规则链路（不启用模型），因此结果可复现——否则同一份代码两次跑分不同，
就没法用于比较改进是否有效。

维度与权重（0–100）：

| 维度 | 权重 | 含义 |
|---|---|---|
| 可行性 | 0.30 | critic 无阻断项（预算、时长、闭馆、重叠、赶机） |
| 预算达成 | 0.20 | 越接近预算越好；超支按超出比例扣分 |
| 偏好命中 | 0.20 | 所选景点中命中用户偏好的比例 |
| 适老/适幼 | 0.15 | 有老人时不出现"老人不宜"类景点 |
| 完整度 | 0.15 | 每天都有安排，且不是只有吃饭住宿 |

用法：
    python scripts/eval_quality.py                 # 打印表格
    python scripts/eval_quality.py --out report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.graph.build import build  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.settings import AppSettings, LLMSettings  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402

# 只跑规则链路，保证可复现
SETTINGS = AppSettings(llm=LLMSettings(enabled=False))

ELDER_UNFIT = {"老人不宜", "老人慎选", "爬山", "体力消耗大"}

WEIGHTS = {
    "feasible": 0.30,
    "budget": 0.20,
    "preference": 0.20,
    "elder": 0.15,
    "complete": 0.15,
}


def case(name: str, expect: str = "", **kw) -> dict:
    base = dict(
        origin="上海",
        destination="成都",
        start_date=date(2026, 11, 20),
        end_date=date(2026, 11, 24),
        travelers=2,
        budget_total=9000,
        companions=["老人"],
        preferences=["人文"],
        pace="适中",
    )
    base.update(kw)
    return {"name": name, "brief": TripBrief(**base), "expect": expect}


CASES = [
    case("成都·标准五人老人人文"),
    case("成都·紧凑节奏", pace="紧凑", budget_total=12000),
    case("成都·轻松节奏", pace="轻松", budget_total=9000),
    case("成都·无偏好", preferences=[]),
    case("成都·情侣无同行", companions=["无"], preferences=["夜景"]),
    case("成都·带儿童亲子", companions=["儿童"], preferences=["自然"]),
    case("西安·人文老人", destination="西安", budget_total=9000),
    case("西安·自然紧凑", destination="西安", companions=["无"], preferences=["自然"], pace="紧凑", budget_total=11000),
    # 老人同行时华山（自然类）必须被挡掉：安全优先于偏好，这里就不该出现自然类景点
    case("西安·自然但带老人", destination="西安", preferences=["自然"], budget_total=9000),
    case("厦门·自然老人", destination="厦门", budget_total=10000),
    case("厦门·无偏好轻松", destination="厦门", preferences=[], pace="轻松"),
    case("北京·人文老人", destination="北京", budget_total=11000),
    case("北京·亲子紧凑", destination="北京", companions=["儿童"], preferences=["自然"], pace="紧凑", budget_total=13000),
    case("成都·14 天长假", start_date=date(2026, 11, 16), end_date=date(2026, 11, 29), budget_total=30000),
    # 单日往返以交通为主，没有景点是合理的
    case("成都·一天往返", expect="no_sights", start_date=date(2026, 11, 21), end_date=date(2026, 11, 21), budget_total=5000),
    # 6500 元走 5 天成都本身就做不到，超支是预期结果，交给协商/中断处理
    case("成都·预算紧（预期不可行）", expect="infeasible", budget_total=6500),
]


async def run_case(brief: TripBrief) -> dict:
    graph = build(MockSource(), SETTINGS)
    state: dict = {}
    async for chunk in graph.astream(
        {"brief": brief, "revision": 0, "errors": []}, stream_mode="values"
    ):
        state = chunk
    return state


def evaluate(item: dict) -> dict:
    brief: TripBrief = item["brief"]
    state = asyncio.run(run_case(brief))
    itinerary = state["draft"]
    critique = state["critique"]
    poi_map = {p.id: p for p in state["poi"].candidates}

    sights = [
        (day.date, item_)
        for day in itinerary.days
        for item_ in day.items
        if item_.type == "sight" and item_.ref_id
    ]

    # 1 可行性
    blockers = [i for i in critique.issues if i.severity == "blocker"]
    feasible = 1.0 if not blockers else 0.0

    # 2 预算达成：预算内给满分，超支按比例线性扣
    over = itinerary.total_cost - brief.budget_total
    over_ratio = over / brief.budget_total if brief.budget_total else 0
    budget_score = 1.0 if over_ratio <= 0 else max(0.0, 1.0 - over_ratio)

    # 3 偏好命中：与 planner 的判定口径保持一致（类别或标签命中都算）
    if brief.preferences:
        hit = 0
        for _, sight in sights:
            poi = poi_map.get(sight.ref_id or "")
            if poi and (poi.category in brief.preferences or set(poi.tags) & set(brief.preferences)):
                hit += 1
        preference = hit / len(sights) if sights else 0.0
    else:
        preference = 1.0  # 没有偏好就没有可违背的

    # 4 适老
    if brief.has_elder:
        unsafe = [
            poi_map[sight.ref_id].name
            for _, sight in sights
            if (poi := poi_map.get(sight.ref_id or "")) and set(poi.tags) & ELDER_UNFIT
        ]
        elder = 1.0 if not unsafe else 0.0
    else:
        unsafe = []
        elder = 1.0

    # 5 完整度：每天都要有非住宿非交通的安排
    empty_days = []
    for day in itinerary.days:
        useful = [i for i in day.items if i.type in ("sight", "meal")]
        if not useful:
            empty_days.append(str(day.date))
    complete = 1.0 - len(empty_days) / len(itinerary.days) if itinerary.days else 0.0

    score = 100 * (
        WEIGHTS["feasible"] * feasible
        + WEIGHTS["budget"] * budget_score
        + WEIGHTS["preference"] * preference
        + WEIGHTS["elder"] * elder
        + WEIGHTS["complete"] * complete
    )

    return {
        "case": item["name"],
        "expect": item.get("expect", ""),
        "destination": brief.destination,
        "days": len(itinerary.days),
        "total_cost": round(itinerary.total_cost, 1),
        "budget": brief.budget_total,
        "over_ratio": round(over_ratio, 3),
        "sights": len(sights),
        "revision": state.get("revision", 0),
        "blockers": [i.code for i in blockers],
        "elder_unsafe": unsafe,
        "empty_days": empty_days,
        "preference_hit": round(preference, 3),
        "metrics": {
            "feasible": feasible,
            "budget": round(budget_score, 3),
            "preference": round(preference, 3),
            "elder": elder,
            "complete": round(complete, 3),
        },
        "score": round(score, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=str, default="", help="把结果写成 JSON")
    args = parser.parse_args()

    rows = [evaluate(item) for item in CASES]

    header = f"{'用例':<22}{'天':>3}{'花费':>7}{'预算':>7}{'超支':>7}{'景点':>5}{'偏好':>6}{'分':>6}  阻断项"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['case']:<22}{row['days']:>3}{row['total_cost']:>7.0f}{row['budget']:>7.0f}"
            f"{row['over_ratio'] * 100:>6.0f}%{row['sights']:>5}{row['preference_hit']:>6.2f}"
            f"{row['score']:>6.1f}  {','.join(row['blockers']) or '-'}"
        )

    scores = [r["score"] for r in rows]
    avg = sum(scores) / len(scores)

    # 只有"没有标记预期"的用例才参与达标判定：
    # 预算本来就不可行、或单日往返没有景点，属于预期结果，不算回归。
    strict = [r for r in rows if not r["expect"]]
    feasible = sum(1 for r in strict if not r["blockers"])
    print("-" * len(header))
    print(
        f"平均分 {avg:.1f} / 100    "
        f"达标 {feasible}/{len(strict)}（另 {len(rows) - len(strict)} 条为预期不可行）    "
        f"最低 {min(scores):.1f}    最高 {max(scores):.1f}"
    )

    unsafe = [r["case"] for r in rows if r["elder_unsafe"]]
    empty = [r["case"] for r in rows if r["empty_days"] and r["expect"] != "no_sights"]
    if unsafe:
        print(f"⚠ 有老人不宜景点：{unsafe}")
    if empty:
        print(f"⚠ 存在空白天：{empty}")

    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "weights": WEIGHTS,
                    "summary": {
                        "average": round(avg, 1),
                        "feasible": f"{feasible}/{len(strict)}",
                        "expected_infeasible": len(rows) - len(strict),
                        "min": round(min(scores), 1),
                        "max": round(max(scores), 1),
                    },
                    "cases": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告已写入 {out}")

    # 出现老人不宜景点、非预期的空白天、或达标用例出现阻断项，都算不达标
    return 0 if not unsafe and not empty and feasible == len(strict) else 1


if __name__ == "__main__":
    sys.exit(main())
