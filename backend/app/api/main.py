"""FastAPI 服务：SSE 流式规划 + 结果持久化 + 单日重排。

事件协议（前端只依赖这三类）：
- trace：编排过程事件，phase = start | end | edge
- done ：最终结果，含 plan_id 与完整行程
- error：规划失败
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel, Field, ValidationError

from .. import access
from .. import cancellation
from .. import settings as config_store
from ..cancellation import RunCancelled
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

logger = logging.getLogger(__name__)

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
    # 兼容旧字符串；新确认使用 kind + decision + accepted_issue_codes。
    decision: str
    kind: str | None = None
    accepted_issue_codes: list[str] = []
    note: str = ""
    # 可选：本次继续时把模型请求上限提高到该值（1-80），并写回设置。
    # 用于“额度耗尽后继续”，避免用户还要跳到设置页重新保存。
    max_model_calls: int | None = Field(default=None, ge=1, le=80)


class RollbackRequest(BaseModel):
    version: int


# interrupt/resume 需要 checkpointer。这里落在磁盘快照上：
# 服务重启后未确认的规划仍可恢复；已完成的结果在 SQLite 里。
_CHECKPOINTER = PersistentSaver(DATA_DIR / "checkpoints.pkl")


def _sse(event: str, data: dict, *, event_id: str | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _prior_run_stats(plan_id: str) -> dict:
    """恢复运行时沿用同一 run 的累计用量；没有历史记录时从 0 开始。"""
    record = store.load_run(plan_id) or {}
    return (record.get("payload") or {}).get("llm_stats") or {}


def _issue_ledger_view(team_session: dict) -> list[dict]:
    """把 Issue Ledger 裁剪成界面需要的字段，不暴露模型私有上下文。"""
    fields = ("code", "severity", "owner", "status", "acceptance",
              "first_seen_version", "fixed_in_version", "evidence")
    ledger = team_session.get("pending_issue_codes") if isinstance(team_session, dict) else None
    if not isinstance(ledger, list):
        return []
    return [
        {key: item.get(key) for key in fields if key in item}
        for item in ledger
        if isinstance(item, dict) and item.get("code")
    ]


async def _run_graph(
    graph,
    config: dict,
    graph_input,
    plan_id: str,
    brief: TripBrief,
    source_kind: str = "mock",
    max_runtime_s: int = 180,
    cancel_token: cancellation.RunCancellation | None = None,
):
    """Stream, persist, and replay one normalized event sequence per run."""
    import asyncio
    trace: list[dict] = []
    final: dict = {}
    snapshot = None
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
                if cancel_token is not None:
                    cancel_token.raise_if_cancelled()
                if mode == "custom":
                    trace.append(payload)
                    for event in factory.from_custom(payload):
                        yield publish(event)
                else:
                    final = payload
    except RunCancelled:
        # 用户主动取消：保留已完成轨迹与用量，但绝不把半成品当行程交付。
        message = "你已取消本次运行。已产生的执行轨迹和模型用量已保留，未完成方案不会作为行程交付。"
        stats = final.get("llm_stats") or {}
        payload = {
            "plan_id": plan_id,
            "message": message,
            "cancelled": True,
            "llm_stats": stats,
            "recovery_status": "cancelled",
        }
        store.save_run(plan_id, "cancelled", payload)
        yield publish(factory.make("run_needs_input", actor="system", payload=payload))
        return
    except Exception as e:  # 任何未预期异常都要让前端收到，而不是静默断开
        error_type = type(e).__name__
        detail = str(e).strip() or "未提供异常消息"
        message = f"规划过程异常（{error_type}）：{detail}"
        logger.exception("Planning stream failed: run_id=%s", plan_id)
        error_traceback = traceback.format_exc()
        error_snapshot = None
        try:
            error_snapshot = await graph.aget_state(config)
        except Exception:
            logger.exception("Failed to read graph state after error: run_id=%s", plan_id)
        values = error_snapshot.values if error_snapshot is not None else {}
        stats = final.get("llm_stats") or values.get("llm_stats") or {}
        payload = {
            "plan_id": plan_id,
            "message": message,
            "error_type": error_type,
            "llm_stats": stats,
            "recovery_status": "resumable" if error_snapshot is not None and bool(error_snapshot.next) else "non_resumable",
        }
        store.save_run(plan_id, "failed", {**payload, "traceback": error_traceback})
        yield publish(factory.make("run_failed", actor="system", payload=payload))
        return

    try:
        snapshot = await graph.aget_state(config)
    except Exception as e:
        error_type = type(e).__name__
        detail = str(e).strip() or "未提供异常消息"
        message = f"读取运行状态失败（{error_type}）：{detail}"
        logger.exception("Failed to read graph state after stream: run_id=%s", plan_id)
        payload = {"plan_id": plan_id, "message": message, "error_type": error_type}
        store.save_run(plan_id, "failed_non_resumable", payload)
        yield publish(factory.make("run_failed", actor="system", payload=payload))
        return

    stats = final.get("llm_stats") or (snapshot.values.get("llm_stats") if snapshot.values else {}) or {}
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
        info["llm_stats"] = stats
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

    if final.get("peer_stopped"):
        reason = str(final.get("peer_stop_reason") or "本次协作已停止，未形成可验证的完整方案。")
        critique = final.get("critique")
        issues = [
            {"code": issue.code, "message": issue.message, "severity": issue.severity}
            for issue in (critique.issues if critique else [])
        ]
        payload = {
            "plan_id": plan_id,
            "message": reason,
            "issues": issues,
            "blockers": [i["message"] for i in issues if i["severity"] == "blocker"],
            "issue_codes": [i["code"] for i in issues if i["severity"] == "blocker"],
            "llm_stats": stats,
            "recovery_status": "stopped",
            "deliverable": False,
        }
        store.save_run(plan_id, "needs_input", payload)
        yield publish(factory.make("run_needs_input", actor="system", payload=payload))
        return

    if final.get("draft") is None or final.get("critique") is None or final.get("blocked"):
        reasons = final.get("peer_state", {}).get("warnings", [])
        stats = final.get("llm_stats") or {}
        payload = {"message": "专业团队未产出可交付结果", "reasons": reasons, "llm_stats": stats, "recovery_status": "non_resumable"}
        store.save_run(plan_id, "failed_non_resumable", payload)
        yield publish(
            factory.make(
                "run_failed",
                actor="system",
                payload={
                    "plan_id": plan_id,
                    "message": "专业团队未产出可交付结果",
                    "reasons": reasons,
                    "llm_stats": stats,
                    "recovery_status": "non_resumable",
                },
            )
        )
        return

    itinerary: Itinerary = final["draft"]
    critique: Critique = final["critique"]
    if not critique.passed:
        blockers = [issue.message for issue in critique.issues if issue.severity == "blocker"]
        failure_payload = {
            "message": "终局硬约束未通过，未保存为可交付行程。",
            "blockers": blockers,
            "critique": critique.model_dump(mode="json"),
            "llm_stats": stats,
            "recovery_status": "non_resumable",
        }
        store.save_run(plan_id, "failed_non_resumable", failure_payload)
        yield publish(
            factory.make(
                "run_failed",
                actor="system",
                payload={
                    "plan_id": plan_id,
                    "message": "终局硬约束未通过，未保存为可交付行程。",
                    "blockers": blockers,
                    "critique": critique.model_dump(mode="json"),
                    "llm_stats": stats,
                    "recovery_status": "non_resumable",
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
            failure_payload = {"plan_id": plan_id, "message": message, "llm_stats": stats, "recovery_status": "non_resumable"}
            store.save_run(plan_id, "failed_non_resumable", failure_payload)
            yield publish(factory.make("run_failed", actor="system", payload=failure_payload))
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
        "issue_ledger": _issue_ledger_view(team_session),
        "approved_version": final.get("approved_version"),
        "llm_stats": final.get("llm_stats") or {},
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
            "issue_ledger": result["issue_ledger"],
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
                "llm_stats": result["llm_stats"],
                "result": result,
            },
        )
    )


def _require_configured_model(cfg: AppSettings) -> None:
    if not cfg.llm.configured:
        raise HTTPException(
            status_code=400,
            detail="未配置模型：深度团队模式只使用真实模型。请先在设置页填写 base_url、model、api_key 并启用模型。",
        )


async def _plan_stream(brief: TripBrief, plan_id: str, cfg: AppSettings):
    cancel_token = cancellation.begin(plan_id)
    try:
        graph = build(make_source(cfg.datasource), cfg, checkpointer=_CHECKPOINTER, cancellation=cancel_token)
        config = {"configurable": {"thread_id": plan_id}}
        async for chunk in _run_graph(
            graph, config, {"brief": brief, "revision": 0, "errors": []}, plan_id, brief,
            cfg.datasource.kind,
            cfg.orchestration.max_runtime_s,
            cancel_token,
        ):
            yield chunk
    finally:
        # 客户端断开时生成器被关闭，也必须清掉登记，避免残留“可取消”的假运行。
        cancellation.finish(plan_id, cancel_token)


@app.post("/api/plan")
async def create_plan(brief: TripBrief):
    cfg = config_store.load()
    _require_configured_model(cfg)
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
        record.update({
            key: deliverable.get(key)
            for key in ("document", "team_tasks", "issue_ledger", "approved_version")
        })
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
    _require_configured_model(cfg)
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
    # “额度耗尽后继续”允许在恢复请求里直接提高上限，并明确写回设置；
    # 只升不降，避免误把全局预算压小；未传该字段时维持当前设置。
    if body.max_model_calls is not None and body.max_model_calls > cfg.orchestration.max_model_calls:
        cfg.orchestration.max_model_calls = body.max_model_calls
        config_store.save(cfg)
    # 恢复段沿用同一 run 的累计用量：不清零调用数，也不让预算被“续杯”重置。
    prior_stats = _prior_run_stats(plan_id)

    async def stream():
        cancel_token = cancellation.begin(plan_id)
        try:
            graph = build(
                make_source(cfg.datasource), cfg,
                checkpointer=_CHECKPOINTER, cancellation=cancel_token,
                initial_stats=prior_stats,
            )
            async for chunk in _run_graph(
                graph,
                config,
                Command(resume={
                    "kind": body.kind or "legacy_decision",
                    "decision": body.decision,
                    "accepted_issue_codes": body.accepted_issue_codes,
                    "note": body.note,
                }),
                plan_id,
                brief,
                cfg.datasource.kind,
                cfg.orchestration.max_runtime_s,
                cancel_token,
            ):
                yield chunk
        finally:
            cancellation.finish(plan_id, cancel_token)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/plan/{plan_id}/cancel")
async def cancel_plan(plan_id: str):
    """请求取消进行中的运行。

    协作式取消：不硬杀线程里的 HTTP 请求，而是在下一个节点/请求边界停下，
    已产生的轨迹、用量和检查点照常保留，未完成方案不会作为行程交付。
    """
    if not cancellation.request_cancel(plan_id):
        raise HTTPException(status_code=404, detail="该运行不在进行中，无法取消")
    return {
        "ok": True,
        "plan_id": plan_id,
        "message": "已请求取消，当前模型请求返回后停止并保留轨迹",
    }


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
    _require_configured_model(cfg)
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
