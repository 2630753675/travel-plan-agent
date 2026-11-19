"""专业 Agent 上下文域隔离回归：工作副本、共享账本、白名单合并。

不调用真实模型/网络。
运行：
    .venv/Scripts/python.exe scripts/verify_isolation.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from app.agents import hub  # noqa: E402
from app.agents.tools_spec import ToolContext, execute  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402
from verify_peer_success import ASSIGNMENT, DAYS, INBOUND, OUTBOUND, brief  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'} {name}{(': ' + str(detail)) if detail and not cond else ''}")
    if not cond:
        failures.append(name)


def main() -> int:
    ctx = ToolContext(brief=brief(), source=MockSource())
    sandbox = hub._sandbox_context(ctx)

    check("工作副本与主上下文相互独立", sandbox is not ctx and sandbox.transport is not ctx.transport)
    sandbox.assignment[DAYS[0]] = ["p01"]
    check("改副本不影响主上下文", ctx.assignment == {}, str(ctx.assignment))
    check("证据目录共享", sandbox.evidence is ctx.evidence)
    check("工具账本共享", sandbox.operations is ctx.operations)
    check("只读缓存共享", sandbox.cache is ctx.cache)

    # logistics 只能改交通住宿；越域改日程必须被丢弃。
    logistics = hub._sandbox_context(ctx)
    logistics_base = {f: getattr(ctx, f) for f in hub.DOMAIN_VERSIONS["logistics"]}
    execute(logistics, "search_flights", {})
    execute(logistics, "set_flights", {"outbound_flight_no": OUTBOUND, "inbound_flight_no": INBOUND})
    execute(logistics, "search_hotels", {})
    execute(logistics, "set_hotel", {"hotel_id": "春熙里和颐"})
    logistics.assignment[DAYS[1]] = ["p02"]
    logistics.fixed_events = {}
    ignored = hub._merge_sandbox(ctx, logistics, "logistics", base_versions=logistics_base)
    check("交通住宿写入被采纳", ctx.transport.chosen_outbound is not None and ctx.hotel_id == "春熙里和颐")
    check("交通住宿的越域日程写入被忽略", ctx.assignment == {} and "assignment" in ignored, ignored)
    check("版本随写入推进", ctx.plan_version == logistics.plan_version and ctx.plan_version > 0)

    # local_expert 只能改日程/固定事件；越域改酒店必须被丢弃。
    local = hub._sandbox_context(ctx)
    local_base = {f: getattr(ctx, f) for f in hub.DOMAIN_VERSIONS["local_expert"]}
    execute(local, "list_pois", {})
    for day in DAYS:
        execute(local, "assign_pois", {"date": day, "poi_ids": ASSIGNMENT[day]})
    execute(local, "materialize_plan", {})
    local.hotel_id = "被篡改的酒店"
    local.transport.chosen_outbound = None
    ignored2 = hub._merge_sandbox(ctx, local, "local_expert", base_versions=local_base)
    check("本地体验的日程写入被采纳", ctx.assignment.get(__import__("datetime").date.fromisoformat(DAYS[2]))
          == ASSIGNMENT[DAYS[2]])
    check("本地体验不能改酒店", ctx.hotel_id == "春熙里和颐" and "hotel_id" in ignored2, ignored2)
    check("本地体验不能清空航班", ctx.transport.chosen_outbound is not None and "transport" in ignored2, ignored2)
    check("落定结果与硬校验被采纳", ctx.itinerary is not None and ctx.itinerary_version == ctx.plan_version)

    # 版本仲裁：副本执行期间如果有别的写入改动了同一领域，不能覆盖，也不能部分生效。
    stale_ctx = hub._sandbox_context(ctx)
    stale = hub._sandbox_context(stale_ctx)
    stale_base = {f: getattr(stale_ctx, f) for f in hub.DOMAIN_VERSIONS["local_expert"]}
    execute(stale, "list_pois", {})
    execute(stale, "assign_pois", {"date": DAYS[0], "poi_ids": ["p01"]})
    before_assignment = dict(stale_ctx.assignment)
    before_version = stale_ctx.plan_version
    stale_ctx.assignment_version += 1  # 模拟另一个并行提议改动了同一领域
    try:
        hub._merge_sandbox(stale_ctx, stale, "local_expert", base_versions=stale_base)
    except hub.SandboxConflict:
        check("同领域版本被改动时判冲突", True)
    else:
        check("同领域版本被改动时判冲突", False)
    check("冲突时不写入任何字段", stale_ctx.assignment == before_assignment)
    check("冲突时版本不被副本推进", stale_ctx.plan_version == before_version)

    # 写不同领域的兄弟任务不应互相判冲突：固定时段任务只声明 fixed_events 域。
    other = hub._sandbox_context(ctx)
    other_domains = ("fixed_events_version",)
    other_base = {f: getattr(ctx, f) for f in other_domains}
    execute(other, "add_fixed_event", {"date": DAYS[4], "title": "送机",
                                       "start_time": "05:40", "end_time": "06:40",
                                       "kind": "airport_transfer"})
    ctx.assignment_version += 1  # 另一个任务改了日程，与固定时段无关
    before_assignment = dict(ctx.assignment)
    hub._merge_sandbox(ctx, other, "local_expert", base_versions=other_base, domains=other_domains)
    check("不同领域变化不判冲突（固定时段仍可合并）", any(
        ctx.fixed_events.get(__import__("datetime").date.fromisoformat(DAYS[4])),
    ))
    check("跨域任务不会覆盖日程", ctx.assignment == before_assignment)
    check("任务声明的领域被识别", hub._task_domains("local_expert", {"writes": ["fixed_events_version"]})
          == ("fixed_events_version",))

    print(f"\n{'PASS' if not failures else 'FAIL'} 上下文域隔离回归{'通过' if not failures else '失败'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
