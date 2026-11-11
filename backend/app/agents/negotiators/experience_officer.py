"""体验官：只对"值不值"负责。

它的立场与预算官相反：反对为了省钱砍掉难以替代的安排。
它的判断依据是工具算出的不可替代性，不是主观感觉。
"""

from __future__ import annotations

import json

from ...llm import LLMClient, LLMError
from ...models import Itinerary, POI, TripBrief
from .protocol import Evidence, Message
from .tools import day_experience

SYSTEM = "你是行程体验官，只关心这趟行程是否值得。只输出 JSON。"

# 不可替代性高于此值的安排，默认应反对砍掉
HIGH_VALUE = 0.6


def _affected(itinerary: Itinerary, poi_ids: list[str], scores: dict[str, float], poi_by_id: dict[str, POI]) -> list[dict]:
    out = []
    for day in itinerary.days:
        exp = day_experience(day, scores, poi_by_id)
        for s in exp["sights"]:
            if s["id"] in poi_ids:
                out.append(
                    {
                        "id": s["id"],
                        "title": s["title"],
                        "irreplaceability": s["irreplaceability"],
                        "tags": s["tags"],
                        "date": exp["date"],
                        "day_experience_after_removal": round(
                            max(
                                [x["irreplaceability"] for x in exp["sights"] if x["id"] not in poi_ids]
                                or [0.0]
                            ),
                            3,
                        ),
                    }
                )
    return out


async def critique(
    brief: TripBrief,
    itinerary: Itinerary,
    proposal_ids: list[str],
    scores: dict[str, float],
    poi_by_id: dict[str, POI],
    client: LLMClient,
    round_no: int = 1,
) -> Message:
    affected = _affected(itinerary, proposal_ids, scores, poi_by_id)
    if not affected:
        return Message(
            round=round_no,
            speaker="experience_officer",
            stance="accept",
            claim="没有涉及可砍的景点，我不反对",
            evidence=[],
            tool_calls=["day_experience"],
        )

    total_irreplaceable = sum(p["irreplaceability"] for p in affected)
    loss_ratio = round(total_irreplaceable / max(1e-6, sum(scores.values())), 3)

    prompt = f"""预算官想砍掉这些安排，你要判断值不值得保留。

被砍的景点（irreplaceability 越高越难被替代，0-1）：
{json.dumps([{ "title": p["title"], "irreplaceability": p["irreplaceability"], "tags": p["tags"], "date": p["date"] } for p in affected], ensure_ascii=False)}

砍掉后当天剩下的最好安排，其不可替代性为：
{json.dumps([{ "date": p["date"], "after": p["day_experience_after_removal"] } for p in affected], ensure_ascii=False)}

用户偏好：{brief.preferences or "无"}
用户同行人群：{brief.companions}

判断规则：
- 有不可替代性 >= {HIGH_VALUE} 的景点被砍 → 通常应反对
- 砍掉后当天没有任何景点 → 必须反对
- 全是低不可替代性的普通景点 → 可以接受

输出 JSON：
{{"verdict":"accept"|"object","worst_id":"p0x","claim":"不超过 40 字，说明你保留或放手的理由"}}"""

    try:
        raw = await client.chat_json(SYSTEM, prompt)
        verdict = str(raw.get("verdict", "")).lower()
        claim = str(raw.get("claim") or "").strip()
        if verdict not in ("accept", "object"):
            raise LLMError("verdict 非法")
    except LLMError:
        worst = max(affected, key=lambda p: p["irreplaceability"])
        verdict = "object" if worst["irreplaceability"] >= HIGH_VALUE else "accept"
        claim = (
            f"{worst['title']} 不可替代性 {worst['irreplaceability']}，砍掉后这天会很空"
            if verdict == "object"
            else "这些安排可替代性较高，可以让步"
        )
        worst_id = worst["id"]
        return Message(
            round=round_no,
            speaker="experience_officer",
            stance=verdict,  # type: ignore[arg-type]
            claim=f"（规则判定）{claim}",
            evidence=[
                Evidence(label="被砍项目不可替代性", value=f"{total_irreplaceable:.2f}"),
                Evidence(label="占全程体验比重", value=f"{loss_ratio:.0%}"),
                Evidence(label="最不可替代", value=f"{worst['title']}（{worst['irreplaceability']}）"),
            ],
            tool_calls=["day_experience", "poi_irreplaceability"],
        )

    worst = max(affected, key=lambda p: p["irreplaceability"])
    stance = "object" if verdict == "object" else "accept"
    return Message(
        round=round_no,
        speaker="experience_officer",
        stance=stance,  # type: ignore[arg-type]
        claim=claim or ("这些安排值得保留" if stance == "object" else "可以让步"),
        evidence=[
            Evidence(label="被砍项目不可替代性", value=f"{total_irreplaceable:.2f}"),
            Evidence(label="占全程体验比重", value=f"{loss_ratio:.0%}"),
            Evidence(label="最不可替代", value=f"{worst['title']}（{worst['irreplaceability']}）"),
        ],
        tool_calls=["day_experience", "poi_irreplaceability"],
    )
