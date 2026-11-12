"""P5 最小子集验收：访问口令、分享签名、SSRF 防护。

所有"外部访问"都用 `X-Forwarded-For` 头模拟——服务端一旦看到这个头，
就不再当成本机（隧道与反向代理都会带它）。

覆盖：
A 本机回环免口令，健康检查永远公开
B 外部无口令 → 401；口令错了 → 401；口令对了 → 200
C 分享签名只能读单个 plan，不能写
D 外部请求 /api/config/test 时拒绝指向内网/本机
E CORS 不再返回通配

需要后端已在 127.0.0.1:8848 运行。
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sse_protocol import final_result, interrupt_info, parse_sse  # noqa: E402

from app import access  # noqa: E402

BASE = "http://127.0.0.1:8848"
EXTERNAL = {"X-Forwarded-For": "203.0.113.9"}
failures: list[str] = []

BRIEF = {
    "origin": "上海",
    "destination": "成都",
    "start_date": "2026-11-20",
    "end_date": "2026-11-24",
    "travelers": 2,
    "budget_total": 8000,
    "companions": ["老人"],
    "preferences": ["人文"],
    "pace": "适中",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name} {'' if ok else detail}")
    if not ok:
        failures.append(name)


def call(
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, str, dict]:
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"Content-Type": "application/json"} if data else {}
    all_headers.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def main() -> int:
    token = access.load_token()
    print(f"  口令文件：{access.ACCESS_TOKEN_PATH.name}，长度 {len(token)}")

    print("\n[A] 本机与健康检查")
    status, _, _ = call("GET", "/api/health")
    check("本机健康检查 200", status == 200, str(status))
    status, _, _ = call("GET", "/api/health", headers=EXTERNAL)
    check("外部也能做健康检查", status == 200, str(status))

    status, _, _ = call("GET", "/api/config")
    check("本机读配置不需要口令", status == 200, str(status))

    print("\n[B] 外部访问必须带口令")
    status, body, _ = call("GET", "/api/config", headers=EXTERNAL)
    check("外部无口令被拒", status == 401, str(status))
    check("给出可读原因", "口令" in body, body[:80])

    status, _, _ = call("GET", "/api/config", headers={**EXTERNAL, "X-Access-Token": "wrong-token"})
    check("错误口令被拒", status == 401, str(status))

    status, _, _ = call("GET", "/api/config", headers={**EXTERNAL, "X-Access-Token": token})
    check("正确口令放行", status == 200, str(status))

    status, _, _ = call("GET", "/api/config", headers=EXTERNAL)
    status_q, _, _ = call("GET", f"/api/config?token={token}", headers=EXTERNAL)
    check("也支持查询参数传口令", status_q == 200, str(status_q))

    print("\n[C] 分享签名只读")
    status, text, _ = call("POST", "/api/plan", BRIEF, headers={"X-Access-Token": token})
    check("带口令可以规划", status == 200, str(status))
    events = parse_sse(text)
    done = final_result(events) or interrupt_info(events)
    check("拿到结果", done is not None)
    if done is None:
        return 1
    plan_id = done["plan_id"]
    share = (done or {}).get("share")
    print(f"  plan_id={plan_id} share={share}")
    check("完成响应给出分享签名", bool(share), str(share))

    status, _, _ = call("GET", f"/api/plan/{plan_id}?share={share}", headers=EXTERNAL)
    check("带签名可在外部只读", status == 200, str(status))

    status, _, _ = call("GET", f"/api/plan/{plan_id}?share=deadbeefdeadbeef", headers=EXTERNAL)
    check("伪造签名被拒", status == 401, str(status))

    status, _, _ = call("GET", f"/api/plan/{plan_id}", headers=EXTERNAL)
    check("外部不带签名也拿不到", status == 401, str(status))

    status, _, _ = call("GET", f"/api/plan/{plan_id}/versions?share={share}", headers=EXTERNAL)
    check("签名不能读其它接口", status == 401, str(status))

    status, _, _ = call(
        "POST", f"/api/plan/{plan_id}/rollback?share={share}", {"version": 1}, headers=EXTERNAL
    )
    check("签名不能用来写", status == 401, str(status))

    print("\n[D] SSRF 防护")
    status, body, _ = call(
        "POST",
        "/api/config/test",
        {"llm": {"base_url": "http://127.0.0.1:9/v1", "model": "x", "api_key": "k"}},
        headers={**EXTERNAL, "X-Access-Token": token},
    )
    payload = json.loads(body)
    print(f"  本机地址: ok={payload.get('ok')} error={payload.get('error')}")
    check("外部不能用测试接口探本机", payload.get("ok") is False, body[:80])
    check("明确说明是内网限制", "内网" in str(payload.get("error")), str(payload.get("error")))

    status, body, _ = call(
        "POST",
        "/api/config/test",
        {"llm": {"base_url": "file:///etc/passwd", "model": "x", "api_key": "k"}},
        headers={**EXTERNAL, "X-Access-Token": token},
    )
    check("外部不能用非 http 协议", json.loads(body).get("ok") is False, body[:80])

    status, body, _ = call(
        "POST",
        "/api/config/test",
        {"llm": {"base_url": "http://169.254.169.254/latest", "model": "x", "api_key": "k"}},
        headers={**EXTERNAL, "X-Access-Token": token},
    )
    check("外部不能访问云元数据地址", json.loads(body).get("ok") is False, body[:80])

    print("\n[E] CORS 不再通配")
    status, _, headers = call("GET", "/api/health", headers={"Origin": "http://evil.example"})
    allow = headers.get("Access-Control-Allow-Origin", "")
    print(f"  Access-Control-Allow-Origin={allow!r}")
    check("没有回显任意来源", allow != "http://evil.example", allow)
    check("不是通配", allow != "*", allow)

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  访问控制验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
