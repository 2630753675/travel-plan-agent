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
    stopped_reason = ""
    proposal_id = (resume or {}).get("proposal_id")
    review_version = (resume or {}).get("review_version")
    pending_rejections = list((resume or {}).get("pending_rejections") or [])
    pending_issue_codes = list((resume or {}).get("pending_issue_codes") or [])
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
            "tasks": tasks, "histories": histories, "trace": trace, "turns": turns,
            "reviews": reviews, "proposal_id": proposal_id, "review_version": review_version,
            "pending_rejections": pending_rejections, "pending_issue_codes": pending_issue_codes, "warnings": warnings,
        }
        return PeerResult(bus=bus, ctx=ctx, itinerary=None, escalation=None,
                          converged=False, rounds=reviews, turns=turns, warnings=warnings,
                          trace=trace, blocked=True, stopped=True,
                          stop_reason=stopped_reason, session=session)
    if pending_agent and not user_answer:
        next_agent = pending_agent
        active_task = pending_task

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
        if pending_rejections:
            # 恢复后收件箱可能已空，必须显式带上待返工问题，否则主控只能盲目再次送审。
            prompt += "\n待返工问题（必须返回 rework 逐项派回责任方）：" + json.dumps(pending_rejections, ensure_ascii=False)
        if agent == "local_expert":
            prompt += "\n交通住宿依赖交付事实：" + json.dumps({
                "outbound": ctx.transport.chosen_outbound.model_dump(mode="json") if ctx.transport.chosen_outbound else None,
                "inbound": ctx.transport.chosen_inbound.model_dump(mode="json") if ctx.transport.chosen_inbound else None,
                "hotel": next((h.model_dump(mode="json") for h in ctx.lodging.candidates if h.name == ctx.hotel_id), None),
            }, ensure_ascii=False)
        if inbox:
            prompt += "\n本回合收件：" + json.dumps([m.to_dict() for m in inbox], ensure_ascii=False)
        if agent == "critic_agent":
            latest_proposal = next((m for m in reversed(inbox) if m.kind == "proposal"), None)
            review_packet = (latest_proposal.payload.get("review_packet") if latest_proposal else None)
            prompt += "\n结构化 ReviewPacket（唯一事实来源）：" + json.dumps(review_packet or {}, ensure_ascii=False)
            prompt += "\n当前 Issue Ledger：" + json.dumps(pending_issue_codes, ensure_ascii=False)
        turn = await run_turn(agent=agent, system=role.system, prompt=prompt, tools=role.tools,
                              ctx=ctx, bus=bus, client=client, emit=emit, max_steps=role.max_steps,
                              history=histories.get(agent))
        histories[agent] = turn.history
        turns += 1
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
                        break
                    # 由真实主控明确提交，而非专业 Agent 一交付就假定通过。
                    if kind != "proposal" or "critic_agent" not in (decision.get("to") or []):
                        raise ValueError("所有任务已交付，请主控以 proposal 通知 critic_agent 评审")
                    review_packet = build_review_packet(ctx)
                    if not review_packet.get("ok"):
                        raise ValueError(f"无法生成 ReviewPacket：{review_packet.get('error')}")
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
                # 把专业交付引用的工具证据转交主控，主控提交评审时据此引用，避免无工具角色凭空造证据。
                delivered_evidence = [
                    item["id"] for item in decision.get("evidence") or []
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                ]
                tasks[active_task]["status"] = "delivered"
                tasks[active_task]["delivery"] = {
                    "version": ctx.plan_version,
                    "evidence_ids": delivered_evidence,
                    **decision["delivery"],
                }
                post(agent, ["orchestrator"], "proposal", f"任务 {active_task} 已交付",
                     {"task": tasks[active_task], "evidence_ids": delivered_evidence})
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
                latest_proposal = next((m for m in reversed(inbox) if m.kind == "proposal"), None)
                review_packet = latest_proposal.payload.get("review_packet") if latest_proposal else None
                if not isinstance(review_packet, dict) or review_packet.get("packet_id") is None:
                    raise ValueError("评审缺少有效 ReviewPacket，必须重新提交评审")
                check = review_packet.get("hard_check") or {}
                if kind == "accept":
                    if decision.get("changes") or decision.get("approved_version") != review_version or not check.get("passed"):
                        raise ValueError("评审接受必须版本一致、没有待办修改且硬约束通过")
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
                ctx.document = {**decision["document"], "approved_version": ctx.approved_version}
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
        "tasks": tasks, "histories": histories, "trace": trace, "turns": turns,
        "reviews": reviews, "proposal_id": proposal_id, "review_version": review_version,
        "pending_rejections": pending_rejections, "pending_issue_codes": pending_issue_codes,
        "pending_agent": pending_agent, "pending_task": pending_task, "warnings": warnings,
    }
    return PeerResult(bus=bus, ctx=ctx, itinerary=ctx.itinerary if converged else None,
                      escalation=escalation, converged=converged, rounds=reviews, turns=turns,
                      warnings=warnings, trace=trace, blocked=blocked, session=session)
