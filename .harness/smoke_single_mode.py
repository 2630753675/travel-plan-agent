"""Post-refactor smoke checks for single-mode and no-op version behavior."""
from __future__ import annotations

import sys
from datetime import date

sys.path.insert(0, r"E:\项目\agent_study\dsh_work2\backend")

from fastapi import HTTPException  # noqa: E402

from app.api.main import _require_configured_model  # noqa: E402
from app.agents.tools_spec import ToolContext, execute  # noqa: E402
from app.graph.build import build  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.settings import AppSettings, LLMSettings  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402


def brief() -> TripBrief:
    return TripBrief(
        origin="上海", destination="成都",
        start_date=date(2026, 11, 20), end_date=date(2026, 11, 24),
        travelers=2, budget_total=8000, companions=["老人"],
        preferences=["人文"], pace="适中",
    )


def test_no_model_guard() -> None:
    try:
        _require_configured_model(AppSettings())
    except HTTPException as exc:
        assert exc.status_code == 400 and "未配置模型" in str(exc.detail)
        print("PASS no-model API guard")
    else:
        raise AssertionError("no-model guard did not raise")

    try:
        build(MockSource(), AppSettings())
    except ValueError as exc:
        assert "未配置模型" in str(exc)
        print("PASS no-model build guard")
    else:
        raise AssertionError("build without model did not raise")


def test_plan_endpoint_returns_400() -> None:
    from fastapi.testclient import TestClient

    import app.api.main as api

    original = api.config_store.load
    api.config_store.load = lambda: AppSettings()
    try:
        client = TestClient(api.app, client=("127.0.0.1", 50000))
        response = client.post("/api/plan", json={
            "origin": "上海", "destination": "成都",
            "start_date": "2026-11-20", "end_date": "2026-11-24",
            "travelers": 2, "budget_total": 8000,
            "companions": ["老人"], "preferences": ["人文"], "pace": "适中",
        })
        assert response.status_code == 400, response.text
        assert "未配置模型" in response.text
        print("PASS POST /api/plan returns 400 without model")
    finally:
        api.config_store.load = original


def test_noop_versions() -> None:
    ctx = ToolContext(brief=brief(), source=MockSource())
    execute(ctx, "list_pois", {})
    v = ctx.plan_version
    execute(ctx, "assign_pois", {"date": "2026-11-21", "poi_ids": ["p02"]})
    v1 = ctx.plan_version
    execute(ctx, "assign_pois", {"date": "2026-11-21", "poi_ids": ["p02"]})
    assert ctx.plan_version == v1, (v, v1, ctx.plan_version)
    execute(ctx, "search_flights", {})
    execute(ctx, "set_flights", {"outbound_flight_no": "3U8890", "inbound_flight_no": "3U8891"})
    v2 = ctx.plan_version
    execute(ctx, "set_flights", {"outbound_flight_no": "3U8890", "inbound_flight_no": "3U8891"})
    assert ctx.plan_version == v2
    execute(ctx, "search_hotels", {})
    execute(ctx, "set_hotel", {"hotel_id": "春熙里和颐"})
    v3 = ctx.plan_version
    execute(ctx, "set_hotel", {"hotel_id": "春熙里和颐"})
    assert ctx.plan_version == v3
    print("PASS no-op writes do not bump plan_version")


test_no_model_guard()
test_plan_endpoint_returns_400()
test_noop_versions()
print("SMOKE PASS")
