"""高德 Web 服务数据源：真实 POI 与道路距离。

只替换 POI 与路线；航班、酒店、天气仍由 fallback 提供。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, time

from ..models import Flight, Hotel, POI, WeatherDay
from .base import SourceError, TravelSource

AMAP_BASE = "https://restapi.amap.com"


class AmapWebSource:
    def __init__(self, fallback: TravelSource, key: str, timeout_s: float = 10.0):
        self.fallback = fallback
        self.key = key
        self.timeout_s = timeout_s

    def _get(self, path: str, params: dict) -> dict:
        query = urllib.parse.urlencode({**params, "key": self.key})
        url = f"{AMAP_BASE}{path}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise SourceError(f"高德 Web 服务 HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise SourceError(f"高德 Web 服务不可达：{e.reason}") from e
        except json.JSONDecodeError as e:
            raise SourceError("高德 Web 服务返回的不是合法 JSON") from e
        if not isinstance(data, dict):
            raise SourceError("高德 Web 服务返回结构不符合约定")
        if str(data.get("status")) != "1":
            raise SourceError(f"高德 Web 服务错误：{data.get('info') or data.get('infocode')}")
        return data

    def list_pois(self, city: str) -> list[POI]:
        data = self._get(
            "/v5/place/text",
            {
                "keywords": "景点",
                "region": city,
                "city_limit": "true",
                "page_size": 25,
                "show_fields": "business",
            },
        )
        out: list[POI] = []
        for row in data.get("pois") or []:
            poi = _to_poi(row)
            if poi is not None:
                out.append(poi)
        return out

    def route(self, origin: tuple[float, float], destination: tuple[float, float], mode: str = "driving") -> dict:
        """origin/destination 为 (lng, lat)，返回道路距离与预计耗时。"""
        path = "/v3/direction/walking" if mode == "walking" else "/v3/direction/driving"
        data = self._get(
            path,
            {
                "origin": f"{origin[0]:.6f},{origin[1]:.6f}",
                "destination": f"{destination[0]:.6f},{destination[1]:.6f}",
                "extensions": "base",
            },
        )
        paths = ((data.get("route") or {}).get("paths") or [])
        if not paths:
            raise SourceError("高德未返回可用路线")
        first = paths[0]
        return {
            "distance_km": round(float(first.get("distance") or 0) / 1000, 2),
            "duration_min": round(float(first.get("duration") or 0) / 60),
            "mode": "amap_walking" if mode == "walking" else "amap_driving",
        }

    def search_flights(self, origin: str, destination: str, dep_date: date) -> list[Flight]:
        return self.fallback.search_flights(origin, destination, dep_date)

    def search_hotels(self, city: str, checkin: date, nights: int) -> list[Hotel]:
        return self.fallback.search_hotels(city, checkin, nights)

    def get_weather(self, city: str, start: date, days: int) -> list[WeatherDay]:
        return self.fallback.get_weather(city, start, days)


def _to_poi(row: dict) -> POI | None:
    location = str(row.get("location") or "")
    if "," not in location:
        return None
    try:
        lng, lat = (float(part) for part in location.split(",", 1))
    except ValueError:
        return None
    name = str(row.get("name") or "").strip()
    if not name:
        return None
    business = row.get("business") if isinstance(row.get("business"), dict) else {}
    open_time, close_time = _hours(str(business.get("opentime_today") or ""))
    type_text = str(row.get("type") or "")
    category = type_text.split(";")[-1] or "景点"
    tags = [part for part in type_text.split(";") if part]
    tags.append("开放时间待核实")
    tags.append("票价待核实")
    tags.append("游览时长待估算")
    return POI(
        id=f"amap:{row.get('id') or name}",
        name=name,
        category=category,
        open_time=open_time,
        close_time=close_time,
        ticket_price=0,
        recommended_slot=["上午", "下午"],
        tags=tags,
        lat=lat,
        lng=lng,
        indoor=_indoor(name, type_text),
    )


def _hours(text: str) -> tuple[time, time]:
    for sep in ("-", "—", "~"):
        if sep in text:
            left, right = text.split(sep, 1)
            try:
                return time.fromisoformat(left.strip()), time.fromisoformat(right.strip())
            except ValueError:
                break
    return time(9, 0), time(17, 0)


def _indoor(name: str, type_text: str) -> bool:
    text = f"{name}{type_text}"
    return any(word in text for word in ("博物馆", "纪念馆", "美术馆", "展览", "寺", "庙", "宫", "塔", "剧院", "商场", "书店"))
