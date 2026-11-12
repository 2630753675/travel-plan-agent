"""验收：协商轨迹（预算官 vs 体验官）。

覆盖：
A 超支时触发协商，产生三条发言（提议 / 表态 / 裁决）
B 每条主张都带证据，且证据来自工具计算
C 两个角色立场确实冲突（预算官提议砍，体验官至少有时反对）
D 协商结论落到 adjustment，被砍景点不出现在最终行程
E 轨迹可持久化并在 GET /api/plan/{id} 取回
F 中断时轨迹也一并下发，用户能据此判断

需要后端已在 127.0.0.1:8848 运行。
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sse_protocol import final_result, interrupt_info  # noqa: E402

BASE = "http://127.0.0.1:8848"
failures: list[str] = []

# 预算 6500：会超支但幅度可控，适合观察协商
BRIEF = {
    "origin": "上海",
    "destination": "成都",
    "start_date": "2026-11-20",
    "end_date": "2026-11-24",
    "travelers": 2,
    "budget_total": 6500,
    "companions": ["老人"],
    "preferences": ["人文"],
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


def post(path: str, body: dict, timeout: int = 300) -> tuple[int, str]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get(path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {}


def describe(neg: dict) -> None:
    print(f"  触发: {neg['triggered_by']}")
    for m in neg["rounds"]:
        print(f"  [{m['speaker']}/{m['stance']}] {m['claim']}")
        for e in m["evidence"]:
            print(f"       · {e['label']}: {e['value']}")
    print(f"  结论: {neg['outcome']} | 调整: {neg['adjustment']}")


def main() -> int:
    print("\n[1] 提交超支需求，观察是否协商")
    status, text = post("/api/plan", BRIEF)
    check("请求成功", status == 200, str(status))
    events = parse_sse(text)
    kinds = [e for e, _ in events]
    print(f"  事件: {kinds[-5:]}")

    print("\n[2] 轨迹结构")
    payload = final_result(events) or interrupt_info(events)
    check("拿到结果或中断", payload is not None)
    if payload is None:
        return 1

    neg = payload.get("negotiation")
    check("包含协商轨迹", neg is not None, "未产生协商")
    if not neg:
        return 1

    describe(neg)
    if neg["outcome"] == "no_change":
        # 当天已无可削减项：只有预算官一条发言，随后仍应交给用户
        check("至少一条发言", len(neg["rounds"]) >= 1, str(len(neg["rounds"])))
        check("结论为无需调整", neg["outcome"] == "no_change")
    else:
        check("有三条发言", len(neg["rounds"]) == 3, str(len(neg["rounds"])))
        speakers = [m["speaker"] for m in neg["rounds"]]
        check(
            "发言者顺序正确",
            speakers == ["budget_officer", "experience_officer", "mediator"],
            str(speakers),
        )
        check("预算官是提议", neg["rounds"][0]["stance"] == "propose", neg["rounds"][0]["stance"])
        check(
            "体验官表态有效",
            neg["rounds"][1]["stance"] in ("object", "accept"),
            neg["rounds"][1]["stance"],
        )
    check("调解者有结论", neg["outcome"] in ("adopted", "escalated", "no_change"), neg["outcome"])

    print("\n[3] 证据必须来自工具")
    for m in neg["rounds"]:
        check(f"{m['speaker']} 有证据", len(m["evidence"]) > 0, "无证据")
        check(f"{m['speaker']} 记录了工具调用", len(m["tool_calls"]) > 0, "无工具记录")
    budget_msg = neg["rounds"][0]
    labels = [e["label"] for e in budget_msg["evidence"]]
    check("预算官给出金额依据", any("超出预算" in x for x in labels), str(labels))
    exp_msg = neg["rounds"][1]
    labels2 = [e["label"] for e in exp_msg["evidence"]]
    check("体验官给出不可替代性", any("不可替代" in x for x in labels2), str(labels2))

    print("\n[4] 冲突是否真实存在")
    if len(neg["rounds"]) >= 2:
        stances = (neg["rounds"][0]["stance"], neg["rounds"][1]["stance"])
        check(
            "存在提议与回应的对立或让步",
            stances[0] == "propose" and stances[1] in ("object", "accept"),
            str(stances),
        )
    else:
        check("无进展路径下只有预算官发言", neg["outcome"] == "no_change", neg["outcome"])
    if neg["outcome"] == "escalated":
        check("升级时不给调整方案", not neg["adjustment"]["drop_poi_ids"], str(neg["adjustment"]))

    print("\n[5] 若已采纳，被砍景点不应出现；轨迹应可回放")
    if final_result(events) is not None:
        plan_id = payload["plan_id"]
        titles = [
            i["title"]
            for d in payload["itinerary"]["days"]
            for i in d["items"]
            if i["type"] == "sight"
        ]
        print(f"  最终景点: {titles}")
        if neg["outcome"] == "adopted" and neg["adjustment"]["drop_poi_ids"]:
            dropped = set(neg["adjustment"]["drop_poi_ids"])
            for d in payload["itinerary"]["days"]:
                for i in d["items"]:
                    if i.get("ref_id") in dropped:
                        check("被砍景点未出现", False, i["title"])
                        break
            else:
                check("被砍景点未出现", True)

        status, record = get(f"/api/plan/{plan_id}")
        check("可从存储回放", status == 200, str(status))
        check("轨迹已持久化", record.get("negotiation") is not None)
    else:
        print("  本次被中断，检查中断载荷是否带轨迹")
        check("中断载荷含轨迹", payload.get("negotiation") is not None)

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  协商轨迹验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
