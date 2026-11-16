"""天气查询工具封装：取逐日天气，供行程规划与硬约束校验 Workflow 使用。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import TripBrief, WeatherDay
from ..tools.base import SourceError, TravelSource


class WeatherPlan(BaseModel):
    days: list[WeatherDay] = Field(default_factory=list)
    error: str | None = None

    def rainy_dates(self) -> set:
        return {w.date for w in self.days if w.is_rainy}


def run(brief: TripBrief, source: TravelSource) -> WeatherPlan:
    try:
        days = source.get_weather(brief.destination, brief.start_date, brief.days)
    except SourceError as e:
        return WeatherPlan(error=f"天气数据源不可用，雨天备选将无法给出：{e}")

    if not days:
        return WeatherPlan(error=f"未查到 {brief.destination} 的天气数据")

    return WeatherPlan(days=days)
