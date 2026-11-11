"""数据源适配器接口。

agent 只依赖这个协议，不依赖具体实现；换真实 API 时只改实现层。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ..models import Flight, Hotel, POI, WeatherDay


class SourceError(Exception):
    """数据源不可用。调用方必须降级处理，不能当作'没有结果'。"""


class TravelSource(Protocol):
    def search_flights(self, origin: str, destination: str, dep_date: date) -> list[Flight]:
        """查询指定日期的单程航班。查不到返回空列表；源不可用抛 SourceError。"""
        ...

    def search_hotels(self, city: str, checkin: date, nights: int) -> list[Hotel]:
        ...

    def list_pois(self, city: str) -> list[POI]:
        ...

    def get_weather(self, city: str, start: date, days: int) -> list[WeatherDay]:
        ...
