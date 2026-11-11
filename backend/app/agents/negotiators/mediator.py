"""调解者：在预算官和体验官之间裁决。

裁决用规则而非第三个模型——为了保证结论可复现、可解释。
理由文本由数据生成，不是模板空话。

裁决顺序：
1. 体验官同意 → 采纳预算官方案
2. 用户明确要省钱 → 采纳
3. 否则折中：只砍低不可替代性的项目
4. 折中后仍大幅超支 → 升级给用户（调用方的 interrupt）
"""

from __future__ import annotations

from ...models import POI, TripBrief
from .protocol import Adjustment, Evidence, Message

HIGH_VALUE = 0.6
# 折中后仍超过预算这个比例，就不替用户决定了
ESCALATE_RATIO = 0.35


def rule(
    brief: TripBrief,
    proposal: Message,
    objection: Message,
    proposed_ids: list[str],
    scores: dict[str, float],
    total_cost: float,
    budget_total: float,
    round_no: int = 1,
    titles_map: dict[str, str] | None = None,
) -> tuple[Message, Adjustment, str]:
    """返回 (调解者发言, 调整方案, 结果状态)。

    结果状态：adopted / escalated / no_change
    """
    titles_map = titles_map or {}
    over = total_cost - budget_total
    user_wants_saving = any(k in "".join(brief.preferences) for k in ("省钱", "经济", "性价比"))

    proposal_adjustment = Adjustment(
        drop_poi_ids=list(proposed_ids),
        use_cheaper_hotel=True,
    )

    # 1. 体验官没有异议
    if objection.stance == "accept":
        saved_hint = next((e.value for e in proposal.evidence if e.label.startswith("砍景点可省")), "-")
        return (
            Message(
                round=round_no,
                speaker="mediator",
                stance="rule",
                claim="体验官无异议，采纳预算官的削减方案",
                evidence=[
                    Evidence(label="裁决依据", value="双方立场一致"),
                    Evidence(label="预算官预估削减", value=saved_hint),
                ],
                tool_calls=["rule_based_mediate"],
            ),
            proposal_adjustment,
            "adopted",
        )

    # 2. 用户自己要省钱
    if user_wants_saving:
        return (
            Message(
                round=round_no,
                speaker="mediator",
                stance="rule",
                claim="你的偏好里明确要求省钱，优先满足预算，采纳削减方案",
                evidence=[
                    Evidence(label="裁决依据", value=f"用户偏好：{brief.preferences}"),
                    Evidence(label="体验损失", value="已记录，可在轨迹中查看"),
                ],
                tool_calls=["rule_based_mediate"],
            ),
            proposal_adjustment,
            "adopted",
        )

    # 3. 折中：只砍低不可替代性的
    low_value = [pid for pid in proposed_ids if scores.get(pid, 0) < HIGH_VALUE]
    keep = [pid for pid in proposed_ids if pid not in low_value]

    if low_value:
        adjustment = Adjustment(drop_poi_ids=list(low_value), use_cheaper_hotel=True)
        return (
            Message(
                round=round_no,
                speaker="mediator",
                stance="adopt",
                claim=(
                    f"折中：砍掉 {len(low_value)} 个可替代性较低的项目，"
                    f"保留 {len(keep)} 个不可替代的"
                ),
                evidence=[
                    Evidence(label="裁决依据", value="体验损失过高，但预算缺口需要部分填补"),
                    Evidence(label="砍掉", value="、".join(low_value)),
                    Evidence(label="保留", value="、".join(keep) or "无"),
                    Evidence(label="仍超支", value=f"{over:.0f} 元（待重排后确认）"),
                ],
                tool_calls=["rule_based_mediate", "irreplaceability_split"],
            ),
            adjustment,
            "adopted",
        )

    # 4. 全部都是高价值项目：不替用户决定
    titles = "、".join(titles_map.get(pid, pid) for pid in proposed_ids)
    return (
        Message(
            round=round_no,
            speaker="mediator",
            stance="escalate",
            claim=(
                f"预算官想砍的项目全部难以替代（最低 {min(scores.get(p, 0) for p in proposed_ids):.2f}），"
                "双方无法达成一致，交给你决定"
            ),
            evidence=[
                Evidence(label="超出预算", value=f"{over:.0f} 元（{over / budget_total:.0%}）"),
                Evidence(label="被砍项目都属高价值", value=titles),
                Evidence(label="裁决依据", value="不存在既不牺牲体验又能补齐预算的方案"),
            ],
            tool_calls=["rule_based_mediate"],
        ),
        Adjustment(),
        "escalated",
    )
