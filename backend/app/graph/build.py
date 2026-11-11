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

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..agents import clarify, critic, lodging, llm_selector, planner, poi, transport, weather
from ..agents import negotiators
from ..llm import LLMClient, TokenBudget
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
            lines.append(
                entry("planner", f"排出 {len(it.days)} 天 {sights} 个景点，预计 {it.total_cost:.0f} 元", group=group)
            )
            over = it.total_cost - it.budget_total
            if over > 0:
                lines.append(entry("planner", f"超出预算 {over:.0f} 元", kind="decision", group=group))

        elif node == "critic":
            c = upd["critique"]
            blockers = [i for i in c.issues if i.severity == "blocker"]
            if c.passed:
                lines.append(entry("critic", f"通过，{len(c.issues)} 条提醒", group=group))
            else:
                lines.append(entry("critic", f"未通过，{len(blockers)} 个阻断项", kind="decision", group=group))
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

    async def clarify_node(state: TripState) -> dict:
        result = clarify.run(state["brief"])
        return {"brief": result.brief, "notes": result.notes}

    async def transport_node(state: TripState) -> dict:
        try:
            plan = transport.run(state["brief"], source)
        except Exception as e:  # 节点异常不抛穿整图
            return {
                "transport": transport.TransportPlan(error=f"交通 agent 异常：{e}"),
                "errors": [f"transport: {e}"],
            }
        return {"transport": plan}

    async def lodging_node(state: TripState) -> dict:
        try:
            plan = lodging.run(state["brief"], source)
        except Exception as e:
            return {
                "lodging": lodging.LodgingPlan(error=f"住宿 agent 异常：{e}"),
                "errors": [f"lodging: {e}"],
            }
        return {"lodging": plan}

    async def poi_node(state: TripState) -> dict:
        try:
            plan = poi.run(state["brief"], source)
        except Exception as e:
            return {
                "poi": poi.PoiPlan(error=f"景点 agent 异常：{e}"),
                "errors": [f"poi: {e}"],
            }
        return {"poi": plan}

    async def weather_node(state: TripState) -> dict:
        try:
            plan = weather.run(state["brief"], source)
        except Exception as e:
            return {
                "weather": weather.WeatherPlan(error=f"天气 agent 异常：{e}"),
                "errors": [f"weather: {e}"],
            }
        return {"weather": plan}

    async def planner_node(state: TripState) -> dict:
        critique = state.get("critique")
        # 预算已被判定超支时不再交给模型自由发挥：
        # 降配是用户明确的省钱诉求，规则层"换便宜酒店 + 优先免费景点"更可预测。
        budget_pressure = bool(
            critique and any(i.code == "BUDGET_EXCEEDED" for i in critique.issues)
        )

        llm_selection = None
        if settings.llm.configured and not budget_pressure:
            budget = TokenBudget(settings.orchestration.token_budget)
            client = LLMClient(settings.llm, settings.orchestration.node_timeout_s, budget)
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
        return {"draft": itinerary}

    async def critic_node(state: TripState) -> dict:
        critique = critic.run(
            brief=state["brief"],
            itinerary=state["draft"],
            poi_plan=state["poi"],
            transport=state["transport"],
            weather=state["weather"],
        )
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

        client = LLMClient(
            settings.llm,
            settings.orchestration.node_timeout_s,
            TokenBudget(settings.orchestration.token_budget),
        )
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

        if critique.passed:
            return END

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

    def wrap(name: str, fn):
        async def node(state: TripState) -> dict:
            _emit({"phase": "start", "node": name})
            result = await fn(state)
            from ..activity import NODE_GROUP, STAGE_NODES, stage

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
    g.add_node("transport", wrap("transport", transport_node))
    g.add_node("lodging", wrap("lodging", lodging_node))
    g.add_node("poi", wrap("poi", poi_node))
    g.add_node("weather", wrap("weather", weather_node))
    g.add_node("planner", wrap("planner", planner_node))
    g.add_node("critic", wrap("critic", critic_node))

    g.add_edge(START, "clarify")
    for name in ("transport", "lodging", "poi", "weather"):
        g.add_edge("clarify", name)
        g.add_edge(name, "planner")
    g.add_edge("planner", "critic")

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
