"""预算官：只对钱负责。

它的立场是「把钱压回预算内」，为此愿意砍掉景点、换便宜酒店。
它没有能力判断一个景点值不值得去——那是体验官的事，它只需要提出自己的主张。

节省金额一律由代码计算，模型只负责挑目标，避免它编造数字。
"""

from __future__ import annotations

import json

from ...llm import LLMClient, LLMError
from ...models import Itinerary, TripBrief
from .protocol import Evidence, Message
from .tools import day_cost_breakdown

SYSTEM = "你是行程预算官，只关心把花费压回预算内。只输出 JSON。"


def _cuttable(itinerary: Itinerary) -> list[dict]:
    """列出所有可以砍掉的付费景点，节省额由代码算。"""
    out = []
    for day in itinerary.days:
        for item in day.items:
            if item.type != "sight" or item.cost <= 0:
                continue
            out.append(
                {
                    "date": str(day.date),
                    "poi_id": item.ref_id,
                    "title": item.title,
                    "saving": round(item.cost, 2),
                }
            )
    # 越省钱越优先考虑，同额时保底保留高价项（通常也是重点项目），由体验官去争
    out.sort(key=lambda x: x["saving"])
    return out


def _hotel_option(itinerary: Itinerary) -> dict | None:
    hotels = [i for d in itinerary.days for i in d.items if i.type == "hotel"]
    if not hotels:
        return None
    per_night = hotels[0].cost
    nights = len(hotels)
    return {
        "current_per_night": round(per_night, 2),
        "nights": nights,
        "note": "还有更便宜的市内酒店可选，具体差价要换过才知道",
    }


async def propose(
    brief: TripBrief,
    itinerary: Itinerary,
    client: LLMClient,
    round_no: int = 1,
    user_hint: str = "",
    rejected: list[str] | None = None,
) -> Message:
    """提出削减方案。

    round_no > 1 时说明上一轮谈崩了、用户已给出倾向：
    - user_hint 决定这次是继续省钱、还是干脆放弃省钱
    - rejected 是上一轮被体验官否掉的项目，不能原样再提
    """
    over = itinerary.total_cost - brief.budget_total
    cuttable = _cuttable(itinerary)
    rejected = rejected or []
    if rejected:
        cuttable = [c for c in cuttable if c["title"] not in rejected]
    hotel = _hotel_option(itinerary)
    day_costs = [day_cost_breakdown(d) for d in itinerary.days]

    round_context = ""
    if round_no > 1:
        round_context = f"""
这是第 {round_no} 轮协商。上一轮你提的方案被体验官驳回，用户已经表态：
「{user_hint}」

如果用户倾向"保住体验"，就不要砍不可替代性高的景点，优先靠换酒店省钱；
如果用户倾向"控制预算"，可以坚持砍，但要说清牺牲了什么。
"""

    prompt = f"""行程超预算了，你要提出削减方案。

总花费 {itinerary.total_cost:.0f} 元，预算 {brief.budget_total:.0f} 元，超出 {over:.0f} 元。
同行人群：{brief.companions}，用户偏好：{brief.preferences or "无"}
{round_context}
每天花费：
{json.dumps([{"date": d["date"], "total": d["total"], "by_type": d["by_type"]} for d in day_costs], ensure_ascii=False)}

可以砍掉的景点（saving 是砍掉后省的总额）：
{json.dumps(cuttable, ensure_ascii=False)}

酒店情况：{json.dumps(hotel, ensure_ascii=False) if hotel else "无"}

请选择要砍掉的景点（0-3 个）以及是否换便宜酒店，让总节省尽量覆盖超支。
不要砍掉最后一个景点导致某天完全没安排。

输出 JSON：
{{"poi_ids":["p0x"],"use_cheaper_hotel":true,"claim":"不超过 50 字，说明你的取舍逻辑"}}"""

    try:
        raw = await client.chat_json(SYSTEM, prompt)
    except LLMError:
        # 模型不可用时退化为"砍最便宜的几个"，保证功能不中断
        fallback = [c["poi_id"] for c in cuttable[:2] if c["poi_id"]]
        return Message(
            round=round_no,
            speaker="budget_officer",
            stance="propose",
            claim=f"模型不可用，按最小损失原则建议去掉 {len(fallback)} 个低价景点，并换便宜酒店",
            evidence=[
                Evidence(label="超出预算", value=f"{over:.0f} 元"),
                Evidence(label="可节省", value=f"{sum(c['saving'] for c in cuttable[:2]):.0f} 元"),
            ],
            tool_calls=["day_cost_breakdown", "cuttable_scan"],
        )

    valid_ids = {c["poi_id"] for c in cuttable if c["poi_id"]}
    picked = [str(i) for i in raw.get("poi_ids", []) if str(i) in valid_ids][:3]
    saving = sum(c["saving"] for c in cuttable if c["poi_id"] in picked)
    use_hotel = bool(raw.get("use_cheaper_hotel"))
    claim = str(raw.get("claim") or "").strip() or "建议削减部分景点"

    evidence = [
        Evidence(label="超出预算", value=f"{over:.0f} 元"),
        Evidence(label="砍景点可省", value=f"{saving:.0f} 元"),
    ]
    if use_hotel:
        # 具体能省多少要换过才知道，这里不给编出来的数字
        evidence.append(Evidence(label="换酒店", value="改为更便宜的市内酒店"))
    if picked:
        titles = [c["title"] for c in cuttable if c["poi_id"] in picked]
        evidence.append(Evidence(label="拟砍掉", value="、".join(titles)))

    return Message(
        round=round_no,
        speaker="budget_officer",
        stance="propose",
        claim=claim,
        evidence=evidence,
        tool_calls=["day_cost_breakdown", "cuttable_scan", "hotel_option_scan"],
    )
