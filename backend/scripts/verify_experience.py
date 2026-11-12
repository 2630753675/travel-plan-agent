"""P4 验收：结果页能不能看懂钱、看懂数据、看懂风险。

三层断言：
  A 成本拆解必须与总花费一致（金额全部分配完，占比加起来是 1）
  B 数据来源必须说清"这是模拟数据"，不能让人误当真实可订
  C 警告能按交通/住宿/景点/天气/预算/模型/行程归类，且分类不丢条目

需要后端已在 127.0.0.1:8848 运行（[6][7] 两组走接口，其余纯单元）。
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sse_protocol import final_result, interrupt_info, parse_sse  # noqa: E402

from app import summary  # noqa: E402
from app.models import DayPlan, Item, Itinerary  # noqa: E402

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


def _item(kind: str, cost: float, title: str = "x") -> Item:
    from datetime import time

    return Item(
        slot="上午", start_time=time(9, 0), end_time=time(10, 0),
        type=kind, title=title, cost=cost, duration_min=60,
    )


def sample_itinerary() -> Itinerary:
    day = DayPlan(
        date=__import__("datetime").date(2026, 11, 20),
        items=[
            _item("transport", 800),
            _item("hotel", 520),
            _item("sight", 90),
            _item("meal", 120),
            _item("rest", 100),
        ],
        day_cost=1630,
    )
    return Itinerary(days=[day], total_cost=1630, budget_total=2000)


def main() -> int:
    print("\n[1] 成本拆解与总花费一致")
    itinerary = sample_itinerary()
    cost = summary.cost_breakdown(itinerary)
    amounts = {row["key"]: row["amount"] for row in cost["items"]}
    print(f"  拆解: {amounts}")
    check("总额等于行程总花费", cost["total"] == itinerary.total_cost, str(cost["total"]))
    check("各类金额之和等于总额", round(sum(amounts.values()), 2) == itinerary.total_cost, str(sum(amounts.values())))
    check("五类都有固定标签", len(cost["items"]) == 5, str(len(cost["items"])))
    # 占比是展示用的四位小数，合计会有 1e-4 量级的舍入误差，不该要求精确等于 1
    check("占比合计约等于 1", abs(sum(r["share"] for r in cost["items"]) - 1.0) < 1e-3, str(sum(r["share"] for r in cost["items"])))
    check("按金额从大到小排", [r["amount"] for r in cost["items"]] == sorted((r["amount"] for r in cost["items"]), reverse=True))
    check("交通最大且是 800", amounts["transport"] == 800.0, str(amounts.get("transport")))

    empty = Itinerary(days=[], total_cost=0, budget_total=1000)
    check("空行程不会除以零", all(r["share"] == 0 for r in summary.cost_breakdown(empty)["items"]))

    print("\n[2] 数据时效必须写清楚")
    mock = summary.source_info("mock")
    print(f"  {mock['label']}：{mock['note']}")
    check("标注了报价时点口径", "快照" in mock["label"], mock["label"])
    check("说明价格会变动", "变动" in mock["note"], mock["note"])
    check("提示预订前以实际库存为准", "实际库存" in mock["note"], mock["note"])
    check("带采集时间", bool(mock["fetched_at"]), mock["fetched_at"])
    real = summary.source_info("http")
    check("实时接口用不同标签", real["label"] == "实时接口", real["label"])
    check("实时接口同样提示以实际为准", "实际" in real["note"], real["note"])

    print("\n[3] 警告按类别分组")
    noisy = Itinerary(
        days=[],
        total_cost=0,
        budget_total=1000,
        warnings=[
            "MU5401 仅剩 1 座，不足 2 人",
            "推荐酒店离市中心较远",
            "已按同行人群排除：都江堰景区",
            "当天有雨，户外项目改期",
            "总花费超出预算 300 元",
            "模型选择不可用，本次行程由规则引擎生成",
            "已根据校验反馈重排 2 次",
            "一句没人归类的话",
        ],
    )
    groups = summary.warning_groups(noisy)
    mapping = {g["category"]: g["items"] for g in groups}
    print(f"  分组: {[(g['category'], len(g['items'])) for g in groups]}")
    check("交通类命中", "MU5401" in " ".join(mapping.get("transport", [])))
    check("住宿类命中", "酒店" in " ".join(mapping.get("lodging", [])))
    check("景点类命中", "都江堰" in " ".join(mapping.get("poi", [])))
    check("天气类命中", "雨" in " ".join(mapping.get("weather", [])))
    check("预算类命中", "超出预算" in " ".join(mapping.get("budget", [])))
    check("模型类命中", "规则引擎" in " ".join(mapping.get("model", [])))
    check("重排归到行程", "重排" in " ".join(mapping.get("plan", [])))
    check("未命中的不丢，进其他", "没人归类" in " ".join(mapping.get("other", [])))
    total_items = sum(len(g["items"]) for g in groups)
    check("条目不增不减", total_items == len(noisy.warnings), f"{total_items} vs {len(noisy.warnings)}")
    check("空警告没有分组", summary.warning_groups(Itinerary(days=[], total_cost=0, budget_total=1)) == [])

    print("\n[4] 组装后的摘要是完整契约")
    payload = summary.build_summary(itinerary, "mock")
    check("含 cost", "cost" in payload)
    check("含 source", "source" in payload)
    check("含 warning_groups", "warning_groups" in payload)

    print("\n[5] 接口把摘要一并返回")
    req = urllib.request.Request(
        BASE + "/api/plan",
        data=json.dumps(BRIEF).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=240) as r:
        events = parse_sse(r.read().decode())
    done = final_result(events) or interrupt_info(events)
    check("拿到结果", done is not None)
    if done is None:
        return 1
    plan_id = done["plan_id"]

    if final_result(events) is None:
        print("  本次停在中断，跳过结果页断言（summary 只在完成时给）")
    else:
        s = done.get("summary")
        check("完成响应带 summary", isinstance(s, dict), str(type(s)))
        if isinstance(s, dict):
            check("金额与行程一致", s["cost"]["total"] == done["itinerary"]["total_cost"], str(s["cost"]["total"]))
            check("带数据时效说明", bool(s["source"]["note"]), str(s["source"]))

    print("\n[6] 读取已保存的规划也带摘要")
    with urllib.request.urlopen(f"{BASE}/api/plan/{plan_id}", timeout=60) as r:
        record = json.loads(r.read().decode())
    check("GET 返回 summary", isinstance(record.get("summary"), dict), str(type(record.get("summary"))))
    if isinstance(record.get("summary"), dict):
        check(
            "回读的金额与行程一致",
            record["summary"]["cost"]["total"] == record["itinerary"]["total_cost"],
            str(record["summary"]["cost"]["total"]),
        )

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  结果页信息验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
