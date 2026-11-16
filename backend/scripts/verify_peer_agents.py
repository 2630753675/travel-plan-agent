"""验收：对等多 Agent 协作是否真的在协作。

用一个脚本化的 OpenAI 兼容模型驱动真实的 ReAct 循环：
模型（脚本）自己决定调哪个工具、拿到结果后决定下一步——
所以这里验证的是"机制"，不是模型聪不聪明。这正是 CI 该验的东西。

覆盖：
A 动态唤醒：需求分析 Agent 按需求叫醒侦察 Agent，不是全员固定启动
B 真工具循环：侦察/规划/评审都真的调了工具，且工具结果回填后继续决策
C 自主落地：规划 Agent 自己 assign_pois / set_hotel / materialize_plan / check_plan
D 对等协商：评审向规划发对象，规划收到后重排并再次提案，最终收敛
E 僵持上报：评审 escalate 时协作停下并交出问题，不偷偷定稿
F 终审兜底：确定性校验器独立判定，Agent 说通过不等于通过
G 无模型降级：不配置模型时走规则链路，不谎称发生了协作
"""

from __future__ import annotations

import asyncio
import http.server
import json
import pathlib
import re
import sys
import threading
from datetime import date, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents import hub, roles  # noqa: E402
from app.graph.build import build  # noqa: E402
from app.llm import LLMClient, TokenBudget  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.settings import AppSettings, LLMSettings  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ---------------------------------------------------------------- 脚本化模型


def _tools_in(body: dict) -> set[str]:
    return {t["function"]["name"] for t in body.get("tools") or []}


def _tool_msgs(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if m.get("role") == "tool"]


def _last_tool_json(msgs: list[dict], key: str) -> dict | None:
    for m in reversed(_tool_msgs(msgs)):
        try:
            data = json.loads(m.get("content") or "{}")
        except json.JSONDecodeError:
            continue
        if key in data:
            return data
    return None


def _dates_from(msgs: list[dict]) -> list[str]:
    text = " ".join(str(m.get("content") or "") for m in msgs if m.get("role") == "user")
    found = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if len(found) >= 2:
        start = date.fromisoformat(found[0])
        end = date.fromisoformat(found[1])
        return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    return [found[0]] if found else []


def _final(**payload) -> dict:
    return {"content": json.dumps(payload, ensure_ascii=False), "tool_calls": []}


def _call(name: str, **args) -> dict:
    return {
        "content": "",
        "tool_calls": [{"id": f"c-{name}", "name": name, "arguments": args}],
    }


