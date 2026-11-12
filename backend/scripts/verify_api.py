"""P4 验收：REST + SSE 接口。

需要先启动服务：
    uvicorn app.api.main:app --port 8848
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sse_protocol import (  # noqa: E402
    final_result,
    is_completed,
    is_failed,
    normalized_event_types,
    parse_sse,
    run_events,
)

BASE = "http://127.0.0.1:8848"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def post(path: str, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def parse_sse_legacy(text: str) -> list[tuple[str, dict]]:
    """本文件早先自带的解析器，保留给旧断言使用。"""
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event and data is not None:
            events.append((event, data))
    return events


BRIEF = {
    "origin": "上海",
    "destination": "成都",
    "start_date": "2026-11-20",
    "end_date": "2026-11-24",
    "travelers": 2,
    "budget_total": 8000,
    "companions": ["老人"],
    "preferences": ["美食", "人文"],
    "pace": "适中",
}


def main() -> int:
    print("\n[1] 健康检查")
    status, body = get("/api/health")
    check("GET /api/health 返回 200", status == 200, str(status))

    print("\n[2] POST /api/plan 流式规划")
    status, text = post("/api/plan", BRIEF)
    check("返回 200", status == 200, str(status))
    events = parse_sse(text)
    kinds = normalized_event_types(events)
    print(f"  事件数 {len(events)}: {kinds[:6]} ... {kinds[-3:]}")
    check("有过程事件", kinds.count("trace") >= 7, f"{kinds.count('trace')}")
    check("有完成事件", is_completed(events))
    check("无错误事件", not is_failed(events))
    check("事件协议统一为 run_event", all(e == "run_event" for e, _ in events))

    done = final_result(events)
    if done is None:
        print("  FAIL  缺少完成事件，后续用例无法执行")
        return 1
    plan_id = done["plan_id"]
    print(f"  plan_id={plan_id}, 天数={len(done['itinerary']['days'])}, "
          f"总花费={done['itinerary']['total_cost']:.0f}, 重排={done['revision']}")
    check("行程 5 天", len(done["itinerary"]["days"]) == 5)
    check("完成载荷自带结果", "itinerary" in done and "critique" in done)

    print("\n[2b] 事件契约")
    evs = run_events(events)
    seqs = [e["seq"] for e in evs]
    ids = [e["event_id"] for e in evs]
    check("序号严格递增", seqs == sorted(seqs) and len(set(seqs)) == len(seqs), str(seqs[:5]))
    check("event_id 不重复", len(set(ids)) == len(ids), f"{len(ids)} ids")
    check("全部带 run_id", all(e["run_id"] == plan_id for e in evs))
    agent_started = [e for e in evs if e["event_type"] == "agent_started"]
    check("记录 agent 启动", len(agent_started) >= 5, str(len(agent_started)))
    tool_calls = [e for e in evs if e["event_type"] == "tool_call"]
    tool_results = [e for e in evs if e["event_type"] == "tool_result"]
    check("记录真实工具调用", len(tool_calls) >= 4, str(len(tool_calls)))
    call_ids = {e["event_id"] for e in tool_calls}
    check("工具结果挂在调用下", all(e.get("parent_id") in call_ids for e in tool_results), str(len(tool_results)))
    check("包含校验事件", any(e["event_type"] == "validation_result" for e in evs))

    print("\n[3] GET /api/plan/{id} 取回结果")
    status, body = get(f"/api/plan/{plan_id}")
    check("返回 200", status == 200, str(status))
    record = json.loads(body)
    check("行程一致", record["itinerary"]["days"][0]["date"] == done["itinerary"]["days"][0]["date"])
    check("事件已持久化", len(record.get("events") or []) >= len(evs), str(len(record.get("events") or [])))
    check("brief 已持久化", record["brief"]["destination"] == "成都")

    print("\n[3b] GET /api/plan/{id}/events 增量回放")
    status, body = get(f"/api/plan/{plan_id}/events?after_seq=3")
    check("返回 200", status == 200, str(status))
    tail = json.loads(body)
    check("只返回游标之后的事件", all(e["seq"] > 3 for e in tail["events"]), str([e["seq"] for e in tail["events"]][:5]))
    check("last_seq 正确", tail["last_seq"] == evs[-1]["seq"], str(tail.get("last_seq")))

    print("\n[4] 不存在的 plan")
    status, _ = get("/api/plan/doesnotexist")
    check("返回 404", status == 404, str(status))

    print("\n[5] 单日重排")
    target = done["itinerary"]["days"][2]["date"]
    status, body = post(f"/api/plan/{plan_id}/refine", {"date": target, "feedback": "这天太赶，想轻松点"})
    check("返回 200", status == 200, str(status))
    if status == 200:
        refined = json.loads(body)
        old_days = done["itinerary"]["days"]
        new_days = refined["itinerary"]["days"]
        changed = [d for d in new_days if d["date"] != target]
        unchanged = True
        for d in changed:
            old = next(x for x in old_days if x["date"] == d["date"])
            if [i["title"] for i in old["items"]] != [i["title"] for i in d["items"]]:
                unchanged = False
                break
        check("其他各天未被改动", unchanged)
        new_target = next(d for d in new_days if d["date"] == target)
        old_target = next(d for d in old_days if d["date"] == target)
        sights_new = [i for i in new_target["items"] if i["type"] == "sight"]
        sights_old = [i for i in old_target["items"] if i["type"] == "sight"]
        print(f"  目标日景点数 {len(sights_old)} → {len(sights_new)}")
        check("目标日确实重排了", [i["title"] for i in sights_new] != [i["title"] for i in sights_old])

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  P4 接口全部验收项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
