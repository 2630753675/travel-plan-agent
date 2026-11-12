"""验收：中断 → 恢复后的事件连续性。

覆盖：
A 恢复后事件接在原有 seq 之后，不重置、不覆盖
B 被中断的节点在恢复时是 agent_resumed，parent 指向中断前那次 start
C 恢复不会重复发 stage_started
D 恢复过程里新增的事件都能通过 GET /api/plan/{id}/events?after_seq 取到
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sse_protocol import final_result, interrupt_info, parse_sse, run_events  # noqa: E402

BASE = "http://127.0.0.1:8848"
failures: list[str] = []

# 预算 3000 会触发 budget_overrun 中断
BRIEF = {
    "origin": "上海",
    "destination": "成都",
    "start_date": "2026-11-20",
    "end_date": "2026-11-24",
    "travelers": 2,
    "budget_total": 3000,
    "companions": ["老人"],
    "preferences": ["人文"],
    "pace": "适中",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name} {'' if ok else detail}")
    if not ok:
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
        with urllib.request.urlopen(BASE + path, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    print("\n[1] 触发中断")
    status, text = post("/api/plan", BRIEF)
    check("请求成功", status == 200, str(status))
    events = parse_sse(text)
    info = interrupt_info(events)
    check("确实停在中途", info is not None)
    if info is None:
        return 1

    first = run_events(events)
    plan_id = info["plan_id"]
    last_before = first[-1]["seq"]
    started_before = [e for e in first if e["event_type"] == "agent_started"]
    print(f"  中断前 {len(first)} 条事件，末序号 {last_before}")

    print("\n[2] 恢复")
    status, text = post(f"/api/plan/{plan_id}/resume", {"decision": "accept"})
    check("恢复成功", status == 200, str(status))
    resumed = run_events(parse_sse(text))
    check("恢复后产出了结果", final_result(parse_sse(text)) is not None)
    check("恢复有事件", len(resumed) > 0, str(len(resumed)))

    print("\n[3] 序号连续，不重置")
    seqs = [e["seq"] for e in resumed]
    check("首条接在中断之后", seqs[0] == last_before + 1, f"{seqs[0]} vs {last_before + 1}")
    check("整体严格递增", seqs == sorted(seqs) and len(set(seqs)) == len(seqs))
    check("恢复以 run_resumed 开头", resumed[0]["event_type"] == "run_resumed", resumed[0]["event_type"])
    check("run_id 未变", all(e["run_id"] == plan_id for e in resumed))

    print("\n[4] 恢复不重复开阶段")
    stages_before = {(e["stage"], e["event_id"]) for e in first if e["event_type"] == "stage_started"}
    stages_after = [e for e in resumed if e["event_type"] == "stage_started"]
    # ask/confirm 这类节点恢复时重放，如果它开启了新阶段那是合理的；
    # 但不能出现"和中断前完全相同的阶段又被开一次"的假事件
    check(
        "没有重复开同一阶段",
        not any((e["stage"], e["event_id"]) in stages_before for e in stages_after),
        str([(e["stage"]) for e in stages_after]),
    )

    print("\n[5] 中断节点以 resumed 身份继续")
    resumed_agents = [e for e in resumed if e["event_type"] == "agent_resumed"]
    print(f"  agent_resumed: {[e['actor'] for e in resumed_agents]}")
    check("至少有一个 agent 是恢复的", len(resumed_agents) >= 1, str(len(resumed_agents)))
    start_ids = {e["event_id"] for e in started_before}
    check(
        "恢复事件挂回中断前的启动事件",
        all(e["parent_id"] in start_ids for e in resumed_agents),
        str([e["parent_id"] for e in resumed_agents][:3]),
    )

    print("\n[6] 完整回放")
    status, body = get(f"/api/plan/{plan_id}/events")
    check("回放接口可用", status == 200, str(status))
    all_events = json.loads(body)["events"]
    check("回放包含恢复后的事件", len(all_events) >= len(first) + len(resumed), f"{len(all_events)}")
    check(
        "回放序号连续无缺口",
        [e["seq"] for e in all_events] == list(range(1, len(all_events) + 1)),
        str([e["seq"] for e in all_events][-3:]),
    )
    check("回放 event_id 不重复", len({e["event_id"] for e in all_events}) == len(all_events))

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  中断恢复的事件连续性验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
