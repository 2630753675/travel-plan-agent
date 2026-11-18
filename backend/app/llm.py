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
import re
import time
import urllib.error
import urllib.request

from .settings import LLMSettings


class LLMError(Exception):
    """调用失败、超时或输出不可解析。调用方必须降级。"""


class ModelCallBudgetExceeded(LLMError):
    """本次运行的供应商请求额度已耗尽。"""


class TokenBudget:
    """粗略的字符级预算，用于防止单次规划失控。"""

    def __init__(self, budget: int):
        self.budget = budget
        self.used = 0

    def add(self, text: str) -> None:
        self.used += len(text)

    def exceeded(self) -> bool:
        return self.used > self.budget


def _post(url: str, payload: dict, api_key: str, timeout: float, host: str) -> dict:
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
        reason = str(getattr(e, "reason", e))
        if "getaddrinfo" in reason or "Name or service not known" in reason or "nodename" in reason:
            raise LLMError(f"域名无法解析：{host}，请确认接口地址是否写错") from e
        raise LLMError(f"无法连接 {host}：{reason}") from e
    except TimeoutError as e:
        raise LLMError("请求超时") from e


class LLMClient:
    def __init__(
        self,
        cfg: LLMSettings,
        timeout_s: int = 60,
        budget: TokenBudget | None = None,
        max_calls: int | None = None,
        max_output_tokens: int | None = None,
        cancellation=None,
        initial_stats: dict | None = None,
    ):
        self.cfg = cfg
        self.timeout_s = timeout_s
        self.budget = budget
        self.max_calls = max_calls
        # 保留字段兼容旧调用方；当前策略是不向供应商发送 max_tokens 硬上限，
        # 输出量只记录在 usage/字符数里，运行边界靠请求数、回合数和总时长。
        self.max_output_tokens = max_output_tokens
        # 运行级取消标记（见 app/cancellation.py）；None 表示不检查。
        self.cancellation = cancellation
        self.calls = 0
        self.input_chars = 0
        self.output_chars = 0
        self.latency_ms = 0
        self.usage_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        # 恢复运行：沿用同一 run_id 的累计用量，不把账本清零。
        if initial_stats:
            self.calls = int(initial_stats.get("calls") or 0)
            self.input_chars = int(initial_stats.get("input_chars") or 0)
            self.output_chars = int(initial_stats.get("output_chars") or 0)
            self.latency_ms = int(initial_stats.get("latency_ms") or 0)
            self.usage_calls = int(initial_stats.get("usage_calls") or 0)
            self.prompt_tokens = int(initial_stats.get("prompt_tokens") or 0)
            self.completion_tokens = int(initial_stats.get("completion_tokens") or 0)

    def _reserve_call(self) -> None:
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise ModelCallBudgetExceeded(f"已达到本次运行模型调用上限 {self.max_calls}")
        self.calls += 1

    def _check_cancelled(self) -> None:
        """在请求前后检查用户取消；检查点之间不做强杀，避免丢已产生的事实。"""
        if self.cancellation is not None:
            self.cancellation.raise_if_cancelled()

    def stats(self) -> dict:
        return {
            "calls": self.calls,
            "max_calls": self.max_calls,
            "input_chars": self.input_chars,
            "output_chars": self.output_chars,
            "latency_ms": self.latency_ms,
            "usage_available": self.usage_calls > 0,
            "usage_calls": self.usage_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }

    @property
    def _endpoint(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/chat/completions"

    @property
    def _host(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self.cfg.base_url).hostname or self.cfg.base_url

    async def _complete(
        self, system: str, user: str, *, max_tokens: int | None = None, json_mode: bool = False
    ) -> str:
        """发一次请求并返回 content 原文。网络与结构错误统一抛 LLMError。

        max_tokens 仅为兼容旧调用签名保留，不再发给供应商：固定输出上限会把
        推理模型的思考长度当成失败条件，已改为只用 usage/字符数记录实际用量。
        """
        if not self.cfg.has_credentials:
            raise LLMError("缺少 base_url / model / api_key")

        self._check_cancelled()
        self._reserve_call()
        self.input_chars += len(system) + len(user)
        if self.budget:
            self.budget.add(system + user)
            if self.budget.exceeded():
                raise LLMError(f"已超出 token 预算 {self.budget.budget}")

        payload = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        # urllib 是阻塞的，放线程里跑，避免阻塞事件循环
        started = time.monotonic()
        data = await asyncio.wait_for(
            asyncio.to_thread(
                _post, self._endpoint, payload, self.cfg.api_key, float(self.timeout_s), self._host
            ),
            timeout=self.timeout_s + 1,
        )
        self._check_cancelled()
        self.latency_ms += int((time.monotonic() - started) * 1000)
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
            self.usage_calls += 1
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens

        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("响应结构不符合 OpenAI 兼容格式") from e

        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        finish = choice.get("finish_reason")

        if not content.strip():
            if finish == "length":
                raise LLMError(
                    "模型未给出正文就达到自身输出上限（finish_reason=length）。"
                    "不是本应用限制，请检查模型/供应商的输出上限配置"
                )
            if reasoning:
                raise LLMError(
                    f"模型只输出了推理过程没有最终答案（finish_reason={finish}）"
                )
            raise LLMError("模型返回了空内容")

        if finish == "length":
            # 内容非空但被截断：JSON 大概率不完整，宁可降级也不要半截数据
            raise LLMError("模型输出被自身上限截断（finish_reason=length），结果不可信")

        self.output_chars += len(content)
        if self.budget:
            self.budget.add(content)
        return content

    async def chat_json(self, system: str, user: str, *, max_tokens: int | None = None) -> dict:
        """要求 JSON 输出。模型偶尔会用代码块包裹或加解释，这里做容错提取。"""
        content = await self._complete(system, user, json_mode=True)
        return _extract_json(content)

    async def chat_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        max_tokens: int | None = None,
    ) -> dict:
        """带工具的一轮对话，返回 {"content": str, "tool_calls": [...]}。

        这是真 Agent 的基础：模型自己决定调哪个工具、传什么参数，
        调用方执行工具后把结果作为 tool 消息回填，再让模型继续判断。
        不支持 function calling 的模型会退化成纯文本，由调用方按协议解析。
        """
        if not self.cfg.has_credentials:
            raise LLMError("缺少 base_url / model / api_key")

        self._check_cancelled()
        self._reserve_call()
        payload: dict = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "messages": messages,
        }
        # 不发送 max_tokens：固定输出上限会在推理模型上先耗尽思考预算，
        # 把正常回合误判为截断失败。用量只记录，不参与放行判断。
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # 统计真实请求输入，不再只截前 4000 字符：否则 UI 的成本/预算都不可信。
        payload_text = json.dumps(payload, ensure_ascii=False)
        self.input_chars += len(payload_text)
        if self.budget:
            self.budget.add(payload_text)
            if self.budget.exceeded():
                raise LLMError(f"已超出 token 预算 {self.budget.budget}")

        started = time.monotonic()
        data = await asyncio.wait_for(
            asyncio.to_thread(
                _post, self._endpoint, payload, self.cfg.api_key, float(self.timeout_s), self._host
            ),
            timeout=self.timeout_s + 1,
        )
        self._check_cancelled()
        self.latency_ms += int((time.monotonic() - started) * 1000)
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
            self.usage_calls += 1
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens

        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("响应结构不符合 OpenAI 兼容格式") from e

        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        finish_reason = choice.get("finish_reason")
        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            args_raw = fn.get("arguments")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": raw.get("id") or name, "name": name, "arguments": args})

        if not content.strip() and not calls:
            if finish_reason == "length":
                raise LLMError("模型未给出正文就达到自身输出上限（finish_reason=length），不是本应用限制")
            if reasoning:
                raise LLMError(f"模型只返回 reasoning_content，未返回答案或工具调用（finish_reason={finish_reason}）")
            raise LLMError(f"模型未返回内容或工具调用（finish_reason={finish_reason or 'unknown'}）")

        self.output_chars += len(content) + len(json.dumps(calls, ensure_ascii=False))
        if self.budget:
            self.budget.add(content)
        return {"content": content, "tool_calls": calls}

    async def ping(self) -> str:
        """设置页的连通性检测。

        只验证"能不能调通"，不要求模型遵守 JSON 或任何格式约定——
        格式约束属于具体任务，不该成为连通性测试的通过条件。
        """
        content = await self._complete(
            "你是一个连通性测试助手，回答越短越好。",
            "回复两个字：正常",
        )
        return content.strip()[:80]


def _extract_json(text: str) -> dict:
    """从模型输出里取出 JSON 对象。

    依次尝试：整体解析 → ```json 代码块 → 第一个 { 到最后一个 }。
    全部失败才算失败，避免因为多说了两句话就整条链路降级。
    """
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise LLMError(f"模型输出不是合法 JSON: {text[:120]}")
