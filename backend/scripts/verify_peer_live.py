"""真实模型多 Agent 端到端体验验收。

这个脚本不接受模型地址覆盖、不启动假模型、不替换数据源：
- 配置只从当前应用数据库读取；
- datasource 工厂按现有用户配置创建；
- 未启用模型时直接失败，而不是静默切到规则路径；
- 完整运行 Agent 图，打印运行轨迹、行程和硬约束检查；
- 任一阻断/未收敛/时间逻辑错误，退出码非 0。

运行：
    cd backend
    .venv/Scripts/python.exe scripts/verify_peer_live.py --origin 上海 --destination 成都 \
      --start 2026-11-20 --end 2026-11-24 --travelers 2 --budget 20000 \
      --companions 老人 --preferences 人文 --pace 适中
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

from app.agents import hub  # noqa: E402
from app.graph.build import build  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.settings import load  # noqa: E402
from app.tools.factory import make_source  # noqa: E402


def arguments() -> TripBrief:
    p = argparse.ArgumentParser()
    p.add_argument("--origin", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--start", required=True, type=date.fromisoformat)
    p.add_argument("--end", required=True, type=date.fromisoformat)
    p.add_argument("--travelers", required=True, type=int)
    p.add_argument("--budget", required=True, type=float)
    p.add_argument("--companions", nargs="*", default=["无"])
    p.add_argument("--preferences", nargs="*", default=[])
    p.add_argument("--pace", choices=["轻松", "適中", "紧凑", "适中"], default="适中")
    a = p.parse_args()
    return TripBrief(
        origin=a.origin,
        destination=a.destination,
        start_date=a.start,
        end_date=a.end,
        travelers=a.travelers,
        budget_total=a.budget,
        companions=a.companions,
        preferences=a.preferences,
        pace="适中" if a.pace == "適中" else a.pace,
    )


def _time_consistency(brief: TripBrief, draft) -> list[str]:
    issues: list[str] = []
    for day in draft.days:
        outbound = [i for i in day.items if i.type == "transport" and i.title.startswith(tuple())]
        # Match explicit flight refs against labels/routes and ensure no meal starts during the flight.
        transport = [i for i in day.items if i.type == "transport"]
        meals = [i for i in day.items if i.type == "meal"]
        for flight in transport:
            fs, fe = flight.start_time, flight.end_time
            for meal in meals:
                if fs <= meal.start_time < fe or fs < meal.end_time <= fe:
                    issues.append(f"{day.date}: {meal.title} overlaps flight {flight.title}")
        if day.date == brief.start_date:
            outbound = transport[:1]
            if outbound:
                arrival = outbound[0].end_time
                for meal in meals:
                    if meal.start_time < arrival:
                        issues.append(f"{day.date}: arrival-day {meal.title} scheduled before arrival")
                    if meal.start_time.hour >= 22:
                        issues.append(f"{day.date}: unreasonable late meal at {meal.start_time}")
    return issues


async def run(brief: TripBrief) -> int:
    settings = load()
    if not settings.llm.configured:
        print("FAIL: 当前应用配置未启用模型；拒绝退回规则模式。")
        return 2
    if settings.orchestration.agent_mode != "peer":
        print("FAIL: 当前应用配置不是 peer 多 Agent 模式；拒绝改写配置。")
        return 2

    # Respect the user's configured provider, model, key, timeout, and datasource.
    source = make_source(settings.datasource)
    client = LLMClient(settings.llm, settings.orchestration.node_timeout_s, budget=None)
    captured: list[tuple[str, dict]] = []

    def emit(kind: str, /, **payload) -> None:
        captured.append((kind, payload))
        if kind in {"wake", "tool_call", "tool_result", "decide", "converged", "escalated", "round_cap", "turn_failed"}:
            print("EVENT", kind, json.dumps(payload, ensure_ascii=False, default=str))

    result = await hub.run_session(
        brief=brief,
        source=source,
        client=client,
        emit=emit,
        max_rounds=max(1, settings.orchestration.max_revisions - 1),
    )

    print("\n=== 对话轨迹 ===")
    for m in result.bus.messages:
        print(f"R{m.round} {m.sender} -> {','.join(m.recipients) or '（无人）'} [{m.kind}] {m.content}")
    print(f"\n=== 会话状态 ===\nconverged={result.converged} rounds={result.rounds} turns={result.turns} blocked={result.blocked}")
    print("warnings:", json.dumps(result.warnings, ensure_ascii=False))

    if result.itinerary is None:
        print("\nFAIL: 未产生可交付行程。")
        return 1

    print(f"\n=== 行程：总价 {result.itinerary.total_cost:.0f} / 预算 {brief.budget_total:.0f} ===")
    for day in result.itinerary.days:
        print(f"\n{day.date} · {day.day_cost:.0f} 元")
        for item in sorted(day.items, key=lambda i: (i.start_time, i.end_time)):
            if item.type == "rest":
                continue
            print(f"  {item.start_time:%H:%M}-{item.end_time:%H:%M} {item.title} [{item.type}] {item.cost:.0f} 元")
        for note in day.notes:
            print("  说明:", note)

    critique = result.ctx.hard_check or {}
    print("\n硬约束:", json.dumps(critique, ensure_ascii=False))
    violations = _time_consistency(brief, result.itinerary)
    if violations:
        print("时间一致性错误:")
        for issue in violations:
            print(" -", issue)
    if not result.converged:
        print("FAIL: Agent 协作未收敛。")
        return 1
    if not critique.get("passed"):
        print("FAIL: 硬约束未通过。")
        return 1
    if violations:
        print("FAIL: 时间安排与航班/用餐不一致。")
        return 1
    print("\nPASS: 真实模型流程已收敛、硬校验通过、时间检查通过。")
    return 0


def main() -> int:
    brief = arguments()
    return asyncio.run(run(brief))


if __name__ == "__main__":
    raise SystemExit(main())
