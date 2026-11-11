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
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel, ValidationError

from .. import settings as config_store
from ..agents import critic, lodging, llm_refiner, planner, poi, transport, weather
from ..graph.build import build
from ..llm import LLMClient, LLMError, TokenBudget
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


class ResumeRequest(BaseModel):
    decision: Literal["accept", "downgrade", "abort"]


# interrupt/resume 需要 checkpointer。这里用进程内存储：
# 服务重启后未完成的待确认规划会失效，已完成的结果在 SQLite 里不受影响。
_CHECKPOINTER = MemorySaver()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _run_graph(graph, config: dict, graph_input, plan_id: str, brief: TripBrief):
    """消费图的事件流，并把「中断待确认」也作为一类事件推给前端。"""
    trace: list[dict] = []
    final: dict = {}
    try:
        async for mode, payload in graph.astream(
            graph_input, config=config, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                trace.append(payload)
                yield _sse("trace", payload)
            else:
                final = payload
    except Exception as e:  # 任何未预期异常都要让前端收到，而不是静默断开
        yield _sse("error", {"message": f"规划过程异常：{e}"})
        return

    snapshot = await graph.aget_state(config)
    pending = [i for task in (snapshot.tasks or []) for i in task.interrupts]
    if pending:
        info = dict(pending[0].value or {})
        info["plan_id"] = plan_id
        info["revision"] = final.get("revision", 0)
        # 中断前若发生过协商，把轨迹一并给出，否则用户无从判断该选哪个
        negotiation = final.get("negotiation")
        if negotiation is not None:
            info["negotiation"] = negotiation.model_dump(mode="json")
        yield _sse("interrupt", info)
        return

    if "draft" not in final or "critique" not in final:
        yield _sse("error", {"message": "规划未产出结果"})
        return

    itinerary: Itinerary = final["draft"]
    critique: Critique = final["critique"]
    revision = final.get("revision", 0)
    decision = final.get("decision")
    negotiation = final.get("negotiation")

    if decision == "accept":
        itinerary.warnings.append("你已确认接受超出预算的方案")
    elif decision == "abort":
        itinerary.warnings.append("你已选择取消，此行程仅供参考")
    elif decision == "downgrade":
        itinerary.warnings.append("已按你的选择降配重排")

    store.save(
        plan_id,
        brief.model_dump(mode="json"),
        itinerary.model_dump(mode="json"),
        critique.model_dump(mode="json"),
        trace,
        revision,
        negotiation.model_dump(mode="json") if negotiation else None,
    )
    negotiation = final.get("negotiation")
    yield _sse(
        "done",
        {
            "plan_id": plan_id,
            "brief": brief.model_dump(mode="json"),
            "itinerary": itinerary.model_dump(mode="json"),
            "critique": critique.model_dump(mode="json"),
            "revision": revision,
            "decision": decision,
            "negotiation": negotiation.model_dump(mode="json") if negotiation else None,
            "errors": final.get("errors") or [],
        },
    )


async def _plan_stream(brief: TripBrief, plan_id: str, cfg: AppSettings):
    graph = build(make_source(cfg.datasource), cfg, checkpointer=_CHECKPOINTER)
    config = {"configurable": {"thread_id": plan_id}}
    async for chunk in _run_graph(
        graph, config, {"brief": brief, "revision": 0, "errors": []}, plan_id, brief
    ):
        yield chunk


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


@app.post("/api/plan/{plan_id}/resume")
async def resume_plan(plan_id: str, body: ResumeRequest):
    """恢复一次因预算超支而暂停的规划。"""
    record = store.load(plan_id)
    brief_dict = record["brief"] if record else None

    # 中断时还没落库，因此从 checkpointer 里取原始 brief
    cfg = config_store.load()
    graph = build(make_source(cfg.datasource), cfg, checkpointer=_CHECKPOINTER)
    config = {"configurable": {"thread_id": plan_id}}
    snapshot = await graph.aget_state(config)
    if not snapshot.next:
        raise HTTPException(status_code=400, detail="该规划没有待确认的中断")

    if brief_dict:
        brief = TripBrief(**brief_dict)
    else:
        raw = snapshot.values.get("brief")
        if raw is None:
            raise HTTPException(status_code=400, detail="找不到该规划的原始需求")
        brief = raw

    async def stream():
        async for chunk in _run_graph(
            graph, config, Command(resume=body.decision), plan_id, brief
        ):
            yield chunk

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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

    target_day = next(d for d in itinerary.days if d.date == target)

    # 模型启用时由模型理解反馈；失败或未启用则回落关键词规则
    llm_selection = None
    refine_source = "rule"
    if cfg.llm.configured:
        used_elsewhere = {
            i.ref_id
            for d in itinerary.days
            if d.date != target
            for i in d.items
            if i.type == "sight" and i.ref_id
        }
        is_rainy = any(w.date == target and w.is_rainy for w in w_plan.days)
        client = LLMClient(
            cfg.llm,
            cfg.orchestration.node_timeout_s,
            TokenBudget(cfg.orchestration.token_budget),
        )
        picked = await llm_refiner.refine_day(
            brief, target_day, body.feedback, p_plan.candidates, used_elsewhere, is_rainy, client
        )
        if picked is not None:
            llm_selection = {target: picked}
            refine_source = "model"

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
        llm_selection=llm_selection,
    )
    new_critique = critic.run(brief, new_itinerary, p_plan, t_plan, w_plan)

    store.save(
        plan_id,
        brief.model_dump(mode="json"),
        new_itinerary.model_dump(mode="json"),
        new_critique.model_dump(mode="json"),
        record["trace"]
        + [{"phase": "refine", "node": "planner", "target": str(target),
            "feedback": body.feedback, "source": refine_source}],
        record.get("revision", 0),
    )
    return {
        "plan_id": plan_id,
        "itinerary": new_itinerary.model_dump(mode="json"),
        "critique": new_critique.model_dump(mode="json"),
        "revision": record.get("revision", 0),
        "refine_source": refine_source,
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
    """连通性检测。body 可带尚未保存的表单值，先合再测。

    只要求凭据齐全，不要求「启用模型」——先测通再决定是否启用是更自然的顺序。
    """
    cfg = config_store.load()
    if body:
        try:
            cfg = config_store.merge_incoming(cfg, body)
        except ValidationError as e:
            first = e.errors()[0]
            raise HTTPException(status_code=400, detail=str(first["msg"])) from e

    missing = [
        name
        for name, value in (
            ("接口地址 base_url", cfg.llm.base_url),
            ("模型名 model", cfg.llm.model),
            ("API Key", cfg.llm.api_key),
        )
        if not value
    ]
    if missing:
        return {"ok": False, "error": "请先填写：" + "、".join(missing)}

    client = LLMClient(cfg.llm, cfg.orchestration.node_timeout_s)
    try:
        reply = await client.ping()
    except LLMError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "reply": reply, "enabled": cfg.llm.enabled}


@app.get("/api/health")
async def health():
    return {"ok": True}


# 生产模式：单端口同时提供 UI 与 API，必须放在所有 API 路由之后
_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
