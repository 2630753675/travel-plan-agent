"""编排图组装。

结构：
    clarify ─┬─ transport ─┐
             ├─ lodging   ─┤
             ├─ poi       ─┼─ planner ─ critic ─┬─ planner（未通过且未达轮次上限）
             └─ weather   ─┘                    └─ END

四条检索边从 clarify 同时出发，LangGraph 在同一 superstep 并行执行；
planner 收到全部四路结果后才开始排程。
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..agents import clarify, critic, lodging, llm_selector, planner, poi, transport, weather
from ..agents import negotiators
from ..agents.protocol import AgentMessage, AgentProposal, CollaborationRequest
from ..agents import reviewers
from ..llm import LLMClient
from ..settings import AppSettings
from ..tools.base import TravelSource
from .state import TripState

# 兼容旧调用：未传配置时用默认值。实际生效值来自界面设置。
DEFAULT_MAX_REVISIONS = AppSettings().orchestration.max_revisions

# revision 记录 critic 执行次数。首次校验算第 1 次，因此重排次数 = revision - 1。
# 上限来自界面配置 orchestration.max_revisions（默认 3，即最多 2 次重排）。
MAX_REVISIONS = DEFAULT_MAX_REVISIONS


def _emit(payload: dict) -> None:
    """向自定义流写一条事件；非流式调用下是 no-op。"""
    try:
        get_stream_writer()(payload)
    except Exception:
        pass


class _ObservedSource:
    """Wrap real data-source calls with truthful call/result stream events."""

    def __init__(self, source: TravelSource, actor: str, stage: str):
        self._source = source
        self._actor = actor
        self._stage = stage

    def _call(self, tool: str, arguments: dict, fn):
        call_id = uuid4().hex
        _emit({
            "phase": "tool_call",
            "tool_call_id": call_id,
            "node": self._actor,
            "stage": self._stage,
            "actor": self._actor,
            "tool": tool,
            "arguments": arguments,
        })
        try:
            result = fn()
        except Exception as exc:
            _emit({
                "phase": "tool_result",
                "tool_call_id": call_id,
                "node": self._actor,
                "stage": self._stage,
                "actor": self._actor,
                "tool": tool,
                "status": "error",
                "summary": type(exc).__name__,
            })
            raise
        count = len(result) if hasattr(result, "__len__") else None
        _emit({
            "phase": "tool_result",
            "tool_call_id": call_id,
            "node": self._actor,
            "stage": self._stage,
            "actor": self._actor,
            "tool": tool,
            "status": "success",
            "summary": f"{count} 条结果" if count is not None else "调用成功",
            "result_count": count,
        })
        return result

    def search_flights(self, origin: str, destination: str, dep_date: date):
        return self._call(
            "search_flights",
            {"origin": origin, "destination": destination, "date": dep_date.isoformat()},
            lambda: self._source.search_flights(origin, destination, dep_date),
        )

    def search_hotels(self, city: str, checkin: date, nights: int):
        return self._call(
            "search_hotels",
            {"city": city, "checkin": checkin.isoformat(), "nights": nights},
            lambda: self._source.search_hotels(city, checkin, nights),
        )

    def list_pois(self, city: str):
        return self._call("list_pois", {"city": city}, lambda: self._source.list_pois(city))

    def get_weather(self, city: str, start: date, days: int):
        return self._call(
            "get_weather",
            {"city": city, "start": start.isoformat(), "days": days},
            lambda: self._source.get_weather(city, start, days),
        )


def _observed_source(source: TravelSource, actor: str, stage: str) -> _ObservedSource:
    return _ObservedSource(source, actor, stage)


def _summarize(node: str, upd: dict) -> str:
    try:
        if node == "clarify":
            return f"需求已归一化，提示 {len(upd.get('notes') or [])} 条"
        if node == "transport":
            p = upd["transport"]
            if p.error:
                return p.error
            out = p.chosen_outbound.flight_no if p.chosen_outbound else "无"
            back = p.chosen_inbound.flight_no if p.chosen_inbound else "无"
            return f"去程 {out} / 返程 {back}，往返 {p.round_trip_cost:.0f} 元"
        if node == "lodging":
            p = upd["lodging"]
            if p.error:
                return p.error
            return f"推荐 {p.chosen.name}（{p.chosen.price_per_night:.0f} 元/晚，共 {len(p.candidates)} 家候选）"
        if node == "poi":
            p = upd["poi"]
            if p.error:
                return p.error
            return f"{len(p.candidates)} 个可用景点，排除 {len(p.excluded)} 个"
        if node == "weather":
            p = upd["weather"]
            if p.error:
                return p.error
            rainy = sum(1 for w in p.days if w.is_rainy)
            return f"{len(p.days)} 天天气，其中 {rainy} 天有雨"
        if node == "planner":
            it = upd["draft"]
            return f"草稿生成：{len(it.days)} 天，总花费 {it.total_cost:.0f} 元"
        if node == "critic":
            c = upd["critique"]
            blockers = len([i for i in c.issues if i.severity == "blocker"])
            if c.passed:
                return f"校验通过，{len(c.issues)} 条提醒"
            return f"未通过：{blockers} 个阻断项"
    except Exception:
        return ""
    return ""


def _log_lines(node: str, upd: dict, state: TripState) -> list:
    """把节点结果写成有层级的轨迹行。

    depth=1 是 agent 的动作，depth=2 是它调用的工具及其返回。
    只写具体事实（选中哪班机、砍掉哪个景点、超了多少）。
    """
    from ..activity import NODE_GROUP, entry, tool_call

    group = NODE_GROUP.get(node, "")
    lines: list = []
    brief = state.get("brief")

    try:
        if node == "clarify":
            if brief is not None:
                lines.append(
                    entry(
                        "clarify",
                        f"{brief.destination} {brief.days} 天，{brief.travelers} 人，"
                        f"预算 {brief.budget_total:.0f} 元，同行 {('、'.join(brief.companions))}",
                        group=group,
                    )
                )
            for note in upd.get("notes") or []:
                lines.append(entry("clarify", note, kind="decision", group=group, depth=2))

        elif node == "transport":
            p = upd["transport"]
            if brief is not None:
                lines.append(
                    tool_call("transport", "search_flights", f"{brief.origin}→{brief.destination} {brief.start_date}",
                              f"{len(p.outbound)} 条" if not p.error else "失败", group=group)
                )
                lines.append(
                    tool_call("transport", "search_flights", f"{brief.destination}→{brief.origin} {brief.end_date}",
                              f"{len(p.inbound)} 条" if not p.error else "失败", group=group)
                )
            if p.error:
                lines.append(entry("transport", p.error, kind="decision", group=group))
            else:
                out, back = p.chosen_outbound, p.chosen_inbound
                lines.append(
                    entry(
                        "transport",
                        f"选中 {out.flight_no if out else '无'} / {back.flight_no if back else '无'}，"
                        f"往返 {p.round_trip_cost:.0f} 元",
                        group=group,
                    )
                )

        elif node == "lodging":
            p = upd["lodging"]
            if brief is not None:
                lines.append(
                    tool_call("lodging", "search_hotels", f"{brief.destination} 距市中心≤12km",
                              f"{len(p.candidates)} 家" if not p.error else "失败", group=group)
                )
            if p.error:
                lines.append(entry("lodging", p.error, kind="decision", group=group))
            else:
                lines.append(
                    entry(
                        "lodging",
                        f"推荐 {p.chosen.name}（{p.chosen.price_per_night:.0f} 元/晚，"
                        f"距市中心 {p.chosen.distance_to_center_km} km）",
                        group=group,
                    )
                )

        elif node == "poi":
            p = upd["poi"]
            if brief is not None:
                lines.append(
                    tool_call("poi", "list_pois", brief.destination,
                              f"{len(p.candidates)} 个" if not p.error else "失败", group=group)
                )
            if p.error:
                lines.append(entry("poi", p.error, kind="decision", group=group))
            else:
                lines.append(entry("poi", f"{len(p.candidates)} 个可用景点", group=group))
                if p.excluded:
                    lines.append(
                        entry("poi", "排除 " + "、".join(p.excluded), kind="decision", group=group, depth=2)
                    )

        elif node == "weather":
            p = upd["weather"]
            if brief is not None:
                lines.append(
                    tool_call("weather", "get_weather", f"{brief.destination} {brief.days} 天",
                              f"{len(p.days)} 天" if not p.error else "失败", group=group)
                )
            if p.error:
                lines.append(entry("weather", p.error, kind="decision", group=group))
            else:
                rainy = [w.date for w in p.days if w.is_rainy]
                lines.append(
                    entry(
                        "weather",
                        f"有雨 {len(rainy)} 天" + (f"（{'、'.join(str(d)[5:] for d in rainy)}）" if rainy else ""),
                        group=group,
                    )
                )

        elif node == "planner":
            it = upd["draft"]
            sights = sum(1 for d in it.days for i in d.items if i.type == "sight")
            revision = state.get("revision", 0)
            over = it.total_cost - it.budget_total
            if revision == 0 and over <= 0:
                lead = "我排出"
            elif revision == 0:
                lead = "我先排出"
            else:
                lead = "按校验意见改好了，现在"
            lines.append(
                entry(
                    "planner",
                    f"{lead} {len(it.days)} 天 {sights} 个景点，总花费 {it.total_cost:.0f} 元",
                    kind="speech",
                    group=group,
                )
            )
            if over > 0:
                lines.append(entry("planner", f"超出预算 {over:.0f} 元", kind="decision", group=group))

        elif node == "critic":
            c = upd["critique"]
            blockers = [i for i in c.issues if i.severity == "blocker"]
            if c.passed:
                lines.append(entry("critic", f"没问题，可以定稿（{len(c.issues)} 条提醒）", kind="speech", group=group))
            else:
                lines.append(entry("critic", f"这份方案不行，{len(blockers)} 个问题：", kind="speech", group=group))
                for i in blockers[:4]:
                    lines.append(entry("critic", i.message, kind="decision", group=group, depth=2))
    except Exception:
        pass
    return lines


def build(source: TravelSource, settings: AppSettings | None = None, checkpointer=None):
    settings = settings or AppSettings()
    max_revisions = settings.orchestration.max_revisions
    # 没有 checkpointer 就无法中断与恢复，此时图退化为"全自动、不询问"
    enable_interrupt = checkpointer is not None
    # fast/expert 都走受控规则图；只有显式 peer 才启动五角色团队。
    # legacy 保留旧协商图，仅用于历史轨迹兼容，不作为默认路径。
    selected_mode = settings.orchestration.agent_mode
    if selected_mode == "legacy":
        selected_mode = "expert"
    peer_mode = settings.llm.configured and selected_mode == "peer"
    mode_call_limit = {
        "fast": 1,
        "expert": min(3, settings.orchestration.max_model_calls),
        "peer": settings.orchestration.max_model_calls,
    }.get(selected_mode, 1)
    shared_llm = (
        LLMClient(
            settings.llm,
            settings.orchestration.node_timeout_s,
            budget=None,
            max_calls=mode_call_limit,
            max_output_tokens=settings.orchestration.max_output_tokens,
        )
        if settings.llm.configured
        else None
    )

    async def clarify_node(state: TripState) -> dict:
        result = clarify.run(state["brief"])
        return {"brief": result.brief, "notes": result.notes}

    async def transport_node(state: TripState) -> dict:
        try:
            plan = transport.run(state["brief"], _observed_source(source, "transport", "retrieval"))
        except Exception as e:  # 节点异常不抛穿整图
            return {
                "transport": transport.TransportPlan(error=f"交通 agent 异常：{e}"),
                "errors": [f"transport: {e}"],
            }
        return {
            "transport": plan,
        }

    async def lodging_node(state: TripState) -> dict:
        try:
            plan = lodging.run(state["brief"], _observed_source(source, "lodging", "retrieval"))
        except Exception as e:
            return {
                "lodging": lodging.LodgingPlan(error=f"住宿 agent 异常：{e}"),
                "errors": [f"lodging: {e}"],
            }
        return {
            "lodging": plan,
        }

    async def poi_node(state: TripState) -> dict:
        try:
            plan = poi.run(state["brief"], _observed_source(source, "poi", "retrieval"))
        except Exception as e:
            return {
                "poi": poi.PoiPlan(error=f"景点 agent 异常：{e}"),
                "errors": [f"poi: {e}"],
            }
        return {
            "poi": plan,
        }

    async def weather_node(state: TripState) -> dict:
        try:
            plan = weather.run(state["brief"], _observed_source(source, "weather", "retrieval"))
        except Exception as e:
            return {
                "weather": weather.WeatherPlan(error=f"天气 agent 异常：{e}"),
                "errors": [f"weather: {e}"],
            }
        return {
            "weather": plan,
        }

    async def coordinator_node(state: TripState) -> dict:
        """四路检索跑完后校对一次：结果之间有没有对不上的地方。

        只做规则检查，不调模型——这类一致性判断不值得花一次 LLM 调用。
        发现问题就向相关 agent 发一次请求，没有问题就直接往下走。
        """
        from ..activity import ACTOR_LABEL, entry, stage
        from ..agents import coordinator

        _emit({"phase": "start", "node": "coordinator"})
        result = coordinator.review(
            state["brief"], state["lodging"].chosen, state["poi"].candidates
        )
        lines = [stage("collab")]

        if not result.needed:
            lines.append(
                entry("coordinator", "四路结果对得上，不需要补充", group="collab", depth=1)
            )
            _emit({"phase": "end", "node": "coordinator", "summary": "无需协作"})
            return {"collab_done": True, "log": lines}

        req = result.requests[0]
        request_id = uuid4().hex
        sender = req.sender
        receiver = req.receiver
        sender_label = ACTOR_LABEL.get(sender, sender)
        receiver_label = ACTOR_LABEL.get(receiver, receiver)
        request_message = AgentMessage(
            message_id=request_id,
            sender=sender,
            receiver=receiver,
            intent="request",
            content=req.question,
            reason=req.reason,
            evidence=[{"kind": "distance_check", **req.payload}],
            payload=req.payload,
        )
        response_message = AgentMessage(
            message_id=uuid4().hex,
            sender=receiver,
            receiver=sender,
            intent="response",
            content=(
                f"我提供 {len(req.payload.get('poi_ids', []))} 个目标景点的坐标，"
                "请据此重新评估酒店位置。"
            ),
            reason="回应住宿 Agent 的位置协作请求",
            reply_to=request_id,
            evidence=[{"kind": "poi_coordinates", "poi_ids": req.payload.get("poi_ids", [])}],
            payload={"poi_ids": req.payload.get("poi_ids", [])},
        )
        lines.append(
            entry(sender, f"→ {receiver_label}：{req.question}", kind="speech", group="collab", depth=1)
        )
        lines.append(
            entry(
                receiver,
                f"← {sender_label}：收到，提供 {len(req.payload.get('poi_ids', []))} 个景点的坐标",
                kind="answer",
                group="collab",
                depth=1,
            )
        )
        for note in result.notes:
            lines.append(entry("coordinator", note, kind="decision", group="collab", depth=2))

        _emit({"phase": "end", "node": "coordinator", "summary": req.reason})
        return {
            "collab_done": True,
            "collab_requests": [
                {
                    "sender": sender_label,
                    "receiver": receiver_label,
                    "question": req.question,
                    "reason": req.reason,
                }
            ],
            "collaboration_requests": [
                CollaborationRequest(
                    request_id=request_id,
                    sender=sender,
                    receiver=receiver,
                    question=req.question,
                    reason=req.reason,
                    payload=req.payload,
                )
            ],
            "focus_poi_ids": req.payload.get("poi_ids", []),
            "log": lines,
        }

    async def lodging_fix_node(state: TripState) -> dict:
        """住宿 agent 带着景点坐标重选一次。"""
        from ..activity import entry, stage

        _emit({"phase": "start", "node": "lodging_fix"})
        focus_ids = set(state.get("focus_poi_ids") or [])
        focus = [p for p in state["poi"].candidates if p.id in focus_ids]
        before = state["lodging"].chosen
        plan = lodging.run(state["brief"], _observed_source(source, "lodging", "collab"), focus_pois=focus)

        lines = [stage("collab")]
        if plan.error:
            lines.append(entry("lodging", plan.error, kind="decision", group="collab", depth=1))
        elif plan.chosen is not None:
            changed = before is None or plan.chosen.name != before.name
            lines.append(
                entry(
                    "lodging",
                    (
                        f"按景点位置重选为 {plan.chosen.name}"
                        if changed
                        else f"重算后仍是 {plan.chosen.name}，位置最合适"
                    ),
                    kind="speech",
                    group="collab",
                    depth=1,
                )
            )
        _emit(
            {"phase": "end", "node": "lodging_fix", "summary": plan.chosen.name if plan.chosen else "无"}
        )
        return {"lodging": plan, "log": lines}

    async def planner_node(state: TripState) -> dict:
        critique = state.get("critique")
        review_feedback = state.get("messages") or []
        budget_review_requested = any(
            m.sender == "budget_reviewer" and any(e.get("action") == "downgrade" for e in m.evidence)
            for m in review_feedback
        )
        experience_review_requested = any(
            m.sender == "experience_reviewer" and any(e.get("action") == "revise" for e in m.evidence)
            for m in review_feedback
        )
        # 预算评审是规划 Agent 的输入，不把评审意见仅仅作为聊天记录。
        # 超预算时优先使用确定性降配策略，保证修订真实降低成本。
        # 预算已被判定超支时不再交给模型自由发挥：
        # 降配是用户明确的省钱诉求，规则层"换便宜酒店 + 优先免费景点"更可预测。
        budget_pressure = bool(
            budget_review_requested
            or critique and any(i.code == "BUDGET_EXCEEDED" for i in critique.issues)
        )

        llm_selection = None
        if settings.llm.configured and not budget_pressure and settings.orchestration.agent_mode in {"fast", "expert"}:
            client = shared_llm
            llm_selection = await llm_selector.select_pois(
                state["brief"], state["poi"].candidates, state["weather"].days, client
            )

        itinerary = planner.run(
            brief=state["brief"],
            transport=state["transport"],
            lodging=state["lodging"],
            poi_plan=state["poi"],
            weather=state["weather"],
            critique=critique,
            revision=state.get("revision", 0),
            llm_selection=llm_selection,
            forced_drops=set(state.get("forced_drops") or []),
            force_cheaper_hotel=bool(state.get("force_cheaper_hotel")),
        )
        if settings.llm.enabled and llm_selection is None and not budget_pressure:
            # 模型启用但没拿到可用结果：必须让用户看到这次是规则生成的
            itinerary.warnings.append("模型选择不可用，本次行程由规则引擎生成")
        if budget_pressure and settings.llm.enabled:
            itinerary.warnings.append("降配重排由规则引擎执行，以保证确实省钱")
        summary = f"我排出 {len(itinerary.days)} 天行程，总花费 {itinerary.total_cost:.0f} 元。"
        constraints = ["单日活动不超过 600 分钟", "景点必须在开放时间内", "返程日预留机场时间"]
        proposals = []
        messages = []
        prior_reviews = {
            reviewer: next(
                (m for m in reversed(state.get("messages") or []) if m.sender == reviewer),
                None,
            )
            for reviewer in ("budget_reviewer", "experience_reviewer")
        }
        if experience_review_requested and critique is None:
            itinerary.warnings.append("体验评审建议复核同行人群或每日景点安排")
        for reviewer_name in ("budget_reviewer", "experience_reviewer"):
            prior_review = prior_reviews[reviewer_name]
            proposal = AgentProposal(
                proposal_id=uuid4().hex,
                sender="planner",
                receiver=reviewer_name,
                revision=state.get("revision", 0) + 1,
                summary=summary,
                constraints=constraints,
                reply_to=prior_reviews[reviewer_name].message_id if prior_reviews[reviewer_name] else None,
            )
            proposals.append(proposal)
            messages.append(
                AgentMessage(
                    message_id=proposal.proposal_id,
                    sender="planner",
                    receiver=reviewer_name,
                    intent="revision" if prior_review else "proposal",
                    content=summary,
                    reason="根据用户约束与工具结果形成行程提案，请独立评审",
                    evidence=[{"kind": "itinerary", "days": len(itinerary.days), "total_cost": itinerary.total_cost}],
                    reply_to=proposal.reply_to,
                    payload={"revision": proposal.revision, "constraints": constraints},
                )
            )
        return {"draft": itinerary, "proposals": proposals, "messages": messages}

    async def budget_review_node(state: TripState) -> dict:
        content, reason, evidence = reviewers.budget_review(state["brief"], state["draft"])
        proposal = next((p for p in reversed(state.get("proposals") or []) if p.receiver == "budget_reviewer"), None)
        message = AgentMessage(
            message_id=uuid4().hex,
            sender="budget_reviewer",
            receiver="planner",
            intent="response",
            content=content,
            reason=reason,
            evidence=evidence,
            reply_to=proposal.proposal_id if proposal else None,
            payload={"review": "budget"},
        )
        action = next((item.get("action") for item in evidence if item.get("kind") == "recommendation"), "accept")
        return {"budget_review": {"content": content, "evidence": evidence}, "budget_review_action": action, "messages": [message]}

    async def experience_review_node(state: TripState) -> dict:
        content, reason, evidence = reviewers.experience_review(
            state["brief"], state["draft"], state["poi"].candidates
        )
        proposal = next((p for p in reversed(state.get("proposals") or []) if p.receiver == "experience_reviewer"), None)
        message = AgentMessage(
            message_id=uuid4().hex,
            sender="experience_reviewer",
            receiver="planner",
            intent="response",
            content=content,
            reason=reason,
            evidence=evidence,
            reply_to=proposal.proposal_id if proposal else None,
            payload={"review": "experience"},
        )
        action = next((item.get("action") for item in evidence if item.get("kind") == "recommendation"), "accept")
        return {"experience_review": {"content": content, "evidence": evidence}, "experience_review_action": action, "messages": [message]}

    async def critic_node(state: TripState) -> dict:
        critique = critic.run(
            brief=state["brief"],
            itinerary=state["draft"],
            poi_plan=state["poi"],
            transport=state["transport"],
            weather=state["weather"],
        )
        _emit({
            "phase": "validation",
            "node": "critic",
            "stage": "check",
            "passed": critique.passed,
            "issues": [issue.model_dump(mode="json") for issue in critique.issues],
            "suggestions": critique.suggestions,
        })
        # 预算/体验评审是 Agent；这里是确定性的硬约束 Workflow，合并结果并决定路由。
        return {"critique": critique, "revision": state.get("revision", 0) + 1}

    async def negotiation_node(state: TripState) -> dict:
        """预算官 vs 体验官，由调解者裁决。支持多轮：谈崩时交给用户表态后重谈。"""
        _emit({"phase": "start", "node": "negotiation"})
        from ..activity import entry

        round_no = int(state.get("neg_round", 1) or 1)
        user_hint = state.get("user_hint", "") or ""

        if not settings.llm.configured:
            _emit(
                {
                    "phase": "end",
                    "node": "negotiation",
                    "summary": "模型未启用，跳过协商，直接走规则降配",
                }
            )
            return {
                "negotiated": True,
                "log": [
                    entry("system", "阶段: 预算与体验协商", kind="stage"),
                    entry("negotiation", "模型未启用，跳过协商", kind="decision"),
                ],
            }

        client = shared_llm
        if client is None:
            return {"negotiated": True, "log": [entry("negotiation", "本次模式未启用模型协商", kind="decision")]}
        try:
            log, drops, cheaper = await negotiators.negotiate(
                state["brief"],
                state["draft"],
                state["poi"].candidates,
                client,
                round_no=round_no,
                user_hint=user_hint,
                history=state.get("negotiation"),
            )
        except Exception as e:
            _emit({"phase": "end", "node": "negotiation", "summary": f"协商异常，回落规则：{e}"})
            return {
                "negotiated": True,
                "errors": [f"negotiation: {e}"],
                "log": [
                    entry("system", "阶段: 预算与体验协商", kind="stage"),
                    entry("negotiation", f"协商异常，回落规则：{e}", kind="decision"),
                ],
            }

        _emit({"phase": "end", "node": "negotiation", "summary": log.summary})

        from ..activity import entry, stage, tool_call

        lines = [stage("negotiation")]
        lines.append(
            entry("negotiation", f"第 {round_no} 轮", kind="action", group="negotiation", depth=1)
        )
        # 只记录本轮新增的发言，历史轮次已经在之前的日志里了
        for m in log.rounds[-3:]:
            lines.append(entry(m.speaker, m.claim, kind="speech", group="negotiation", depth=1))
            for t in m.tool_calls:
                lines.append(
                    tool_call(m.speaker, t, "", "已执行", group="negotiation")
                )
            for e in m.evidence:
                lines.append(
                    entry(m.speaker, f"{e.label}：{e.value}", group="negotiation", depth=2)
                )

        # 逐条推送，协商过程能实时看到"谁在说什么"
        for line in lines:
            _emit({"phase": "log", "entry": line.model_dump(mode="json")})

        return {
            "negotiation": log,
            "negotiated": True,
            "neg_round": round_no + 1,
            "forced_drops": sorted(drops),
            "force_cheaper_hotel": cheaper,
            "log": lines,
        }

    async def ask_node(state: TripState) -> dict:
        """协商僵持时暂停，让用户给倾向，然后带着这个倾向重谈。

        这是 negotiate 的第二轮入口：用户表态是"继续协商"的输入，
        不是"由用户自己拍板结束"。
        """
        from ..activity import entry

        log = state.get("negotiation")
        draft = state["draft"]
        brief = state["brief"]
        over = draft.total_cost - brief.budget_total
        ratio = over / brief.budget_total if brief.budget_total else 0
        deadlock = log.summary if log else "预算与体验无法两全"

        _emit({"phase": "start", "node": "ask"})
        choice = interrupt(
            {
                "type": "negotiation_deadlock",
                "message": deadlock,
                "total": round(draft.total_cost),
                "budget": round(brief.budget_total),
                "over": round(over),
                "ratio": round(ratio, 3),
                "options": [
                    {"value": "体验优先：接受超支，保住这几个必去项", "label": "体验优先，接受超支"},
                    {"value": "预算优先：可以砍掉高价值项目，控制在预算内", "label": "预算优先，砍掉贵的"},
                    {"value": "先换便宜酒店或少排一天，再谈", "label": "先换酒店再谈"},
                ],
            }
        )
        _emit({"phase": "end", "node": "ask", "summary": f"你选择：{choice}"})

        return {
            "user_hint": str(choice),
            "log": [
                entry(
                    "negotiation",
                    f"僵持：{deadlock}",
                    kind="question",
                    group="negotiation",
                    depth=1,
                ),
                entry("user", str(choice), kind="answer", group="negotiation", depth=1),
            ],
        }

    async def confirm_node(state: TripState) -> dict:
        """预算严重超支时暂停，让用户在三个选项里决定。

        这是副作用极小的节点：只调 interrupt()，不做任何计算，
        因为恢复时该节点会从头重放。
        """
        from ..activity import entry, stage

        draft = state["draft"]
        brief = state["brief"]
        over = draft.total_cost - brief.budget_total
        ratio = over / brief.budget_total if brief.budget_total else 0
        _emit({"phase": "start", "node": "confirm"})
        decision = interrupt(
            {
                "type": "budget_overrun",
                "total": round(draft.total_cost),
                "budget": round(brief.budget_total),
                "over": round(over),
                "ratio": round(ratio, 3),
                "options": [
                    {"value": "accept", "label": "接受超支，就这样"},
                    {"value": "downgrade", "label": "降配重排（换便宜酒店/减少付费景点）"},
                    {"value": "abort", "label": "取消，不要这份行程"},
                ],
            }
        )
        _emit(
            {
                "phase": "end",
                "node": "confirm",
                "summary": {"accept": "已接受超支", "downgrade": "要求降配重排", "abort": "已取消"}.get(
                    str(decision), str(decision)
                ),
            }
        )
        picked = {"accept": "接受超支", "downgrade": "要求降配重排", "abort": "取消"}.get(
            str(decision), str(decision)
        )
        return {
            "decision": str(decision),
            "log": [
                stage("confirm"),
                entry(
                    "confirm",
                    f"超出预算 {over:.0f} 元（{ratio:.0%}），问你接受 / 降配 / 取消",
                    kind="question",
                    group="confirm",
                    depth=1,
                ),
                entry("user", f"选择：{picked}", kind="answer", group="confirm", depth=1),
            ],
        }

    def route_after_critic(state: TripState) -> str:
        critique = state["critique"]
        draft = state["draft"]
        brief = state["brief"]
        over_ratio = (
            (draft.total_cost - brief.budget_total) / brief.budget_total if brief.budget_total else 0
        )
        is_budget_issue = any(i.code == "BUDGET_EXCEEDED" for i in critique.issues)

        review_needs_revision = state.get("budget_review_action") == "downgrade" or state.get("experience_review_action") == "revise"
        if critique.passed and not review_needs_revision:
            return END
        if review_needs_revision and state.get("revision", 0) >= max_revisions:
            return END
        if critique.passed and review_needs_revision:
            _emit({
                "phase": "edge",
                "from": "critic",
                "to": "planner",
                "reason": "评审 Agent 建议修订，回到规划 Agent",
            })
            return "planner"

        # 1. 预算严重超支且还没协商过：让两个角色先谈一轮
        #    （用和"是否打扰用户"相同的 20% 阈值，避免为几块钱惊动用户）
        if (
            settings.llm.configured
            and is_budget_issue
            and not state.get("negotiated")
            and over_ratio > 0.2
        ):
            return "negotiation"

        # 2. 谈完仍严重超支：交给用户拍板，不替他决定
        if (
            enable_interrupt
            and over_ratio > 0.2
            and state.get("revision", 0) < max_revisions
            and not state.get("decision")
        ):
            return "confirm"

        if state.get("revision", 0) >= max_revisions:
            # 达到上限强制放行，由 warnings 提示未解决的 blocker
            return END
        _emit(
            {
                "phase": "edge",
                "from": "critic",
                "to": "planner",
                "reason": "校验未通过，回退重排",
            }
        )
        return "planner"

    def route_after_negotiation(state: TripState) -> str:
        log = state.get("negotiation")
        if log is None:
            return "planner"
        if log.outcome in ("escalated", "no_change"):
            # 僵持或压根没方案：先问用户要倾向，再带着倾向重谈一轮。
            # 不能因为"没得砍"就悄悄给一份超支行程。
            if enable_interrupt and not state.get("user_hint"):
                return "ask"
            return "confirm" if enable_interrupt else "planner"
        if log.adjustment.is_empty():
            return END
        _emit(
            {
                "phase": "edge",
                "from": "negotiation",
                "to": "planner",
                "reason": "按协商结论执行降配",
            }
        )
        return "planner"

    def route_after_confirm(state: TripState) -> str:
        if state.get("decision") == "downgrade":
            _emit(
                {
                    "phase": "edge",
                    "from": "confirm",
                    "to": "planner",
                    "reason": "按你的选择降配重排",
                }
            )
            return "planner"
        return END

    def route_after_confirm_peer(state: TripState) -> str:
        """协作模式下的"降配"不是重跑规则，而是把用户意见送回 Agent 继续谈。"""
        if state.get("decision") == "downgrade":
            _emit(
                {
                    "phase": "edge",
                    "from": "confirm",
                    "to": "peer_run",
                    "reason": "用户要求降配，回到 Agent 协作继续调整",
                }
            )
            return "peer_run"
        return END

    # ---- 对等多 Agent 协作（仅在配置了模型时启用）----
    _peer_tool_ids: dict[str, str] = {}

    def _peer_emit(kind: str, /, **payload) -> None:
        """把 Agent 运行时的动作翻译成前端能读懂的事件。

        工具调用沿用既有 tool_call/tool_result 契约，其余动作写成 agent_message，
        这样对话抽屉、事件回放和 SQLite 落库都不用为多 Agent 单开一套。
        """
        agent = str(payload.get("agent") or "system")
        if kind == "tool_call":
            cid = uuid4().hex
            _peer_tool_ids[agent] = cid
            _emit(
                {
                    "phase": "tool_call",
                    "node": agent,
                    "actor": agent,
                    "stage": "peer",
                    "tool": payload.get("tool"),
                    "arguments": payload.get("arguments") or {},
                    "tool_call_id": cid,
                }
            )
        elif kind == "tool_result":
            _emit(
                {
                    "phase": "tool_result",
                    "node": agent,
                    "actor": agent,
                    "stage": "peer",
                    "tool": payload.get("tool"),
                    "status": "success" if payload.get("ok") else "error",
                    "summary": payload.get("summary", ""),
                    "tool_call_id": _peer_tool_ids.get(agent) or uuid4().hex,
                }
            )
        elif kind == "decide":
            _emit(
                {
                    "phase": "peer_message",
                    "agent": agent,
                    "text": payload.get("text", ""),
                    "kind": payload.get("kind", "note"),
                    "to": payload.get("to") or [],
                    "message_id": payload.get("message_id"),
                    "reply_to": payload.get("reply_to"),
                    "payload": payload.get("payload") or {},
                }
            )
        else:
            _emit({"phase": "peer_status", "step": kind, "agent": agent, **payload})

    async def peer_run_node(state: TripState) -> dict:
        """跑一场对等协作：Agent 互相唤醒、协商，僵持则请示用户。"""
        from ..agents import hub as peer_hub

        # 深度团队也受运行级请求数和单次输出上限保护，达到上限立即停止。
        client = shared_llm
        result = await peer_hub.run_session(
            brief=state["brief"],
            source=source,
            client=client,
            emit=_peer_emit,
            max_rounds=max(1, settings.orchestration.max_revisions),
            max_turns=min(settings.orchestration.max_agent_turns, settings.orchestration.max_model_calls),
            resume=state.get("peer_state"),
            user_answer=state.get("peer_answer") or "",
            finalize_feedback=str(state.get("peer_finalize_feedback") or ""),
        )

        updates: dict = {
            "peer_state": result.session,
            "peer_trace": result.trace,
            "peer_document": result.ctx.document or {},
            "team_tasks": result.session.get("tasks", {}),
            "peer_answer": "",
            "blocked": result.blocked,
            "transport": result.ctx.transport,
            "lodging": result.ctx.lodging,
            "poi": result.ctx.poi,
            "weather": result.ctx.weather,
        }
        if result.escalation:
            updates["peer_question"] = result.escalation
            return updates
        if result.blocked:
            updates["blocked"] = True

        updates["peer_question"] = None
        if result.itinerary is not None:
            for w in result.warnings:
                result.itinerary.warnings.append(w)
            updates["draft"] = result.itinerary
        else:
            updates["draft"] = None
        if result.converged:
            updates["approved_version"] = result.ctx.approved_version
            updates["document"] = result.ctx.document
        _emit(
            {
                "phase": "peer_status",
                "step": "session_end",
                "agent": "system",
                "rounds": result.rounds,
                "turns": result.turns,
                "converged": result.converged,
            }
        )
        return updates

    async def peer_ask_node(state: TripState) -> dict:
        """Agent 之间无法协调时，把选择权交回用户。"""
        q = state.get("peer_question") or {}
        answer = interrupt(
            {
                "type": "peer_escalation",
                "question": q.get("question") or "需要你确认一件事",
                "from": q.get("from"),
                "options": q.get("options") or [],
            }
        )
        return {"peer_answer": str(answer)}

    async def peer_finalize_node(state: TripState) -> dict:
        """确定性校验器终审：Agent 谈成什么都可以，能不能执行由这里说了算。"""
        from ..models import Critique, Issue

        draft = state.get("draft")
        if draft is None:
            return {
                "critique": Critique(
                    passed=False,
                    issues=[
                        Issue(severity="blocker", code="NO_PLAN", message="协作未能产出行程方案")
                    ],
                    suggestions=["检查模型配置后重试"],
                ),
                "revision": 1,
            }
        critique = critic.run(
            brief=state["brief"],
            itinerary=draft,
            poi_plan=state["poi"],
            transport=state["transport"],
            weather=state["weather"],
        )
        _emit(
            {
                "phase": "validation",
                "node": "critic",
                "stage": "check",
                "passed": critique.passed,
                "issues": [i.model_dump(mode="json") for i in critique.issues],
                "suggestions": critique.suggestions,
            }
        )
        feedback = "；".join(i.message for i in critique.issues if i.severity == "blocker")
        if not critique.passed and feedback:
            _emit({"phase": "peer_finalize_feedback", "text": feedback})
        return {"critique": critique, "revision": int(state.get("revision", 0)) + 1, "peer_finalize_feedback": feedback if not critique.passed else ""}

    def route_after_peer(state: TripState) -> str:
        return "peer_ask" if state.get("peer_question") else "peer_finalize"

    def route_after_peer_finalize(state: TripState) -> str:
        draft = state.get("draft")
        critique = state.get("critique")
        brief = state["brief"]
        if draft is None or critique is None:
            return END
        if not critique.passed:
            if state.get("revision", 0) + 1 >= max_revisions:
                return END
            return "peer_run"
        approved = state.get("approved_version")
        if approved is None or approved != state.get("peer_state", {}).get("context", {}).get("plan_version"):
            return END
        over_ratio = (
            (draft.total_cost - brief.budget_total) / brief.budget_total if brief.budget_total else 0
        )
        if enable_interrupt and over_ratio > 0.2 and not state.get("decision"):
            return "confirm"
        return END

    def wrap(name: str, fn):
        async def node(state: TripState) -> dict:
            _emit({"phase": "start", "node": name})
            result = await fn(state)
            from ..activity import NODE_GROUP, STAGE_NODES, stage

            # 状态中的 AgentMessage 是协作事实；只从这里统一发事件，避免某个节点
            # 既手动 _emit 又被 wrapper 重复投影。
            for message in result.get("messages") or []:
                if isinstance(message, AgentMessage):
                    _emit(
                        {
                            "phase": "message",
                            "stage": NODE_GROUP.get(name, "collab"),
                            "message": message.model_dump(mode="json"),
                        }
                    )

            lines = []
            if name in STAGE_NODES:
                lines.append(stage(NODE_GROUP[name]))
            lines.extend(_log_lines(name, result, state))
            result["log"] = list(result.get("log") or []) + lines
            # 逐条推给前端，让轨迹像思考过程一样流式出现，而不是最后一次性给出
            for line in lines:
                _emit({"phase": "log", "entry": line.model_dump(mode="json")})
            _emit({"phase": "end", "node": name, "summary": _summarize(name, result)})
            return result

        return node

    g = StateGraph(TripState)
    g.add_node("clarify", wrap("clarify", clarify_node))
    g.add_node("critic", wrap("critic", critic_node))
    g.add_edge(START, "clarify")

    if peer_mode:
        # 配置了模型：走真正的多 Agent 对等协作。
        # 规则节点不参与，避免"Agent 谈完又被规则重排"这种自相矛盾的路径。
        g.add_node("peer_run", wrap("peer_run", peer_run_node))
        g.add_node("peer_ask", wrap("peer_ask", peer_ask_node))
        g.add_node("peer_finalize", wrap("peer_finalize", peer_finalize_node))
        g.add_edge("clarify", "peer_run")
        g.add_conditional_edges(
            "peer_run",
            route_after_peer,
            {"peer_ask": "peer_ask", "peer_finalize": "peer_finalize"},
        )
        g.add_edge("peer_ask", "peer_run")
        peer_targets: dict = {END: END, "peer_run": "peer_run"}
        if enable_interrupt:
            peer_targets["confirm"] = "confirm"
        g.add_conditional_edges("peer_finalize", route_after_peer_finalize, peer_targets)

        if enable_interrupt:
            g.add_node("confirm", confirm_node)
            g.add_conditional_edges(
                "confirm", route_after_confirm_peer, {"peer_run": "peer_run", END: END}
            )
        return g.compile(checkpointer=checkpointer)

    # 未配置模型：走确定性规则链路（界面会明示"规则模式"，不冒充 Agent）。
    g.add_node("transport", wrap("transport", transport_node))
    g.add_node("lodging", wrap("lodging", lodging_node))
    g.add_node("poi", wrap("poi", poi_node))
    g.add_node("weather", wrap("weather", weather_node))
    g.add_node("planner", wrap("planner", planner_node))
    g.add_node("budget_review", wrap("budget_review", budget_review_node))
    g.add_node("experience_review", wrap("experience_review", experience_review_node))

    g.add_edge(START, "clarify")
    for name in ("transport", "lodging", "poi", "weather"):
        g.add_edge("clarify", name)
        g.add_edge(name, "coordinator")

    g.add_node("coordinator", coordinator_node)
    g.add_node("lodging_fix", lodging_fix_node)
    g.add_conditional_edges(
        "coordinator",
        lambda s: "lodging_fix" if s.get("collab_requests") else "planner",
        {"lodging_fix": "lodging_fix", "planner": "planner"},
    )
    g.add_edge("lodging_fix", "planner")
    g.add_edge("planner", "budget_review")
    g.add_edge("planner", "experience_review")
    g.add_edge("budget_review", "critic")
    g.add_edge("experience_review", "critic")

    g.add_node("negotiation", negotiation_node)

    critic_targets: dict = {"planner": "planner", "negotiation": "negotiation", END: END}
    if enable_interrupt:
        critic_targets["confirm"] = "confirm"

    g.add_conditional_edges("critic", route_after_critic, critic_targets)

    negotiation_targets: dict = {"planner": "planner", END: END}
    if enable_interrupt:
        negotiation_targets["confirm"] = "confirm"
        g.add_node("ask", ask_node)
        negotiation_targets["ask"] = "ask"
        # 用户表态后带着倾向重谈一轮
        g.add_edge("ask", "negotiation")
    g.add_conditional_edges("negotiation", route_after_negotiation, negotiation_targets)

    if enable_interrupt:
        g.add_node("confirm", confirm_node)
        g.add_conditional_edges("confirm", route_after_confirm, {"planner": "planner", END: END})

    return g.compile(checkpointer=checkpointer)
