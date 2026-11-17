"""确定性回归：输出 token 不再硬限制、旧配置迁移、运行级取消。

不调用真实模型/网络：模型 HTTP 层替换为记录 payload 的假实现，
checkpoint/store 使用临时目录与内存替身。
运行：
    .venv/Scripts/python.exe scripts/verify_budget_cancel.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
import sys
import tempfile
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import cancellation, settings as config_store  # noqa: E402
from app.cancellation import RunCancelled  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.settings import AppSettings, LLMSettings  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def brief() -> TripBrief:
    return TripBrief(
        origin="上海", destination="成都",
        start_date=date(2026, 11, 20), end_date=date(2026, 11, 24),
        travelers=2, budget_total=8000, companions=["老人"],
        preferences=["人文"], pace="适中", planning_mode="peer",
    )


def fake_reply(content: str = "ok") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_no_max_tokens() -> None:
    print("\n[A] 请求不再发送 max_tokens")
    import app.llm as llm

    captured: list[dict] = []
    original = llm._post

    def fake_post(url, payload, api_key, timeout, host):
        captured.append(payload)
        return fake_reply('{"ok": true}')

    llm._post = fake_post
    try:
        client = LLMClient(
            LLMSettings(enabled=True, base_url="http://x", model="x", api_key="k"),
            max_output_tokens=1400,  # 旧调用方即使传了也不能影响请求
        )
        asyncio.run(client.chat_tools([{"role": "user", "content": "hi"}], []))
        asyncio.run(client.chat_json("s", "u"))
        asyncio.run(client.ping())
    finally:
        llm._post = original

    check("三次请求都发到了模型层", len(captured) == 3, str(len(captured)))
    check("chat_tools 不含 max_tokens", "max_tokens" not in captured[0], str(captured[0].keys()))
    check("chat_json 不含 max_tokens", "max_tokens" not in captured[1], str(captured[1].keys()))
    check("ping 不含 max_tokens", "max_tokens" not in captured[2], str(captured[2].keys()))
    check("仍然记录 usage", client.usage_calls == 3 and client.stats()["total_tokens"] == 45,
          f"{client.usage_calls}/{client.prompt_tokens}/{client.completion_tokens}")


def test_settings_migration() -> None:
    print("\n[B] 旧配置里的 1400 被迁移为不限制")
    tmp = pathlib.Path(tempfile.mkdtemp()) / "cfg.db"
    old_payload = {
        "llm": {"enabled": True, "base_url": "http://x", "model": "x", "api_key": "k"},
        "orchestration": {"max_output_tokens": 1400, "max_model_calls": 8},
        "datasource": {"kind": "mock"},
    }
    conn = sqlite3.connect(tmp)
    conn.execute("CREATE TABLE app_config (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)")
    conn.execute("INSERT INTO app_config (id, payload) VALUES (1, ?)", (json.dumps(old_payload),))
    conn.commit()
    conn.close()

    original_path = config_store.DB_PATH
    config_store.DB_PATH = tmp
    try:
        loaded = config_store.load()
        check("load 后输出上限为 None", loaded.orchestration.max_output_tokens is None,
              str(loaded.orchestration.max_output_tokens))
        check("其余旧配置保留", loaded.orchestration.max_model_calls == 8,
              str(loaded.orchestration.max_model_calls))
        merged = config_store.merge_incoming(loaded, {"orchestration": {"max_output_tokens": 900}})
        check("前端回传旧字段也被忽略", merged.orchestration.max_output_tokens is None,
              str(merged.orchestration.max_output_tokens))
    finally:
        config_store.DB_PATH = original_path


def test_cancellation_registry() -> None:
    print("\n[C] 取消登记语义")
    token = cancellation.begin("run-reg")
    check("begin 后可查到", cancellation.get("run-reg") is token)
    check("默认未取消", not token.cancelled)
    check("request_cancel 生效", cancellation.request_cancel("run-reg") is True)
    check("标记已取消", token.cancelled)
    try:
        token.raise_if_cancelled()
    except RunCancelled:
        check("raise_if_cancelled 抛 RunCancelled", True)
    else:
        check("raise_if_cancelled 抛 RunCancelled", False)
    cancellation.finish("run-reg", token)
    check("finish 后登记移除", cancellation.get("run-reg") is None)
    check("未知运行不能取消", cancellation.request_cancel("run-reg") is False)


def test_client_cancel() -> None:
    print("\n[D] 取消后客户端不再发起请求")
    import app.llm as llm

    called: list[dict] = []
    original = llm._post
    llm._post = lambda *a, **k: called.append({"args": a}) or fake_reply()
    token = cancellation.begin("run-client")
    token.cancel()
    client = LLMClient(
        LLMSettings(enabled=True, base_url="http://x", model="x", api_key="k"),
        cancellation=token,
    )
    try:
        asyncio.run(client.chat_tools([{"role": "user", "content": "hi"}], []))
    except RunCancelled:
        check("已取消时抛出 RunCancelled", True)
    except Exception as exc:  # noqa: BLE001
        check("已取消时抛出 RunCancelled", False, type(exc).__name__)
    else:
        check("已取消时抛出 RunCancelled", False, "没有抛出")
    finally:
        llm._post = original
        cancellation.finish("run-client", token)

    check("没有真正发出请求", called == [], str(len(called)))
    check("也没有消耗调用额度", client.calls == 0, str(client.calls))


def test_run_graph_cancel() -> None:
    print("\n[E] 流式运行中途取消返回 needs_input")
    from fastapi.testclient import TestClient

    import app.api.main as api

    token = cancellation.begin("run-graph")
    saved: dict = {}

    class FakeGraph:
        def __init__(self) -> None:
            self.yielded = 0

        async def astream(self, graph_input, config=None, stream_mode=None):
            for i in range(5):
                self.yielded += 1
                if i == 2:
                    token.cancel()
                yield ("custom", {"phase": "peer_status", "step": f"s{i}", "agent": "a"})
            yield ("values", {"revision": 9, "llm_stats": {"calls": 3}})

        async def aget_state(self, config):
            raise AssertionError("取消路径不应读取终态")

    graph = FakeGraph()
    original_list = api.store.list_events
    original_append = api.store.append_event
    original_save = api.store.save_run
    api.store.list_events = lambda run_id, after_seq=0: []
    api.store.append_event = lambda event: None
    api.store.save_run = lambda run_id, status, payload: saved.update({"status": status, "payload": payload})
    try:
        async def collect() -> list[str]:
            out = []
            async for chunk in api._run_graph(
                graph, {"configurable": {"thread_id": "run-graph"}},
                {"brief": brief(), "revision": 0, "errors": []},
                "run-graph", brief(), "mock", 30, token,
            ):
                out.append(chunk)
            return out

        chunks = asyncio.run(collect())
    finally:
        api.store.list_events = original_list
        api.store.append_event = original_append
        api.store.save_run = original_save
        cancellation.finish("run-graph", token)

    events = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    kinds = [e["event_type"] for e in events]
    check("中途停止，没有跑完全部 5 个片段", graph.yielded == 3, str(graph.yielded))
    check("没有伪造完成事件", "run_completed" not in kinds, str(kinds))
    check("返回 run_needs_input", kinds.count("run_needs_input") == 1, str(kinds))
    needs = next(e for e in events if e["event_type"] == "run_needs_input")
    check("载荷标记 cancelled", needs["payload"].get("cancelled") is True, str(needs["payload"]))
    check("保存状态为 cancelled", saved.get("status") == "cancelled", str(saved.get("status")))


def test_cancel_endpoint() -> None:
    print("\n[F] 取消接口")
    from fastapi.testclient import TestClient

    import app.api.main as api

    client = TestClient(api.app, headers={"X-Access-Token": api._ACCESS_TOKEN})
    r = client.post("/api/plan/not-running/cancel")
    check("没有运行中任务返回 404", r.status_code == 404, str(r.status_code))

    token = cancellation.begin("run-api")
    try:
        r = client.post("/api/plan/run-api/cancel")
        check("进行中的运行可取消", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        check("接口返回 ok", r.json().get("ok") is True, r.text[:120])
        check("取消标记已设置", token.cancelled)
    finally:
        cancellation.finish("run-api", token)

    check("结束后不能再取消", client.post("/api/plan/run-api/cancel").status_code == 404)


def test_build_accepts_cancellation() -> None:
    print("\n[G] 图构建接受取消标记")
    from app.graph.build import build
    from app.tools.mock import MockSource

    token = cancellation.begin("run-build")
    try:
        build(MockSource(), AppSettings(), cancellation=token)
        check("build(cancellation=...) 正常", True)
    except Exception as exc:  # noqa: BLE001
        check("build(cancellation=...) 正常", False, f"{type(exc).__name__}: {exc}")
    finally:
        cancellation.finish("run-build", token)


def main() -> int:
    test_no_max_tokens()
    test_settings_migration()
    test_cancellation_registry()
    test_client_cancel()
    test_run_graph_cancel()
    test_cancel_endpoint()
    test_build_accepts_cancellation()
    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  预算与取消回归通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
