"""FastAPI 服务：SSE 流式规划 + 结果持久化 + 单日重排。

事件协议（前端只依赖这三类）：
- trace：编排过程事件，phase = start | end | edge
- done ：最终结果，含 plan_id 与完整行程
- error：规划失败
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .. import settings as config_store
from ..agents import critic, lodging, planner, poi, transport, weather
from ..graph.build import build
from ..llm import LLMClient, LLMError
from ..models import Critique, Itinerary, TripBrief
from ..settings import AppSettings
from ..tools.factory import make_source
from . import store

app = FastAPI(title="旅行规划多 agent 编排助手", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RefineRequest(BaseModel):
    date: date
    feedback: str = ""


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _plan_stream(brief: TripBrief, plan_id: str, cfg: AppSettings):
    graph = build(make_source(cfg.datasource), cfg)
    trace: list[dict] = []
    final: dict = {}
    try:
        async for mode, payload in graph.astream(
            {"brief": brief, "revision": 0, "errors": []},
            stream_mode=["custom", "values"],
        ):
            if mode == "custom":
                trace.append(payload)
                yield _sse("trace", payload)
            else:
                final = payload
    except Exception as e:  # 任何未预期异常都要让前端收到，而不是静默断开
        yield _sse("error", {"message": f"规划过程异常：{e}"})
        return

    itinerary: Itinerary = final["draft"]
    critique: Critique = final["critique"]
    revision = final.get("revision", 0)
    store.save(
        plan_id,
        brief.model_dump(mode="json"),
        itinerary.model_dump(mode="json"),
        critique.model_dump(mode="json"),
        trace,
        revision,
    )
    yield _sse(
        "done",
        {
            "plan_id": plan_id,
            "brief": brief.model_dump(mode="json"),
            "itinerary": itinerary.model_dump(mode="json"),
            "critique": critique.model_dump(mode="json"),
            "revision": revision,
            "errors": final.get("errors") or [],
        },
    )


@app.post("/api/plan")
async def create_plan(brief: TripBrief):
    cfg = config_store.load()
    try:
        make_source(cfg.datasource)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    plan_id = store.new_id()
    return StreamingResponse(
        _plan_stream(brief, plan_id, cfg),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/plan/{plan_id}")
async def get_plan(plan_id: str):
    record = store.load(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail="规划不存在")
    return record


@app.post("/api/plan/{plan_id}/refine")
async def refine_plan(plan_id: str, body: RefineRequest):
    record = store.load(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail="规划不存在")

    brief = TripBrief(**record["brief"])
    itinerary = Itinerary(**record["itinerary"])
    target = body.date
    if not any(d.date == target for d in itinerary.days):
        raise HTTPException(status_code=400, detail=f"{target} 不在该行程日期范围内")

    cfg = config_store.load()
    src = make_source(cfg.datasource)
    t_plan = transport.run(brief, src)
    l_plan = lodging.run(brief, src)
    p_plan = poi.run(brief, src)
    w_plan = weather.run(brief, src)

    # 单日重排沿用原行程的酒店，避免同一次行程里两种住宿
    existing_hotel = next(
        (i.ref_id for d in itinerary.days for i in d.items if i.type == "hotel"),
        None,
    )
    if existing_hotel:
        match = next((h for h in l_plan.candidates if h.name == existing_hotel), None)
        if match:
            l_plan.chosen = match

    frozen = {d.date: d for d in itinerary.days}
    new_itinerary = planner.run(
        brief=brief,
        transport=t_plan,
        lodging=l_plan,
        poi_plan=p_plan,
        weather=w_plan,
        frozen=frozen,
        target_date=target,
        feedback=body.feedback,
    )
    new_critique = critic.run(brief, new_itinerary, p_plan, t_plan, w_plan)

    store.save(
        plan_id,
        brief.model_dump(mode="json"),
        new_itinerary.model_dump(mode="json"),
        new_critique.model_dump(mode="json"),
        record["trace"]
        + [{"phase": "refine", "node": "planner", "target": str(target), "feedback": body.feedback}],
        record.get("revision", 0),
    )
    return {
        "plan_id": plan_id,
        "itinerary": new_itinerary.model_dump(mode="json"),
        "critique": new_critique.model_dump(mode="json"),
        "revision": record.get("revision", 0),
    }


@app.get("/api/config")
def get_config():
    """返回掩码视图：api_key 只给前后各 4 位，完整值永不回传。"""
    return config_store.public_view(config_store.load())


@app.put("/api/config")
def put_config(body: dict):
    try:
        merged = config_store.merge_incoming(config_store.load(), body)
    except ValidationError as e:
        first = e.errors()[0]
        raise HTTPException(status_code=400, detail=f"{'.'.join(str(x) for x in first['loc'])}: {first['msg']}") from e
    config_store.save(merged)
    return config_store.public_view(merged)


@app.post("/api/config/test")
async def test_config(body: dict | None = None):
    """连通性检测。body 可带尚未保存的表单值，先合再测。"""
    cfg = config_store.load()
    if body:
        try:
            cfg = config_store.merge_incoming(cfg, body)
        except ValidationError as e:
            first = e.errors()[0]
            raise HTTPException(status_code=400, detail=str(first["msg"])) from e
    if not cfg.llm.configured:
        return {"ok": False, "error": "模型未启用，或 base_url / model / api_key 不完整"}
    client = LLMClient(cfg.llm, cfg.orchestration.node_timeout_s)
    try:
        reply = await client.ping()
    except LLMError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "reply": reply}


@app.get("/api/health")
async def health():
    return {"ok": True}


# 生产模式：单端口同时提供 UI 与 API，必须放在所有 API 路由之后
_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
