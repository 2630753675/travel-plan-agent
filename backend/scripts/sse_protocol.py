"""SSE 解析与协议兼容。

新协议只有一种事件名 `run_event`，用 payload.event_type 区分语义；
旧协议是 trace / done / interrupt / error。验收脚本统一走这里，
避免每个脚本各自判断两套格式。
"""

from __future__ import annotations

import json


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name and data is not None:
            events.append((name, data))
    return events


def run_events(events: list[tuple[str, dict]]) -> list[dict]:
    return [d for n, d in events if n == "run_event"]


def is_completed(events: list[tuple[str, dict]]) -> bool:
    if any(n == "done" for n, _ in events):
        return True
    return any(d.get("event_type") == "run_completed" for d in run_events(events))


def is_interrupted(events: list[tuple[str, dict]]) -> bool:
    if any(n == "interrupt" for n, _ in events):
        return True
    return any(d.get("event_type") == "run_interrupted" for d in run_events(events))


def is_failed(events: list[tuple[str, dict]]) -> bool:
    if any(n == "error" for n, _ in events):
        return True
    return any(d.get("event_type") == "run_failed" for d in run_events(events))


def final_result(events: list[tuple[str, dict]]) -> dict | None:
    for name, data in events:
        if name == "done":
            return data
    for data in run_events(events):
        if data.get("event_type") == "run_completed":
            result = (data.get("payload") or {}).get("result")
            if isinstance(result, dict):
                return result
    return None


def interrupt_info(events: list[tuple[str, dict]]) -> dict | None:
    for name, data in events:
        if name == "interrupt":
            return data
    for data in run_events(events):
        if data.get("event_type") == "run_interrupted":
            info = (data.get("payload") or {}).get("interrupt")
            if isinstance(info, dict):
                return info
    return None


def normalized_event_types(events: list[tuple[str, dict]]) -> list[str]:
    """把两套协议归一成一组可断言的语义名。"""
    out: list[str] = []
    for name, data in events:
        if name == "run_event":
            et = str(data.get("event_type") or "")
            out.append(
                {
                    "run_completed": "done",
                    "run_interrupted": "interrupt",
                    "run_failed": "error",
                    "agent_started": "trace",
                    "agent_completed": "trace",
                    "stage_started": "trace",
                    "route_decision": "trace",
                }.get(et, "event")
            )
        else:
            out.append(name)
    return out