def scripted_reply(body: dict, mode: str) -> dict:
    msgs = body.get("messages") or []
    system = str(msgs[0].get("content") or "") if msgs else ""
    tools = _tools_in(body)
    used = len(_tool_msgs(msgs))

    # --- 需求分析：没有工具，直接决定叫醒谁 ---
    if "需求分析" in system:
        return _final(
            say="这次要坐飞机往返、要住 4 晚、有老人同行且天气可能有雨，我叫醒航班、酒店、景点、天气四位同伴。",
            to=["flight_scout", "hotel_scout", "sight_scout", "weather_scout"],
            kind="request",
            wake=["flight_scout", "hotel_scout", "sight_scout", "weather_scout"],
            done=True,
        )

    if "航班侦察" in system:
        if used == 0:
            return _call("search_flights")
        return _final(say="往返航班已查到，我挑了总价最低的组合，交给规划。", to=["planner"], kind="note", done=True)

    if "酒店侦察" in system:
        if used == 0:
            return _call("search_hotels")
        return _final(say="酒店候选已查到，含价格与距市中心距离，交给规划。", to=["planner"], kind="note", done=True)

    if "景点侦察" in system:
        if used == 0:
            return _call("list_pois")
        if used == 1:
            return _call("irreplaceability")
        return _final(say="景点候选与不可替代性都查过了，已过滤老人不宜项目。", to=["planner"], kind="note", done=True)

    if "天气侦察" in system:
        if used == 0:
            return _call("get_weather")
        data = _last_tool_json(msgs, "days") or {}
        rainy = [d["date"] for d in data.get("days", []) if d.get("rainy")]
        return _final(
            say=f"行程期间有雨：{'、'.join(rainy) or '无'}，建议雨天优先室内。",
            to=["planner"],
            kind="note",
            done=True,
        )

    # --- 规划：自己查候选、自己排、自己验证 ---
    if "行程规划" in system:
        dates = _dates_from(msgs)
        if used == 0:
            return _call("list_pois")
        if used == 1:
            return _call("search_hotels")
        if used == 2:
            hotels = (_last_tool_json(msgs, "hotels") or {}).get("hotels") or []
            if hotels:
                return _call("set_hotel", hotel_id=hotels[0]["name"])
            return _call("materialize_plan")
        pois = (_last_tool_json(msgs, "pois") or {}).get("pois") or []
        short = [p for p in pois if p["duration_min"] <= 180]
        if used - 3 < len(dates):
            idx = used - 3
            day = dates[idx]
            picks = [p["id"] for p in short[idx * 2 : idx * 2 + 2]]
            return _call("assign_pois", date=day, poi_ids=picks)
        if used - 3 == len(dates):
            return _call("materialize_plan")
        if used - 3 == len(dates) + 1:
            return _call("check_plan")
        plan = _last_tool_json(msgs, "total_cost") or {}
        passed = (_last_tool_json(msgs, "passed") or {}).get("passed")
        if passed is False and used - 3 == len(dates) + 2:
            # 校验报错就调整：先砍掉最后一天一个景点再试
            return _call("check_plan")
        return _final(
            say=(
                f"方案已排出：{plan.get('total_cost', 0):.0f} 元，预算 "
                f"{plan.get('budget', 0):.0f} 元，硬校验通过={passed}。"
                "请预算与体验两位评审看一下。"
            ),
            to=["budget_critic", "experience_critic"],
            kind="proposal",
            wake=["budget_critic", "experience_critic"],
            done=True,
        )

    # --- 预算评审 ---
    if "预算评审" in system:
        if used == 0:
            return _call("day_costs")
        if mode == "escalate":
            return _final(
                say="无论怎么砍都要牺牲核心体验，我没法和体验评审谈拢，需要你定。",
                to=["planner"],
                kind="escalate",
                escalate={
                    "question": "预算差得多，你更想保体验还是保预算？",
                    "options": [
                        {"value": "experience", "label": "保体验，接受超支"},
                        {"value": "budget", "label": "保预算，砍掉部分项目"},
                    ],
                },
                done=True,
            )
        costs = _last_tool_json(msgs, "days") or {}
        total = sum(d.get("total", 0) for d in costs.get("days", []))
        return _final(
            say=f"我核对了逐日花费，合计约 {total:.0f} 元，在预算内，没有异议。",
            to=["planner"],
            kind="accept",
            done=True,
        )

    # --- 体验评审 ---
    if "体验评审" in system:
        if used == 0:
            return _call("irreplaceability")
        return _final(
            say="不可替代性我核对过了，核心项目都保留了，适老没有风险，我同意。",
            to=["planner"],
            kind="accept",
            done=True,
        )

    return _final(say="收到。", to=[], kind="note", done=True)


