"""深度团队专项回归：跨恢复账本、工具缓存/幂等、排程餐食一致性。

不调用真实模型/网络。
运行：
    .venv/Scripts/python.exe scripts/verify_peer_deep_team.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents import runtime, tools_spec  # noqa: E402
from app.agents.tools_spec import ToolContext, execute  # noqa: E402
from app.llm import LLMClient, ModelCallBudgetExceeded  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.settings import LLMSettings  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def brief() -> TripBrief:
    return TripBrief(
        origin="上海", destination="成都",
        start_date=date(2026, 11, 20), end_date=date(2026, 11, 24),
        travelers=2, budget_total=8000, companions=["老人"],
        preferences=["人文"], pace="适中", planning_mode="peer",
    )


def test_stats_continue_across_resume() -> None:
    print("\n[A] 恢复段累计用量与预算")
    cfg = LLMSettings(enabled=True, base_url="http://x", model="x", api_key="k")
    prior = {"calls": 5, "input_chars": 100, "output_chars": 50, "latency_ms": 900,
             "usage_calls": 5, "prompt_tokens": 70, "completion_tokens": 30}
    client = LLMClient(cfg, max_calls=6, initial_stats=prior)
    stats = client.stats()
    check("调用数延续历史", stats["calls"] == 5, str(stats["calls"]))
    check("字符与 token 延续历史", stats["total_tokens"] == 100 and stats["input_chars"] == 100, str(stats))
    client._reserve_call()
    check("继续后只增加一次", client.calls == 6, str(client.calls))
    try:
        client._reserve_call()
    except ModelCallBudgetExceeded:
        check("累计到上限后拒绝新请求", True)
    else:
        check("累计到上限后拒绝新请求", False, "未抛出")


def test_read_cache() -> None:
    print("\n[B] 只读工具缓存")
    ctx = ToolContext(brief=brief(), source=MockSource())
    calls = {"n": 0}
    original = ctx.source.search_hotels

    def counting(*a, **k):
        calls["n"] += 1
        return original(*a, **k)

    ctx.source.search_hotels = counting  # type: ignore[method-assign]
    first = execute(ctx, "search_hotels", {})
    second = execute(ctx, "search_hotels", {})
    check("首次查询打数据源", first.get("ok") and calls["n"] == 1, str(calls))
    check("同参数第二次走缓存", second.get("cached") is True and calls["n"] == 1, str(second.keys()))

    execute(ctx, "list_pois", {})
    version_before = ctx.plan_version
    execute(ctx, "assign_pois", {"date": "2026-11-20", "poi_ids": []})
    check("写操作递增版本", ctx.plan_version > version_before, f"{version_before}->{ctx.plan_version}")


def test_operation_replay() -> None:
    print("\n[C] 恢复后不重复执行已成功写操作")
    ctx = ToolContext(brief=brief(), source=MockSource())
    execute(ctx, "search_hotels", {})
    execute(ctx, "set_hotel", {"hotel_id": "春熙里和颐"})
    version = ctx.plan_version
    call_id = "call-fixed"
    ctx.operations[call_id] = {
        "agent": "local_expert", "tool": "add_fixed_event", "arguments": {},
        "status": "succeeded",
        "result": {"ok": True, "event_id": "fe-x", "plan_version": version},
        "before_version": version - 1, "after_version": version,
    }

    class Scripted:
        max_calls = 10
        calls = 0

        async def chat_tools(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {"content": "", "tool_calls": [{
                    "id": call_id, "name": "add_fixed_event",
                    "arguments": {"date": "2026-11-24", "title": "重复", "start_time": "05:40", "end_time": "07:00"},
                }]}
            decision = {"say": "done", "kind": "note", "to": [], "wake": [], "done": True,
                        "changes": [], "evidence": [], "escalate": None}
            return {"content": json.dumps(decision, ensure_ascii=False), "tool_calls": []}

    turn = asyncio.run(runtime.run_turn(
        agent="local_expert", system="本地体验与每日活动专员",
        prompt="继续", tools=["add_fixed_event"], ctx=ctx, bus=None,  # type: ignore[arg-type]
        client=Scripted(), emit=lambda *a, **k: None,
    ))
    check("恢复后回合正常结束", turn.ok, str(turn.error))
    check("版本没有被重复推进", ctx.plan_version == version, f"{version}->{ctx.plan_version}")
    check("没有新增固定事件", not ctx.fixed_events, str(ctx.fixed_events))


def test_meal_consistency() -> None:
    print("\n[D] 餐食口径与返程日费用")
    ctx = ToolContext(brief=brief(), source=MockSource())
    execute(ctx, "search_flights", {})
    chosen = execute(ctx, "set_flights", {"outbound_flight_no": "3U8890", "inbound_flight_no": "3U8891"})
    if not chosen.get("ok"):
        check("航班选择可用", False, str(chosen))
        return
    execute(ctx, "search_hotels", {})
    execute(ctx, "set_hotel", {"hotel_id": "春熙里和颐"})
    execute(ctx, "list_pois", {})
    assignment = {"2026-11-20": [], "2026-11-21": ["p02", "p03"], "2026-11-22": ["p08", "p12"],
                  "2026-11-23": ["p07", "p01"], "2026-11-24": []}
    for day, ids in assignment.items():
        execute(ctx, "assign_pois", {"date": day, "poi_ids": ids})
    added = execute(ctx, "add_fixed_event", {
        "date": "2026-11-20", "title": "酒店入住+午餐+老人午休",
        "start_time": "12:45", "end_time": "14:20", "kind": "activity",
    })
    check("固定午餐事件可登记", added.get("ok"), str(added))
    execute(ctx, "add_fixed_event", {
        "date": "2026-11-24", "title": "送机", "start_time": "05:40",
        "end_time": "07:00", "kind": "airport_transfer",
    })
    materialized = execute(ctx, "materialize_plan", {})
    check("排程成功", materialized.get("ok"), str(materialized.get("error")))
    # 这里断言排程器内部字段（notes / meal_estimate），需要完整视图。
    plan = execute(ctx, "get_plan", {"view": "full"})
    days = {d["date"]: d for d in plan.get("itinerary", {}).get("days", [])}
    arrival = days.get("2026-11-20", {})
    last = days.get("2026-11-24", {})
    arrival_notes = " ".join(arrival.get("notes") or [])
    check("固定午餐不再报没有时间窗", "没有足够午餐时间窗" not in arrival_notes, arrival_notes[:160])
    check("固定午餐计入费用", arrival.get("meal_estimate", 0) >= 120, str(arrival.get("meal_estimate")))
    check("返程日餐费不为 0", last.get("meal_estimate", 0) >= 180, str(last.get("meal_estimate")))

    # 追加一条错误的送机事件：应被识别为重复阻塞，并给出可执行修复工具。
    duplicate = execute(ctx, "add_fixed_event", {
        "date": "2026-11-24", "title": "错误的旧送机", "start_time": "07:00",
        "end_time": "08:20", "kind": "airport_transfer",
    })
    check("可登记第二条送机（用于复现冲突）", duplicate.get("ok"), str(duplicate))
    blocked = execute(ctx, "materialize_plan", {})
    codes = [i["code"] for i in (blocked.get("hard_check") or {}).get("issues", [])]
    check("重复送机被判为 blocker", "AIRPORT_TRANSFER_DUPLICATE" in codes, str(codes))
    repair = next(
        (i.get("repair_tools") for i in (blocked.get("hard_check") or {}).get("issues", [])
         if i["code"] == "AIRPORT_TRANSFER_DUPLICATE"),
        [],
    )
    check("重复送机给出可执行修复工具", "remove_fixed_event" in repair, str(repair))

    fixed = execute(ctx, "remove_fixed_event", {"event_id": duplicate.get("event_id")})
    check("按 ID 删除错误送机", fixed.get("ok"), str(fixed))
    after = execute(ctx, "materialize_plan", {})
    check("修复后硬校验通过", (after.get("hard_check") or {}).get("passed") is True,
          str((after.get("hard_check") or {}).get("issues")))


def test_declarative_submit() -> None:
    print("\n[E] 声明式批量提交")
    ctx = ToolContext(brief=brief(), source=MockSource())
    execute(ctx, "list_pois", {})
    version = ctx.plan_version
    closed = execute(ctx, "submit_day_plans", {
        "days": [{"date": "2026-11-23", "poi_ids": ["p08"]}],  # 成都博物馆周一闭馆
    })
    check("闭馆安排被拒绝", closed.get("ok") is False and "闭馆" in closed.get("error", ""), str(closed))
    check("失败不推进版本", ctx.plan_version == version, f"{version}->{ctx.plan_version}")

    conflict = execute(ctx, "submit_day_plans", {
        "days": [{"date": "2026-11-21", "poi_ids": ["p02"]}],
        "expected_version": version + 5,
    })
    check("版本冲突被拒绝", conflict.get("ok") is False and "版本冲突" in conflict.get("error", ""), str(conflict))

    ok = execute(ctx, "submit_day_plans", {
        "days": [
            {"date": "2026-11-21", "poi_ids": ["p02", "p03"]},
            {"date": "2026-11-22", "poi_ids": ["p08", "p12"]},
        ],
        "expected_version": version,
    })
    check("批量提交成功", ok.get("ok") is True, str(ok))
    check("批量提交只递增一次版本", ctx.plan_version == version + 1, f"{version}->{ctx.plan_version}")
    check("提交结果带前后差异", len(ok.get("changed") or []) == 2, str(ok.get("changed")))

    execute(ctx, "search_hotels", {})
    execute(ctx, "set_hotel", {"hotel_id": "春熙里和颐"})
    routes = execute(ctx, "route_distances", {"poi_ids": ["p01"]})
    check("路线工具声明没有道路数据",
          routes.get("ok") and routes.get("road_data_available") is False and routes.get("mode") == "straight_line",
          str(routes)[:200])

def test_prior_stats_loader() -> None:
    print("\n[F] 恢复前读取累计用量")
    import app.api.main as api

    original = api.store.load_run
    api.store.load_run = lambda run_id: {"payload": {"llm_stats": {"calls": 18, "total_tokens": 255305}}}
    try:
        stats = api._prior_run_stats("run-x")
    finally:
        api.store.load_run = original
    check("从上次运行读取调用数", stats.get("calls") == 18, str(stats))
    check("从上次运行读取 token", stats.get("total_tokens") == 255305, str(stats))
    api.store.load_run = lambda run_id: None
    try:
        empty = api._prior_run_stats("run-y")
    finally:
        api.store.load_run = original
    check("没有历史时从 0 开始", empty == {}, str(empty))


def test_resume_passes_cumulative_stats() -> None:
    print("\n[G] 恢复接口把累计用量带进新图")
    from fastapi.testclient import TestClient

    import app.api.main as api
    from app.settings import AppSettings, LLMSettings, OrchestrationSettings

    captured: dict = {}
    inner = brief()

    class Snap:
        next = ("peer_ask",)
        values = {"brief": inner}

    class Probe:
        async def aget_state(self, config):
            return Snap()

    def fake_build_probe(source, settings=None, checkpointer=None, cancellation=None, initial_stats=None):
        captured["initial_stats"] = initial_stats
        captured["probe"] = True
        return Probe()

    async def fake_run_graph(*a, **k):
        if False:
            yield ""

    originals = (api.build, api.store.load, api.store.load_run, api.config_store.load, api._run_graph)
    api.build = fake_build_probe
    api.store.load = lambda plan_id: None
    api.store.load_run = lambda plan_id: {"payload": {"llm_stats": {"calls": 18, "total_tokens": 255305}}}
    api.config_store.load = lambda: AppSettings(
        llm=LLMSettings(enabled=True, base_url="http://stub", model="stub", api_key="k"),
        orchestration=OrchestrationSettings(agent_mode="peer"),
    )
    api._run_graph = fake_run_graph
    try:
        client = TestClient(api.app, headers={"X-Access-Token": api._ACCESS_TOKEN})
        r = client.post("/api/plan/run-resume/resume", json={"decision": "continue"})
    finally:
        api.build, api.store.load, api.store.load_run, api.config_store.load, api._run_graph = originals
    check("恢复请求被接受", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    stats = captured.get("initial_stats") or {}
    check("恢复图收到累计调用数", stats.get("calls") == 18, str(stats))
    check("恢复图收到累计 token", stats.get("total_tokens") == 255305, str(stats))


def main() -> int:
    test_stats_continue_across_resume()
    test_read_cache()
    test_operation_replay()
    test_meal_consistency()
    test_declarative_submit()
    test_prior_stats_loader()
    test_resume_passes_cumulative_stats()
    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  深度团队专项回归通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
