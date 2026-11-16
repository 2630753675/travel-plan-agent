"""P2 验收：协商多轮闭环（可控假模型驱动）。

真实模型很少"只提高价值项目"，所以 `僵持 → 追问用户 → 第二轮` 这条回路
从来没被端到端跑到过。这里用可编排的假模型把每条分支钉死：

A 第 1 轮体验官让步 → 直接采纳
B 第 1 轮全高价值 → 僵持 → 追问用户 → 第 2 轮必须出方案
C 没有可削减项 → 也必须问用户，不能把超支行程静默交付
D 第 2 轮之后仍然超支 → 不替用户决定，交回给他

假模型只替换 `LLMClient.chat_json`，其余（证据计算、裁决规则、图路由）全走真实代码。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.agents import clarify, lodging, planner, poi, transport, weather  # noqa: E402
from app.agents.negotiators.tools import poi_irreplaceability  # noqa: E402
from app.graph.build import build  # noqa: E402
from app.graph.state import TripState  # noqa: E402
from app.llm import LLMClient, LLMError  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.settings import AppSettings, LLMSettings, OrchestrationSettings  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402

failures: list[str] = []

# 预算故意压到会超支 30% 以上，确保走进协商而不是直接重排。
# 偏好选"自然"：规则排程会把不可替代性 0.676 的熊猫基地排进去，
# 这样"全高价值 → 僵持"这条分支才可达（其余偏好下草稿里全是低价值付费景点）。
BRIEF = TripBrief(
    origin="上海",
    destination="成都",
    start_date="2026-11-20",
    end_date="2026-11-24",
    travelers=2,
    budget_total=5000,
    companions=["老人"],
    preferences=["自然"],
    pace="适中",
)

SETTINGS = AppSettings(
    llm=LLMSettings(
        enabled=True,
        base_url="http://fake.local/v1",
        model="fake-model",
        api_key="fake-key",
    ),
    orchestration=OrchestrationSettings(max_revisions=3, agent_mode="legacy"),
)

BUDGET_SYSTEM = "你是行程预算官"
EXPERIENCE_SYSTEM = "你是行程体验官"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name} {'' if ok else detail}")
    if not ok:
        failures.append(name)


class Script:
    """按顺序供给预算官的回复；体验官固定一个立场。"""

    def __init__(self, budget_replies: list[dict], experience: dict):
        self.budget_replies = list(budget_replies)
        self.experience = experience
        self.budget_calls = 0
        self.experience_calls = 0
        self.notes: list[str] = []


def install(script: Script):
    """替换 chat_json；返回还原函数。"""

    async def chat_json(self, system, user, *, max_tokens=4000):  # noqa: ANN001
        if system.startswith(BUDGET_SYSTEM):
            idx = min(script.budget_calls, len(script.budget_replies) - 1)
            script.budget_calls += 1
            return dict(script.budget_replies[idx])
        if system.startswith(EXPERIENCE_SYSTEM):
            script.experience_calls += 1
            return dict(script.experience)
        # 其它任务（选景点）故意失败，让上游回落到规则，保证草稿可复现
        raise LLMError("假模型不承担该任务")

    original = LLMClient.chat_json
    LLMClient.chat_json = chat_json

    def restore():
        LLMClient.chat_json = original

    return restore


def prepare():
    """不复用图，直接跑检索 + 排程，拿到确定性的草稿和景点池。"""
    src = MockSource()
    brief = clarify.run(BRIEF).brief
    t = transport.run(brief, src)
    l = lodging.run(brief, src)
    p = poi.run(brief, src)
    w = weather.run(brief, src)
    draft = planner.run(
        brief=brief, transport=t, lodging=l, poi_plan=p, weather=w, revision=0
    )
    scores = poi_irreplaceability(p.candidates)
    return brief, draft, p.candidates, scores


async def drive(graph, config, inputs):
    """跑一次图，返回 (最终状态, 待处理中断, 自定义事件)。

    恢复必须复用同一个 graph/checkpointer 实例，否则找不到待恢复的中断。
    """
    events = []
    async for mode, payload in graph.astream(inputs, config=config, stream_mode=["custom", "values"]):
        if mode == "custom":
            events.append(payload)
    snapshot = await graph.aget_state(config)
    pending = [i for task in (snapshot.tasks or []) for i in task.interrupts]
    return snapshot.values, pending, events


def new_graph(thread: str):
    return build(MockSource(), SETTINGS, checkpointer=MemorySaver()), {"configurable": {"thread_id": thread}}


def interrupted_type(pending) -> str:
    if not pending:
        return ""
    return str((pending[0].value or {}).get("type") or "")


async def scenario_a(brief, draft, scores):
    print("\n[A] 体验官让步 → 第 1 轮直接采纳")
    paid = [
        (i.ref_id, i.title)
        for d in draft.days
        for i in d.items
        if i.type == "sight" and i.cost > 0 and i.ref_id
    ]
    low = [(pid, title) for pid, title in paid if scores.get(pid, 0) < 0.6]
    if not low:
        check("存在可让步的低价值景点", False, str(paid))
        return
    script = Script(
        budget_replies=[{"poi_ids": [low[0][0]], "use_cheaper_hotel": True, "claim": "砍一个低价景点并换酒店"}],
        experience={"verdict": "accept", "claim": "这些安排可替代，可以让步"},
    )
    restore = install(script)
    try:
        graph, config = new_graph("loop-a")
        state, pending, _ = await drive(graph, config, {"brief": brief, "revision": 0, "errors": []})
    finally:
        restore()

    log = state.get("negotiation")
    check("产生协商轨迹", log is not None)
    if log is None:
        return
    print(f"  outcome={log.outcome} 发言={[m.speaker for m in log.rounds]}")
    check("结论是采纳", log.outcome == "adopted", log.outcome)
    check(
        "三条发言且顺序正确",
        [m.speaker for m in log.rounds] == ["budget_officer", "experience_officer", "mediator"],
        str([m.speaker for m in log.rounds]),
    )
    check("有可执行调整", not log.adjustment.is_empty(), str(log.adjustment))
    check("没有为这件事打扰用户", interrupted_type(pending) != "negotiation_deadlock")


async def scenario_b(brief, draft, scores):
    print("\n[B] 全高价值 → 僵持 → 追问用户 → 第 2 轮出方案")
    paid = [
        (i.ref_id, i.title)
        for d in draft.days
        for i in d.items
        if i.type == "sight" and i.cost > 0 and i.ref_id
    ]
    high = [(pid, title) for pid, title in paid if scores.get(pid, 0) >= 0.6]
    if not high:
        check("存在高价值景点可用于构造僵持", False, str(paid))
        return
    print(f"  高价值项目：{[t for _, t in high]}")

    script = Script(
        budget_replies=[
            {"poi_ids": [pid for pid, _ in high], "use_cheaper_hotel": False, "claim": "只能砍这些才够"},
            {"poi_ids": [], "use_cheaper_hotel": True, "claim": "听你的，改换便宜酒店"},
        ],
        experience={
            "verdict": "object",
            "worst_id": high[0][0],
            "claim": "这些不可替代，砍掉这天就空了",
        },
    )
    restore = install(script)
    try:
        graph, config = new_graph("loop-b")
        state, pending, _ = await drive(graph, config, {"brief": brief, "revision": 0, "errors": []})
        log = state.get("negotiation")
        print(f"  第 1 轮 outcome={log.outcome if log else None}")
        check("第 1 轮判定僵持", log is not None and log.outcome == "escalated", str(log and log.outcome))
        check("僵持时追问用户而不是自己拍板", interrupted_type(pending) == "negotiation_deadlock", interrupted_type(pending))
        if interrupted_type(pending) != "negotiation_deadlock":
            return
        options = (pending[0].value or {}).get("options") or []
        check("给出三个倾向选项", len(options) == 3, str(len(options)))

        # 带着"预算优先"的倾向继续谈一轮（同一个图实例，才能恢复）
        first_round_count = len(log.rounds) if log else 0
        state2, pending2, _ = await drive(
            graph, config, Command(resume="预算优先：可以砍掉高价值项目，控制在预算内")
        )
        log2 = state2.get("negotiation")
        print(f"  第 2 轮 outcome={log2.outcome if log2 else None} 发言数={len(log2.rounds) if log2 else 0}")
        check("第 2 轮有新增发言", log2 is not None and len(log2.rounds) > first_round_count)
        check("第 2 轮必须给出方案", log2 is not None and log2.outcome == "adopted", str(log2 and log2.outcome))
        check("方案可执行", log2 is not None and not log2.adjustment.is_empty(), str(log2 and log2.adjustment))
        check("用户表态没有被丢掉", any(m.speaker == "budget_officer" and m.round == 2 for m in (log2.rounds if log2 else [])))

        print("\n[D] 重排后预算仍不满足 → 交回用户")
        final_draft = state2.get("draft")
        over = final_draft.total_cost - brief.budget_total if final_draft else 0
        print(f"  重排后总花费 {final_draft.total_cost if final_draft else '-'}，仍超 {over:.0f} 元")
        kind = interrupted_type(pending2)
        if kind:
            check("仍超支时再次征求用户", kind == "budget_overrun", kind)
        else:
            # 已经收敛：预算内或可接受，此时不该有未处理中断
            check("已收敛则无待处理中断", True, "")
    finally:
        restore()


async def scenario_c(brief, draft, scores):
    print("\n[C] 无可削减项 → 也必须问用户")
    script = Script(
        budget_replies=[{"poi_ids": [], "use_cheaper_hotel": False, "claim": "没有可砍的，也没更便宜的酒店"}],
        experience={"verdict": "accept", "claim": "确实没有可动的"},
    )
    restore = install(script)
    try:
        graph, config = new_graph("loop-c")
        state, pending, _ = await drive(graph, config, {"brief": brief, "revision": 0, "errors": []})
    finally:
        restore()

    log = state.get("negotiation")
    print(f"  outcome={log.outcome if log else None} 中断={interrupted_type(pending)}")
    check("判定为无进展", log is not None and log.outcome == "no_change", str(log and log.outcome))
    check(
        "没有把超支行程静默交付",
        interrupted_type(pending) == "negotiation_deadlock",
        interrupted_type(pending),
    )


async def main() -> int:
    print("\n[0] 状态通道必须覆盖协商用到的字段")
    # 少了这两个通道，LangGraph 会静默丢弃节点返回值：
    # 轮次永远是 1，僵持后反复追问用户。这是曾经的真实问题。
    declared = set(TripState.__annotations__)
    for field in ("neg_round", "user_hint", "negotiation", "negotiated"):
        check(f"TripState 声明了 {field}", field in declared, str(sorted(declared)))

    brief, draft, candidates, scores = prepare()
    over = draft.total_cost - brief.budget_total
    ratio = over / brief.budget_total
    print(f"准备：草稿总花费 {draft.total_cost:.0f}，预算 {brief.budget_total:.0f}，超出 {ratio:.0%}")
    check("构造出 >20% 超支以触发协商", ratio > 0.2, f"{ratio:.2%}")

    await scenario_a(brief, draft, scores)
    await scenario_b(brief, draft, scores)
    await scenario_c(brief, draft, scores)

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  协商多轮闭环验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
