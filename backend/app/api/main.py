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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel, ValidationError

from .. import access
from .. import settings as config_store
from ..agents import critic, lodging, llm_refiner, planner, poi, transport, weather
from ..checkpoint import PersistentSaver
from ..graph.build import build
from ..llm import LLMClient, LLMError
from ..event_stream import EventFactory, legacy_activity
from ..models import Critique, Itinerary, TripBrief
from ..paths import DATA_DIR
from ..settings import AppSettings
from .. import summary
from ..tools.factory import make_source
from . import store

app = FastAPI(title="旅行规划助手", version="0.2.0")

# 只允许同源与本地开发端口。认证走自定义头，不靠宽松 CORS。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8848",
        "http://localhost:8848",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

_ACCESS_TOKEN = access.load_token()
_SHARE_SECRET = access.load_share_secret()

# 需要放行的路径：健康检查与只读分享
_OPEN_PATHS = {"/api/health"}


@app.middleware("http")
async def access_guard(request: Request, call_next):
    """本机免口令；外部访问需要口令，或带合法签名读单个分享。"""
    path = request.url.path
    if not path.startswith("/api/") or path in _OPEN_PATHS:
        return await call_next(request)

    client_host = request.client.host if request.client else None
    forwarded = request.headers.get("x-forwarded-for")
    if access.is_local_client(client_host, forwarded):
        return await call_next(request)

    supplied = request.headers.get("x-access-token") or request.query_params.get("token")
    if access.token_matches(supplied, _ACCESS_TOKEN):
        return await call_next(request)

    if request.method == "GET" and path.count("/") == 3 and path.startswith("/api/plan/"):
        plan_id = path.rsplit("/", 1)[-1]
        if access.verify_share(plan_id, request.query_params.get("share"), _SHARE_SECRET):
            return await call_next(request)

    return JSONResponse(
        {"detail": "这个服务需要访问口令。请向提供者索取，或在浏览器里输入。"},
        status_code=401,
    )


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    """入口 HTML 不缓存：前端重新构建后文件名会变，缓存旧入口会白屏。"""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith("index.html"):
        response.headers["Cache-Control"] = "no-store"
    return response


class RefineRequest(BaseModel):
    date: date
    feedback: str = ""


class ResumeRequest(BaseModel):
    # 预算超支用 accept/downgrade/abort；
    # 协商僵持时这里是用户选中的倾向文本，两种都在同一个恢复入口
    decision: str


class RollbackRequest(BaseModel):
    version: int


# interrupt/resume 需要 checkpointer。这里落在磁盘快照上：
# 服务重启后未确认的规划仍可恢复；已完成的结果在 SQLite 里。
_CHECKPOINTER = PersistentSaver(DATA_DIR / "checkpoints.pkl")


