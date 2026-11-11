"""协商裁决逻辑的单元验收（不依赖模型，可稳定复现）。

覆盖调解者的三条分支：一方让步 / 折中 / 僵持升级，以及最后一轮强制出方案。
另验证不可替代性评分与"被驳回项不再重提"的提取逻辑。
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.agents.negotiators import mediator
from app.agents.negotiators.negotiation import _rejected
from app.agents.negotiators.protocol import Evidence, Message, NegotiationLog
from app.agents.negotiators.tools import poi_irreplaceability
from app.models import POI, TripBrief

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def brief(**kw) -> TripBrief:
    base = dict(
        origin="上海",
        destination="成都",
        start_date=date(2026, 11, 20),
        end_date=date(2026, 11, 24),
        travelers=2,
        budget_total=6000,
        companions=["老人"],
        preferences=["人文"],
    )
    base.update(kw)
    return TripBrief(**base)


def poi(pid: str, name: str, category: str, **kw) -> POI:
    base = dict(
        id=pid,
        name=name,
        category=category,
        open_time="08:00",
        close_time="18:00",
        closed_days=[],
        duration_min=90,
        ticket_price=50,
        recommended_slot=["上午"],
        tags=[],
        lat=30.0,
        lng=104.0,
        indoor=False,
    )
    base.update(kw)
    return POI(**base)


def proposal_msg(titles: list[str]) -> Message:
    return Message(
        round=1,
        speaker="budget_officer",
        stance="propose",
        claim="建议削减",
        evidence=[
            Evidence(label="超出预算", value="2000 元"),
            Evidence(label="砍景点可省", value="300 元"),
            Evidence(label="拟砍掉", value="、".join(titles)),
        ],
        tool_calls=["day_cost_breakdown"],
    )


def objection(stance: str) -> Message:
    return Message(
        round=1,
        speaker="experience_officer",
        stance=stance,  # type: ignore[arg-type]
        claim="反对或让步",
        evidence=[Evidence(label="被砍项目不可替代性", value="0.70")],
        tool_calls=["day_experience"],
    )


def main() -> int:
    # 同城只有一个"自然"类景点 → 它最不可替代
    candidates = [
        poi("p1", "熊猫基地", "自然", ticket_price=55, duration_min=180),
        poi("p2", "武侯祠", "人文", ticket_price=50),
        poi("p3", "杜甫草堂", "人文", ticket_price=50),
        poi("p4", "锦里", "人文", ticket_price=0),
    ]
    scores = poi_irreplaceability(candidates)

    print("\n[1] 不可替代性评分")
    print(f"  {[(c.name, scores[c.id]) for c in candidates]}")
    check("熊猫基地分数最高", scores["p1"] == max(scores.values()), str(scores))
    check("同类多的景点分数较低", scores["p2"] < scores["p1"], f"{scores['p2']} vs {scores['p1']}")
    check("分数在 0-1 之间", all(0 <= v <= 1 for v in scores.values()), str(scores))

    b = brief()
    titles_map = {c.id: c.name for c in candidates}

    print("\n[2] 体验官让步 → 采纳预算官方案")
    msg, adj, outcome = mediator.rule(
        brief=b,
        proposal=proposal_msg(["武侯祠"]),
        objection=objection("accept"),
        proposed_ids=["p2"],
        scores=scores,
        total_cost=8000,
        budget_total=6000,
        titles_map=titles_map,
    )
    check("结论 adopted", outcome == "adopted", outcome)
    check("砍掉项被采纳", adj.drop_poi_ids == ["p2"], str(adj.drop_poi_ids))
    check("同时换酒店", adj.use_cheaper_hotel is True)

    print("\n[3] 体验官反对 + 全高价值 → 僵持升级")
    msg, adj, outcome = mediator.rule(
        brief=b,
        proposal=proposal_msg(["熊猫基地"]),
        objection=objection("object"),
        proposed_ids=["p1"],
        scores=scores,
        total_cost=8000,
        budget_total=6000,
        titles_map=titles_map,
    )
    check("结论 escalated", outcome == "escalated", outcome)
    check("不擅自给调整方案", adj.is_empty(), str(adj))
    check("发言立场为 escalate", msg.stance == "escalate", msg.stance)
    check("证据用景点名而非 id", any("熊猫基地" in e.value for e in msg.evidence), str([e.value for e in msg.evidence]))

    print("\n[4] 体验官反对 + 有低价值项目 → 折中")
    msg, adj, outcome = mediator.rule(
        brief=b,
        proposal=proposal_msg(["熊猫基地", "杜甫草堂"]),
        objection=objection("object"),
        proposed_ids=["p1", "p3"],
        scores=scores,
        total_cost=8000,
        budget_total=6000,
        titles_map=titles_map,
    )
    low = scores["p3"] < mediator.HIGH_VALUE
    if low:
        check("结论 adopted", outcome == "adopted", outcome)
        check("只砍低价值那个", adj.drop_poi_ids == ["p3"], str(adj.drop_poi_ids))
        check("保留高价值那个", "p1" not in adj.drop_poi_ids)
    else:
        check("该组合无低价值项时应升级", outcome == "escalated", outcome)

    print("\n[5] 最后一轮：用户已表态，必须给方案")
    msg, adj, outcome = mediator.rule(
        brief=b,
        proposal=proposal_msg(["熊猫基地"]),
        objection=objection("object"),
        proposed_ids=["p1"],
        scores=scores,
        total_cost=8000,
        budget_total=6000,
        titles_map=titles_map,
        final_round=True,
    )
    check("结论 adopted", outcome == "adopted", outcome)
    check("给出可执行方案", not adj.is_empty(), str(adj))
    check("写明牺牲项", any("熊猫基地" in e.value for e in msg.evidence), str([e.value for e in msg.evidence]))

    print("\n[6] 用户明确要省钱 → 优先预算")
    b_save = brief(preferences=["省钱", "人文"])
    msg, adj, outcome = mediator.rule(
        brief=b_save,
        proposal=proposal_msg(["熊猫基地"]),
        objection=objection("object"),
        proposed_ids=["p1"],
        scores=scores,
        total_cost=8000,
        budget_total=6000,
        titles_map=titles_map,
    )
    check("结论 adopted", outcome == "adopted", outcome)
    check("写明依据是用户偏好", any("用户偏好" in e.value for e in msg.evidence), str([e.value for e in msg.evidence]))

    print("\n[7] 上一轮被驳回的项目不重复提")
    log = NegotiationLog(triggered_by="test")
    log.rounds.append(proposal_msg(["熊猫基地", "杜甫草堂"]))
    rejected = _rejected(log)
    print(f"  提取到: {rejected}")
    check("能提取被驳回项", "熊猫基地" in rejected and "杜甫草堂" in rejected, str(rejected))

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  协商裁决逻辑全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
