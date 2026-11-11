"""HTTP 数据源适配器。

⚠ 未验证：本机没有可用的真实数据接口，此实现只做了协议映射。
接入真实服务时需要通过 verify_* 脚本或设置页实测后再使用。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from ..models import Flight, Hotel, POI, WeatherDay
from .base import SourceError


class HttpSource:
    def __init__(self, base_url: str, timeout_s: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _get(self, path: str, params: dict) -> list[dict]:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise SourceError(f"{path} 返回 HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise SourceError(f"{path} 不可达: {e.reason}") from e
        except json.JSONDecodeError as e:
            raise SourceError(f"{path} 返回的不是合法 JSON") from e
        if isinstance(data, dict) and "items" in data:
            data = data["items"]
        if not isinstance(data, list):
            raise SourceError(f"{path} 返回结构不符合约定，期望数组")
        return data

    def search_flights(self, origin: str, destination: str, dep_date: date) -> list[Flight]:
        rows = self._get(
            "/flights",
            {"origin": origin, "destination": destination, "date": str(dep_date)},
        )
        return [Flight(**{**r, "dep_date": r.get("dep_date", str(dep_date))}) for r in rows]

    def search_hotels(self, city: str, checkin: date, nights: int) -> list[Hotel]:
        return [Hotel(**r) for r in self._get("/hotels", {"city": city, "checkin": str(checkin), "nights": nights})]

    def list_pois(self, city: str) -> list[POI]:
        return [POI(**r) for r in self._get("/pois", {"city": city})]

    def get_weather(self, city: str, start: date, days: int) -> list[WeatherDay]:
        return [WeatherDay(**r) for r in self._get("/weather", {"city": city, "start": str(start), "days": days})]