def _sse(event: str, data: dict, *, event_id: str | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _run_graph(graph, config: dict, graph_input, plan_id: str, brief: TripBrief, source_kind: str = "mock", max_runtime_s: int = 180):
    """Stream, persist, and replay one normalized event sequence per run."""
    import asyncio
    trace: list[dict] = []
    final: dict = {}
    prior_events = store.list_events(plan_id)
    seq_start = (prior_events[-1]["seq"] + 1) if prior_events else 1
    factory = EventFactory(
        plan_id,
        seq_start=seq_start,
        prior_events=prior_events,
    )

    def publish(event) -> str:
        payload = event.model_dump(mode="json")
        store.append_event(payload)
        return _sse("run_event", payload, event_id=f"{plan_id}:{event.seq}")

    lifecycle = "run_started" if seq_start == 1 else "run_resumed"
    yield publish(factory.make(lifecycle, actor="system", payload={"plan_id": plan_id}))
    try:
        async with asyncio.timeout(max_runtime_s):
            async for mode, payload in graph.astream(
                graph_input, config=config, stream_mode=["custom", "values"]
            ):
                if mode == "custom":
                    trace.append(payload)
                    for event in factory.from_custom(payload):
                        yield publish(event)
                else:
                    final = payload
    except Exception as e:  # 任何未预期异常都要让前端收到，而不是静默断开
        message = f"规划过程异常：{e}"
        store.save_run(plan_id, "failed", {"message": message})
        yield publish(
            factory.make(
                "run_failed",
                actor="system",
                payload={"plan_id": plan_id, "message": message},
            )
        )
        return

    snapshot = await graph.aget_state(config)
    pending = [i for task in (snapshot.tasks or []) for i in task.interrupts]
    if pending:
        info = dict(pending[0].value or {})
        info["plan_id"] = plan_id
        info["revision"] = final.get("revision", 0)
        negotiation = final.get("negotiation")
        if negotiation is not None:
            info["negotiation"] = negotiation.model_dump(mode="json")
        activity = final.get("log") or []
        info["activity"] = [e.model_dump(mode="json") for e in activity]
        store.save_run(
            plan_id,
            "interrupted",
            {
                "interrupt": info,
                "revision": info["revision"],
                "created_at": None,
            },
        )
        yield publish(
            factory.make(
                "run_interrupted",
                actor="system",
                payload={
                    "plan_id": plan_id,
                    "interrupt": info,
                    "interrupt_type": info.get("type"),
                    "revision": info["revision"],
                },
            )
        )
        return

    if final.get("draft") is None or final.get("critique") is None or final.get("blocked"):
        reasons = final.get("peer_state", {}).get("warnings", [])
        store.save_run(plan_id, "failed", {"message": "专业团队未产出可交付结果", "reasons": reasons})
        yield publish(
            factory.make(
                "run_failed",
                actor="system",
                payload={
                    "plan_id": plan_id,
                    "message": "专业团队未产出可交付结果",
                    "reasons": reasons,
                },
            )
        )
        return

    itinerary: Itinerary = final["draft"]
    critique: Critique = final["critique"]
    if not critique.passed:
        blockers = [issue.message for issue in critique.issues if issue.severity == "blocker"]
        store.save_run(
            plan_id,
            "failed",
            {
                "message": "终局硬约束未通过，未保存为可交付行程。",
                "blockers": blockers,
                "critique": critique.model_dump(mode="json"),
            },
        )
        yield publish(
            factory.make(
                "run_failed",
                actor="system",
                payload={
                    "plan_id": plan_id,
                    "message": "终局硬约束未通过，未保存为可交付行程。",
                    "blockers": blockers,
                    "critique": critique.model_dump(mode="json"),
                },
            )
        )
        return
    team_session = final.get("peer_state") or {}
    if team_session.get("architecture") == "professional_team":
        context = team_session.get("context") or {}
        if (final.get("approved_version") != context.get("plan_version")
                or final.get("approved_version") is None or not final.get("document")):
            message = "方案尚未完成版本评审或 Writer 整合，拒绝交付"
            store.save_run(plan_id, "failed", {"message": message})
            yield publish(factory.make("run_failed", actor="system", payload={"plan_id": plan_id, "message": message}))
            return
    revision = final.get("revision", 0)
    decision = final.get("decision")
    negotiation = final.get("negotiation")

    if decision == "accept":
        itinerary.warnings.append("你已确认接受超出预算的方案")
    elif decision == "abort":
        itinerary.warnings.append("你已选择取消，此行程仅供参考")
    elif decision == "downgrade":
        itinerary.warnings.append("已按你的选择降配重排")

    activity = [e.model_dump(mode="json") for e in (final.get("log") or [])]
    result = {
        "plan_id": plan_id,
        "brief": brief.model_dump(mode="json"),
        "itinerary": itinerary.model_dump(mode="json"),
        "critique": critique.model_dump(mode="json"),
        "revision": revision,
        "decision": decision,
        "negotiation": negotiation.model_dump(mode="json") if negotiation else None,
        "activity": activity,
        "document": final.get("document"),
        "team_tasks": final.get("team_tasks") or {},
        "approved_version": final.get("approved_version"),
        "errors": final.get("errors") or [],
        "summary": summary.build_summary(itinerary, source_kind),
        # 只读分享用的签名：拿到它就能看这一份行程，但动不了别的
        "share": access.share_signature(plan_id, _SHARE_SECRET),
    }
    # 先落库再发完成事件：否则前端收到 run_completed 立刻刷新分享链接，
    # 可能读到还没写入的记录（404）或上一版结果。
    if decision == "downgrade":
        source, note = "downgrade", "按你的选择降配重排"
    elif seq_start > 1:
        source, note = "resume", "中断后继续执行"
    else:
        source, note = "initial", ""
    if team_session.get("architecture") == "professional_team":
        trace.append({
            "phase": "team_deliverable",
            "itinerary": itinerary.model_dump(mode="json"),
            "document": result["document"],
            "team_tasks": result["team_tasks"],
            "approved_version": result["approved_version"],
        })
    version = store.save(
        plan_id,
        brief.model_dump(mode="json"),
        itinerary.model_dump(mode="json"),
        critique.model_dump(mode="json"),
        trace,
        revision,
        negotiation.model_dump(mode="json") if negotiation else None,
        activity,
        source=source,
        note=note,
    )
    result["version"] = version
    yield publish(
        factory.make(
            "run_completed",
            actor="system",
            payload={
                "revision": revision,
                "version": version,
                "passed": critique.passed,
                "total_cost": itinerary.total_cost,
                "result": result,
            },
        )
    )


async def _plan_stream(brief: TripBrief, plan_id: str, cfg: AppSettings):
    # 请求级模式优先于全局配置：普通请求默认 fast，peer 必须由用户显式选择。
    cfg.orchestration.agent_mode = brief.planning_mode
    graph = build(make_source(cfg.datasource), cfg, checkpointer=_CHECKPOINTER)
    config = {"configurable": {"thread_id": plan_id}}
    async for chunk in _run_graph(
        graph, config, {"brief": brief, "revision": 0, "errors": []}, plan_id, brief,
        cfg.datasource.kind,
        cfg.orchestration.max_runtime_s,
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


@app.get("/api/run/{run_id}")
async def get_run(run_id: str):
    """失败或中断的运行摘要：页面刷新后据此恢复原因/确认面板。"""
    record = store.load_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return record


@app.get("/api/plan/{plan_id}")
async def get_plan(plan_id: str):
    record = store.load(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail="规划不存在")
    deliverable = next((row for row in reversed(record.get("trace") or [])
                        if row.get("phase") == "team_deliverable"
                        and row.get("itinerary") == record["itinerary"]), None)
    if deliverable:
        record.update({key: deliverable.get(key) for key in ("document", "team_tasks", "approved_version")})
    record["events"] = store.list_events(plan_id)
    record["version"] = store.current_version(plan_id)
    record["summary"] = summary.build_summary(
        Itinerary(**record["itinerary"]), config_store.load().datasource.kind
    )
    return record


@app.get("/api/plan/{plan_id}/events")
async def get_plan_events(plan_id: str, after_seq: int = 0):
    """增量拉取事件，用于断点续看与回放。"""
    if after_seq < 0:
        raise HTTPException(status_code=400, detail="after_seq 不能小于 0")
    events = store.list_events(plan_id, after_seq)
    return {"run_id": plan_id, "events": events, "last_seq": events[-1]["seq"] if events else after_seq}


@app.post("/api/plan/{plan_id}/resume")
async def resume_plan(plan_id: str, body: ResumeRequest):
    """恢复一次因预算超支而暂停的规划。"""
    record = store.load(plan_id)
    brief_dict = record["brief"] if record else None

    # 中断时还没落库，因此从 checkpointer 里取原始 brief
    cfg = config_store.load()
    config = {"configurable": {"thread_id": plan_id}}
    probe_graph = build(make_source(cfg.datasource), cfg, checkpointer=_CHECKPOINTER)
    snapshot = await probe_graph.aget_state(config)
    if not snapshot.next:
        raise HTTPException(status_code=400, detail="该规划没有待确认的中断")

    if brief_dict:
        brief = TripBrief(**brief_dict)
    else:
        raw = snapshot.values.get("brief")
        if raw is None:
            raise HTTPException(status_code=400, detail="找不到该规划的原始需求")
        brief = raw
    cfg.orchestration.agent_mode = brief.planning_mode
    graph = build(make_source(cfg.datasource), cfg, checkpointer=_CHECKPOINTER)

    async def stream():
        async for chunk in _run_graph(
            graph, config, Command(resume=body.decision), plan_id, brief,
            cfg.datasource.kind,
            cfg.orchestration.max_runtime_s,
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
        client = LLMClient(cfg.llm, cfg.orchestration.node_timeout_s, budget=None)
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
    if not new_critique.passed:
        blockers = [i.message for i in new_critique.issues if i.severity == "blocker"]
        raise HTTPException(
            status_code=409,
            detail="重排后的方案未通过硬校验，未保存：" + "；".join(blockers),
        )

    store.save(
        plan_id,
        brief.model_dump(mode="json"),
        new_itinerary.model_dump(mode="json"),
        new_critique.model_dump(mode="json"),
        record["trace"]
        + [{"phase": "refine", "node": "planner", "target": str(target),
            "feedback": body.feedback, "source": refine_source}],
        record.get("revision", 0),
        # 单日重排不该抹掉协商轨迹与既有事件投影
        record.get("negotiation"),
        record.get("activity"),
        source="refine",
        note=f"重排 {target}：{body.feedback or '无具体意见'}",
    )
    return {
        "plan_id": plan_id,
        "itinerary": new_itinerary.model_dump(mode="json"),
        "critique": new_critique.model_dump(mode="json"),
        "revision": record.get("revision", 0),
        "version": store.current_version(plan_id),
        "refine_source": refine_source,
        "summary": summary.build_summary(new_itinerary, cfg.datasource.kind),
    }


@app.get("/api/plan/{plan_id}/versions")
async def list_plan_versions(plan_id: str):
    if store.load(plan_id) is None:
        raise HTTPException(status_code=404, detail="规划不存在")
    return {
        "plan_id": plan_id,
        "current": store.current_version(plan_id),
        "versions": store.list_versions(plan_id),
    }


@app.get("/api/plan/{plan_id}/versions/{version}")
async def get_plan_version(plan_id: str, version: int):
    record = store.load_version(plan_id, version)
    if record is None:
        raise HTTPException(status_code=404, detail="该版本不存在")
    record["plan_id"] = plan_id
    return record


@app.post("/api/plan/{plan_id}/rollback")
async def rollback_plan(plan_id: str, body: RollbackRequest):
    """回滚到历史版本。回滚本身也记一个新版本，历史不被覆盖。"""
    record = store.load(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail="规划不存在")
    target = store.load_version(plan_id, body.version)
    if target is None:
        raise HTTPException(status_code=404, detail="该版本不存在")

    new_version = store.save(
        plan_id,
        record["brief"],
        target["itinerary"],
        target["critique"],
        record["trace"] + [{"phase": "rollback", "to": body.version}],
        record.get("revision", 0),
        target["negotiation"] if target["negotiation"] is not None else record.get("negotiation"),
        record.get("activity"),
        source="rollback",
        note=f"回滚到 v{body.version}",
    )
    return {
        "plan_id": plan_id,
        "version": new_version,
        "rolled_back_to": body.version,
        "itinerary": target["itinerary"],
        "critique": target["critique"],
        "summary": summary.build_summary(
            Itinerary(**target["itinerary"]), config_store.load().datasource.kind
        ),
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


def _reject_unsafe_endpoint(base_url: str) -> str | None:
    """挡住指向内网/回环的接口地址，避免把服务器当探测器用。

    只对非本机调用方生效：本机开发时要指向 127.0.0.1 的假服务验证链路。
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        return "接口地址必须以 http:// 或 https:// 开头"
    host = parsed.hostname
    if not host:
        return "接口地址缺少主机名"
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443)
    except OSError:
        return f"无法解析主机 {host}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return "接口地址指向内网或本机，出于安全考虑不允许"
    return None


@app.post("/api/config/test")
async def test_config(request: Request, body: dict | None = None):
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

    client_host = request.client.host if request.client else None
    if not access.is_local_client(client_host, request.headers.get("x-forwarded-for")):
        unsafe = _reject_unsafe_endpoint(cfg.llm.base_url)
        if unsafe:
            return {"ok": False, "error": unsafe}

    client = LLMClient(cfg.llm, cfg.orchestration.node_timeout_s)
    try:
        reply = await client.ping()
    except LLMError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "reply": reply, "enabled": cfg.llm.enabled}


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.on_event("startup")
async def _announce_access():
    # 只打印路径，不打印口令本身——口令不进日志。
    print(f"[access] 外部访问口令文件：{access.ACCESS_TOKEN_PATH}")
    print(f"[access] 分享签名密钥文件：{access.SHARE_SECRET_PATH}")


# 生产模式：单端口同时提供 UI 与 API，必须放在所有 API 路由之后
_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
