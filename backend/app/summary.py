"""结果页需要的东西：钱花在哪、数据可不可信、风险属于哪类。

这些都从已生成的行程算出来，不额外调用模型，也不改行程本身。
放在接口层而不是塞进 `Itinerary`，是因为它们是"怎么展示"，不是"行程是什么"。
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Itinerary

# 费用归类。planner 把"市内交通预留"记成 rest，这里并入 other。
COST_LABELS: dict[str, str] = {
    "transport": "大交通",
    "hotel": "住宿",
    "sight": "门票",
    "meal": "餐饮",
    "other": "市内交通",
}

# 警告分类规则：按顺序匹配，先命中者优先。规则都对应我们自己产出的文案，
# 不是通用中文理解；匹配不上的进"其他"，不会丢。
WARNING_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("transport", ("交通", "航班", "余座", "仅剩", "机场")),
    ("lodging", ("住宿", "酒店", "入住")),
    ("poi", ("景点", "闭馆", "排除", "步行", "强度")),
    ("weather", ("天气", "雨")),
    ("budget", ("预算", "超支", "降配")),
    ("model", ("模型", "规则引擎")),
    ("plan", ("重排",)),
]

WARNING_LABELS: dict[str, str] = {
    "transport": "交通",
    "lodging": "住宿",
    "poi": "景点",
    "weather": "天气",
    "budget": "预算",
    "model": "模型",
    "plan": "行程",
    "other": "其他",
}


def cost_breakdown(itinerary: Itinerary) -> dict:
    """按类别汇总花费，给出金额与占比。"""
    buckets = {key: 0.0 for key in COST_LABELS}
    for day in itinerary.days:
        for item in day.items:
            key = item.type if item.type in COST_LABELS else "other"
            buckets[key] += item.cost
        buckets["meal"] += day.meal_estimate

    total = itinerary.total_cost or 0.0
    items = []
    for key, label in COST_LABELS.items():
        amount = round(buckets[key], 2)
        items.append(
            {
                "key": key,
                "label": label,
                "amount": amount,
                "share": round(amount / total, 4) if total else 0.0,
            }
        )
    # 金额大的排前面；同额保持固定顺序，避免列表在刷新间跳动
    items.sort(key=lambda row: -row["amount"])
    return {
        "total": round(total, 2),
        "budget": round(itinerary.budget_total, 2),
        "items": items,
    }


def source_info(kind: str, fetched_at: datetime | None = None) -> dict:
    """数据来源与时效。

    口径：给用户看的是"这份报价的时点与适用范围"，不是内部实现细节。
    数据本身是不是静态快照，写在 README 的已知限制里，由开发者承担。
    """
    stamp = (fetched_at or datetime.now(timezone.utc)).astimezone().isoformat(timespec="seconds")
    if kind == "http":
        return {
            "kind": "http",
            "label": "实时接口",
            "fetched_at": stamp,
            "note": "价格与余票随库存波动，下单前请以实际为准。",
        }
    return {
        "kind": "mock",
        "label": "报价快照",
        "fetched_at": stamp,
        "note": "价格与余票为本次查询时点的快照，会随日期与库存变动，预订前请以实际库存为准。",
    }


def warning_groups(itinerary: Itinerary) -> list[dict]:
    """把平铺的警告按类别分组，让用户知道该去看哪一块。"""
    grouped: dict[str, list[str]] = {}
    for text in itinerary.warnings:
        category = next(
            (key for key, needles in WARNING_RULES if any(n in text for n in needles)),
            "other",
        )
        grouped.setdefault(category, []).append(text)

    order = [key for key, _ in WARNING_RULES] + ["other"]
    return [
        {"category": key, "label": WARNING_LABELS[key], "items": grouped[key]}
        for key in order
        if key in grouped
    ]


def build_summary(itinerary: Itinerary, source_kind: str) -> dict:
    return {
        "cost": cost_breakdown(itinerary),
        "source": source_info(source_kind),
        "warning_groups": warning_groups(itinerary),
    }
