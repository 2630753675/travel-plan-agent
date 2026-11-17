"""确定性回归：peer 整改（证据绑定、重试、停止终态、协议报错、Writer 校验、checkpoint 并发、API 中断）。

不调用真实模型/API；用脚本化 client、临时 checkpoint 和内存 store。
运行：
    .venv/Scripts/python.exe scripts/verify_peer_remediation.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
import threading
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents import hub, runtime  # noqa: E402
from app.checkpoint import PersistentSaver  # noqa: E402
from app.models import DayPlan, Itinerary, TripBrief  # noqa: E402
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


class StubClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def chat_tools(self, messages, tools):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def decision(**kw) -> dict:
    base = {"say": "ok", "kind": "note", "to": [], "wake": [], "done": True,
            "changes": [], "evidence": [], "escalate": None}
    base.update(kw)
    return {"content": json.dumps(base, ensure_ascii=False), "tool_calls": []}


def tool_call(name: str, args: dict | None = None) -> dict:
    return {"content": "", "tool_calls": [{"id": f"c-{name}", "name": name, "arguments": args or {}}]}


def _run_api_flow() -> tuple[list[tuple[str, dict]], dict]:
    """走真实 /api/plan 与 /resume 流式路径；模型、checkpoint、store 全部替换为测试实现。"""
    from fastapi.testclient import TestClient

    import app.api.main as api
    from app.llm import LLMClient
    from app.settings import AppSettings, LLMSettings, OrchestrationSettings

    temp = pathlib.Path(tempfile.mkdtemp())
    api._CHECKPOINTER = PersistentSaver(temp / "cp.pkl")
    api.config_store.load = lambda: AppSettings(
        llm=LLMSettings(enabled=True, base_url="http://127.0.0.1:9/v1", model="stub", api_key="k"),
        orchestration=OrchestrationSettings(agent_mode="peer"),
    )

    store_events: dict[str, list[dict]] = {}
    runs: dict[str, dict] = {}
    api.store.list_events = lambda run_id, after_seq=0: [
        e for e in store_events.get(run_id, []) if e["seq"] > after_seq
    ]
    api.store.append_event = lambda event: store_events.setdefault(event["run_id"], []).append(event)
    api.store.save_run = lambda run_id, status, payload: runs.update({"status": status, "payload": payload})
    api.store.load = lambda plan_id: None
    api.store.load_run = lambda run_id: None

    async def fake_chat_tools(self, messages, tools, **kwargs):
        d = {"say": "数据源缺少必要事实，未核实，需要你确认取舍", "kind": "escalate",
             "to": [], "wake": [], "done": False, "changes": [], "evidence": [],
             "escalate": {"question": "需要你确认预算取舍", "options": [
                 {"value": "continue", "label": "继续协作处理"},
                 {"value": "stop", "label": "停止并保留未完成状态"}]}}
        return {"content": json.dumps(d, ensure_ascii=False), "tool_calls": []}

    LLMClient.chat_tools = fake_chat_tools
    client = TestClient(api.app, headers={"X-Access-Token": api._ACCESS_TOKEN})
    body = {"origin": "上海", "destination": "成都", "start_date": "2026-11-20", "end_date": "2026-11-24",
            "travelers": 2, "budget_total": 8000, "companions": ["老人"], "preferences": ["人文"],
            "pace": "适中", "planning_mode": "peer"}

    events = _consume(client, "POST", "/api/plan", body)
    plan_id = next(((p.get("payload") or {}).get("plan_id") for t, p in events if t == "run_started"), "")
    resumed = _consume(client, "POST", f"/api/plan/{plan_id}/resume", {"decision": "stop", "kind": "peer_escalation"})
    return events + resumed, runs


def _consume(client, method: str, url: str, body: dict) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    with client.stream(method, url, json=body) as response:
        buffer = ""
        for chunk in response.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                for line in block.splitlines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        events.append((data.get("event_type"), data))
    return events


def main() -> int:
    print("\n[A] 空响应重试")
    from app.llm import LLMClient, LLMError, ModelCallBudgetExceeded
    from app.settings import LLMSettings
    budget_client = LLMClient(LLMSettings(enabled=True, base_url="http://x", model="x", api_key="k"), max_calls=1)
    budget_client._reserve_call()
    try:
        budget_client._reserve_call()
    except Exception as exc:
        check("调用预算有专用异常类型", isinstance(exc, ModelCallBudgetExceeded), type(exc).__name__)
    else:
        check("调用预算有专用异常类型", False)
    client = StubClient([
        LLMError("模型未返回内容或工具调用（finish_reason=None）"),
        decision(kind="note"),
    ])
    ctx = hub.ToolContext(brief=brief(), source=MockSource())
    events: list[tuple[str, dict]] = []
    turn = asyncio.run(runtime.run_turn(
        agent="orchestrator", system="s", prompt="p", tools=[], ctx=ctx,
        bus=__import__("app.agents.bus", fromlist=["Bus"]).Bus(), client=client,
        emit=lambda kind, /, **p: events.append((kind, p)),
    ))
    check("瞬时空响应重试后成功", turn.ok, str(turn.error))
    check("记录了 retry 事件", any(k == "retry" for k, _ in events), str([k for k, _ in events]))
    check("只消耗两次调用", client.calls == 2, str(client.calls))
    timed = StubClient([TimeoutError("deadline"), TimeoutError("deadline")])
    timeout_turn = asyncio.run(runtime.run_turn(
        agent="orchestrator", system="s", prompt="p", tools=[], ctx=ctx,
        bus=__import__("app.agents.bus", fromlist=["Bus"]).Bus(), client=timed,
        emit=lambda kind, /, **p: None,
    ))
    check("超时有限重试后留下明确类型", not timeout_turn.ok and timeout_turn.error_type == "TimeoutError",
          f"{timeout_turn.error_type}: {timeout_turn.error}")
    check("超时恰好重试一次", timed.calls == 2, str(timed.calls))

    print("\n[B] 证据必须引用真实工具结果")
    bus = __import__("app.agents.bus", fromlist=["Bus"]).Bus()
    fake = StubClient([decision(kind="proposal", evidence=[{"id": "made-up"}])])
    bad = asyncio.run(runtime.run_turn(
        agent="critic_agent", system="s", prompt="p", tools=[], ctx=ctx, bus=bus,
        client=fake, emit=lambda kind, /, **p: None, max_steps=2,
    ))
    check("伪造 evidence 被拒绝", not bad.ok, str(bad.error))

    ctx2 = hub.ToolContext(brief=brief(), source=MockSource())
    holder: dict = {}

    class TwoPhase:
        def __init__(self):
            self.n = 0

        async def chat_tools(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return tool_call("search_flights")
            evidence_id = next(iter(ctx2.evidence))
            return decision(kind="proposal", evidence=[{"id": evidence_id}])

    good = asyncio.run(runtime.run_turn(
        agent="logistics", system="s", prompt="p", tools=["search_flights"], ctx=ctx2,
        bus=bus, client=TwoPhase(), emit=lambda kind, /, **p: None, max_steps=3,
    ))
    check("真实 evidence_id 通过", good.ok, str(good.error))
    check("工具成功结果登记证据", len(ctx2.evidence) == 1, str(len(ctx2.evidence)))

    print("\n[C] 回合失败升级为可恢复确认，而不是运行上限")
    failing = StubClient([decision(kind="request", to=["logistics"], tasks=[
        {"id": "T1", "owner": "logistics", "goal": "订票"},
    ]), LLMError("模型未返回内容或工具调用（finish_reason=None）"),
        LLMError("模型未返回内容或工具调用（finish_reason=None）")])
    result = asyncio.run(hub.run_session(
        brief=brief(), source=MockSource(), client=failing, emit=lambda kind, /, **p: None,
        max_turns=6, max_rounds=1,
    ))
    question = (result.escalation or {}).get("question", "")
    check("失败产生升级", bool(result.escalation), str(result.escalation))
    check("升级原因是回合失败而非上限", "回合失败" in question, question[:120])
    check("提供继续/停止选项",
          {o["value"] for o in (result.escalation or {}).get("options", [])} == {"continue", "stop"},
          str((result.escalation or {}).get("options")))
    check("未伪造收敛", not result.converged)

    print("\n[D] 用户停止是终态")
    resume = {
        "architecture": "professional_team", "bus": {}, "context": {},
        "tasks": {}, "histories": {}, "trace": [], "turns": 1, "reviews": 0,
        "proposal_id": None, "review_version": None, "pending_rejections": [],
        "pending_issue_codes": [], "pending_agent": "logistics", "pending_task": None,
        "warnings": [],
    }
    stopped = asyncio.run(hub.run_session(
        brief=brief(), source=MockSource(), client=StubClient([]), emit=lambda kind, /, **p: None,
        max_turns=6, max_rounds=1, resume=resume, user_answer=json.dumps({"decision": "stop"}),
    ))
    check("stop 返回终态标记", stopped.stopped, str(stopped))
    check("stop 不再抛出问题", stopped.escalation is None, str(stopped.escalation))

    print("\n[E] 任务协议报错可执行")
    try:
        hub._validate_tasks([{"id": "T1", "owner": "critic_agent", "goal": "评审"}])
    except ValueError as exc:
        message = str(exc)
    else:
        message = ""
    check("指出评审角色不能当交付任务", "critic_agent" in message and "评审" in message, message)
    try:
        hub._validate_tasks([
            {"id": "T1", "owner": "logistics", "goal": "订票"},
            {"id": "T1", "owner": "local_expert", "goal": "排程"},
        ])
    except ValueError as exc:
        message = str(exc)
    else:
        message = ""
    check("指出重复 id", "重复" in message, message)

    print("\n[F] Writer 事实校验")
    ctx3 = hub.ToolContext(brief=brief(), source=MockSource())
    ctx3.itinerary = Itinerary(
        days=[DayPlan(date=date(2026, 11, 20))],
        budget_total=8000,
    )
    good_doc = {"title": "成都五日", "overview": "含老人节奏", "days": [{"date": "2026-11-20", "summary": "抵达"}],
                "practical_notes": ["房型未核实，需向酒店确认"]}
    check("正常文档通过", hub._writer_valid(good_doc, ctx3))
    bad_doc = {**good_doc, "practical_notes": ["已预订双床房"]}
    check("编造预订被拒绝", not hub._writer_valid(bad_doc, ctx3))

    print("\n[G] checkpoint 并发写入")
    tmp = pathlib.Path(tempfile.mkdtemp()) / "cp.pkl"
    saver = PersistentSaver(tmp)
    def worker(i: int) -> None:
        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": "", "checkpoint_id": f"cp-{i}"}}
        for _ in range(5):
            saver.put_writes(config, [("channel", i)], f"task-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    reloaded = PersistentSaver(tmp)
    check("并发写后分片可重新加载", any(key[0] == "t1" for key in reloaded.writes), str(list(reloaded.writes)[:3]))
    check("临时文件已清理", not list(tmp.parent.glob("*.tmp")), str(list(tmp.parent.glob("*.tmp"))))

    async def async_checkpoint_writes() -> None:
        async def write_one(i: int) -> None:
            await saver.aput_writes({"configurable": {"thread_id": f"async-{i}", "checkpoint_ns": "", "checkpoint_id": f"cp-{i}"}}, [("channel", i)], f"task-{i}")
        await asyncio.gather(*(write_one(i) for i in range(8)))

    asyncio.run(async_checkpoint_writes())
    async_reloaded = PersistentSaver(tmp)
    async_keys = {key[0] for key in async_reloaded.writes if isinstance(key, tuple) and key}
    check("LangGraph 异步写入并发安全", all(f"async-{i}" in async_keys for i in range(8)), str(list(async_reloaded.writes)))
    check("异步写入后无临时文件", not list(tmp.parent.glob("*.tmp")), str(list(tmp.parent.glob("*.tmp"))))

    print("\n[H] API 中断与停止终态（经真实流式路径）")
    api_events, api_runs = _run_api_flow()
    kinds = [event_type for event_type, _ in api_events]
    check("请求返回中断而不是失败", "run_interrupted" in kinds and "run_failed" not in kinds, str(kinds))
    interrupt_event = next((payload for event_type, payload in api_events if event_type == "run_interrupted"), {})
    interrupt = interrupt_event.get("payload") or {}
    interrupt = interrupt.get("interrupt") or {}
    check("中断携带问题与选项",
          bool(interrupt.get("question")) and bool(interrupt.get("options")),
          str(interrupt)[:200])
    check("停止后返回未完成终态", api_runs.get("status") == "needs_input", str(api_runs.get("status")))
    check("停止不产生 run_failed", "run_failed" not in [event_type for event_type, _ in api_events[-3:]], str(kinds[-3:]))

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  peer 整改回归通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
