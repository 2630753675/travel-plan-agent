"""旧协商链路的验收夹具：脚本化模型 + 配置切换。

为什么需要它：`verify_negotiation*` 之前依赖数据库里恰好有一个可用模型配置，
在别人机器上要么失败、要么真的去打外部接口。测试必须自带模型，不能靠环境。

用法：
    model = LegacyModel()
    backup = configure(model.base_url, agent_mode="legacy")
    try:
        ...跑验收...
    finally:
        restore(backup)
        model.stop()
"""

from __future__ import annotations

import http.server
import json
import threading
import urllib.request

BASE = "http://127.0.0.1:8848"


class LegacyModel:
    """按系统提示区分预算官/体验官，返回旧的 chat_json 结构。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.calls.append(body)
                system = str((body.get("messages") or [{}])[0].get("content") or "")
                user = str((body.get("messages") or [{}, {}])[-1].get("content") or "")

                if "预算官" in system:
                    content = json.dumps(
                        {
                            "poi_ids": _cheap_poi_ids(user),
                            "use_cheaper_hotel": True,
                            "claim": "先换便宜酒店并砍掉部分付费景点，把花费压回预算",
                        },
                        ensure_ascii=False,
                    )
                elif "体验官" in system:
                    content = json.dumps(
                        {"verdict": "accept", "claim": "这些安排可替代性尚可，可以让步"},
                        ensure_ascii=False,
                    )
                else:
                    content = json.dumps({"claim": "收到"}, ensure_ascii=False)

                payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
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


def _cheap_poi_ids(prompt: str) -> list[str]:
    """从提示里挑一个真实存在的可砍景点 id，避免模型编造。

    预算官的提示里带 cuttable 列表，取第一个 poi_id 即可。
    """
    try:
        start = prompt.index("可以砍掉的景点")
        chunk = prompt[start : start + 600]
        at = chunk.index("poi_id")
        tail = chunk[at:]
        import re

        m = re.search(r'"poi_id":\s*"([^"]+)"', tail)
        return [m.group(1)] if m else []
    except (ValueError, IndexError):
        return []


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def configure(base_url: str, *, agent_mode: str = "legacy") -> dict:
    """把服务切到"旧协商 + 本地假模型"，返回可恢复的原配置。"""
    original = _request("GET", "/api/config")
    payload = json.loads(json.dumps(original))
    payload.setdefault("llm", {})
    payload["llm"].update(
        {
            "enabled": True,
            "provider": "openai-compatible",
            "base_url": base_url,
            "model": "fake-model",
            "api_key": "sk-test-1234567890",
            "temperature": 0.3,
        }
    )
    payload.setdefault("orchestration", {})
    payload["orchestration"]["agent_mode"] = agent_mode
    _request("PUT", "/api/config", payload)
    return original


def restore(original: dict) -> None:
    payload = json.loads(json.dumps(original))
    # 掩码不能当密钥写回：原配置里的 api_key 是掩码，去掉它才不会被覆盖逻辑挡住
    key = (payload.get("llm") or {}).get("api_key")
    if key and "****" in str(key):
        payload["llm"].pop("api_key", None)
    try:
        _request("PUT", "/api/config", payload)
    except Exception:
        pass
