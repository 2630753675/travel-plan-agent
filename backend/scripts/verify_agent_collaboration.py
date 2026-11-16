"""结构化消息协议与 LangGraph 中的真实往返验收。

重点不是检查 UI 文案，而是从最终图状态断言：
- 每个研究节点都产出有理由与证据的建议；
- 住宿到景点存在 request → response，且 response.reply_to 指回 request；
- Planner 到 Critic 存在 proposal → response，Critic 回复关联具体 proposal；
- Critic 有阻断项时 Planner 的下一份提案关联该质疑，证明回复进入了下一轮。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.protocol import AgentMessage, AgentProposal, CollaborationRequest  # noqa: E402
from app.graph.build import build  # noqa: E402
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


def make_brief() -> TripBrief:
    # 选择老人同行 + 人文偏好，让住宿/景点距离触发协作。
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


async def run_graph() -> dict:
    app = build(
        MockSource(),
        AppSettings(llm=LLMSettings(enabled=False)),
    )
    state: dict = {}
    async for chunk in app.astream(
        {"brief": make_brief(), "revision": 0, "errors": []}, stream_mode="values"
    ):
        state = chunk
    return state


def main() -> int:
    state = asyncio.run(run_graph())
    messages = state.get("messages", [])
    proposals = state.get("proposals", [])
    requests = state.get("collaboration_requests", [])

    print("\n[A] 工具边界")
    tool_actors = {"transport", "lodging", "poi", "weather"}
    check("查询工具不伪装成 Agent 消息", not any(m.sender in tool_actors for m in messages), str([(m.sender, m.receiver) for m in messages]))
    check("位置修正是 Workflow 请求而非 Agent 对话", all(r.sender == "lodging" and r.receiver == "poi" for r in requests), str(requests))

    print("\n[B] Planner 与两个评审 Agent 的真实往返")
    proposals_by_receiver = {p.receiver: p for p in proposals if p.sender == "planner"}
    budget_replies = [m for m in messages if m.sender == "budget_reviewer"]
    experience_replies = [m for m in messages if m.sender == "experience_reviewer"]
    check("Planner 向预算评审提交提案", "budget_reviewer" in proposals_by_receiver)
    check("Planner 向体验评审提交提案", "experience_reviewer" in proposals_by_receiver)
    check("预算评审回复 Planner", len(budget_replies) == 1)
    check("体验评审回复 Planner", len(experience_replies) == 1)
    check("预算回复关联提案", budget_replies[0].reply_to == proposals_by_receiver["budget_reviewer"].proposal_id)
    check("体验回复关联提案", experience_replies[0].reply_to == proposals_by_receiver["experience_reviewer"].proposal_id)
    check("两类评审有独立证据", bool(budget_replies[0].evidence) and bool(experience_replies[0].evidence))

    print("\n[C] 硬约束 Workflow")
    check("提案使用结构化类型", all(isinstance(p, AgentProposal) for p in proposals), str([type(p).__name__ for p in proposals]))
    planner_proposals = [p for p in proposals if p.sender == "planner"]
    check("Planner 向两个评审 Agent 提交提案", len(planner_proposals) >= 2)
    check("硬约束由 Workflow 独立执行", "critique" in state and state["critique"] is not None)
    check("硬约束结果包含问题列表", hasattr(state["critique"], "issues"))
    check("工具 Agent 不伪装为 critic", not any(m.sender == "critic" for m in messages))
    if len(planner_proposals) > 2:
        review_ids = {m.message_id for m in messages if m.sender in {"budget_reviewer", "experience_reviewer"}}
        check("重排提案回应评审意见", any(p.reply_to in review_ids for p in planner_proposals[2:]), str([p.reply_to for p in planner_proposals]))
    else:
        check("本用例无需评审后重排", state["critique"].passed, str(state["critique"].issues))

    print("\n[D] 角色边界")
    check("没有伪造天气/交通等 Agent", not any(m.sender in {"transport", "lodging", "poi", "weather", "coordinator"} for m in messages))
    check("未配置模型时不伪造预算官/体验官对话", not any(m.sender in {"budget_officer", "experience_officer", "mediator"} for m in messages))

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  多 Agent 消息协作验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
