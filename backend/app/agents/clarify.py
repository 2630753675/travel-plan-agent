"""诉求归一化 agent。

表单已经是结构化的，这一层只做：
- 缺省值补齐（偏好、节奏）
- 可行性提示（出发日太近、预算过低）
- 输出稳定的 TripBrief 供下游使用

它是纯函数，不调 LLM，也不访问数据源。
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, Field

from ..models import TripBrief

# 人均日均可支配预算低于此值时提示风险
LOW_BUDGET_PER_PERSON_DAY = 400


class ClarifyResult(BaseModel):
    brief: TripBrief
    notes: list[str] = Field(default_factory=list)


def run(brief: TripBrief, today: date | None = None) -> ClarifyResult:
    notes: list[str] = []

    current = today or date.today()
    if brief.start_date < current:
        notes.append(f"出发日期 {brief.start_date} 已过，请确认是否改期")
    elif (brief.start_date - current).days <= 2:
        notes.append("距出发不足 3 天，机票与酒店价格可能偏高")

    per_person_day = brief.budget_total / brief.travelers / brief.days
    if per_person_day < LOW_BUDGET_PER_PERSON_DAY:
        notes.append(
            f"人均每日可支配约 {per_person_day:.0f} 元，低于 {LOW_BUDGET_PER_PERSON_DAY} 元，"
            "行程将优先经济型选择"
        )

    if brief.has_elder:
        notes.append("有老人同行：单日步行与爬升强度将受限")
    if brief.has_child:
        notes.append("有儿童同行：夜生活与长时排队项目将被排除")

    if not brief.preferences:
        notes.append("未选择偏好，将按城市经典路线安排")

    return ClarifyResult(brief=brief, notes=notes)
