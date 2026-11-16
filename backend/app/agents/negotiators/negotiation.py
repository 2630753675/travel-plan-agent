"""预算与体验评审的可选 LLM 协商协议。

查询天气、航班、酒店、景点是工具；预算官和体验官只有在严重预算冲突时才作为
独立评审角色参与。调解结果是规则 Workflow 输出，不把调解者伪装成自主 Agent。
"""

from __future__ import annotations

from ...llm import LLMClient
from ...models import Itinerary, POI, TripBrief
from . import budget_officer, experience_officer, mediator
from .protocol import Adjustment, Evidence, Message, NegotiationLog
from .tools import poi_irreplaceability


async def negotiate(
    brief: TripBrief,
    itinerary: Itinerary,
    candidates: list[POI],
    client: LLMClient,
    round_no: int = 1,
    user_hint: str = "",
    history: NegotiationLog | None = None,
) -> tuple[NegotiationLog, set[str], bool]:
    """返回 (轨迹, 要砍掉的 poi_id 集合, 是否换便宜酒店)。

    history 存在时在其后追加，保证轨迹是完整的多轮对话。
    """
    scores = poi_irreplaceability(candidates)
    poi_by_id = {p.id: p for p in candidates}

    log = history or NegotiationLog(
        triggered_by=f"总花费 {itinerary.total_cost:.0f} 元超出预算 {brief.budget_total:.0f} 元"
    )
    rejected = _rejected(log)

    proposal = await budget_officer.propose(
        brief, itinerary, client, round_no=round_no, user_hint=user_hint, rejected=rejected
    )
    log.rounds.append(proposal)

    proposed_ids = _proposed_ids(proposal, itinerary)
    if not proposed_ids:
        # 没有可砍的景点，但如果预算官提了换酒店，那仍是一个可执行方案，
        # 不能因为"没景点可砍"就把整条协商判成无进展。
        wants_hotel = any(e.label == "换酒店" for e in proposal.evidence)
        if wants_hotel:
            log.outcome = "adopted"
            log.adjustment = Adjustment(drop_poi_ids=[], use_cheaper_hotel=True)
            log.summary = "没有可砍的景点，改为换便宜酒店压低支出"
            log.rounds.append(
                Message(
                    round=round_no,
                    speaker="mediator",
                    stance="adopt",
                    claim=log.summary,
                    evidence=[
                        Evidence(label="裁决依据", value="换酒店不牺牲任何景点，是当前唯一无损方案"),
                        Evidence(label="仍需注意", value="换后可能依旧超支，需重新校验"),
                    ],
                )
            )
            return log, set(), True

        log.outcome = "no_change"
        log.summary = "预算官没有可执行的削减项（当天已无付费景点，也没有更便宜的酒店）"
        return log, set(), False

    objection = await experience_officer.critique(
        brief, itinerary, proposed_ids, scores, poi_by_id, client, round_no=round_no
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
        round_no=round_no,
        titles_map={p.id: p.name for p in candidates},
        final_round=round_no >= 2,
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


def _rejected(log: NegotiationLog) -> list[str]:
    """上一轮预算官提过、但没被采纳的景点标题，下一轮不要原样再提。"""
    for m in reversed(log.rounds):
        if m.speaker == "budget_officer":
            for e in m.evidence:
                if e.label == "拟砍掉" and e.value:
                    return [t for t in e.value.split("、") if t]
            return []
    return []
