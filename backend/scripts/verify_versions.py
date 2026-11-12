"""P3 验收：行程版本与回滚。

核心断言不是"接口能返回东西"，而是两条不变量：
  A 重排某一天后，其余各天的 JSON 必须逐字节一致（不能让"没动的地方"悄悄变样）
  B 回滚到 v1 后，行程必须与 v1 完全一致，且回滚本身也留了一份历史

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

from sse_protocol import final_result, interrupt_info, parse_sse  # noqa: E402

BASE = "http://127.0.0.1:8848"
failures: list[str] = []

BRIEF = {
    "origin": "上海",
    "destination": "成都",
    "start_date": "2026-11-20",
    "end_date": "2026-11-24",
    "travelers": 2,
    "budget_total": 8000,
    "companions": ["老人"],
    "preferences": ["人文"],
    "pace": "适中",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name} {'' if ok else detail}")
    if not ok:
        failures.append(name)


def request(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def day_map(itinerary: dict) -> dict[str, str]:
    """把每天序列化成可比字符串，用于严格相等判断。"""
    return {d["date"]: json.dumps(d, ensure_ascii=False, sort_keys=True) for d in itinerary["days"]}


def main() -> int:
    print("\n[1] 生成初始行程")
    status, text = request("POST", "/api/plan", BRIEF)
    check("规划请求成功", status == 200, str(status))
    events = parse_sse(text)
    done = final_result(events) or interrupt_info(events)
    if done is None:
        print("  FAIL  既没有完成也没有中断")
        return 1
    plan_id = done["plan_id"]
    v1_itinerary = done["itinerary"]
    print(f"  plan_id={plan_id} 天数={len(v1_itinerary['days'])}")

    print("\n[2] 初始版本已记录")
    status, body = request("GET", f"/api/plan/{plan_id}/versions")
    check("版本列表可用", status == 200, str(status))
    versions = json.loads(body)["versions"]
    print(f"  版本: {[(v['version'], v['source']) for v in versions]}")
    check("至少有 v1", len(versions) >= 1 and versions[0]["version"] == 1, str(len(versions)))
    check("v1 来源是首次生成", versions[0]["source"] == "initial", versions[0]["source"])

    print("\n[3] 重排某一天")
    target = v1_itinerary["days"][2]["date"]
    status, body = request(
        "POST", f"/api/plan/{plan_id}/refine", {"date": target, "feedback": "这天太赶，想轻松点"}
    )
    check("重排成功", status == 200, str(status))
    refined = json.loads(body)
    v2_itinerary = refined["itinerary"]

    print("\n[4] 未编辑的日期必须逐字节一致")
    before = day_map(v1_itinerary)
    after = day_map(v2_itinerary)
    check("天数不变", len(before) == len(after), f"{len(before)} vs {len(after)}")
    untouched = [d for d in before if d != target]
    drifted = [d for d in untouched if before[d] != after.get(d)]
    print(f"  未编辑日期 {len(untouched)} 天，发生变化的 {len(drifted)} 天")
    check("未编辑日期完全未变", not drifted, str(drifted))
    check("目标日确实变了", before[target] != after.get(target))

    print("\n[5] 重排后多出一个版本")
    status, body = request("GET", f"/api/plan/{plan_id}/versions")
    versions2 = json.loads(body)
    print(f"  当前 v{versions2['current']}，来源 {[v['source'] for v in versions2['versions']]}")
    check("版本数增加", len(versions2["versions"]) > len(versions), str(len(versions2["versions"])))
    check("新版本来源是重排", versions2["versions"][-1]["source"] == "refine", versions2["versions"][-1]["source"])
    check("新版本带了备注", bool(versions2["versions"][-1]["note"]), versions2["versions"][-1]["note"])

    print("\n[6] 能单独取回历史版本")
    status, body = request("GET", f"/api/plan/{plan_id}/versions/1")
    check("取回 v1 成功", status == 200, str(status))
    v1_again = json.loads(body)["itinerary"]
    check("v1 内容与原行程一致", day_map(v1_again) == before)

    print("\n[7] 回滚到 v1")
    status, body = request("POST", f"/api/plan/{plan_id}/rollback", {"version": 1})
    check("回滚成功", status == 200, str(status))
    rolled = json.loads(body)
    print(f"  回滚后是 v{rolled['version']}，回到 v{rolled['rolled_back_to']}")
    check("回滚结果等于 v1", day_map(rolled["itinerary"]) == before)
    check("回滚没有删历史", rolled["version"] > versions2["current"])

    print("\n[8] 当前结果已换成回滚后的内容")
    status, body = request("GET", f"/api/plan/{plan_id}")
    check("读取规划成功", status == 200, str(status))
    record = json.loads(body)
    check("当前行程等于 v1", day_map(record["itinerary"]) == before)
    check("当前版本号已推进", record.get("version") == rolled["version"], str(record.get("version")))

    status, body = request("GET", f"/api/plan/{plan_id}/versions")
    last = json.loads(body)["versions"][-1]
    check("回滚也留下历史", last["source"] == "rollback", last["source"])
    check("回滚备注说明了目标版本", "v1" in last["note"], last["note"])

    print("\n[9] 不存在的版本")
    status, _ = request("GET", f"/api/plan/{plan_id}/versions/999")
    check("取不到时返回 404", status == 404, str(status))
    status, _ = request("POST", f"/api/plan/{plan_id}/rollback", {"version": 999})
    check("回滚不存在的版本返回 404", status == 404, str(status))

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  行程版本与回滚验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
