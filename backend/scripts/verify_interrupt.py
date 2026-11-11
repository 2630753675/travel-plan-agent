"""验收：预算超支时的人工介入（interrupt / resume）。

覆盖：
A 超支 > 20% 时暂停，抛出 interrupt 而不是直接给结果
B 选择 accept → 产出行程并注明"已接受超支"
C 选择 downgrade → 回退重排，费用下降
D 选择 abort → 仍然返回行程但标注"已取消"
E 没有待确认中断时 resume 返回 400

需要后端已在 127.0.0.1:8848 运行。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8848"
failures: list[str] = []

LOW_BUDGET = {
    "origin": "上海",
    "destination": "成都",
    "start_date": "2026-11-20",
    "end_date": "2026-11-24",
    "travelers": 2,
    "budget_total": 3000,
    "companions": ["无"],
    "preferences": [],
    "pace": "适中",
}


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
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


def plan(brief: dict) -> list[tuple[str, dict]]:
    req = urllib.request.Request(
        BASE + "/api/plan",
        data=json.dumps(brief).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=240) as r:
        return parse_sse(r.read().decode())


def resume(plan_id: str, decision: str) -> tuple[int, list[tuple[str, dict]]]:
    req = urllib.request.Request(
        BASE + f"/api/plan/{plan_id}/resume",
        data=json.dumps({"decision": decision}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return r.status, parse_sse(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, []


def run_case(label: str, decision: str) -> tuple[dict, dict] | None:
    print(f"\n[{label}]")
    events = plan(LOW_BUDGET)
    kinds = [e for e, _ in events]
    print(f"  事件: {kinds[-4:]}")
    check("被中断而不是直接完成", "interrupt" in kinds and "done" not in kinds, str(kinds[-3:]))
    if "interrupt" not in kinds:
        return None

    info = next(d for e, d in events if e == "interrupt")
    print(f"  超支 {info['over']} 元 / 预算 {info['budget']}（{info['ratio']:.0%}）")
    check("中断类型正确", info.get("type") == "budget_overrun", str(info.get("type")))
    check("带 plan_id", bool(info.get("plan_id")), str(info.get("plan_id")))
    check("给出三个选项", len(info.get("options", [])) == 3, str(len(info.get("options", []))))
    check("超出比例 > 20%", info.get("ratio", 0) > 0.2, str(info.get("ratio")))

    status, resumed = resume(info["plan_id"], decision)
    check("恢复返回 200", status == 200, str(status))
    done = next((d for e, d in resumed if e == "done"), None)
    check("恢复后产出结果", done is not None)
    if done:
        it = done["itinerary"]
        print(f"  最终花费 {it['total_cost']:.0f}，决策={done.get('decision')}")
        print(f"  warnings: {[w for w in it['warnings'] if '接受' in w or '降配' in w or '取消' in w]}")
        check("决策被记录", done.get("decision") == decision, str(done.get("decision")))
    return info, done or {}


def main() -> int:
    a = run_case("A/B 超支中断 + 接受", "accept")
    if a and a[1]:
        warns = a[1]["itinerary"]["warnings"]
        check("接受后注明超支", any("接受超出预算" in w for w in warns), str(warns[-2:]))
        # 已完成的规划再 resume 应报错
        status, _ = resume(a[0]["plan_id"], "accept")
        check("无待确认中断时 resume 返回 400", status == 400, str(status))

    b = run_case("C 超支中断 + 降配", "downgrade")
    if b and b[1]:
        cost = b[1]["itinerary"]["total_cost"]
        check("降配后费用不高于初版", cost <= b[0]["total"] + 1, f"{cost} vs {b[0]['total']}")

    c = run_case("D 超支中断 + 取消", "abort")
    if c and c[1]:
        warns = c[1]["itinerary"]["warnings"]
        check("取消后仍返回行程并标注", any("已选择取消" in w for w in warns), str(warns[-2:]))

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  人工介入（interrupt/resume）验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
