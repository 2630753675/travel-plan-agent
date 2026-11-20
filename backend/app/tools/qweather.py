"""和风天气数据源：真实预报，失败时由调用方降级。

只替换天气；航班、酒店、景点仍由 fallback（当前为 mock）提供。
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from ..models import Flight, Hotel, POI, WeatherDay
from .base import SourceError, TravelSource


class QWeatherSource:
    def __init__(self, fallback: TravelSource, key: str, host: str, timeout_s: float = 10.0):
        self.fallback = fallback
        self.key = key
        self.host = host.rstrip("/") or "https://devapi.qweather.com"
        if not self.host.startswith("http"):
            self.host = "https://" + self.host
        self.timeout_s = timeout_s

    def _get(self, path: str, params: dict) -> dict:
        query = urllib.parse.urlencode({**params, "key": self.key})
        url = f"{self.host}{path}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                data = json.loads(raw.decode())
        except urllib.error.HTTPError as e:
            raise SourceError(f"和风天气 HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise SourceError(f"和风天气不可达：{e.reason}") from e
        except json.JSONDecodeError as e:
            raise SourceError("和风天气返回的不是合法 JSON") from e
        if not isinstance(data, dict):
            raise SourceError("和风天气返回结构不符合约定")
        if str(data.get("code")) != "200":
            raise SourceError(f"和风天气错误码 {data.get('code')}")
        return data

    def _city_id(self, city: str) -> str:
        data = self._get("/geo/v2/city/lookup", {"location": city, "range": "cn", "number": 1})
        locations = data.get("location") or []
        if not locations:
            raise SourceError(f"和风天气未找到城市：{city}")
        return str(locations[0]["id"])

    def get_weather(self, city: str, start: date, days: int) -> list[WeatherDay]:
        location = self._city_id(city)
        data = self._get("/v7/weather/7d", {"location": location})
        daily = data.get("daily") or []
        wanted = {start + timedelta(days=i) for i in range(max(1, days))}
        out: list[WeatherDay] = []
        for row in daily:
            try:
                day = date.fromisoformat(str(row.get("fxDate")))
            except ValueError:
                continue
            if day not in wanted:
                continue
            text = str(row.get("textDay") or "")
            out.append(
                WeatherDay(
                    date=day,
                    condition=_condition(text),
                    temp_low=_num(row.get("tempMin"), 0),
                    temp_high=_num(row.get("tempMax"), 0),
                    rain_prob=_rain_prob(text, row.get("precip")),
                )
            )
        return out

    # 其余数据仍由当前数据源提供
    def search_flights(self, origin: str, destination: str, dep_date: date) -> list[Flight]:
        return self.fallback.search_flights(origin, destination, dep_date)

    def search_hotels(self, city: str, checkin: date, nights: int) -> list[Hotel]:
        return self.fallback.search_hotels(city, checkin, nights)

    def list_pois(self, city: str) -> list[POI]:
        return self.fallback.list_pois(city)


def _num(value: object, default: int) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _condition(text: str) -> str:
    if "大雨" in text or "暴雨" in text:
        return "大雨"
    if "中雨" in text:
        return "中雨"
    if "雨" in text:
        return "小雨"
    if "雪" in text:
        return "阴"
    if "阴" in text:
        return "阴"
    if "多云" in text:
        return "多云"
    return "晴"


def _rain_prob(text: str, precip: object) -> float:
    if "雨" in text or "雪" in text:
        return 0.8
    try:
        return 0.6 if float(precip or 0) > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0
