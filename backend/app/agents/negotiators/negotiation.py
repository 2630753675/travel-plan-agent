"""协商编排：预算官提议 → 体验官表态 → 调解者裁决。

一次协商固定三轮发言，不做多轮辩论：
在"预算 vs 体验"这个二维冲突上，再加轮次不会产生新信息，
只会增加成本，并让结论更难追溯。
"""

from __future__ import annotations

from ...llm import LLMClient
from ...models import Itinerary, POI, TripBrief
from . import budget_officer, experience_officer, mediator
from .protocol import NegotiationLog
from .tools import poi_irreplaceability


async def negotiate(
    brief: TripBrief,
    itinerary: Itinerary,
    candidates: list[POI],
    client: LLMClient,
) -> tuple[NegotiationLog, set[str], bool]:
    """返回 (轨迹, 要砍掉的 poi_id 集合, 是否换便宜酒店)。"""
    scores = poi_irreplaceability(candidates)
    poi_by_id = {p.id: p for p in candidates}

    log = NegotiationLog(triggered_by=f"总花费 {itinerary.total_cost:.0f} 元超出预算 {brief.budget_total:.0f} 元")

    proposal = await budget_officer.propose(brief, itinerary, client, round_no=1)
    log.rounds.append(proposal)

    proposed_ids = _proposed_ids(proposal, itinerary)
    if not proposed_ids:
        log.outcome = "no_change"
        log.summary = "预算官没有可执行的削减项（当天已无付费景点）"
        return log, set(), True

    objection = await experience_officer.critique(
        brief, itinerary, proposed_ids, scores, poi_by_id, client, round_no=1
    )
    log.rounds.append(objection)

    verdict, adjustment, outcome = mediator.rule(
        brief=brief,
        proposal=proposal,
        objection=objection,
        proposed_ids=proposed_ids,
        scores=scores,
        total_cost=itinerary.total_cost,
        budget_total=itinerary.budget_total,
        round_no=1,
        titles_map={p.id: p.name for p in candidates},
    )
    log.rounds.append(verdict)
    log.outcome = outcome  # type: ignore[assignment]
    log.adjustment = adjustment
    log.summary = verdict.claim

    return log, set(adjustment.drop_poi_ids), adjustment.use_cheaper_hotel


def _proposed_ids(proposal, itinerary: Itinerary) -> list[str]:
    """从预算官的 evidence 还原拟砍项目。

    以 itinerary 里的真实标题匹配，避免模型直接给出 id 造成编造。
    """
    text = " ".join(e.value for e in proposal.evidence)
    ids: list[str] = []
    for day in itinerary.days:
        for item in day.items:
            if item.type == "sight" and item.cost > 0 and item.ref_id and item.title in text:
                ids.append(item.ref_id)
    return ids
