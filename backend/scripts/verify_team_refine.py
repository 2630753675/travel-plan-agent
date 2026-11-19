"""Verify team-mode single-day rework endpoint (wiring + guards). No real model."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from app.api import main as api  # noqa: E402
from app.api import store as plan_store  # noqa: E402
from app.models import Critique, DayPlan, Itinerary, TripBrief  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {detail}")


BRIEF = {"origin": "上海", "destination": "成都", "start_date": "2026-11-20",
         "end_date": "2026-11-22", "travelers": 2, "budget_total": 8000, "companions": ["老人"]}
ITINERARY = {"days": [{"date": "2026-11-20", "items": []}], "total_cost": 5000, "budget_total": 8000}
CRITIQUE = {"passed": True, "issues": [], "suggestions": []}


class FakeArtifacts:
    def __init__(self):
        self.payloads: dict[str, dict] = {}

    def put(self, payload):
        aid = f"{len(self.payloads):064x}"
        self.payloads[aid] = json.loads(json.dumps(payload, default=str))
        return {"artifact_id": aid, "bytes": 1}

    def get(self, ref):
        return self.payloads[ref["artifact_id"]]


def session_with_delivered_tasks() -> dict:
    return {
        "architecture": "professional_team",
        "tasks": {"T1": {"id": "T1", "owner": "logistics", "status": "delivered"}},
        "reviews": 1,
        "context": {"plan_version": 7},
        "pending_issue_codes": [],
    }


def fake_result(brief: TripBrief):
    class Result:
        converged = True
        escalation = None
        warnings: list[str] = []
        rounds = 1
        turns = 2
        itinerary = Itinerary(days=[DayPlan(date=date(2026, 11, 20))], budget_total=8000, total_cost=5100)

        def __init__(self):
            from app.agents.tools_spec import ToolContext
            self.ctx = ToolContext(brief=brief, source=MockSource())
            self.ctx.document = {"title": "返工后计划书", "overview": "x", "days": []}
            self.ctx.approved_version = 8
            self.session = {
                "architecture": "professional_team",
                "tasks": {
                    "T1": {"id": "T1", "owner": "logistics", "status": "delivered"},
                    "R2": {"id": "R2", "owner": "local_expert", "status": "delivered"},
                },
                "pending_issue_codes": [{"code": "CLOSED_DAY", "status": "closed", "owner": "local_expert"}],
                "reviews": 2,
            }

    return Result()


def build_completed_session() -> dict:
    """用真实工具构造一份“团队已完成”的会话状态，供局部返工续跑。"""
    from app.agents import hub
    from app.agents.tools_spec import ToolContext, execute
    from app.models import TripBrief

    brief = TripBrief(**BRIEF)
    ctx = ToolContext(brief=brief, source=MockSource())
    execute(ctx, "search_flights", {})
    outbound = ctx.transport.outbound[0].flight_no
    inbound = ctx.transport.inbound[0].flight_no
    execute(ctx, "set_flights", {"outbound_flight_no": outbound, "inbound_flight_no": inbound})
    execute(ctx, "search_hotels", {})
    execute(ctx, "set_hotel", {"hotel_id": ctx.lodging.candidates[0].name})
    execute(ctx, "list_pois", {})
    dates = [date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22)]
    open_pois = [p.id for p in ctx.poi.candidates if all(p.is_open_on(d) for d in dates)]
    assignment = {dates[0]: open_pois[:1], dates[1]: open_pois[1:2], dates[2]: []}
    execute(ctx, "submit_day_plans", {"days": [{"date": str(d), "poi_ids": v} for d, v in assignment.items()]})
    execute(ctx, "materialize_plan", {})
    check_result = execute(ctx, "check_plan", {})
    assert check_result.get("passed"), check_result
    ctx.approved_version = ctx.plan_version
    ctx.document = {"title": "已批准计划书", "overview": "x",
                    "days": [{"date": str(d), "summary": ""} for d in dates]}
    return {
        "architecture": "professional_team",
        "bus": {"messages": [], "mailboxes": {}, "awake": [], "sleeping": [], "wake_log": [], "round": 1},
        "context": hub.dump_context(ctx),
        "tasks": {
            "T1": {"id": "T1", "owner": "logistics", "goal": "交通住宿", "depends_on": [],
                   "status": "delivered", "delivery": {}, "changes": [], "notes": ""},
            "T2": {"id": "T2", "owner": "local_expert", "goal": "本地行程", "depends_on": ["T1"],
                   "status": "delivered", "delivery": {}, "changes": [], "notes": ""},
        },
        "histories": {},
        "trace": [],
        "turns": 5,
        "reviews": 1,
        "proposal_id": None,
        "review_version": ctx.plan_version,
        "pending_rejections": [],
        "pending_issue_codes": [],
        "warnings": [],
    }


def run_real_resume_case() -> None:
    """真实 hub 续跑：已完成的会话 + 注入单日任务，必须能重新评审并交付。"""
    from app.agents import hub

    session = build_completed_session()
    session["tasks"]["R3"] = {
        "id": "R3", "owner": "local_expert", "depends_on": [], "status": "pending",
        "goal": "按用户意见修改 2026-11-21 的行程：换一个室内景点", "delivery": None,
        "changes": [], "writes": ["assignment_version"], "notes": "",
    }
    brief = TripBrief(**BRIEF)
    start_version = int(session["context"]["plan_version"])

    class Client:
        max_calls = 40

        def __init__(self):
            self.calls = 0
            self.local_steps = 0
            self.writer_steps = 0

        async def chat_tools(self, messages, tools):
            self.calls += 1
            system = messages[0]["content"]
            names = {tool["function"]["name"] for tool in tools}

            def call(name, arguments=None):
                return {"content": "", "tool_calls": [
                    {"id": f"c{self.calls}", "name": name, "arguments": arguments or {}}]}

            def decision(**kwargs):
                base = {"kind": "note", "say": "ok", "to": [], "wake": [], "done": True,
                        "changes": [], "evidence": [], "escalate": None}
                base.update(kwargs)
                return {"content": "", "tool_calls": [
                    {"id": f"d{self.calls}", "name": "submit_decision", "arguments": base}]}

            if "本地体验与每日活动专员" in system:
                self.local_steps += 1
                if "assign_pois" in names and self.local_steps == 1:
                    return call("assign_pois", {"date": "2026-11-21", "poi_ids": []})
                if "materialize_plan" in names and self.local_steps == 2:
                    return call("materialize_plan")
                if "check_plan" in names and self.local_steps == 3:
                    return call("check_plan")
                return decision(kind="proposal", to=["orchestrator"],
                                say="按用户意见把这一天改为室内安排（具体场馆以当日开放为准，待确认）",
                                delivery={"rationale": "按用户意见把这一天改为室内安排"})
            if "财务与风控评审" in system:
                return decision(kind="accept", say="按评审包复核通过")
            if "行程整合与文案专员" in system:
                self.writer_steps += 1
                if self.writer_steps == 1:
                    return call("get_plan", {"view": "summary"})
                days = [{"date": "2026-11-20", "summary": "按获批行程抵达并入住，具体以当日现场为准"},
                        {"date": "2026-11-21", "summary": "按用户意见改为室内安排，具体以当日现场为准"},
                        {"date": "2026-11-22", "summary": "按获批行程返程，具体以当日现场为准"}]
                return decision(kind="final", to=[], say="已按获批版本整合成计划书，未核实项已标注，待确认",
                                document={
                                    "title": "返工后的计划", "overview": "按意见调整", "days": days})
            return decision(kind="note", say="无需动作")

    emitted: list[tuple[str, str]] = []

    def capture(kind, /, **payload):
        detail = payload.get("summary") or payload.get("tool") or payload.get("error") or payload.get("text") or ""
        emitted.append((kind, str(detail)[:160]))

    result = asyncio.run(hub.run_session(
        brief=brief, source=MockSource(), client=Client(),
        emit=capture,
        max_turns=20, max_rounds=max(2, 2), resume=session,
    ))
    failures_detail = [item for item in emitted
                       if item[0] == "protocol_rejected" or (item[0] == "tool_result" and "失败" in item[1])][-4:]
    check("完成态会话可继续局部返工", result.converged,
          f"{str(result.escalation)[:100]} | 失败动作: {failures_detail}")
    check("返工后版本推进", result.ctx.plan_version > start_version, f"{start_version}->{result.ctx.plan_version}")
    check("返工后重新批准", result.ctx.approved_version == result.ctx.plan_version,
          f"{result.ctx.approved_version}/{result.ctx.plan_version}")
    check("返工后计划书重写", (result.ctx.document or {}).get("title") == "返工后的计划",
          str(result.ctx.document)[:120])
    check("原会话快照未被续跑改写", session["tasks"]["T2"]["status"] == "delivered"
          and "allowed_repair_tools" not in session["tasks"].get("R3", {}),
          str(session["tasks"].get("R3")))


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp())
    original_db = plan_store.DB_PATH
    plan_store.DB_PATH = tmp / "refine.db"
    fake = FakeArtifacts()
    original_store = api.default_store
    original_session_runner = api.peer_hub.run_session
    original_critic = api.critic.run
    original_config = api.config_store.load
    original_source = api.make_source
    captured: dict = {}

    api.default_store = lambda: fake
    api.critic.run = lambda *a, **k: Critique(passed=True, issues=[], suggestions=[])
    api.make_source = lambda cfg: MockSource()

    class LlmCfg:
        class llm:
            configured = True
            enabled = True
            provider = "openai-compatible"
            base_url = "https://example.com/v1"
            model = "m"
            api_key = "k" * 12
            temperature = 0.3

        class orchestration:
            node_timeout_s = 30
            max_revisions = 2
            max_agent_turns = 20
            max_runtime_s = 180

        class datasource:
            kind = "mock"
            http_base_url = ""

    api.config_store.load = lambda: LlmCfg()
    try:
        session_ref = fake.put(session_with_delivered_tasks())
        plan_store.save("p-rework", BRIEF, ITINERARY, CRITIQUE, [], revision=0, team={
            "state": "approved", "document": {"title": "原计划书"}, "tasks": {},
            "issue_ledger": [], "approved_version": 7, "session": session_ref,
        })

        async def fake_run_session(**kwargs):
            captured.update(kwargs)
            return fake_result(kwargs["brief"])

        api.peer_hub.run_session = fake_run_session
        response = asyncio.run(api.refine_plan(
            "p-rework", api.RefineRequest(date=date(2026, 11, 20), feedback="换一个室内景点")
        ))
        resumed = captured.get("resume") or {}
        injected = (resumed.get("tasks") or {}).get("R2") or {}
        check("返工注入单日任务", injected.get("owner") == "local_expert" and "2026-11-20" in injected.get("goal", ""),
              str(injected))
        check("返工任务只声明行程域", injected.get("writes") == ["assignment_version"], str(injected.get("writes")))
        check("返工不是整场重排", resumed.get("pending_agent") is None and resumed.get("pending_rejections") == [],
              str(resumed.get("pending_agent")))
        check("评审额度至少给一次", captured.get("max_rounds", 0) >= 2, str(captured.get("max_rounds")))
        check("返回团队交付视图", response.get("team_state") == "approved"
              and (response.get("document") or {}).get("title") == "返工后计划书", str(response)[:200])
        check("返回问题清单", isinstance(response.get("issue_ledger"), list) and response["issue_ledger"])
        saved = plan_store.load("p-rework") or {}
        check("新版本带团队视图", (saved.get("team") or {}).get("approved_version") == 8, str(saved.get("team"))[:120])
        check("新版本可继续返工", bool((saved.get("team") or {}).get("session")), "缺少 session 引用")

        # 旧/手工重排过的版本没有会话：必须明确拒绝，而不是偷偷走旧流水线。
        plan_store.save("p-detached", BRIEF, ITINERARY, CRITIQUE, [], team={"state": "detached"})
        try:
            asyncio.run(api.refine_plan("p-detached", api.RefineRequest(date=date(2026, 11, 20), feedback="x")))
        except HTTPException as exc:
            check("未关联团队的版本拒绝局部返工", exc.status_code == 409 and "团队会话" in exc.detail, exc.detail)
        else:
            check("未关联团队的版本拒绝局部返工", False)

        try:
            asyncio.run(api.refine_plan("p-missing", api.RefineRequest(date=date(2026, 11, 20), feedback="x")))
        except HTTPException as exc:
            check("不存在的规划返回 404", exc.status_code == 404, str(exc.status_code))
        else:
            check("不存在的规划返回 404", False)
    finally:
        plan_store.DB_PATH = original_db
        api.default_store = original_store
        api.peer_hub.run_session = original_session_runner
        api.critic.run = original_critic
        api.config_store.load = original_config
        api.make_source = original_source

    try:
        run_real_resume_case()
    except AssertionError as exc:
        check("真实续跑用例可执行", False, str(exc))

    print()
    if failures:
        print(f"FAIL {len(failures)} 项未通过")
        return 1
    print("PASS 团队局部返工回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
