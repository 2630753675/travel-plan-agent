"""专业团队的任务执行器：LLM 主控决定拆解/返工，代码执行依赖与交付权限。

任务表、版本绑定和工具权限是确定性协议；任务内容、专业选择、拒绝理由及文案来自真实模型。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
import asyncio
import json
import re

from ..artifacts import default_store
from ..llm import LLMClient
from ..models import Itinerary, TripBrief
from ..tools.base import TravelSource
from . import roles
from .lodging import rooms_for
from .bus import Bus, Envelope
from .runtime import run_turn
from .review import build_review_packet, make_issue_ledger
from .tools_spec import ToolContext, execute

DEFAULT_MAX_TURNS = 10
DEFAULT_MAX_ROUNDS = 2


@dataclass
class PeerResult:
    bus: Bus
    ctx: ToolContext
    itinerary: Itinerary | None
    escalation: dict | None = None
    converged: bool = False
    rounds: int = 0
    turns: int = 0
    warnings: list[str] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    blocked: bool = False
    stopped: bool = False
    stop_reason: str = ""
    session: dict = field(default_factory=dict)


def _task_view(task: dict) -> dict:
    """任务表的可读投影：只给决策需要的状态与交付摘要，不带整份交付内容。"""
    view = {
        "id": task.get("id"),
        "owner": task.get("owner"),
        "status": task.get("status"),
        "depends_on": list(task.get("depends_on") or []),
    }
    delivery = task.get("delivery") or {}
    if delivery:
        summary = {"version": delivery.get("version")}
        if "days" in delivery:
            summary["days"] = len(delivery.get("days") or [])
        for key in ("outbound_flight_no", "inbound_flight_no", "hotel_id", "rooms", "hotel_total"):
            if delivery.get(key) is not None:
                summary[key] = delivery[key]
        view["delivered"] = summary
    if task.get("allowed_repair_tools"):
        view["allowed_repair_tools"] = list(task["allowed_repair_tools"])
    base = task.get("delivery_base")
    if isinstance(base, dict):
        # 差异交付必须引用基线版本；只给版本号，不重复整份交付内容。
        view["delivery_base"] = {"version": base.get("version")}
    return view


def _compact_payload(payload: dict) -> dict:
    """消息 payload 的精简投影。

    原始 payload 往往就是整份决策对象（say/kind/to/...），还会带上整份 ReviewPacket
    和任务表；这些要么与 content 重复，要么已由专门的提示段单独提供。
    """
    keep = {}
    for key in ("version", "approved_version", "evidence_ids", "changes", "escalation",
                "task_id", "issue_ledger", "max_calls", "suggested_max_calls"):
        if key in payload and payload[key] not in (None, [], {}):
            keep[key] = payload[key]
    task = payload.get("task")
    if isinstance(task, dict):
        keep["task"] = _task_view(task)
    return keep


def _compact_message(env: Envelope, content_limit: int = 300) -> dict:
    content = env.content if len(env.content) <= content_limit else env.content[:content_limit] + "…"
    view = {"sender": env.sender, "kind": env.kind, "content": content}
    payload = _compact_payload(env.payload or {})
    if payload:
        view["payload"] = payload
    return view


def _compact_histories(histories: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """会话里只保留恢复时真正会回填的窗口，避免整段历史进入每次 checkpoint。

    裁剪复用 runtime 的完整工具事务规则：assistant.tool_calls 与 tool 结果不会被拆开。
    """
    from .runtime import _complete_history

    return {agent: _complete_history(messages, limit=12) for agent, messages in histories.items()}


# 代码预取：一次并发拉齐专业 Agent 起步就要的事实，避免每个角色各花一轮模型请求去查。
PREFETCH_TOOLS = ("search_flights", "search_hotels", "list_pois", "get_weather")


async def _prefetch_facts(ctx: ToolContext, emit) -> int:
    """并发预取航班/酒店/景点/天气。失败不影响后续流程，角色仍可自行查询。"""
    results = await asyncio.gather(
        *(asyncio.to_thread(execute, ctx, name, {}) for name in PREFETCH_TOOLS),
        return_exceptions=True,
    )
    ok = 0
    for name, result in zip(PREFETCH_TOOLS, results):
        if isinstance(result, Exception) or not (isinstance(result, dict) and result.get("ok")):
            emit("prefetch_failed", tool=name, error=str(result)[:200])
        else:
            ok += 1
    emit("prefetch_done", ok=ok, total=len(PREFETCH_TOOLS))
    return ok


def _prefetch_summary(agent: str, ctx: ToolContext) -> dict:
    """给角色的紧凑事实摘要：只保留做选择需要的字段，不灌原始 JSON。"""
    if agent == "logistics":
        def flight(row):
            return {"flight_no": row.flight_no, "dep": row.dep_time.strftime("%H:%M"),
                    "arr": row.arr_time.strftime("%H:%M"), "price": row.price,
                    "seats": row.seats_left}
        return {
            "outbound": [flight(f) for f in ctx.transport.outbound[:3]],
            "inbound": [flight(f) for f in ctx.transport.inbound[:3]],
            "hotels": [
                {"name": h.name, "price": h.price_per_night, "distance_km": h.distance_to_center_km,
                 "tags": list(h.tags)[:3]}
                for h in ctx.lodging.candidates[:4]
            ],
        }
    if agent == "local_expert":
        return {
            "pois": [
                {"id": p.id, "name": p.name, "duration_min": p.duration_min,
                 "open": f"{p.open_time.strftime('%H:%M')}-{p.close_time.strftime('%H:%M')}",
                 "closed_days": list(p.closed_days),
                 "tags": list(p.tags)[:3]}
                for p in ctx.poi.candidates[:8]
            ],
            "excluded": list(ctx.poi.excluded)[:3],
            "weather": [
                {"date": str(w.date), "rainy": w.is_rainy}
                for w in ctx.weather.days
            ],
        }
    return {}


def _pack_session(session: dict, store) -> dict:
    """把大对象移出图状态：状态里只留工件引用，内容按哈希存到工件库。"""
    refs = {}
    for key in ("histories", "bus", "trace"):
        value = session.get(key)
        if value:
            session[key] = store.put(value)
            refs[key] = session[key]
    context = session.get("context")
    if isinstance(context, dict):
        for key in ("evidence", "operations"):
            value = context.get(key)
            if value:
                context[key] = store.put(value)
                refs[key] = context[key]
    if refs:
        session["artifact_format"] = 1
    return session


def _unpack_session(session: dict, store) -> dict:
    """恢复前把工件引用换回真实对象；取不到就抛错，不能静默当成空会话。"""
    if session.get("artifact_format") != 1:
        return session
    for key in ("histories", "bus", "trace"):
        value = session.get(key)
        if isinstance(value, dict) and value.get("artifact_id"):
            session[key] = store.get(value)
    context = session.get("context")
    if isinstance(context, dict):
        for key in ("evidence", "operations"):
            value = context.get(key)
            if isinstance(value, dict) and value.get("artifact_id"):
                context[key] = store.get(value)
    return session


def dump_context(ctx: ToolContext) -> dict:
    return {
        "transport": ctx.transport.model_dump(mode="json"),
        "lodging": ctx.lodging.model_dump(mode="json"),
        "poi": ctx.poi.model_dump(mode="json"),
        "weather": ctx.weather.model_dump(mode="json"),
        "assignment": {str(k): list(v) for k, v in ctx.assignment.items()},
        "fixed_events": {str(k): [e.model_dump(mode="json") for e in v] for k, v in ctx.fixed_events.items()},
        "hotel_id": ctx.hotel_id,
        "itinerary": ctx.itinerary.model_dump(mode="json") if ctx.itinerary else None,
        "hard_check": ctx.hard_check,
        "plan_version": ctx.plan_version,
        "domain_versions": {
            "transport_version": ctx.transport_version,
            "lodging_version": ctx.lodging_version,
            "assignment_version": ctx.assignment_version,
            "fixed_events_version": ctx.fixed_events_version,
        },
        "itinerary_version": ctx.itinerary_version,
        "approved_version": ctx.approved_version,
        "document": ctx.document,
        "evidence": ctx.evidence,
        "operations": ctx.operations,
    }


def _restore_context(brief: TripBrief, source: TravelSource, raw: dict) -> ToolContext:
    from .lodging import LodgingPlan
    from .poi import PoiPlan
    from .transport import TransportPlan
    from .weather import WeatherPlan

    ctx = ToolContext(brief=brief, source=source)
    ctx.transport = TransportPlan.model_validate(raw.get("transport") or {})
    ctx.lodging = LodgingPlan.model_validate(raw.get("lodging") or {})
    ctx.poi = PoiPlan.model_validate(raw.get("poi") or {})
    ctx.weather = WeatherPlan.model_validate(raw.get("weather") or {})
    ctx.assignment = {date.fromisoformat(k): list(v) for k, v in (raw.get("assignment") or {}).items()}
    from ..models import FixedEvent
    ctx.fixed_events = {
        date.fromisoformat(k): [FixedEvent.model_validate(e) for e in events]
        for k, events in (raw.get("fixed_events") or {}).items()
    }
    ctx.hotel_id = raw.get("hotel_id")
    ctx.itinerary = Itinerary.model_validate(raw["itinerary"]) if raw.get("itinerary") else None
    ctx.hard_check = raw.get("hard_check")
    ctx.plan_version = int(raw.get("plan_version", 0))
    versions = raw.get("domain_versions") or {}
    # 旧会话没有按域版本：缺省为 0，恢复后仍按领域字段是否变化判定冲突。
    ctx.transport_version = int(versions.get("transport_version", 0))
    ctx.lodging_version = int(versions.get("lodging_version", 0))
    ctx.assignment_version = int(versions.get("assignment_version", 0))
    ctx.fixed_events_version = int(versions.get("fixed_events_version", 0))
    ctx.itinerary_version = raw.get("itinerary_version")
    ctx.approved_version = raw.get("approved_version")
    ctx.document = raw.get("document")
    ctx.evidence = dict(raw.get("evidence") or {})
    ctx.operations = dict(raw.get("operations") or {})
    return ctx


def _validate_tasks(raw: object) -> dict[str, dict]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("主控必须交付非空 tasks 列表")
    tasks = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("任务必须是对象")
        tid, owner = str(item.get("id") or ""), item.get("owner")
        if not tid:
            raise ValueError("任务缺少非空 id")
        if tid in tasks:
            raise ValueError(f"任务 id 重复：{tid}")
        if owner not in {"logistics", "local_expert"}:
            raise ValueError(f"任务 {tid} 的 owner 必须是 logistics 或 local_expert；critic_agent 是评审角色，不能作为交付任务负责人")
        if not isinstance(item.get("goal"), str) or not item["goal"].strip():
            raise ValueError(f"任务 {tid} 缺少具体 goal")
        deps = item.get("depends_on") or []
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ValueError(f"任务 {tid} 的 depends_on 必须是任务编号数组")
        tasks[tid] = {"id": tid, "owner": owner, "goal": item["goal"].strip(), "depends_on": deps, "status": "pending", "delivery": None, "rework": []}
    if {t["owner"] for t in tasks.values()} != {"logistics", "local_expert"}:
        raise ValueError("tasks 必须至少包含一项 logistics 交通住宿任务和一项 local_expert 本地体验任务")
    for task in tasks.values():
        unknown = set(task["depends_on"]) - tasks.keys()
        if unknown:
            raise ValueError(f"任务 {task['id']} 依赖不存在的任务：{', '.join(sorted(unknown))}")
    remaining = set(tasks)
    resolved = set()
    while remaining:
        ready = {tid for tid in remaining if set(tasks[tid]["depends_on"]) <= resolved}
        if not ready:
            raise ValueError("任务依赖存在循环：" + ", ".join(sorted(remaining)))
        remaining -= ready
        resolved |= ready
    for task in tasks.values():
        if task["owner"] == "local_expert" and not any(tasks[d]["owner"] == "logistics" for d in task["depends_on"]):
            raise ValueError("每日活动必须依赖交通住宿，避免在航班尚未选定时排程")
    return tasks


def _request_rework(tasks: dict[str, dict], requests: object) -> set[str]:
    if not isinstance(requests, list) or not requests:
        raise ValueError("返工必须列出 rework 数组和原任务编号")
    affected: set[str] = set()
    propagate = False
    for request in requests:
        tid = request.get("task_id") if isinstance(request, dict) else None
        if tid not in tasks or not request.get("reason"):
            raise ValueError("返工必须引用已存在任务和具体 reason")
        tasks[tid]["rework"].append(str(request["reason"]))
        affected.add(tid)
        propagate = propagate or bool(request.get("propagate", True))
    if propagate:
        changed = True
        while changed:
            new = {tid for tid, task in tasks.items() if set(task["depends_on"]) & affected} - affected
            changed = bool(new)
            affected |= new
    for tid in affected:
        if tasks[tid].get("delivery") is not None:
            tasks[tid]["delivery_base"] = deepcopy(tasks[tid]["delivery"])
        tasks[tid]["status"] = "pending"
        tasks[tid]["delivery"] = None
    return affected


def _merge_delivery(task: dict, decision: dict) -> dict:
    """Merge a version-bound delivery patch without mutating its stored baseline."""
    patch = decision.get("delivery_patch")
    if patch is None:
        return decision.get("delivery")
    if decision.get("delivery") is not None:
        raise ValueError("delivery 与 delivery_patch 不能同时提交")
    base = task.get("delivery_base")
    if not isinstance(base, dict) or not isinstance(patch, dict):
        raise ValueError("差异交付必须有已交付基线和对象类型 patch")
    if type(patch.get("base_version")) is not int or patch["base_version"] != base.get("version"):
        raise ValueError("差异交付 base_version 与任务基线不一致")
    fields = {"rationale", "days"} if task["owner"] == "local_expert" else {
        "rationale", "hotel_id", "rooms", "hotel_total", "outbound_flight_no", "inbound_flight_no"
    }
    if set(patch) - fields - {"base_version"}:
        raise ValueError("差异交付包含未知字段")
    if not isinstance(patch.get("rationale"), str) or not patch["rationale"].strip():
        raise ValueError("差异交付必须说明本次修改依据")
    merged = {k: deepcopy(v) for k, v in base.items() if k not in {"version", "evidence_ids"}}
    if "days" in patch:
        rows = patch["days"]
        if not isinstance(rows, list):
            raise ValueError("差异日期必须是数组")
        by_date = {row["date"]: row for row in merged.get("days", [])}
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"date", "poi_ids"}:
                raise ValueError("差异日期必须包含 date 和 poi_ids")
            day = row["date"]
            if not isinstance(day, str) or day not in by_date or day in seen:
                raise ValueError("差异日期未知或重复")
            if not isinstance(row["poi_ids"], list) or not all(isinstance(pid, str) for pid in row["poi_ids"]):
                raise ValueError("poi_ids 必须是字符串数组")
            seen.add(day)
            by_date[day] = deepcopy(row)
        merged["days"] = list(by_date.values())
    merged.update({k: deepcopy(v) for k, v in patch.items() if k not in {"base_version", "days"}})
    return merged


class SandboxConflict(RuntimeError):
    """工作副本与共享上下文版本冲突：不能用副本覆盖更新的方案。"""


# 专业 Agent 的写入域：只有这些字段会从它自己的工作副本合并回共享上下文。
# 目的是让“交通住宿”改不了日程、“本地体验”改不了航班酒店，而不是靠提示词自觉。
DOMAIN_VERSIONS: dict[str, tuple[str, ...]] = {
    "logistics": ("transport_version", "lodging_version"),
    "local_expert": ("assignment_version", "fixed_events_version"),
}

SANDBOX_FIELDS: dict[str, tuple[str, ...]] = {
    "logistics": ("transport", "lodging", "hotel_id", "plan_version",
                  "transport_version", "lodging_version"),
    "local_expert": (
        "poi", "weather", "assignment", "fixed_events", "itinerary", "hard_check",
        "itinerary_version", "approved_version", "document", "plan_version",
        "assignment_version", "fixed_events_version",
    ),
}


def _sandbox_context(ctx: ToolContext) -> ToolContext:
    """专业 Agent 的工作副本。

    证据目录、工具账本与只读缓存与主上下文共享（它们记录的是“发生过什么”，必须全局一致），
    其余方案状态全部拷贝，避免执行中途直接改共享状态。
    """
    sandbox = ToolContext(brief=ctx.brief, source=ctx.source)
    sandbox.transport = ctx.transport.model_copy(deep=True)
    sandbox.lodging = ctx.lodging.model_copy(deep=True)
    sandbox.poi = ctx.poi.model_copy(deep=True)
    sandbox.weather = ctx.weather.model_copy(deep=True)
    sandbox.assignment = {d: list(ids) for d, ids in ctx.assignment.items()}
    sandbox.fixed_events = {d: [e.model_copy(deep=True) for e in events] for d, events in ctx.fixed_events.items()}
    sandbox.hotel_id = ctx.hotel_id
    sandbox.itinerary = ctx.itinerary.model_copy(deep=True) if ctx.itinerary else None
    sandbox.hard_check = deepcopy(ctx.hard_check)
    sandbox.plan_version = ctx.plan_version
    sandbox.transport_version = ctx.transport_version
    sandbox.lodging_version = ctx.lodging_version
    sandbox.assignment_version = ctx.assignment_version
    sandbox.fixed_events_version = ctx.fixed_events_version
    sandbox.itinerary_version = ctx.itinerary_version
    sandbox.approved_version = ctx.approved_version
    sandbox.document = deepcopy(ctx.document)
    sandbox.evidence = ctx.evidence
    sandbox.operations = ctx.operations
    sandbox.cache = ctx.cache
    return sandbox


_SANDBOX_MUTABLE = (
    "transport", "lodging", "poi", "weather", "assignment", "fixed_events", "hotel_id",
    "itinerary", "hard_check", "plan_version", "itinerary_version", "approved_version", "document",
    "transport_version", "lodging_version", "assignment_version", "fixed_events_version",
)


_DOMAIN_FIELDS = ("transport_version", "lodging_version", "assignment_version", "fixed_events_version")


def _task_domains(agent: str, task: dict | None) -> tuple[str, ...]:
    """该任务实际会写哪些领域。

    协议允许任务显式声明 `writes`（例如"固定时段"只写 fixed_events，日程任务只写 assignment），
    这样两个任务可以真正并行；没有声明时退回角色默认域。
    """
    if isinstance(task, dict) and isinstance(task.get("writes"), list):
        declared = tuple(field for field in task["writes"] if field in _DOMAIN_FIELDS)
        if declared:
            return declared
    return DOMAIN_VERSIONS.get(agent, ())


DOMAIN_MODEL_FIELDS = {
    "transport_version": "transport",
    "lodging_version": "lodging",
    "assignment_version": "assignment",
    "fixed_events_version": "fixed_events",
}


def _merge_sandbox(ctx: ToolContext, sandbox: ToolContext, owner: str, *, base_versions: dict[str, int],
                   domains: tuple[str, ...] | None = None) -> list[str]:
    """按角色白名单把工作副本里允许的字段写回共享上下文，返回被忽略的越域字段。

    base_versions 是副本开始时所辖领域的版本。只要其中之一被别的写入改过，就判冲突：
    不覆盖、不部分生效，让调用方把任务退回重做。

    比较与写入都按**任务声明的领域**收敛：只写固定时段的任务不会因为别人改了日程而冲突，
    也不会顺手把日程覆盖掉——这是多任务真并行的前提。
    """
    checked = domains if domains is not None else tuple(base_versions)
    changed = [
        f"{field}=v{getattr(ctx, field)}（副本基于 v{base_versions[field]}）"
        for field in checked
        if field in base_versions and getattr(ctx, field) != base_versions[field]
    ]
    if changed:
        raise SandboxConflict(f"{owner} 的领域版本已变化：" + "、".join(changed))
    allowed = set(SANDBOX_FIELDS.get(owner, ()))
    if domains is not None:
        write_models = {DOMAIN_MODEL_FIELDS[d] for d in domains if d in DOMAIN_MODEL_FIELDS}
        other_models = {m for d, m in DOMAIN_MODEL_FIELDS.items() if d not in domains}
        allowed = {field for field in allowed if field not in (other_models - write_models)}
    ignored: list[str] = []
    for field in _SANDBOX_MUTABLE:
        value = getattr(sandbox, field)
        if value == getattr(ctx, field):
            continue
        if field in allowed:
            setattr(ctx, field, value)
        else:
            ignored.append(field)
    if sandbox.plan_version > ctx.plan_version:
        ctx.plan_version = sandbox.plan_version
    return ignored


def _delivery_change_summary(base: dict, patch: dict, plan_version: int) -> dict:
    """差异交付的人类可读摘要：只记录真正变化的日期/字段，用于界面展示。"""
    fields = []
    if "days" in patch:
        before = {row["date"]: row["poi_ids"] for row in base.get("days") or []}
        for row in patch["days"]:
            day, after = row["date"], row["poi_ids"]
            previous = before.get(day)
            if previous != after:
                fields.append({"kind": "days", "date": day, "before": previous, "after": after})
    for key in ("outbound_flight_no", "inbound_flight_no", "hotel_id", "rooms", "hotel_total"):
        if key in patch and patch[key] != base.get(key):
            fields.append({"kind": key, "before": base.get(key), "after": patch[key]})
    return {
        "base_version": base.get("version"),
        "plan_version": plan_version,
        "fields": fields,
        "rationale": patch.get("rationale", ""),
    }


def _expected_delivery(agent: str, ctx: ToolContext, brief: TripBrief) -> dict:
    """把"当前工具状态对应的交付字段"写进驳回信息，让模型一次改对而不是反复试。"""
    if agent == "logistics":
        out, back = ctx.transport.chosen_outbound, ctx.transport.chosen_inbound
        hotel = next((h for h in ctx.lodging.candidates if h.name == ctx.hotel_id), None)
        rooms = rooms_for(brief.travelers, hotel.room_capacity) if hotel else 0
        nights = max(0, brief.days - 1)
        return {
            "outbound_flight_no": out.flight_no if out else None,
            "inbound_flight_no": back.flight_no if back else None,
            "hotel_id": ctx.hotel_id,
            "rooms": rooms,
            "hotel_total": round(hotel.price_per_night * rooms * nights, 2) if hotel else 0,
        }
    return {"days": [
        {"date": str(day), "poi_ids": list(ctx.assignment.get(day, []))}
        for day in sorted(ctx.assignment)
    ]}


def _effective_delivery(agent: str, delivery: object, ctx: ToolContext, brief: TripBrief) -> tuple[dict, bool]:
    """返回（可信交付, 是否被代码修正）。

    交付字段（航班号、酒店、逐日景点）在服务端是确定事实：写没写入由工具状态决定。
    模型复述错了不该退回重跑——直接按工具状态生成交付，模型只负责 rationale。
    """
    if _delivery_valid(agent, delivery, ctx, brief):
        return delivery, False
    rationale = delivery.get("rationale") if isinstance(delivery, dict) else ""
    derived = {**_expected_delivery(agent, ctx, brief), "rationale": rationale or "按工具事实交付"}
    if _delivery_valid(agent, derived, ctx, brief):
        return derived, True
    raise ValueError(
        "专业交付与工具状态不一致，且工具状态本身不完整（航班/酒店/逐日安排或落定版本缺失）；"
        "请先用工具把方案写完再交付"
    )


def _delivery_valid(owner: str, delivery: object, ctx: ToolContext, brief: TripBrief) -> bool:
    if not isinstance(delivery, dict) or not delivery.get("rationale"):
        return False
    if owner == "logistics":
        out, back = ctx.transport.chosen_outbound, ctx.transport.chosen_inbound
        expected = _expected_delivery("logistics", ctx, brief)
        return bool(out and back
                    and delivery.get("outbound_flight_no") == out.flight_no
                    and delivery.get("inbound_flight_no") == back.flight_no
                    and (brief.days == 1 or (
                        ctx.hotel_id
                        and delivery.get("hotel_id") == ctx.hotel_id
                        and delivery.get("rooms") == expected["rooms"]
                        and abs(float(delivery.get("hotel_total") or -1) - expected["hotel_total"]) < 0.01
                    )))
    days = delivery.get("days")
    if not isinstance(days, list) or ctx.itinerary is None or ctx.itinerary_version != ctx.plan_version:
        return False
    try:
        assignments = {date.fromisoformat(d["date"]): d["poi_ids"] for d in days}
    except (KeyError, TypeError, ValueError):
        return False
    return len(days) == brief.days and assignments == ctx.assignment and len(assignments) == brief.days


def _assemble_document(document: object, ctx: ToolContext) -> dict:
    """按获批行程重建文档：模型只提供叙述，事实（项目与时间）由代码填入。

    Writer 的自由文本无法被通用校验，所以逐日摘要采用"叙述 + 事实"的受控模板：
    日期与项目来自行程本身，模型编不出不存在的时间或项目；未核实事项固定追加。
    """
    raw = document if isinstance(document, dict) else {}
    itinerary = ctx.itinerary
    assert itinerary is not None  # 调用方已确认 approved_version == plan_version
    narratives = {
        str(row.get("date")): str(row.get("summary") or "").strip()
        for row in (raw.get("days") or [])
        if isinstance(row, dict)
    }
    days = []
    for day in itinerary.days:
        date = str(day.date)
        items = [item for item in day.items if item.type != "rest"]
        facts = "；".join(f"{item.start_time.strftime('%H:%M')} {item.title}" for item in items) or "当天没有固定安排"
        narrative = narratives.get(date, "")
        if narrative:
            # 叙述里提到的时间必须真的出现在当天行程里，否则视为编造，直接丢弃叙述。
            allowed = {item.start_time.strftime("%H:%M") for item in items} | {
                item.end_time.strftime("%H:%M") for item in items
            }
            mentioned = set(re.findall(r"\b(?:[01]\d|2[0-3]):[0-5]\d\b", narrative))
            if mentioned - allowed:
                narrative = ""
        summary = f"{narrative}（实际安排：{facts}）" if narrative and narrative not in facts else facts
        days.append({"date": date, "summary": summary})
    notes = [str(note) for note in (raw.get("practical_notes") or []) if isinstance(note, str)]
    for note in (
        "酒店房型、早餐与发票政策未核实，需向酒店确认",
        "景点预约与实时开放时间未核实，请出行前确认",
    ):
        if note not in notes:
            notes.append(note)
    return {
        "title": str(raw.get("title") or "").strip() or f"{ctx.brief.destination}行程计划",
        "overview": str(raw.get("overview") or "").strip() or f"{ctx.brief.days} 天行程，含交通住宿与每日安排。",
        "days": days,
        "practical_notes": notes,
    }


def _writer_valid(document: object, ctx: ToolContext) -> bool:
    if not isinstance(document, dict) or not document.get("title") or not document.get("overview"):
        return False
    days = document.get("days")
    if not (ctx.itinerary and isinstance(days, list)
            and [d.get("date") for d in days if isinstance(d, dict)] == [str(d.date) for d in ctx.itinerary.days]
            and all(isinstance(d.get("summary"), str) and d["summary"].strip() for d in days)):
        return False

    text_parts = [document["title"], document["overview"]]
    text_parts.extend(d["summary"] for d in days)
    notes = document.get("practical_notes") or []
    if isinstance(notes, str):
        text_parts.append(notes)
    elif isinstance(notes, list) and all(isinstance(note, str) for note in notes):
        text_parts.extend(notes)
    else:
        return False
    text = " ".join(text_parts)
    unsupported_claims = (
        "已预订", "已确认", "确认号", "含早餐", "免费取消", "有电梯", "无障碍", "双床房已安排",
        # 没有道路数据时，步行可达属于不可验证的断言。
        "步行可达", "步行即到",
    )
    if any(claim in text and "未核实" not in text and "待确认" not in text for claim in unsupported_claims):
        return False
    return True


async def run_session(*, brief: TripBrief, source: TravelSource, client: LLMClient, emit,
                      max_turns: int = DEFAULT_MAX_TURNS, max_rounds: int = DEFAULT_MAX_ROUNDS,
                      resume: dict | None = None, user_answer: str = "", finalize_feedback: str = "",
                      artifact_store=None) -> PeerResult:
    store = artifact_store or default_store()
    if resume:
        if resume.get("architecture") != "professional_team":
            raise ValueError("旧版对等会话无法作为专业团队会话恢复，请重新提交需求")
        resume = _unpack_session(resume, store)
        bus = Bus.from_dict(resume.get("bus"))
        ctx = _restore_context(brief, source, resume.get("context") or {})
        # 深拷贝：恢复过程会改写任务状态（status/delivery_base/allowed_repair_tools），
        # 不复制就会把调用方手里的会话快照一起改掉。
        tasks = deepcopy(resume.get("tasks") or {})
        histories = dict(resume.get("histories") or {})
        trace = list(resume.get("trace") or [])
        # 每次恢复按新的回合预算执行；否则中断前的消耗会让恢复后几乎没有余量。
        turns = 0
        reviews = int(resume.get("reviews", 0))
    else:
        bus, ctx = Bus(), ToolContext(brief=brief, source=source)
        tasks, histories, trace = {}, {}, []
        turns, reviews = 0, 0
    warnings = []
    escalation = None
    converged = False
    blocked = False
    stopped_reason = ""
    proposal_id = (resume or {}).get("proposal_id")
    review_version = (resume or {}).get("review_version")
    pending_rejections = list((resume or {}).get("pending_rejections") or [])
    pending_issue_codes = list((resume or {}).get("pending_issue_codes") or [])
    review_packet_state = (resume or {}).get("last_review_packet") or None
    pending_agent = (resume or {}).get("pending_agent")
    pending_task = (resume or {}).get("pending_task")
    next_agent = "orchestrator"
    active_task = None

    def post(sender, to, kind, text, payload=None, reply_to=None):
        env = bus.post(Envelope(sender=sender, recipients=list(to), kind=kind, content=text,
                                payload=payload or {}, reply_to=reply_to, round=reviews))
        emit("decide", agent=sender, kind=kind, to=list(to), text=text,
             message_id=env.mid, reply_to=reply_to, payload=payload or {})
        return env

    if user_answer:
        try:
            answer = json.loads(user_answer)
        except (TypeError, json.JSONDecodeError):
            answer = {"decision": user_answer}
        decision = str(answer.get("decision") or "") if isinstance(answer, dict) else user_answer
        accepted_codes = answer.get("accepted_issue_codes") or [] if isinstance(answer, dict) else []
        post("user", ["orchestrator"], "note", f"用户答复：{user_answer}")
        for task in tasks.values():
            if task["status"] == "running":
                task["status"] = "pending"
        if decision == "accept_risk":
            raise ValueError("当前版本不允许绕过 Critic objection 或确定性硬约束；请继续返工或停止")
        elif decision == "continue":
            # 用户只允许继续协作，不改变任何事实或方案选择。
            blocked = False
            if pending_agent:
                next_agent = pending_agent
                active_task = pending_task
                pending_agent = None
                pending_task = None
        elif decision in {"stop", "restart"}:
            # 明确保留未完成状态；不伪造草案，也不把停止误报成系统异常。
            blocked = True
            stopped_reason = (
                "你选择停止，本次协作未形成可验证的完整方案。"
                if decision == "stop"
                else "你选择调整需求后重新规划；当前运行到此结束，未生成新方案。"
            )
        elif decision == "revise":
            decision = "restart"
            stopped_reason = "当前运行已达到复审上限；请调整需求或提高相关预算后重新发起规划。"
    if finalize_feedback:
        post("hard_constraint_workflow", ["orchestrator"], "objection", finalize_feedback)
        ctx.approved_version = None
        ctx.document = None
    if not resume:
        post("user", ["orchestrator"], "request", json.dumps(brief.model_dump(mode="json"), ensure_ascii=False))
    if user_answer and decision in {"stop", "restart"}:
        session = {
            "architecture": "professional_team", "bus": bus.to_dict(), "context": dump_context(ctx),
            "tasks": tasks, "histories": _compact_histories(histories), "trace": trace, "turns": turns,
            "reviews": reviews, "proposal_id": proposal_id, "review_version": review_version,
            "pending_rejections": pending_rejections, "pending_issue_codes": pending_issue_codes, "warnings": warnings,
            "pending_agent": pending_agent, "pending_task": pending_task,
            "last_review_packet": review_packet_state,
        }
        return PeerResult(bus=bus, ctx=ctx, itinerary=None, escalation=None,
                          converged=False, rounds=reviews, turns=turns, warnings=warnings,
                          trace=trace, blocked=True, stopped=True,
                          stop_reason=stopped_reason,
                          session=_pack_session(session, store))
    if pending_agent and not user_answer:
        next_agent = pending_agent
        active_task = pending_task

    def schedule():
        for task in tasks.values():
            if task["status"] == "pending" and all(tasks[d]["status"] == "delivered" for d in task["depends_on"]):
                return task["owner"], task["id"]
        return "orchestrator", None

    def submit_review() -> str:
        """所有专业任务交付后由代码直接提交评审。

        主控的职责是拆解与派工；"把合并方案转交评审"这一步没有任何判断，
        再消耗一次模型请求纯属浪费，所以由代码完成。返回 "escalate" 表示
        评审轮次已用尽、需要用户决策。
        """
        nonlocal reviews, review_version, proposal_id, pending_issue_codes, escalation, review_packet_state
        if reviews >= max_rounds:
            changes = pending_rejections or []
            detail = "；".join(
                f"{c.get('owner')}：{c.get('problem')} → {c.get('required_change')}"
                for c in changes if isinstance(c, dict)
            ) or "评审仍有未解决项"
            escalation = {
                "from": "critic_agent",
                "question": (
                    f"评审连续 {max_rounds} 轮未通过：{detail}。"
                    "继续返工会消耗新的回合；也可以停止并保留问题清单。"
                ),
                "type": "review_rounds_exhausted",
                "options": [
                    {"value": "restart", "label": "调整需求后重新规划"},
                    {"value": "stop", "label": "停止并保留问题清单"},
                ],
                "say": detail,
            }
            emit("escalated", agent="critic_agent", question=escalation["question"])
            return "escalate"
        review_packet = build_review_packet(ctx)
        if not review_packet.get("ok"):
            raise ValueError(f"无法生成 ReviewPacket：{review_packet.get('error')}")
        # 记住本轮评审包：Critic 若因协议被退回重做，收件箱里的 proposal 已经被消费掉了，
        # 没有这份快照它就拿不到评审依据，只能反复报"缺少 ReviewPacket"直到回合耗尽。
        review_packet_state = review_packet
        pending_issue_codes = make_issue_ledger(review_packet, [], pending_issue_codes)
        reviews += 1
        bus.round = reviews
        review_version = ctx.plan_version
        proposal = post("orchestrator", ["critic_agent"], "proposal", "提交结构化 ReviewPacket 供独立评审", {
            "version": review_version,
            "review_packet": review_packet,
            "tasks": tasks,
            "issue_ledger": pending_issue_codes,
        })
        proposal_id = proposal.mid
        emit("review_submitted", version=review_version, rounds=reviews)
        return "review"

    while turns < max_turns:
        agent = next_agent
        role = roles.get(agent)
        if role is None:
            raise ValueError(f"未知团队角色 {agent}")
        if active_task:
            tasks[active_task]["status"] = "running"
        emit("wake", agent=agent, reason=f"执行任务 {active_task}" if active_task else "处理团队交付或评审", unread=bus.unread(agent))
        inbox = bus.take(agent)
        prompt = role.prompt(ctx, bus)
        prompt += "\n任务状态：" + json.dumps({tid: _task_view(t) for tid, t in tasks.items()}, ensure_ascii=False)
        prompt += f"\n当前任务：{active_task}；待审版本：{review_version}；已批准版本：{ctx.approved_version}"
        if pending_rejections:
            # 恢复后收件箱可能已空，必须显式带上待返工问题，否则主控只能盲目再次送审。
            prompt += "\n待返工问题（必须返回 rework 逐项派回责任方）：" + json.dumps(pending_rejections, ensure_ascii=False)
            scoped = [item for item in pending_rejections if item.get("owner") == agent]
            if scoped:
                allowed = sorted({tool for item in scoped for tool in item.get("repair_tools", [])})
                prompt += "\n本次返工允许的工具（服务端将强制限制）：" + "、".join(allowed)
        active_task_state = tasks.get(active_task) if active_task else None
        prefetched = _prefetch_summary(agent, ctx) if agent in {"logistics", "local_expert"} else {}
        if prefetched:
            prompt += "\n服务端已预取的候选事实（直接据此选择，不要重复查询）：" + json.dumps(prefetched, ensure_ascii=False)
        if agent == "local_expert":
            if active_task_state and active_task_state.get("allowed_repair_tools"):
                prompt += "\n本次返工强制工具范围：" + json.dumps(active_task_state["allowed_repair_tools"], ensure_ascii=False)
            prompt += "\n交通住宿依赖交付事实：" + json.dumps({
                "outbound": ctx.transport.chosen_outbound.model_dump(mode="json") if ctx.transport.chosen_outbound else None,
                "inbound": ctx.transport.chosen_inbound.model_dump(mode="json") if ctx.transport.chosen_inbound else None,
                "hotel": next((h.model_dump(mode="json") for h in ctx.lodging.candidates if h.name == ctx.hotel_id), None),
            }, ensure_ascii=False)
        if inbox:
            prompt += "\n本回合收件：" + json.dumps([_compact_message(m) for m in inbox], ensure_ascii=False)
        if agent == "critic_agent":
            latest_proposal = next((m for m in reversed(inbox) if m.kind == "proposal"), None)
            review_packet = (latest_proposal.payload.get("review_packet") if latest_proposal else None)
            if not review_packet:
                # 协议退回重做时收件箱已空：用本轮已提交的评审包快照，而不是给模型一个空对象。
                review_packet = review_packet_state
            prompt += "\n结构化 ReviewPacket（唯一事实来源）：" + json.dumps(review_packet or {}, ensure_ascii=False)
            prompt += "\n当前 Issue Ledger：" + json.dumps(pending_issue_codes, ensure_ascii=False)
        allowed_repair_tools = (
            set(active_task_state.get("allowed_repair_tools") or [])
            if active_task_state and active_task_state.get("allowed_repair_tools") is not None
            else None
        )
        # 专业 Agent 只在自己那份上下文里改；主控/评审/文案没有领域写入，直接用共享上下文。
        task_domains = _task_domains(agent, active_task_state)
        sandbox_base = {field: getattr(ctx, field) for field in task_domains}
        turn_ctx = _sandbox_context(ctx) if agent in SANDBOX_FIELDS else ctx
        turn = await run_turn(agent=agent, system=role.system_text(), prompt=prompt, tools=role.tools,
                              ctx=turn_ctx, bus=bus, client=client, emit=emit, max_steps=role.max_steps,
                              history=histories.get(agent), allowed_tools=allowed_repair_tools)
        histories[agent] = turn.history
        turns += 1
        if turn_ctx is not ctx:
            # 无论本回合是否成功，工具已经产生的真实写入都按域白名单合并回共享上下文；
            # 越域字段直接丢弃，不进入方案。若期间上下文版本已被推进，则不覆盖。
            try:
                ignored_fields = _merge_sandbox(ctx, turn_ctx, agent, base_versions=sandbox_base,
                                                domains=task_domains)
            except SandboxConflict as exc:
                ignored_fields = []
                warnings.append(f"{exc}；本回合结果作废")
                if active_task:
                    tasks[active_task]["status"] = "pending"
                emit("sandbox_conflict", agent=agent, reason=str(exc),
                     base_versions=sandbox_base, current_version=ctx.plan_version)
                trace.append({"agent": agent, "task_id": active_task, "ok": False,
                              "error": str(exc), "error_type": "SandboxConflict",
                              "tools": turn.tool_calls, "decision": {}, "plan_version": ctx.plan_version})
                next_agent, active_task = "orchestrator", None
                continue
            if ignored_fields:
                warnings.append(f"{agent} 的越域写入已忽略：{', '.join(ignored_fields)}")
        trace.append({"agent": agent, "task_id": active_task, "ok": turn.ok, "error": turn.error,
                      "error_type": turn.error_type, "tools": turn.tool_calls,
                      "decision": turn.decision, "plan_version": ctx.plan_version})
        if not turn.ok:
            error_text = turn.error or "未提供异常消息"
            budget_exhausted = turn.error_type == "ModelCallBudgetExceeded"
            current_limit = getattr(client, "max_calls", None)
            at_hard_limit = bool(budget_exhausted and current_limit is not None and current_limit >= 80)
            # 额度耗尽时给出可执行建议值；已经到硬上限 80 时不再建议“提高到 80”，否则就是死循环。
            suggested_limit = (
                None if at_hard_limit else min(80, max(40, (current_limit or 8) * 3))
                if budget_exhausted else None
            )
            reason = (
                f"供应商模型请求预算已耗尽且已达上限 {current_limit} 次。"
                "继续提高上限不可行；请缩小题量/日期范围后重新发起，或停止并保留未完成状态。"
                if at_hard_limit
                else f"供应商模型请求预算已耗尽（当前上限 {current_limit} 次，本次流程通常需要约 24 次）；"
                "可直接提高上限后继续，或停止本次规划。"
                if budget_exhausted
                else f"{role.title} 回合失败：{error_text}"
            )
            warnings.append(reason)
            pending_agent, pending_task = agent, active_task
            options = (
                [{"value": "stop", "label": "停止并保留未完成状态"}]
                if at_hard_limit
                else [
                    {"value": "continue", "label": "提高上限后继续" if budget_exhausted else "重试该 Agent 回合"},
                    {"value": "stop", "label": "停止并保留未完成状态"},
                ]
            )
            escalation = {
                "from": agent,
                "type": "model_call_budget_exhausted" if budget_exhausted else "agent_turn_failed",
                "question": f"{reason} 事实与方案保持不变。",
                "options": options,
                "say": reason,
            }
            if budget_exhausted:
                escalation["max_calls"] = current_limit
                if suggested_limit is not None:
                    escalation["suggested_max_calls"] = suggested_limit
            emit("turn_failed", agent=agent, error=turn.error)
            emit("escalated", agent=agent, question=escalation["question"])
            blocked = True
            break
        decision = turn.decision
        kind = decision.get("kind")
        post(agent, decision.get("to") or ["orchestrator"], kind, decision["say"], decision,
             proposal_id if agent == "critic_agent" else None)
        if kind == "escalate":
            info = decision.get("escalate")
            if not isinstance(info, dict) or not info.get("question") or not info.get("options"):
                post("team_protocol", [agent], "objection", "escalate 必须包含具体 question 和可选 options，请重答")
                continue
            if agent != "orchestrator":
                post(agent, ["orchestrator"], "request", "请协调本角色无法解决的用户取舍", {"escalation": info})
                next_agent, active_task = "orchestrator", None
                continue
            escalation = {"from": agent, **info}
            emit("escalated", agent=agent, question=info["question"])
            break

        try:
            if agent == "orchestrator":
                if not tasks:
                    tasks = _validate_tasks(decision.get("tasks"))
                    # 任务一确定就用代码并发拉齐基础事实，专业 Agent 不必各自花一轮去查。
                    await _prefetch_facts(ctx, emit)
                elif decision.get("rework"):
                    candidate_tasks = deepcopy(tasks)
                    rework_requests = decision["rework"]
                    if pending_rejections:
                        policy_by_owner = {}
                        for issue in pending_rejections:
                            owner = issue.get("owner")
                            policy_by_owner[owner] = policy_by_owner.get(owner, False) or bool(issue.get("propagate", True))
                        rework_requests = [
                            {**request, "propagate": policy_by_owner.get(
                                candidate_tasks.get(request.get("task_id"), {}).get("owner"), True
                            )}
                            for request in rework_requests
                        ]
                    affected = _request_rework(candidate_tasks, rework_requests)
                    # Critic 指明的每个责任方都必须被主控实际重新派工。
                    owners = {candidate_tasks[tid]["owner"] for tid in affected}
                    if pending_rejections and not {c["owner"] for c in pending_rejections} <= owners:
                        raise ValueError("主控返工遗漏 Critic 指定责任方")
                    for task in candidate_tasks.values():
                        if task["owner"] in owners and task["status"] == "pending":
                            scoped_issues = [item for item in pending_rejections if item["owner"] == task["owner"]]
                            if scoped_issues:
                                repair_tools = {
                                    tool for issue in scoped_issues for tool in issue.get("repair_tools", [])
                                }
                                if task["owner"] == "local_expert":
                                    # get_plan 是只读视图，用于取得 fixed_events[].event_id；
                                    # materialize/check 用于落定并复核修复结果，都不是任意写入。
                                    repair_tools.update({"get_plan", "materialize_plan", "check_plan"})
                                task["allowed_repair_tools"] = sorted(repair_tools)
                            else:
                                # 依赖传播导致的返工没有本角色的具体问题：必须清掉上一次的
                                # 工具限制，否则会沿用旧范围、连改日程的工具都被禁掉。
                                task.pop("allowed_repair_tools", None)
                    tasks = candidate_tasks
                    pending_rejections = []
                    ctx.approved_version, ctx.document = None, None
                elif pending_rejections:
                    raise ValueError("必须按 Critic 的 changes 返回 rework，不能直接再次送审")
                if any(t["status"] != "delivered" for t in tasks.values()):
                    next_agent, active_task = schedule()
                    if active_task is None:
                        raise ValueError("任务无法就绪，检查依赖或先返工失败任务")
                    task = tasks[active_task]
                    goal = task["goal"]
                    if task.get("allowed_repair_tools"):
                        scoped = [item for item in pending_issue_codes if item.get("owner") == next_agent and item.get("status") == "open"]
                        goal += "\n返工问题与验收：" + json.dumps(scoped, ensure_ascii=False)
                        goal += "\n只能使用 allowed_repair_tools 指定的工具；修复后必须 materialize_plan 并 check_plan。"
                    post("orchestrator", [next_agent], "request", goal, {"task": task})
                else:
                    # 兜底路径：主控回合恰好发生在“所有任务已交付”时（例如恢复后）。
                    outcome = submit_review()
                    if outcome == "escalate":
                        break
                    next_agent, active_task = "critic_agent", None
            elif agent in {"logistics", "local_expert"}:
                if kind in {"objection", "request"}:
                    if active_task:
                        tasks[active_task]["status"] = "pending"
                        tasks[active_task]["rework"].append(decision["say"])
                    post(agent, ["orchestrator"], kind, decision["say"], {"task_id": active_task, "changes": decision.get("changes") or []})
                    next_agent, active_task = "orchestrator", None
                    continue
                delivery = _merge_delivery(tasks[active_task], decision) if active_task else None
                if not active_task or kind != "proposal":
                    raise ValueError("专业交付必须用 proposal，并说明 rationale")
                delivery, corrected = _effective_delivery(agent, delivery, ctx, brief)
                if corrected:
                    warnings.append(f"{agent} 的 delivery 与工具状态不一致，已按工具状态修正")
                # 把专业交付引用的工具证据转交主控，主控提交评审时据此引用，避免无工具角色凭空造证据。
                delivered_evidence = [
                    item["id"] for item in decision.get("evidence") or []
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                ]
                tasks[active_task]["status"] = "delivered"
                tasks[active_task]["delivery"] = {
                    "version": ctx.plan_version,
                    "evidence_ids": delivered_evidence,
                    **delivery,
                }
                base = tasks[active_task].get("delivery_base")
                if isinstance(base, dict) and isinstance(decision.get("delivery_patch"), dict):
                    summary = _delivery_change_summary(base, decision["delivery_patch"], ctx.plan_version)
                    history = list(tasks[active_task].get("changes") or [])
                    history.append(summary)
                    tasks[active_task]["changes"] = history[-5:]
                tasks[active_task].pop("delivery_base", None)
                # 交付完成后清掉返工工具限制：它只对"这一次返工"有效。
                tasks[active_task].pop("allowed_repair_tools", None)
                post(agent, ["orchestrator"], "proposal", f"任务 {active_task} 已交付",
                     {"task": tasks[active_task], "evidence_ids": delivered_evidence})
                # 依赖 DAG 已由主控定义，交付后按依赖直接调度下一个任务，
                # 不再为“批准下一步”额外消耗一轮主控模型调用。
                next_owner, next_task = schedule()
                if next_task:
                    next_agent, active_task = next_owner, next_task
                    post("orchestrator", [next_owner], "request", tasks[next_task]["goal"], {"task": tasks[next_task]})
                else:
                    # 所有专业任务已交付：代码直接提交评审，不再让主控做一次纯转发。
                    outcome = submit_review()
                    if outcome == "escalate":
                        break
                    next_agent, active_task = "critic_agent", None
            elif agent == "critic_agent":
                if ctx.plan_version != review_version:
                    raise ValueError("评审期间方案版本发生变化，必须重新提交")
                latest_proposal = next((m for m in reversed(inbox) if m.kind == "proposal"), None)
                review_packet = latest_proposal.payload.get("review_packet") if latest_proposal else None
                if not review_packet:
                    review_packet = review_packet_state
                if not isinstance(review_packet, dict) or review_packet.get("packet_id") is None:
                    # 旧会话没有评审包快照时（收件箱已在上一轮消费）不能空转：
                    # 直接按当前方案重新提交评审，让 Critic 重新拿到 packet。
                    outcome = submit_review()
                    if outcome == "escalate":
                        break
                    next_agent, active_task = "critic_agent", None
                    continue
                check = review_packet.get("hard_check") or {}
                # Ledger closure is computed from this packet's deterministic issue list before Critic runs.
                pending_issue_codes = make_issue_ledger(review_packet, [], pending_issue_codes)
                if kind == "accept":
                    # 版本号由服务端决定：模型没填就按待审版本补，不因此退回重跑。
                    if decision.get("approved_version") is None:
                        decision["approved_version"] = review_version
                    # 分开报错：笼统一句话会让模型只能猜哪里不对，白白多跑几轮。
                    if decision.get("changes"):
                        raise ValueError("接受时 changes 必须为空；如有问题请用 objection")
                    if decision.get("approved_version") != review_version:
                        raise ValueError(
                            f"接受时 approved_version 必须等于待审版本 {review_version}，"
                            f"当前为 {decision.get('approved_version')}"
                        )
                    if not check.get("passed"):
                        raise ValueError("硬校验未通过，不能接受")
                    open_issues = [item for item in pending_issue_codes if item.get("status") != "closed"]
                    if open_issues:
                        raise ValueError("Issue Ledger 仍有未关闭问题，不能接受")
                    ctx.approved_version = review_version
                    post(agent, ["writer"], "accept", "当前版本通过结构化评审及硬校验", {"approved_version": review_version}, proposal_id)
                    next_agent, active_task = "writer", None
                elif kind == "objection":
                    changes = decision.get("changes")
                    if not isinstance(changes, list) or not changes:
                        raise ValueError("拒绝必须给出非空结构化 changes")
                    pending_issue_codes = make_issue_ledger(review_packet, changes, pending_issue_codes)
                    pending_rejections = [
                        {
                            "issue_id": item["issue_id"],
                            "code": item["code"],
                            "owner": item["owner"],
                            "problem": item["evidence"],
                            "required_change": item["acceptance"],
                            "acceptance": item["acceptance"],
                            "repair_tools": item["repair_tools"],
                            "propagate": item["propagate_rework"],
                        }
                        for item in pending_issue_codes if item.get("status") == "open"
                    ]
                    ctx.approved_version = None
                    post(agent, ["orchestrator"], "objection", "请按 Issue Ledger 将问题打回对应责任任务", {
                        "changes": pending_rejections,
                        "issue_ledger": pending_issue_codes,
                        "version": review_version,
                    }, proposal_id)
                    next_agent, active_task = "orchestrator", None
                else:
                    raise ValueError("Critic 必须明确 accept、objection 或 escalate")
            elif agent == "writer":
                if kind != "final" or ctx.approved_version != ctx.plan_version or not _writer_valid(decision.get("document"), ctx):
                    raise ValueError("Writer 只能整合已获批的相同版本，且需覆盖全部日期")
                if "get_plan" not in turn.tool_calls:
                    raise ValueError("Writer 必须读取获批方案后整合")
                # 先按事实重建文档，再复核一次：模型叙述不能覆盖行程事实。
                document = _assemble_document(decision.get("document"), ctx)
                if not _writer_valid(document, ctx):
                    raise ValueError("文档与获批行程不一致，请按给定日期与项目重写")
                ctx.document = {**document, "approved_version": ctx.approved_version}
                # Writer 无写工具；最终数值方案始终保持原样，再执行硬校验。
                if not execute(ctx, "check_plan", {}).get("passed"):
                    raise ValueError("最终硬校验失败，不交付")
                converged = True
                emit("converged", rounds=reviews, turns=turns)
                break
        except ValueError as exc:
            post("team_protocol", [agent], "objection",
                 f"{exc}。若已有工具结果可以复用，请直接基于这些结果补正交付字段，不要重复调用成功过的工具。")
            emit("protocol_rejected", agent=agent, error=str(exc))
            # 继续由原角色纠正协议；不能因异常分支已设置 next_agent 而跳到未验收角色。
            next_agent = agent
            continue

    if not escalation and not converged:
        blocked = True
        detail = "协作回合预算已耗尽，专业团队尚未完成评审与整合"
        if ctx.itinerary is not None:
            detail += "；当前草案仍须解决已报告的硬约束问题"
        else:
            detail += "；当前没有完整、可验证的行程草案"
        warnings.append(detail)
        escalation = {
            "from": "orchestrator",
            "question": f"{detail}。请提高本次协作预算后继续，或停止并保留未完成状态。",
            "options": [
                {"value": "continue", "label": "继续协作处理"},
                {"value": "stop", "label": "停止并保留未完成状态"},
            ],
            "say": detail,
        }
        pending_agent, pending_task = "orchestrator", None
        emit("escalated", agent="orchestrator", question=escalation["question"])
    session = {
        "architecture": "professional_team", "bus": bus.to_dict(), "context": dump_context(ctx),
        "tasks": tasks, "histories": _compact_histories(histories), "trace": trace, "turns": turns,
        "reviews": reviews, "proposal_id": proposal_id, "review_version": review_version,
        "pending_rejections": pending_rejections, "pending_issue_codes": pending_issue_codes,
        "pending_agent": pending_agent, "pending_task": pending_task, "warnings": warnings,
        "last_review_packet": review_packet_state,
    }
    return PeerResult(bus=bus, ctx=ctx, itinerary=ctx.itinerary if converged else None,
                      escalation=escalation, converged=converged, rounds=reviews, turns=turns,
                      warnings=warnings, trace=trace, blocked=blocked,
                      session=_pack_session(session, store))
