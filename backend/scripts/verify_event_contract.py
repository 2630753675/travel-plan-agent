"""P1 事件契约单测：序号、唯一 ID、阶段/agent 因果关系、工具 call/result 关联与 SQLite 回放。"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import store
from app.event_stream import EventFactory
from app.models import LogEntry

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name} {'' if ok else detail}")
    if not ok:
        failures.append(name)


def main() -> int:
    print("\n[1] RunEvent identity and monotonic sequence")
    f = EventFactory("run-test", plan_version=2)
    stage = f.make("stage_started", stage="retrieval", payload={"label": "并行检索"})
    agent = f.make("agent_started", stage="retrieval", actor="lodging", parent_id=stage.event_id)
    check("run_id/version carried", agent.run_id == "run-test" and agent.plan_version == 2)
    check("seq monotonic", [e.seq for e in f.events] == [1, 2])
    check("event_id unique", len({e.event_id for e in f.events}) == 2)
    check("parent causality", agent.parent_id == stage.event_id)

    print("\n[2] Tool call/result forms a truthful parent-child pair")
    # 数据源工具已有真实 call/result 事件，旧的摘要行必须丢弃，否则会重复显示
    source_tool = LogEntry(ts="12:00:00", actor="住宿", kind="tool", text="search_hotels(成都) → 5 家", group="retrieval", depth=2, tool="search_hotels")
    check("source tool 摘要去重", f.from_log(source_tool) == [])
    other_tool = LogEntry(ts="12:00:00", actor="预算官", kind="tool", text="day_cost_breakdown() → 已执行", group="negotiation", depth=2, tool="day_cost_breakdown")
    kept = f.from_log(other_tool)
    check("非数据源工具保留摘要", len(kept) == 1 and kept[0].event_type == "tool_exchange")
    call = f.make("tool_call", stage="retrieval", actor="lodging", payload={"tool_name": "search_hotels", "arguments": {"city": "成都"}})
    result = f.make("tool_result", stage="retrieval", actor="lodging", parent_id=call.event_id, payload={"status": "success", "result_count": 5})
    check("call/result distinct events", call.event_type == "tool_call" and result.event_type == "tool_result")
    check("result references call", result.parent_id == call.event_id)

    print("\n[3] SQLite append, dedupe, ordered replay, incremental cursor")
    old_path = store.DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        store.DB_PATH = pathlib.Path(tmp) / "events.db"
        try:
            for event in f.events:
                payload = event.model_dump(mode="json")
                store.append_event(payload)
            # at-least-once delivery: duplicate event_id should be idempotent
            store.append_event(f.events[0].model_dump(mode="json"))
            rows = store.list_events("run-test")
            check("all events replayed exactly once", len(rows) == len(f.events), str(len(rows)))
            check("replay ordered by seq", [r["seq"] for r in rows] == sorted(r["seq"] for r in rows))
            check("after_seq cursor works", [r["seq"] for r in store.list_events("run-test", after_seq=2)] == [3, 4, 5])
        finally:
            store.DB_PATH = old_path

    print("\n[4] Resume keeps causality instead of restarting agents")
    # 中断前留下一个未闭合的 agent 与一个未闭合的 stage（真实情况：
    # confirm 节点发完 start 后调 interrupt() 抛异常，end 永远不会发出）
    before = EventFactory("run-resume")
    stage_a = before.make("stage_started", stage="confirm", payload={"stage": "confirm"})
    open_start = before.make("agent_started", stage="confirm", actor="confirm", parent_id=stage_a.event_id)
    expect = [e.model_dump(mode="json") for e in before.events]

    after = EventFactory("run-resume", seq_start=3, prior_events=expect)
    resumed = after.from_custom({"phase": "start", "node": "confirm"})
    check("恢复时沿用同一 run_id", all(e.run_id == "run-resume" for e in resumed))
    check("序号接着历史继续", [e.seq for e in resumed] == [3], str([e.seq for e in resumed]))
    check("恢复的 agent 标记为 resumed", len(resumed) == 1 and resumed[0].event_type == "agent_resumed")
    check("parent 指向中断前的启动事件", resumed[0].parent_id == open_start.event_id)

    completed = after.from_custom({"phase": "end", "node": "confirm", "summary": "你选择：accept"})
    check("闭合事件挂在恢复事件下", completed[0].parent_id == resumed[0].event_id)
    check("恢复后不再产生第二个 stage", not any(e.event_type == "stage_started" for e in resumed))

    print("\n[5] Duplicate lifecycle markers are idempotent")
    dup = EventFactory("run-dup")
    first = dup.from_custom({"phase": "start", "node": "planner"})
    second = dup.from_custom({"phase": "start", "node": "planner"})
    check("重复 start 不再产生事件", len(first) == 2 and second == [], str(len(second)))
    ended = dup.from_custom({"phase": "end", "node": "planner", "summary": "x"})
    again = dup.from_custom({"phase": "end", "node": "planner", "summary": "x"})
    check("一次 start 只对应一个 end", len(ended) == 1 and again == [])

    print("\n[6] Tool results survive a resume through payload tool_call_id")
    t = EventFactory("run-tool")
    t.from_custom({"phase": "start", "node": "lodging"})
    t.from_custom({"phase": "tool_call", "node": "lodging", "tool_call_id": "abc", "tool": "search_hotels"})
    saved = [e.model_dump(mode="json") for e in t.events]
    check("tool_call 记录了 tool_call_id", saved[-1]["payload"]["tool_call_id"] == "abc")

    replayed = EventFactory("run-tool", seq_start=len(saved) + 1, prior_events=saved)
    result_events = replayed.from_custom(
        {"phase": "tool_result", "node": "lodging", "tool_call_id": "abc", "tool": "search_hotels", "summary": "6 条结果"}
    )
    call_event = next(e for e in t.events if e.event_type == "tool_call")
    check("恢复后 tool_result 仍挂在原调用下", result_events[0].parent_id == call_event.event_id)

    print()
    if failures:
        print(f"FAIL  {len(failures)} event-contract assertions")
        return 1
    print("PASS  event contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())
