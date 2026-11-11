"""OpenAI 兼容的 LLM 客户端。

设计约束：
- 只用标准库，不引入额外 SDK，避免版本漂移；
- 超时可配（来自界面设置），超时或网络错误一律抛 LLMError，由调用方降级；
- 只解析 JSON 输出，解析失败视为失败而不是猜；
- 任何异常信息里都不含密钥。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from .settings import LLMSettings


class LLMError(Exception):
    """调用失败、超时或输出不可解析。调用方必须降级。"""


class TokenBudget:
    """粗略的字符级预算，用于防止单次规划失控。"""

    def __init__(self, budget: int):
        self.budget = budget
        self.used = 0

    def add(self, text: str) -> None:
        self.used += len(text)

    def exceeded(self) -> bool:
        return self.used > self.budget


def _post(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # 不回显请求头，避免密钥进日志
        detail = e.read().decode(errors="replace")[:200]
        raise LLMError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"网络不可达: {e.reason}") from e
    except TimeoutError as e:
        raise LLMError("请求超时") from e


class LLMClient:
    def __init__(self, cfg: LLMSettings, timeout_s: int = 60, budget: TokenBudget | None = None):
        self.cfg = cfg
        self.timeout_s = timeout_s
        self.budget = budget

    @property
    def _endpoint(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/chat/completions"

    async def chat_json(self, system: str, user: str, *, max_tokens: int = 2000) -> dict:
        if not self.cfg.configured:
            raise LLMError("模型未配置")

        if self.budget:
            self.budget.add(system + user)
            if self.budget.exceeded():
                raise LLMError(f"已超出 token 预算 {self.budget.budget}")

        payload = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        # urllib 是阻塞的，放线程里跑，避免阻塞事件循环
        data = await asyncio.wait_for(
            asyncio.to_thread(_post, self._endpoint, payload, self.cfg.api_key, float(self.timeout_s)),
            timeout=self.timeout_s + 1,
        )

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("响应结构不符合 OpenAI 兼容格式") from e

        if self.budget:
            self.budget.add(content)

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMError(f"模型输出不是合法 JSON: {content[:120]}") from e

    async def ping(self) -> str:
        """设置页的连通性检测：发一个最小请求，返回模型回显内容。"""
        result = await self.chat_json(
            "只输出 JSON。",
            '回 {"ok": true, "model": "<你的模型名>"}',
            max_tokens=64,
        )
        return str(result)
