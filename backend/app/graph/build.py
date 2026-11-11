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
        """预算官 vs 体验官，由调解者裁决。

        只在模型可用时启用：没有模型就没有"主张"可言，直接走规则降配。
        """
        _emit({"phase": "start", "node": "negotiation"})
        if not settings.llm.configured:
            _emit(
                {
                    "phase": "end",
                    "node": "negotiation",
                    "summary": "模型未启用，跳过协商，直接走规则降配",
                }
            )
            return {"negotiated": True}

        client = LLMClient(
            settings.llm,
            settings.orchestration.node_timeout_s,
            TokenBudget(settings.orchestration.token_budget),
        )
        try:
            log, drops, cheaper = await negotiators.negotiate(
                state["brief"], state["draft"], state["poi"].candidates, client
            )
        except Exception as e:
            _emit({"phase": "end", "node": "negotiation", "summary": f"协商异常，回落规则：{e}"})
            return {"negotiated": True, "errors": [f"negotiation: {e}"]}

        _emit({"phase": "end", "node": "negotiation", "summary": log.summary})
        return {
            "negotiation": log,
            "negotiated": True,
            "forced_drops": sorted(drops),
            "force_cheaper_hotel": cheaper,
        }

    async def confirm_node(state: TripState) -> dict:
        """预算严重超支时暂停，让用户在三个选项里决定。

        这是副作用极小的节点：只调 interrupt()，不做任何计算，
        因为恢复时该节点会从头重放。
        """
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
        return {"decision": str(decision)}

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
            # 谈不拢，或压根提不出可执行的削减项：都该由用户决定，
            # 不能因为"没得砍"就悄悄给一份超支行程。
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
    g.add_conditional_edges("negotiation", route_after_negotiation, negotiation_targets)

    if enable_interrupt:
        g.add_node("confirm", confirm_node)
        g.add_conditional_edges("confirm", route_after_confirm, {"planner": "planner", END: END})

    return g.compile(checkpointer=checkpointer)
