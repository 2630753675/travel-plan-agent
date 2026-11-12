"""P3 验收：服务重启后，未确认的中断仍能恢复。

分两阶段执行，中间必须真的重启后端：

    python scripts/verify_restart.py prepare     # 触发中断，记下 plan_id
    <重启 uvicorn>
    python scripts/verify_restart.py resume      # 断言仍能恢复并出结果

只靠进程内 checkpointer 时，第二阶段会拿到 400「该规划没有待确认的中断」。
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sse_protocol import final_result, interrupt_info, parse_sse  # noqa: E402

BASE = "http://127.0.0.1:8848"
STATE_FILE = ROOT / "data" / ".restart_probe.json"
failures: list[str] = []

# 预算 3000 一定超支 >20%，会停在"接受 / 降配 / 取消"
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


def prepare() -> int:
    print("\n[1] 触发一次中断")
    status, text = post("/api/plan", BRIEF)
    check("规划请求成功", status == 200, str(status))
    info = interrupt_info(parse_sse(text))
    check("确实停在待确认状态", info is not None)
    if info is None:
        return 1

    plan_id = info["plan_id"]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"plan_id": plan_id, "type": info.get("type")}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  plan_id={plan_id} 类型={info.get('type')}")
    print(f"  已记录到 {STATE_FILE.name}，现在重启后端再跑 resume")

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  阶段一完成（中断已产生）")
    return 0


def resume() -> int:
    print("\n[2] 重启后恢复")
    if not STATE_FILE.is_file():
        print(f"  FAIL  找不到 {STATE_FILE.name}，请先跑 prepare")
        return 1
    saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    plan_id = saved["plan_id"]
    print(f"  续用 plan_id={plan_id}")

    status, text = post(f"/api/plan/{plan_id}/resume", {"decision": "accept"})
    check("恢复返回 200（不是 400 没有中断）", status == 200, f"{status} {text[:160]}")
    if status != 200:
        return 1

    events = parse_sse(text)
    done = final_result(events)
    check("恢复后产出了完整行程", done is not None)
    if done:
        print(f"  天数={len(done['itinerary']['days'])} 总花费={done['itinerary']['total_cost']:.0f} 决策={done.get('decision')}")
        check("行程仍为 5 天", len(done["itinerary"]["days"]) == 5)
        check("决策被记录", done.get("decision") == "accept", str(done.get("decision")))
        check(
            "恢复了中断前的事件链",
            any(e.get("event_type") == "run_resumed" for e in
                [d for n, d in events if n == "run_event"]),
        )

    STATE_FILE.unlink(missing_ok=True)
    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  重启后仍可恢复未确认的中断")
    return 0


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    if phase == "prepare":
        return prepare()
    if phase == "resume":
        return resume()
    print("用法：verify_restart.py prepare | resume")
    return 2


if __name__ == "__main__":
    sys.exit(main())