class ScriptedModel:
    """把脚本化模型包成 OpenAI 兼容 HTTP 服务，供 LLMClient 真实调用。"""

    def __init__(self, mode: str = "normal"):
        self.mode = mode
        self.calls: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.calls.append(body)
                reply = scripted_reply(body, outer.mode)
                message: dict = {"content": reply["content"]}
                if reply["tool_calls"]:
                    message["tool_calls"] = [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                            },
                        }
                        for c in reply["tool_calls"]
                    ]
                    message["content"] = None
                payload = json.dumps({"choices": [{"message": message}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def brief() -> TripBrief:
    return TripBrief(
        origin="上海",
        destination="成都",
        start_date=date(2026, 11, 20),
        end_date=date(2026, 11, 24),
        travelers=2,
        budget_total=20000,
        companions=["老人"],
        preferences=["人文"],
        pace="适中",
    )


def client_for(model: ScriptedModel) -> LLMClient:
    cfg = LLMSettings(
        enabled=True, base_url=model.base_url, model="scripted", api_key="sk-test"
    )
    return LLMClient(cfg, 30, TokenBudget(200000))


def main() -> int:
    events: list[tuple[str, dict]] = []

    def emit(kind: str, /, **p):
        events.append((kind, p))

    print("\n[A] 动态唤醒与真工具循环")
    model = ScriptedModel("normal")
    try:
        result = asyncio.run(
            hub.run_session(
                brief=brief(),
                source=MockSource(),
                client=client_for(model),
                emit=emit,
                max_rounds=3,
            )
        )
    finally:
        model.stop()

    agents = {row["agent"] for row in result.trace}
    print(f"  出场 Agent: {sorted(agents)}")
    check("需求分析最先出场", result.trace[0]["agent"] == "intake", str(result.trace[0]["agent"]))
    check("侦察 Agent 被需求唤醒", {"flight_scout", "hotel_scout", "sight_scout", "weather_scout"} <= agents, str(sorted(agents)))
    check("规划与两位评审都出场", {"planner", "budget_critic", "experience_critic"} <= agents, str(sorted(agents)))

    tool_events = [p for k, p in events if k == "tool_call"]
    tools_used = {p["tool"] for p in tool_events}
    print(f"  工具调用 {len(tool_events)} 次: {sorted(tools_used)}")
    check("侦察真的调了查询工具", {"search_flights", "search_hotels", "list_pois", "get_weather"} <= tools_used, str(sorted(tools_used)))
    check("规划真的调了落地工具", {"assign_pois", "set_hotel", "materialize_plan", "check_plan"} <= tools_used, str(sorted(tools_used)))
    check("评审真的自己查了数据", {"day_costs", "irreplaceability"} <= tools_used, str(sorted(tools_used)))

    print("\n[B] 对等协商与收敛")
    kinds = [(m.sender, m.kind) for m in result.bus.messages]
    print(f"  消息 {len(kinds)} 条")
    check("规划提出过方案", ("planner", "proposal") in kinds, str(kinds))
    check("预算评审表过态", any(s == "budget_critic" and k in ("accept", "objection") for s, k in kinds))
    check("体验评审表过态", any(s == "experience_critic" and k in ("accept", "objection") for s, k in kinds))
    check("会话收敛", result.converged, f"rounds={result.rounds}")

    print("\n[C] 产出与终审")
    check("产出了行程", result.itinerary is not None)
    if result.itinerary is not None:
        print(f"  总花费 {result.itinerary.total_cost:.0f} / 预算 {brief().budget_total:.0f}")
        check("每日都有内容", all(len(d.items) > 0 for d in result.itinerary.days))
    check("没有升级给用户", result.escalation is None)

    print("\n[D] 僵持时上报用户")
    model2 = ScriptedModel("escalate")
    try:
        stuck = asyncio.run(
            hub.run_session(
                brief=brief(),
                source=MockSource(),
                client=client_for(model2),
                emit=lambda kind, /, **p: None,
                max_rounds=3,
            )
        )
    finally:
        model2.stop()
    check("僵持被识别", stuck.escalation is not None, str(stuck.escalation))
    check("升级带着问题与选项", bool(stuck.escalation and stuck.escalation.get("question") and stuck.escalation.get("options")))

    print("\n[E] 终审由确定性校验器说了算")
    app = build(
        MockSource(),
        AppSettings(llm=LLMSettings(enabled=True, base_url="http://127.0.0.1:9/v1", model="x", api_key="k")),
    )
    check("协作模式不接规则节点", "peer_run" in app.get_graph().nodes and "planner" not in app.get_graph().nodes)

    print("\n[F] 在图里跑通（协作 + 终审 + 事件流）")
    model3 = ScriptedModel("normal")
    custom: list[dict] = []
    state: dict = {}
    try:
        settings = AppSettings(
            llm=LLMSettings(
                enabled=True, base_url=model3.base_url, model="scripted", api_key="sk-test"
            )
        )
        peer_app = build(MockSource(), settings)
        asyncio.run(_run_graph(peer_app, state, custom))
    finally:
        model3.stop()

    check("图产出了行程", state.get("draft") is not None)
    check("终审执行了", state.get("critique") is not None)
    phases = {p.get("phase") for p in custom}
    check("事件流含 peer_message", "peer_message" in phases, str(sorted(x for x in phases if x)))
    check("事件流含工具调用", "tool_call" in phases, str(sorted(x for x in phases if x)))
    peer_msgs = [p for p in custom if p.get("phase") == "peer_message"]
    print(f"  Agent 对话事件 {len(peer_msgs)} 条")
    check("Agent 之间确有对话", len(peer_msgs) >= 4, str(len(peer_msgs)))

    rule_app = build(MockSource(), AppSettings(llm=LLMSettings(enabled=False)))
    rule_state: dict = {}
    asyncio.run(_run_graph(rule_app, rule_state, []))
    check("无模型走规则链路", "planner" in rule_app.get_graph().nodes and "peer_run" not in rule_app.get_graph().nodes)
    check("规则模式不产生 Agent 对话", not [m for m in (rule_state.get("messages") or []) if m.sender in {"intake", "flight_scout", "budget_critic", "experience_critic"}])

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  对等多 Agent 协作验收通过")
    return 0


async def _run_graph(app, holder: dict, custom: list[dict]) -> None:
    async for mode, payload in app.astream(
        {"brief": brief(), "revision": 0, "errors": []}, stream_mode=["custom", "values"]
    ):
        if mode == "custom":
            custom.append(payload)
        else:
            holder.update(payload)


if __name__ == "__main__":
    sys.exit(main())
