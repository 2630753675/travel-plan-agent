"""专业团队的任务执行器：LLM 主控决定拆解/返工，代码执行依赖与交付权限。

任务表、版本绑定和工具权限是确定性协议；任务内容、专业选择、拒绝理由及文案来自真实模型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import json

from ..llm import LLMClient
from ..models import Itinerary, TripBrief
from ..tools.base import TravelSource
from . import roles
from .bus import Bus, Envelope
from .runtime import run_turn
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
    session: dict = field(default_factory=dict)


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
        "itinerary_version": ctx.itinerary_version,
        "approved_version": ctx.approved_version,
        "document": ctx.document,
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
    ctx.itinerary_version = raw.get("itinerary_version")
    ctx.approved_version = raw.get("approved_version")
    ctx.document = raw.get("document")
    return ctx


def _validate_tasks(raw: object) -> dict[str, dict]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("主控必须交付非空 tasks 列表")
    tasks = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("任务必须是对象")
        tid, owner = str(item.get("id") or ""), item.get("owner")
        if not tid or tid in tasks or owner not in {"logistics", "local_expert"} or not item.get("goal"):
            raise ValueError("任务需要唯一 id、专业负责人和具体 goal")
        deps = item.get("depends_on") or []
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ValueError("depends_on 必须是任务编号数组")
        tasks[tid] = {"id": tid, "owner": owner, "goal": str(item["goal"]), "depends_on": deps, "status": "pending", "delivery": None, "rework": []}
    if {t["owner"] for t in tasks.values()} != {"logistics", "local_expert"}:
        raise ValueError("本产品行程必须有交通住宿和本地体验任务")
    remaining = set(tasks)
    resolved = set()
    while remaining:
        ready = {tid for tid in remaining if set(tasks[tid]["depends_on"]) <= resolved}
        if not ready:
            raise ValueError("任务依赖存在未知编号或循环")
        remaining -= ready
        resolved |= ready
    for task in tasks.values():
        if task["owner"] == "local_expert" and not any(tasks[d]["owner"] == "logistics" for d in task["depends_on"]):
            raise ValueError("每日活动必须依赖交通住宿，避免在航班尚未选定时排程")
    return tasks


def _request_rework(tasks: dict[str, dict], requests: object) -> set[str]:
    if not isinstance(requests, list) or not requests:
        raise ValueError("返工必须列出 rework 数组和原任务编号")
    affected = set()
    for request in requests:
        tid = request.get("task_id") if isinstance(request, dict) else None
        if tid not in tasks or not request.get("reason"):
            raise ValueError("返工必须引用已存在任务和具体 reason")
        tasks[tid]["rework"].append(str(request["reason"]))
        affected.add(tid)
    changed = True
    while changed:
        new = {tid for tid, task in tasks.items() if set(task["depends_on"]) & affected} - affected
        changed = bool(new)
        affected |= new
    for tid in affected:
        tasks[tid]["status"] = "pending"
        tasks[tid]["delivery"] = None
    return affected


def _delivery_valid(owner: str, delivery: object, ctx: ToolContext, brief: TripBrief) -> bool:
    if not isinstance(delivery, dict) or not delivery.get("rationale"):
        return False
    if owner == "logistics":
        out, back = ctx.transport.chosen_outbound, ctx.transport.chosen_inbound
        return bool(out and back
                    and delivery.get("outbound_flight_no") == out.flight_no
                    and delivery.get("inbound_flight_no") == back.flight_no
                    and (brief.days == 1 or (ctx.hotel_id and delivery.get("hotel_id") == ctx.hotel_id)))
    days = delivery.get("days")
    if not isinstance(days, list) or ctx.itinerary is None or ctx.itinerary_version != ctx.plan_version:
        return False
    try:
        assignments = {date.fromisoformat(d["date"]): d["poi_ids"] for d in days}
    except (KeyError, TypeError, ValueError):
        return False
    return len(days) == brief.days and assignments == ctx.assignment and len(assignments) == brief.days


def _writer_valid(document: object, ctx: ToolContext) -> bool:
    if not isinstance(document, dict) or not document.get("title") or not document.get("overview"):
        return False
    days = document.get("days")
    return bool(ctx.itinerary and isinstance(days, list)
                and [d.get("date") for d in days if isinstance(d, dict)] == [str(d.date) for d in ctx.itinerary.days]
                and all(isinstance(d.get("summary"), str) and d["summary"].strip() for d in days))


async def run_session(*, brief: TripBrief, source: TravelSource, client: LLMClient, emit,
                      max_turns: int = DEFAULT_MAX_TURNS, max_rounds: int = DEFAULT_MAX_ROUNDS,
                      resume: dict | None = None, user_answer: str = "", finalize_feedback: str = "") -> PeerResult:
    if resume:
        if resume.get("architecture") != "professional_team":
            raise ValueError("旧版对等会话无法作为专业团队会话恢复，请重新提交需求")
        bus = Bus.from_dict(resume.get("bus"))
        ctx = _restore_context(brief, source, resume.get("context") or {})
        tasks = dict(resume.get("tasks") or {})
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
    accepted_risk = False
    active_task = None
    proposal_id = (resume or {}).get("proposal_id")
    review_version = (resume or {}).get("review_version")
    pending_rejections = list((resume or {}).get("pending_rejections") or [])

    def post(sender, to, kind, text, payload=None, reply_to=None):
        env = bus.post(Envelope(sender=sender, recipients=list(to), kind=kind, content=text,
                                payload=payload or {}, reply_to=reply_to, round=reviews))
        emit("decide", agent=sender, kind=kind, to=list(to), text=text,
             message_id=env.mid, reply_to=reply_to, payload=payload or {})
        return env

    if user_answer:
        post("user", ["orchestrator"], "note", f"用户答复：{user_answer}")
        for task in tasks.values():
            if task["status"] == "running":
                task["status"] = "pending"
        if user_answer.startswith("accept_risk"):
            # 用户明确接受未解决评审项：允许 Writer 整合当前版本，但保留风险提示。
            ctx.approved_version = ctx.plan_version
            ctx.document = None
            accepted_risk = True
        elif user_answer.startswith("revise"):
            # 用户选择再改一轮：给一次额外复审额度。
            reviews = max(0, reviews - 1)
    if finalize_feedback:
        post("hard_constraint_workflow", ["orchestrator"], "objection", finalize_feedback)
        ctx.approved_version = None
        ctx.document = None
    if not resume:
        post("user", ["orchestrator"], "request", json.dumps(brief.model_dump(mode="json"), ensure_ascii=False))
    next_agent = "writer" if accepted_risk else "orchestrator"

    def schedule():
        for task in tasks.values():
            if task["status"] == "pending" and all(tasks[d]["status"] == "delivered" for d in task["depends_on"]):
                return task["owner"], task["id"]
        return "orchestrator", None

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
        prompt += "\n任务状态：" + json.dumps(tasks, ensure_ascii=False)
        prompt += f"\n当前任务：{active_task}；待审版本：{review_version}；已批准版本：{ctx.approved_version}"
        if agent == "local_expert":
            prompt += "\n交通住宿依赖交付事实：" + json.dumps({
                "outbound": ctx.transport.chosen_outbound.model_dump(mode="json") if ctx.transport.chosen_outbound else None,
                "inbound": ctx.transport.chosen_inbound.model_dump(mode="json") if ctx.transport.chosen_inbound else None,
                "hotel": next((h.model_dump(mode="json") for h in ctx.lodging.candidates if h.name == ctx.hotel_id), None),
            }, ensure_ascii=False)
        if inbox:
            prompt += "\n本回合收件：" + json.dumps([m.to_dict() for m in inbox], ensure_ascii=False)
        turn = await run_turn(agent=agent, system=role.system, prompt=prompt, tools=role.tools,
                              ctx=ctx, bus=bus, client=client, emit=emit, max_steps=role.max_steps,
                              history=histories.get(agent))
        histories[agent] = turn.history
        turns += 1
        trace.append({"agent": agent, "task_id": active_task, "ok": turn.ok, "error": turn.error,
                      "tools": turn.tool_calls, "decision": turn.decision, "plan_version": ctx.plan_version})
        if not turn.ok:
            warnings.append(f"{role.title} 执行失败：{turn.error}")
            emit("turn_failed", agent=agent, error=turn.error)
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
                elif decision.get("rework"):
                    from copy import deepcopy
                    candidate_tasks = deepcopy(tasks)
                    affected = _request_rework(candidate_tasks, decision["rework"])
                    # Critic 指明的每个责任方都必须被主控实际重新派工。
                    owners = {candidate_tasks[tid]["owner"] for tid in affected}
                    if pending_rejections and not {c["owner"] for c in pending_rejections} <= owners:
                        raise ValueError("主控返工遗漏 Critic 指定责任方")
                    tasks = candidate_tasks
                    pending_rejections = []
                    ctx.approved_version, ctx.document = None, None
                elif pending_rejections:
                    raise ValueError("必须按 Critic 的 changes 返回 rework，不能直接再次送审")
                if any(t["status"] != "delivered" for t in tasks.values()):
                    next_agent, active_task = schedule()
                    if active_task is None:
                        raise ValueError("任务无法就绪，检查依赖或先返工失败任务")
                    post("orchestrator", [next_agent], "request", tasks[active_task]["goal"], {"task": tasks[active_task]})
                else:
                    if reviews >= max_rounds:
                        changes = pending_rejections or []
                        detail = "；".join(
                            f"{c.get('owner')}：{c.get('problem')} → {c.get('required_change')}"
                            for c in changes if isinstance(c, dict)
                        ) or "评审仍有未解决项"
                        escalation = {
                            "from": "critic_agent",
                            "question": f"评审连续 {max_rounds} 轮未通过：{detail}。你希望怎么处理？",
                            "options": [
                                {"value": "revise", "label": "再调整一轮"},
                                {"value": "accept_risk", "label": "按当前方案出稿，并标注未解决项"},
                            ],
                            "say": detail,
                        }
                        emit("escalated", agent="critic_agent", question=escalation["question"])
                        break
                    # 由真实主控明确提交，而非专业 Agent 一交付就假定通过。
                    if kind != "proposal" or "critic_agent" not in (decision.get("to") or []):
                        raise ValueError("所有任务已交付，请主控以 proposal 通知 critic_agent 评审")
                    check = execute(ctx, "check_plan", {})
                    reviews += 1
                    bus.round = reviews
                    review_version = ctx.plan_version
                    proposal = post("orchestrator", ["critic_agent"], "proposal", "提交专业团队合并方案供独立评审", {"version": review_version, "hard_check": check, "tasks": tasks})
                    proposal_id = proposal.mid
                    next_agent, active_task = "critic_agent", None
            elif agent in {"logistics", "local_expert"}:
                if kind in {"objection", "request"}:
                    if active_task:
                        tasks[active_task]["status"] = "pending"
                        tasks[active_task]["rework"].append(decision["say"])
                    post(agent, ["orchestrator"], kind, decision["say"], {"task_id": active_task, "changes": decision.get("changes") or []})
                    next_agent, active_task = "orchestrator", None
                    continue
                if not active_task or kind != "proposal" or not _delivery_valid(agent, decision.get("delivery"), ctx, brief):
                    raise ValueError("专业交付必须用 proposal，并且 delivery 与实际工具选择/完整日期一致")
                tasks[active_task]["status"] = "delivered"
                tasks[active_task]["delivery"] = {"version": ctx.plan_version, **decision["delivery"]}
                post(agent, ["orchestrator"], "proposal", f"任务 {active_task} 已交付", {"task": tasks[active_task]})
                # 依赖 DAG 已由主控定义，交付后按依赖直接调度下一个任务，
                # 不再为“批准下一步”额外消耗一轮主控模型调用。
                next_owner, next_task = schedule()
                if next_task:
                    next_agent, active_task = next_owner, next_task
                    post("orchestrator", [next_owner], "request", tasks[next_task]["goal"], {"task": tasks[next_task]})
                else:
                    next_agent, active_task = "orchestrator", None
            elif agent == "critic_agent":
                if ctx.plan_version != review_version:
                    raise ValueError("评审期间方案版本发生变化，必须重新提交")
                required_tools = {"get_plan", "check_plan", "day_costs", "experience_check"}
                if not required_tools <= set(turn.tool_calls):
                    raise ValueError("独立评审必须自行读取方案并核对费用、硬约束、体验证据")
                check = execute(ctx, "check_plan", {})
                if kind == "accept":
                    if decision.get("changes") or decision.get("approved_version") != review_version or not check.get("passed"):
                        raise ValueError("评审接受必须版本一致、没有待办修改且硬约束通过")
                    ctx.approved_version = review_version
                    post(agent, ["writer"], "accept", "当前版本通过独立评审及硬校验", {"approved_version": review_version}, proposal_id)
                    next_agent, active_task = "writer", None
                elif kind == "objection":
                    changes = decision.get("changes")
                    if not isinstance(changes, list) or not changes or any(not isinstance(c, dict) or c.get("owner") not in {"logistics", "local_expert"} or not c.get("problem") or not c.get("required_change") for c in changes):
                        raise ValueError("拒绝必须给出责任方、证据问题和明确返工要求")
                    pending_rejections = changes
                    ctx.approved_version = None
                    post(agent, ["orchestrator"], "objection", "请主控将这些问题打回对应责任任务", {"changes": changes, "version": review_version}, proposal_id)
                    next_agent, active_task = "orchestrator", None
                else:
                    raise ValueError("Critic 必须明确 accept、objection 或 escalate")
            elif agent == "writer":
                if kind != "final" or ctx.approved_version != ctx.plan_version or not _writer_valid(decision.get("document"), ctx):
                    raise ValueError("Writer 只能整合已获批的相同版本，且需覆盖全部日期")
                if "get_plan" not in turn.tool_calls:
                    raise ValueError("Writer 必须读取获批方案后整合")
                ctx.document = {**decision["document"], "approved_version": ctx.approved_version}
                # Writer 无写工具；最终数值方案始终保持原样，再执行硬校验。
                if not execute(ctx, "check_plan", {}).get("passed"):
                    raise ValueError("最终硬校验失败，不交付")
                if accepted_risk:
                    note = "用户确认接受评审未解决项后出稿，相关风险未消除"
                    ctx.itinerary.warnings.append(note)
                    notes = list(ctx.document.get("practical_notes") or [])
                    ctx.document["practical_notes"] = [note, *notes]
                    warnings.append(note)
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
        warnings.append("专业团队尚未完成有效评审与整合，不交付草案")
    session = {
        "architecture": "professional_team", "bus": bus.to_dict(), "context": dump_context(ctx),
        "tasks": tasks, "histories": histories, "trace": trace, "turns": turns,
        "reviews": reviews, "proposal_id": proposal_id, "review_version": review_version,
        "pending_rejections": pending_rejections, "warnings": warnings,
    }
    return PeerResult(bus=bus, ctx=ctx, itinerary=ctx.itinerary if converged else None,
                      escalation=escalation, converged=converged, rounds=reviews, turns=turns,
                      warnings=warnings, trace=trace, blocked=blocked, session=session)
