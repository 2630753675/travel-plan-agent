"""P2 验收：mock 数据层 + Pydantic 模型 + 交通 agent。

断言覆盖五条路径：
1. 正常航线：往返都有候选，且按设计偏好选中
2. 去程偏好：中午前落地
3. 返程偏好：下午再飞
4. 无此航线：返回空结果 + 明确提示，不抛异常
5. 数据源不可用：降级为 error 文本，不抛穿
外加 TripBrief 的非法输入必须被拒绝。
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from pydantic import ValidationError

from app.agents import transport
from app.models import TripBrief
from app.tools import mock as mock_mod
from app.tools.mock import MockSource

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def brief(**kw) -> TripBrief:
    base = dict(
        origin="上海",
        destination="成都",
        start_date=date(2026, 11, 20),
        end_date=date(2026, 11, 24),
        travelers=2,
        budget_total=8000,
        companions=["老人"],
    )
    base.update(kw)
    return TripBrief(**base)


def main() -> int:
    src = MockSource()

    print("\n[1] 正常航线上海→成都")
    plan = transport.run(brief(), src)
    print(f"  去程候选 {len(plan.outbound)} 条, 返程候选 {len(plan.inbound)} 条")
    print(f"  选中: {plan.chosen_outbound.flight_no} {plan.chosen_outbound.dep_time}-{plan.chosen_outbound.arr_time}"
          f" / {plan.chosen_inbound.flight_no} {plan.chosen_inbound.dep_time}-{plan.chosen_inbound.arr_time}")
    print(f"  往返成本 {plan.round_trip_cost:.0f}")
    check("去程有候选", len(plan.outbound) == 4, f"实际 {len(plan.outbound)}")
    check("返程有候选", len(plan.inbound) == 3, f"实际 {len(plan.inbound)}")
    check("无错误", plan.error is None, str(plan.error))

    print("\n[2] 去程偏好：中午前落地")
    check(
        "选中航班 12:00 前到达",
        plan.chosen_outbound.arr_time.hour < 12,
        f"实际 {plan.chosen_outbound.arr_time}",
    )

    print("\n[3] 返程偏好：下午再飞")
    check(
        "选中返程 15:00 后起飞",
        plan.chosen_inbound.dep_time.hour >= 15,
        f"实际 {plan.chosen_inbound.dep_time}",
    )

    print("\n[4] 无此航线 上海→拉萨")
    plan2 = transport.run(brief(destination="拉萨"), src)
    print(f"  error = {plan2.error}")
    check("无候选", plan2.chosen_outbound is None)
    check("给出明确提示而非异常", plan2.error is not None and "未查到" in plan2.error)

    print("\n[5] 数据源不可用")
    original = mock_mod.DATA_DIR
    mock_mod.DATA_DIR = pathlib.Path(BACKEND) / "data" / "__missing__"
    try:
        plan3 = transport.run(brief(), src)
    finally:
        mock_mod.DATA_DIR = original
    print(f"  error = {plan3.error}")
    check("降级为 error 文本", plan3.error is not None and "不可用" in plan3.error)
    check("降级后无候选", plan3.chosen_outbound is None)

    print("\n[6] TripBrief 非法输入")
    cases = [
        ("日期倒置", dict(end_date=date(2026, 11, 1))),
        ("出发地等于目的地", dict(destination="上海")),
        ("同行人群互斥冲突", dict(companions=["无", "老人"])),
        ("预算为负", dict(budget_total=-1)),
        ("天数过长", dict(end_date=date(2026, 12, 30))),
    ]
    for name, patch in cases:
        try:
            brief(**patch)
        except ValidationError:
            print(f"  PASS  拒绝: {name}")
        else:
            print(f"  FAIL  未拒绝: {name}")
            failures.append(f"TripBrief 未拒绝 {name}")

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  数据层 / 模型校验 / 交通 agent 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
