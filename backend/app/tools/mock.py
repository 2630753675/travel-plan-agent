"""mock 数据源：从 data/mock/*.json 读取，按接口约定填充查询条件。

刻意保留两个行为，用来驱动真实场景：
- 城市/航线不匹配时返回空列表（对应"飞这条线没有航班"）；
- 文件缺失或结构错误抛 SourceError（对应"数据源挂了"）。
二者在 agent 层必须区别对待。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..models import Flight, Hotel, POI, WeatherDay
from .base import SourceError

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "mock"


def _load(name: str) -> list[dict]:
    path = DATA_DIR / name
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise SourceError(f"mock 数据文件缺失: {path.name}") from e
    except json.JSONDecodeError as e:
        raise SourceError(f"mock 数据文件损坏: {path.name}: {e}") from e
    if not isinstance(data, list):
        raise SourceError(f"mock 数据格式错误，期望数组: {path.name}")
    return data


def _parse_time(value: str):
    hh, mm = value.split(":")
    return int(hh), int(mm)


class MockSource:
    def search_flights(self, origin: str, destination: str, dep_date: date) -> list[Flight]:
        out: list[Flight] = []
        for row in _load("flights.json"):
            if row["dep_city"] != origin or row["arr_city"] != destination:
                continue
            try:
                out.append(
                    Flight(
                        flight_no=row["flight_no"],
                        dep_city=row["dep_city"],
                        arr_city=row["arr_city"],
                        dep_date=dep_date,
                        dep_time=row["dep_time"],
                        arr_time=row["arr_time"],
                        duration_min=row["duration_min"],
                        price=row["price"],
                        seats_left=row["seats_left"],
                    )
                )
            except ValueError as e:
                raise SourceError(f"航班数据校验失败 {row.get('flight_no')}: {e}") from e
        return out

    def search_hotels(self, city: str, checkin: date, nights: int) -> list[Hotel]:
        # mock 只覆盖成都，其他城市视为查不到
        if city != "成都":
            return []
        out: list[Hotel] = []
        for row in _load("hotels.json"):
            try:
                out.append(Hotel(**row))
            except ValueError as e:
                raise SourceError(f"酒店数据校验失败 {row.get('id')}: {e}") from e
        return out

    def list_pois(self, city: str) -> list[POI]:
        if city != "成都":
            return []
        out: list[POI] = []
        for row in _load("pois.json"):
            try:
                out.append(POI(**row))
            except ValueError as e:
                raise SourceError(f"景点数据校验失败 {row.get('id')}: {e}") from e
        return out

    def get_weather(self, city: str, start: date, days: int) -> list[WeatherDay]:
        if city != "成都":
            return []
        templates = _load("weather.json")
        if not templates:
            raise SourceError("mock 天气数据为空")
        from datetime import timedelta

        out: list[WeatherDay] = []
        for i in range(days):
            tpl = templates[i % len(templates)]
            out.append(WeatherDay(date=start + timedelta(days=i), **tpl))
        return out
