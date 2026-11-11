"""按界面配置选择数据源实现。"""

from __future__ import annotations

from ..settings import DataSourceSettings
from .base import TravelSource
from .http import HttpSource
from .mock import MockSource


def make_source(cfg: DataSourceSettings) -> TravelSource:
    if cfg.kind == "http":
        if not cfg.http_base_url:
            raise ValueError("数据源选择 http 但未填写地址")
        return HttpSource(cfg.http_base_url)
    return MockSource()
