"""P1 验证：LangGraph 三个关键行为是否符合我们的设计假设。

1. 并行 superstep —— transport / lodging 两路并发，而不是串行
2. 条件回边     —— critic 不通过时回到 planner，revision 递增，第二次通过后结束
3. astream      —— 能逐 superstep 拿到增量事件，供前端实时展示

失败即断言退出，不静默通过。
"""

import asyncio
import sys
import time
from typing import Annotated, TypedDict
import operator

from langgraph.graph import END, START, StateGraph

# 控制台默认 GBK，中文断言信息会乱码
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class State(TypedDict):
    transport: list[str]
    lodging: list[str]
    revision: int
    passed: bool
    errors: Annotated[list[str], operator.add]


marks: list[tuple[str, float]] = []


def mark(name: str) -> None:
    marks.append((name, time.perf_counter()))


async def transport(state: State) -> dict:
    mark("transport:start")
    await asyncio.sleep(0.20)
    mark("transport:end")
    return {"transport": ["CA4101 09:00-12:00"]}


async def lodging(state: State) -> dict:
    mark("lodging:start")
    await asyncio.sleep(0.20)
    mark("lodging:end")
    return {"lodging": ["锦江宾馆 480/晚"]}


def planner(state: State) -> dict:
    mark(f"planner:turn{state.get('revision', 0)}")
    return {"passed": False}


def critic(state: State) -> dict:
    rev = state.get("revision", 0) + 1
    passed = rev >= 2
    mark(f"critic:turn{rev}:passed={passed}")
    return {"revision": rev, "passed": passed}


def route(state: State) -> str:
    return END if state["passed"] else "planner"


def build():
    g = StateGraph(State)
    g.add_node("transport", transport)
    g.add_node("lodging", lodging)
    g.add_node("planner", planner)
    g.add_node("critic", critic)

    # 从 START 同时连两条边 -> 同一 superstep 并行
    g.add_edge(START, "transport")
    g.add_edge(START, "lodging")
    # 两条边都汇入 planner -> fan-in
    g.add_edge("transport", "planner")
    g.add_edge("lodging", "planner")
    g.add_edge("planner", "critic")
    g.add_conditional_edges("critic", route, {"planner": "planner", END: END})
    return g.compile()


def find(name: str) -> float | None:
    for label, t in marks:
        if label == name:
            return t
    return None


async def main() -> int:
    app = build()

    started = time.perf_counter()
    chunks = []
    async for chunk in app.astream(
        {"revision": 0, "passed": False, "errors": []},
        stream_mode="updates",
    ):
        chunks.append(chunk)
        # 实测：updates 模式按节点完成顺序逐条产出，不是按 superstep 批量，
        # 所以并行节点会分成多条事件。前端时间线应以此为单位，不要假设批量合并。
        print(f"[event {len(chunks)}] {list(chunk.keys())}")
    elapsed = time.perf_counter() - started

    failures: list[str] = []

    # 1. 并行：两路起点几乎同时
    t_start = find("transport:start")
    l_start = find("lodging:start")
    if t_start is None or l_start is None:
        failures.append("并行节点未执行")
    else:
        gap = abs(t_start - l_start)
        print(f"并行起点间隔 {gap * 1000:.1f}ms, 总耗时 {elapsed * 1000:.0f}ms")
        if gap > 0.05:
            failures.append(f"两路不是并行启动，间隔 {gap * 1000:.0f}ms")
        if elapsed > 0.35:
            failures.append(f"总耗时 {elapsed * 1000:.0f}ms 接近串行（两个 200ms 节点）")

    # 2. 条件回边：critic 恰好两轮
    critic_turns = [x for x, _ in marks if x.startswith("critic:")]
    print(f"critic 轮次: {critic_turns}")
    if len(critic_turns) != 2:
        failures.append(f"回边次数异常，critic 执行了 {len(critic_turns)} 次，期望 2 次")

    # 3. astream：至少覆盖 fan-out / fan-in / 回边 / 结束
    print(f"事件数量: {len(chunks)}")
    if len(chunks) < 4:
        failures.append(f"astream 事件过少: {len(chunks)}")
    node_names = [k for c in chunks for k in c]
    print(f"事件顺序: {node_names}")

    print()
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print("PASS  并行 superstep / 条件回边 / astream 三个行为均符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
