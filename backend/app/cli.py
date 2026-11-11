"""P3 验收入口：命令行跑完整编排图。

用法：
    python -m app.cli
    python -m app.cli --origin 上海 --destination 成都 --start 2026-11-20 --end 2026-11-24 \
        --travelers 2 --budget 8000 --elder --preferences 美食,人文
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

from .graph.build import build
from .models import TripBrief
from .tools.mock import MockSource

WEEKDAY = "一二三四五六日"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="旅行规划多 agent 编排（CLI）")
    p.add_argument("--origin", default="上海")
    p.add_argument("--destination", default="成都")
    p.add_argument("--start", default="2026-11-20")
    p.add_argument("--end", default="2026-11-24")
    p.add_argument("--travelers", type=int, default=2)
    p.add_argument("--budget", type=float, default=8000)
    p.add_argument("--elder", action="store_true")
    p.add_argument("--child", action="store_true")
    p.add_argument("--preferences", default="")
    p.add_argument("--pace", default="适中", choices=["轻松", "适中", "紧凑"])
    p.add_argument("--quiet", action="store_true", help="只打印最终行程，不打印编排过程")
    return p.parse_args(argv)


def to_brief(a: argparse.Namespace) -> TripBrief:
    companions = []
    if a.elder:
        companions.append("老人")
    if a.child:
        companions.append("儿童")
    if not companions:
        companions.append("无")
    prefs = [x.strip() for x in a.preferences.split(",") if x.strip()]
    return TripBrief(
        origin=a.origin,
        destination=a.destination,
        start_date=date.fromisoformat(a.start),
        end_date=date.fromisoformat(a.end),
        travelers=a.travelers,
        budget_total=a.budget,
        companions=companions,
        preferences=prefs,
        pace=a.pace,
    )


def render_final(state: dict) -> str:
    it = state["draft"]
    critique = state.get("critique")
    lines: list[str] = []

    lines.append("=" * 60)
    lines.append(f"{state['brief'].destination} {len(it.days)} 日行程")
    lines.append("=" * 60)

    for day in it.days:
        lines.append("")
        lines.append(f"【{day.date} 周{WEEKDAY[day.date.weekday()]}】当日花费 {day.day_cost:.0f} 元")
        for item in day.items:
            if item.type == "rest":
                continue
            span = f"{item.start_time.strftime('%H:%M')}"
            if item.end_time != item.start_time:
                span += f"-{item.end_time.strftime('%H:%M')}"
            cost = f"  {item.cost:.0f}元" if item.cost else ""
            note = f"  ({item.note})" if item.note else ""
            lines.append(f"  {span:<12} [{item.slot}] {item.title}{cost}{note}")
        for note in day.notes:
            lines.append(f"  · {note}")

    lines.append("")
    lines.append(f"总花费 {it.total_cost:.0f} 元 / 预算 {it.budget_total:.0f} 元"
                 f"（{it.total_cost / it.budget_total:.0%}）")

    if critique:
        lines.append("")
        lines.append(f"校验结果：{'通过' if critique.passed else '未完全通过'}")
        for issue in critique.issues:
            tag = "阻断" if issue.severity == "blocker" else "提醒"
            lines.append(f"  [{tag}] {issue.message}")

    if it.warnings:
        lines.append("")
        lines.append("提示：")
        for w in it.warnings:
            lines.append(f"  - {w}")

    errors = state.get("errors") or []
    if errors:
        lines.append("")
        lines.append("节点异常：")
        for e in errors:
            lines.append(f"  - {e}")

    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    brief = to_brief(args)
    app = build(MockSource())

    initial = {"brief": brief, "revision": 0, "errors": []}
    final_state: dict = {}
    seen: list[str] = []

    async for mode, payload in app.astream(initial, stream_mode=["updates", "values"]):
        if mode == "updates":
            for node in payload:
                seen.append(node)
                if not args.quiet:
                    print(f"  → {node}")
        else:
            final_state = payload

    if not args.quiet:
        print(f"\n执行顺序: {' → '.join(seen)}")
        print(f"校验轮次: {final_state.get('revision', 0)}（重排 {max(0, final_state.get('revision', 0) - 1)} 次）")

    print()
    print(render_final(final_state))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
