"""S5 验收：界面配置层 + LLM 接入（含降级）。

用本地假 OpenAI 兼容服务验证链路，不需要真实 key：
- 正常模式返回合法的行程 JSON
- 失败模式返回 500，验证模型失败时的规则回落

需要后端已在 127.0.0.1:8848 运行。
"""

from __future__ import annotations

import http.server
import json
import re
import sys
import threading
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8848"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip().startswith("{") else {})
    except urllib.error.HTTPError as e:
        return e.code, {"detail": e.read().decode()[:200]}


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name and data is not None:
            events.append((name, data))
    return events


def plan_once(brief: dict) -> dict:
    """POST /api/plan 是 SSE，取 done 事件；失败返回 {'error': ...}。"""
    req = urllib.request.Request(
        BASE + "/api/plan",
        data=json.dumps(brief).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            events = parse_sse(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:120]}"}
    done = next((d for e, d in events if e == "done"), None)
    if done is None:
        err = next((d for e, d in events if e == "error"), None)
        return {"error": str(err or "没有 done 事件")}
    return done


BRIEF = {
    "origin": "上海",
    "destination": "成都",
    "start_date": "2026-11-20",
    "end_date": "2026-11-24",
    "travelers": 2,
    "budget_total": 9000,
    "companions": ["无"],
    "preferences": [],
    "pace": "适中",
}


class FakeLLM:
    """最小的 OpenAI 兼容 /chat/completions 服务。"""

    def __init__(self, mode: str = "ok"):
        self.mode = mode
        mode_ref = mode

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静音
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                if mode_ref == "fail":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error":"boom"}')
                    return
                try:
                    body = json.loads(raw)
                    user = body["messages"][-1]["content"]
                except Exception:
                    user = ""
                dates = re.findall(r"\d{4}-\d{2}-\d{2}", user.split("需覆盖这些日期：")[-1])
                dates = dates[:5] or ["2026-11-20"]
                days = [
                    {"date": d, "poi_ids": ["p13", "p14"], "reason": "假模型选择"}
                    for d in dates
                ]
                content = json.dumps({"days": days}, ensure_ascii=False)
                payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def llm_payload(port_base: str, *, enabled: bool, key: str = "sk-test-1234567890") -> dict:
    return {
        "llm": {
            "enabled": enabled,
            "provider": "openai-compatible",
            "base_url": port_base,
            "model": "fake-model",
            "api_key": key,
            "temperature": 0.3,
        }
    }


def snapshot() -> dict:
    _, cfg = call("GET", "/api/config")
    return cfg


def restore(cfg: dict) -> None:
    """恢复到测试前的状态。

    api_key 传的是掩码，后端会保留库里现有值——这正好保证：
    如果测试开始时已有真实密钥，恢复时不会把它改掉。
    """
    call(
        "PUT",
        "/api/config",
        {
            "llm": {
                "enabled": cfg["llm"]["enabled"],
                "base_url": cfg["llm"]["base_url"],
                "model": cfg["llm"]["model"],
                "temperature": cfg["llm"]["temperature"],
                "api_key": cfg["llm"]["api_key"],
            },
            "orchestration": cfg["orchestration"],
            "datasource": cfg["datasource"],
        },
    )


def main() -> int:
    initial = snapshot()
    has_real_key = bool(initial["llm"]["api_key_set"])
    if has_real_key:
        print("\n注意：检测到已配置真实密钥。为免覆盖，将跳过需要写入密钥的用例")
        print("      （掩码生成、假模型链路、故障回落）。其余用例照常执行。")

    ok_llm = FakeLLM("ok")
    bad_llm = FakeLLM("fail")
    try:
        print("\n[1] 配置可读")
        status, cfg = call("GET", "/api/config")
        check("GET /api/config 200", status == 200, str(status))
        check(
            "三段齐全",
            all(k in cfg for k in ("llm", "orchestration", "datasource")),
            str(list(cfg.keys())),
        )
        if cfg["llm"]["api_key_set"]:
            check("密钥以掩码回传", "****" in cfg["llm"]["api_key"], cfg["llm"]["api_key"])
        else:
            check("未设置密钥时为空", cfg["llm"]["api_key"] == "")

        print("\n[2] 修改配置并回读")
        new_rev = 5 if initial["orchestration"]["max_revisions"] != 5 else 4
        status, saved = call("PUT", "/api/config", {"orchestration": {"max_revisions": new_rev}})
        check("PUT 200", status == 200, str(status))
        check("max_revisions 已更新", saved["orchestration"]["max_revisions"] == new_rev)
        _, reread = call("GET", "/api/config")
        check("回读一致", reread["orchestration"]["max_revisions"] == new_rev)

        print("\n[3] 非法值被拒绝")
        status, _ = call("PUT", "/api/config", {"orchestration": {"max_revisions": 99}})
        check("超范围返回 400", status == 400, str(status))
        _, after_bad = call("GET", "/api/config")
        check("非法值未写入", after_bad["orchestration"]["max_revisions"] == new_rev)

        if has_real_key:
            print("\n[4][5][6] 跳过：需要写入测试密钥，会覆盖你已配置的真实密钥")
        else:
            print("\n[4] 密钥掩码")
            call("PUT", "/api/config", llm_payload(ok_llm.base_url, enabled=False))
            _, m = call("GET", "/api/config")
            masked = m["llm"]["api_key"]
            print(f"  掩码: {masked}")
            check("返回掩码而非明文", masked.startswith("sk-t") and "****" in masked, masked)
            check("api_key_set=True", m["llm"]["api_key_set"] is True)

            print("\n[5] 回传掩码不覆盖真实密钥")
            call("PUT", "/api/config", {"llm": {"api_key": masked}})
            status, tested = call("POST", "/api/config/test", {"llm": {"enabled": True}})
            check("用已存密钥可连通", tested.get("ok") is True, str(tested))

            print("\n[6] 测试连接：正常 / 失败")
            status, good = call("POST", "/api/config/test", llm_payload(ok_llm.base_url, enabled=True))
            check("正常服务 ok=True", good.get("ok") is True, str(good))
            status, bad = call("POST", "/api/config/test", llm_payload(bad_llm.base_url, enabled=True))
            check("失败服务 ok=False", bad.get("ok") is False, str(bad))
            check("失败带错误信息", bool(bad.get("error")), str(bad))

        print("\n[7] 数据源 http 但缺地址")
        call("PUT", "/api/config", {"datasource": {"kind": "http", "http_base_url": ""}})
        status, _ = call("POST", "/api/plan", BRIEF)
        check("返回 400", status == 400, str(status))
        call("PUT", "/api/config", {"datasource": {"kind": "mock"}})

        print("\n[8] 模型关闭：规则生成，不应有回落提示")
        call("PUT", "/api/config", {"llm": {"enabled": False}, "orchestration": {"max_revisions": 5}})
        plan = plan_once(BRIEF)
        check("规划成功", "itinerary" in plan, str(plan.get("error")))
        warnings = plan["itinerary"]["warnings"]
        check("无模型回落提示", not any("模型选择不可用" in w for w in warnings), str(warnings[:2]))

        if has_real_key:
            print("\n[9][10] 跳过：需要把 base_url/key 指向假服务，会覆盖你的真实配置")
        else:
            print("\n[9] 模型开启：行程应包含假模型指定的景点")
            call("PUT", "/api/config", llm_payload(ok_llm.base_url, enabled=True))
            plan_llm = plan_once(BRIEF)
            check("规划成功", "itinerary" in plan_llm, str(plan_llm.get("error")))
            titles = [i["title"] for d in plan_llm["itinerary"]["days"] for i in d["items"] if i["type"] == "sight"]
            print(f"  景点: {titles}")
            check("出现假模型选的景点", any(t in ("文殊院", "大慈寺") for t in titles), str(titles))
            check("无回落提示", not any("模型选择不可用" in w for w in plan_llm["itinerary"]["warnings"]))

            print("\n[10] 模型服务故障：回落到规则并给出提示")
            call("PUT", "/api/config", llm_payload(bad_llm.base_url, enabled=True))
            plan_bad = plan_once(BRIEF)
            check("规划仍然成功", "itinerary" in plan_bad, str(plan_bad.get("error")))
            warn = plan_bad["itinerary"]["warnings"]
            print(f"  warnings: {[w for w in warn if '模型' in w]}")
            check("给出模型回落提示", any("模型选择不可用" in w for w in warn), str(warn[:2]))
            check("行程仍有 5 天", len(plan_bad["itinerary"]["days"]) == 5)

        print("\n[11] 编排参数生效（max_revisions=1 时不应回退）")
        call("PUT", "/api/config", {"llm": {"enabled": False}, "orchestration": {"max_revisions": 1}})
        low_budget = {**BRIEF, "budget_total": 3000}
        plan_low = plan_once(low_budget)
        check("规划成功", "itinerary" in plan_low, str(plan_low.get("error")))
        print(f"  revision={plan_low.get('revision')}")
        check("critic 只执行 1 次", plan_low.get("revision") == 1, str(plan_low.get("revision")))
    finally:
        restore(initial)
        ok_llm.stop()
        bad_llm.stop()

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print(f"PASS  配置层与 LLM 接入验收通过（{'部分用例按你的真实密钥跳过' if has_real_key else '全量'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
