"""编排图组装。

当前只保留深度团队单模式：

    clarify → peer_run → peer_ask（中断）
                    ↘ peer_finalize（确定性硬校验）
                         ├─ 未通过且未到上限 → peer_run 返工
                         └─ 通过 → END

多 Agent 会话（Orchestrator / Logistics / Local Expert / Critic / Writer）
在 peer_run 内部运行，不在外层图上逐角色展开。
"""

from __future__ import annotations

import json
from datetime import date
from uuid import uuid4

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..agents import clarify, critic
from ..agents.protocol import AgentMessage
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


def build(source: TravelSource, settings: AppSettings | None = None, checkpointer=None, cancellation=None, initial_stats=None):
    settings = settings or AppSettings()
    if not settings.llm.configured:
        raise ValueError(
            "未配置模型：深度团队模式只使用真实模型。请先在设置页填写 base_url、model、api_key 并启用模型。"
        )
    max_revisions = settings.orchestration.max_revisions
    mode_call_limit = settings.orchestration.max_model_calls
    # 没有 checkpointer 就无法中断与恢复，此时图退化为"全自动、不询问"。
    enable_interrupt = checkpointer is not None
    shared_llm = LLMClient(
        settings.llm,
        settings.orchestration.node_timeout_s,
        budget=None,
        max_calls=mode_call_limit,
        # 不再下发 max_tokens；旧配置里的数值在 settings.load 已迁移为 None。
        max_output_tokens=None,
        cancellation=cancellation,
        initial_stats=initial_stats,
    )

    async def clarify_node(state: TripState) -> dict:
        result = clarify.run(state["brief"])
        return {"brief": result.brief, "notes": result.notes}

    # ---- 对等多 Agent 协作（仅在配置了模型时启用）----
    _peer_tool_ids: dict[str, str] = {}

    def _peer_emit(kind: str, /, **payload) -> None:
        """把 Agent 运行时的动作翻译成前端能读懂的事件。

        工具调用沿用既有 tool_call/tool_result 契约，其余动作写成 agent_message，
        这样对话抽屉、事件回放和 SQLite 落库都不用为多 Agent 单开一套。
        """
        agent = str(payload.get("agent") or "system")
        if kind == "tool_call":
            # 以运行时给出的调用 id 为准：同一个 Agent 一次返回多个工具调用时，
            # 用"每 Agent 一个槽位"会把后面的结果配到错误的调用上。
            cid = str(payload.get("call_id") or "")
            if not cid:
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
                    "evidence_id": payload.get("evidence_id"),
                    "tool_call_id": str(payload.get("call_id") or _peer_tool_ids.get(agent) or uuid4().hex),
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

        # 深度团队受运行级请求数、Agent 回合数和总时长保护，达到上限立即停下并请示用户。
        client = shared_llm
        result = await peer_hub.run_session(
            brief=state["brief"],
            source=source,
            client=client,
            emit=_peer_emit,
            max_rounds=max(1, settings.orchestration.max_revisions),
            max_turns=settings.orchestration.max_agent_turns,
            resume=state.get("peer_state"),
            user_answer=state.get("peer_answer") or "",
            finalize_feedback=str(state.get("peer_finalize_feedback") or ""),
        )

        updates: dict = {
            "peer_state": result.session,
            "llm_stats": shared_llm.stats() if shared_llm else {},

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
            updates["peer_stopped"] = False
            updates["peer_stop_reason"] = ""
            return updates
        if result.stopped:
            updates["peer_question"] = None
            updates["peer_stopped"] = True
            updates["peer_stop_reason"] = result.stop_reason
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
                "kind": "peer_review_risk",
                "type": q.get("type") or "peer_escalation",
                "question": q.get("question") or "需要你确认一件事",
                "from": q.get("from"),
                "options": q.get("options") or [],
                "issue_codes": list(q.get("issue_codes") or []),
            }
        )
        if isinstance(answer, dict):
            decision = str(answer.get("decision") or "")
            issue_codes = answer.get("accepted_issue_codes") or []
            accepted = set(issue_codes) if isinstance(issue_codes, list) else set()
            question_data = state.get("peer_question") or {}
            question = question_data.get("question", "")
            if decision == "accept_risk" and not accepted:
                raise ValueError("接受评审风险时必须明确接受的 issue code")
            if decision == "accept_risk" and set(accepted) & set(question_data.get("issue_codes") or []):
                raise ValueError("确定性硬约束 blocker 不允许通过 accept_risk 绕过；请继续修正或停止")
            answer_text = json.dumps(
                {"kind": answer.get("kind", "peer_review_risk"), "decision": decision,
                 "accepted_issue_codes": sorted(str(code) for code in accepted),
                 "note": str(answer.get("note") or ""), "question": question,
                 "issue_codes": list(question_data.get("issue_codes") or [])},
                ensure_ascii=False,
            )
        else:
            answer_text = str(answer)
        return {"peer_answer": answer_text}

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
        blockers = [i for i in critique.issues if i.severity == "blocker"]
        feedback = "；".join(f"[{i.code}] {i.message}" for i in blockers)
        if not critique.passed and feedback:
            _emit({"phase": "peer_finalize_feedback", "text": feedback})
        next_revision = int(state.get("revision", 0)) + 1
        unresolved = bool(not critique.passed and next_revision >= max_revisions)
        return {
            "critique": critique,
            "revision": next_revision,
            "peer_finalize_feedback": feedback if not critique.passed else "",
            "peer_issue_codes": [i.code for i in critique.issues],
            "peer_question": {
                "from": "critic_agent",
                "question": (
                    f"硬约束复核仍未通过（{feedback}），不能把当前方案标记为可交付。"
                    "Agent 已达到本次返工上限；请选择停止并保留问题清单，或调整预算/需求后重新发起规划。"
                ),
                "type": "hard_constraint_unresolved",
                "issue_codes": [i.code for i in critique.issues if i.severity == "blocker"],
                "options": [
                    {"value": "stop", "label": "停止并保留问题清单"},
                    {"value": "restart", "label": "调整需求后重新规划"},
                ],
            } if unresolved and enable_interrupt else None,
            "llm_stats": shared_llm.stats() if shared_llm else {},
        }

    def route_after_peer(state: TripState) -> str:
        if state.get("peer_stopped"):
            return END
        return "peer_ask" if state.get("peer_question") else "peer_finalize"

    def route_after_peer_finalize(state: TripState) -> str:
        draft = state.get("draft")
        critique = state.get("critique")
        if draft is None or critique is None:
            return END
        if not critique.passed:
            if state.get("peer_question"):
                return "peer_ask"
            if state.get("revision", 0) >= max_revisions:
                return END
            return "peer_run"
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
    g.add_edge(START, "clarify")

    # 单模式：只保留深度团队图，不再生成 fast/expert/legacy 分支。
    g.add_node("peer_run", wrap("peer_run", peer_run_node))
    g.add_node("peer_ask", wrap("peer_ask", peer_ask_node))
    g.add_node("peer_finalize", wrap("peer_finalize", peer_finalize_node))
    g.add_edge("clarify", "peer_run")
    g.add_conditional_edges(
        "peer_run",
        route_after_peer,
        {END: END, "peer_ask": "peer_ask", "peer_finalize": "peer_finalize"},
    )
    g.add_edge("peer_ask", "peer_run")
    g.add_conditional_edges(
        "peer_finalize", route_after_peer_finalize, {END: END, "peer_run": "peer_run", "peer_ask": "peer_ask"}
    )
    return g.compile(checkpointer=checkpointer)
