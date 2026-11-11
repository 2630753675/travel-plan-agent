"""协商消息结构。

轨迹的价值在于「可追溯」：每条主张都必须带证据，且证据来自工具计算而非模型自述。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Speaker = Literal["budget_officer", "experience_officer", "mediator", "user"]
Stance = Literal["propose", "object", "accept", "rule", "escalate", "adopt"]
Outcome = Literal["adopted", "escalated", "no_change"]


class Evidence(BaseModel):
    label: str
    value: str


class Message(BaseModel):
    round: int
    speaker: Speaker
    stance: Stance
    claim: str
    evidence: list[Evidence] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)


class Adjustment(BaseModel):
    """协商产出、交给 planner 执行的调整方案。"""

    drop_poi_ids: list[str] = Field(default_factory=list)
    use_cheaper_hotel: bool = False

    def is_empty(self) -> bool:
        return not self.drop_poi_ids and not self.use_cheaper_hotel


class NegotiationLog(BaseModel):
    triggered_by: str
    rounds: list[Message] = Field(default_factory=list)
    outcome: Outcome = "no_change"
    adjustment: Adjustment = Field(default_factory=Adjustment)
    summary: str = ""

    def transcript(self) -> str:
        """给前端/日志用的纯文本轨迹。"""
        lines = [f"协商触发：{self.triggered_by}"]
        for m in self.rounds:
            lines.append(f"[{m.round}] {m.speaker}（{m.stance}）：{m.claim}")
            for e in m.evidence:
                lines.append(f"      · {e.label}: {e.value}")
        lines.append(f"结论：{self.outcome} —— {self.summary}")
        return "\n".join(lines)
