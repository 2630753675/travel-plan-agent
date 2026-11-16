"""运行时边界验收（无模型调用）：只直接验证消息路由与收敛守门。
真实体验验收另由 verify_peer_live.py 使用当前已配置模型执行，绝不安装脚本模型。
"""
from __future__ import annotations

import pathlib
import sys
from uuid import uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.agents.bus import Bus, Envelope  # noqa: E402
from app.agents.hub import _converged  # noqa: E402


def check(name: str, cond: bool) -> None:
    print(f"{'PASS' if cond else 'FAIL'} {name}")
    if not cond:
        raise SystemExit(1)


def main() -> int:
    print("[1] 未通过硬约束不能收敛")
    bus = Bus()
    proposal_id = uuid4().hex
    bus.post(Envelope(sender="planner", recipients=["budget_critic", "experience_critic"], kind="proposal", content="提案" , mid=proposal_id))
    bus.post(Envelope(sender="planner", recipients=["budget_critic", "experience_critic"], kind="note", content="硬校验失败", payload={"proposal_id": proposal_id, "hard_check": {"passed": False}}, reply_to=proposal_id))
    bus.post(Envelope(sender="budget_critic", recipients=["planner"], kind="accept", content="预算可接受", reply_to=proposal_id))
    bus.post(Envelope(sender="experience_critic", recipients=["planner"], kind="accept", content="体验可接受", reply_to=proposal_id))
    check("硬约束失败，即使双评审 accept 也不收敛", not _converged(bus))

    print("[2] 评审须回复最新提案")
    bus = Bus()
    p1 = uuid4().hex
    p2 = uuid4().hex
    bus.post(Envelope(sender="planner", recipients=[], kind="proposal", content="旧提案", mid=p1))
    bus.post(Envelope(sender="planner", recipients=[], kind="note", content="旧校验", payload={"proposal_id": p1, "hard_check": {"passed": True}}, reply_to=p1))
    bus.post(Envelope(sender="budget_critic", recipients=[], kind="accept", content="旧预算接受", reply_to=p1))
    bus.post(Envelope(sender="experience_critic", recipients=[], kind="accept", content="旧体验接受", reply_to=p1))
    bus.post(Envelope(sender="planner", recipients=[], kind="proposal", content="新提案", mid=p2))
    bus.post(Envelope(sender="planner", recipients=[], kind="note", content="新校验", payload={"proposal_id": p2, "hard_check": {"passed": True}}, reply_to=p2))
    check("上一轮评审不能接受新提案", not _converged(bus))
    bus.post(Envelope(sender="budget_critic", recipients=[], kind="accept", content="新预算接受", reply_to=p2))
    bus.post(Envelope(sender="experience_critic", recipients=[], kind="accept", content="新体验接受", reply_to=p2))
    check("同一 proposal ID 的双评审与硬校验才收敛", _converged(bus))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
