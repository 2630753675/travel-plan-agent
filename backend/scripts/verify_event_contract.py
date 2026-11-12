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

    print()
    if failures:
        print(f"FAIL  {len(failures)} event-contract assertions")
        return 1
    print("PASS  event contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())
