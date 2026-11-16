"""Agent 运行时：一个真正的 ReAct 回合。

一个回合 = 模型看着自己的上下文，决定调哪个工具；工具结果回填后模型继续判断，
直到它给出结论（或触到步数上限）。

这里不做任何"替模型决定"的事：查什么、查几次、说什么、叫醒谁，全部写在模型输出里。
代码只负责三件必须确定的事——工具白名单、步数上限、输出格式校验。
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable

from ..llm import LLMClient, LLMError
from . import tools_spec
from .bus import Bus, KINDS
from .tools_spec import ToolContext

# 单回合最多几轮"模型→工具"。上限的意义是防止模型原地打转烧钱，不是限制它的判断。
DEFAULT_MAX_STEPS = 6


@dataclass
class AgentTurn:
    content: str = ""
    decision: dict = field(default_factory=dict)
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


def _assistant_msg(content: str, calls: list[dict]) -> dict:
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)},
            }
            for c in calls
        ],
    }


def _parse_decision(text: str) -> dict:
    """严格解析顶层对象；不从非法回复中拼凑或猜测消息。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError("回复必须是合法的 JSON 对象") from e
    if not isinstance(data, dict):
        raise ValueError("回复顶层必须是 JSON 对象")
    return data


def _validate_decision(decision: dict) -> None:
    # 延迟导入：roles 的提示词构造也会使用本模块的 render_bus。
    from . import roles

    required = {"kind", "say", "to", "wake", "done", "escalate"}
    missing = required - decision.keys()
    if missing:
        raise ValueError("缺少字段：" + "、".join(sorted(missing)))
    kind = decision["kind"]
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("kind 必须是以下字符串之一：" + ", ".join(sorted(KINDS)))
    if not isinstance(decision["say"], str):
        raise ValueError("say 必须是字符串")
    peers = set(roles.peers())
    for key in ("to", "wake"):
        targets = decision[key]
        if not isinstance(targets, list) or any(
            not isinstance(target, str) or target not in peers for target in targets
        ):
            raise ValueError(f"{key} 必须是有效角色代号的数组，可用角色：" + ", ".join(sorted(peers)))
    if not isinstance(decision["done"], bool):
        raise ValueError("done 必须是布尔值")
    escalation = decision["escalate"]
    if escalation is not None and not isinstance(escalation, dict):
        raise ValueError("escalate 必须是对象或 null")
    if kind == "escalate" or escalation is not None:
        if not isinstance(escalation, dict):
            raise ValueError("kind=escalate 时必须提供 escalate 对象")
        question = escalation.get("question")
        options = escalation.get("options")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("escalate.question 必须是非空字符串")
        if not isinstance(options, list) or not options or any(
            not isinstance(option, dict)
            or any(not isinstance(option.get(key), str) or not option[key].strip() for key in ("value", "label"))
            for option in options
        ):
            raise ValueError("escalate.options 必须是非空数组，每项包含非空字符串 value、label")
        if kind != "escalate":
            raise ValueError("提供 escalate 对象时 kind 必须为 escalate")
    if kind == "accept" and any(decision.get(key) for key in ("changes", "requested_changes")):
        raise ValueError("changes 或 requested_changes 非空时不能 accept，请修改 kind 后重新回复")


async def run_turn(
    *,
    agent: str,
    system: str,
    prompt: str,
    tools: list[str],
    ctx: ToolContext,
    bus: Bus,
    client: LLMClient,
    emit: Callable[..., None],
    max_steps: int = DEFAULT_MAX_STEPS,
    history: list[dict] | None = None,
) -> AgentTurn:
    """跑完一个 Agent 回合。

    history 只接受本角色上一回合的 AgentTurn.history，由调用方按角色保存。
    不读取其他角色的私有历史，不修改传入列表；工具消息也完整保留。
    """
    messages: list[dict] = [{"role": "system", "content": system}]
    # 只保留最近两轮，避免工具 JSON 和协议纠错无限堆积到上下文。
    prior = [m for m in (history or []) if m.get("role") != "system"]
    messages.extend(deepcopy(prior[-12:]))
    messages.append({"role": "user", "content": prompt})
    schemas = tools_spec.schemas_for(tools)
    allowed_tools = set(tools) & {schema["function"]["name"] for schema in schemas}
    used: list[str] = []

    for _ in range(max_steps):
        try:
            resp = await client.chat_tools(messages, schemas)
        except LLMError as e:
            return AgentTurn(error=f"{e}", tool_calls=used, history=messages)

        calls = resp.get("tool_calls") or []
        if calls:
            messages.append(_assistant_msg(resp.get("content") or "", calls))
            for call in calls:
                used.append(call["name"])
                emit("tool_call", agent=agent, tool=call["name"], arguments=call["arguments"])
                if call["name"] not in allowed_tools:
                    result = {"ok": False, "error": f"角色 {agent} 无权调用工具：{call['name']}"}
                elif not isinstance(call["arguments"], dict):
                    result = {"ok": False, "error": "工具参数必须是 JSON 对象"}
                else:
                    result = tools_spec.execute(ctx, call["name"], call["arguments"])
                emit(
                    "tool_result",
                    agent=agent,
                    tool=call["name"],
                    ok=bool(result.get("ok")),
                    summary=_summarize_result(result),
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
            continue

        content = resp.get("content") or ""
        messages.append({"role": "assistant", "content": content})
        try:
            decision = _parse_decision(content)
            _validate_decision(decision)
        except ValueError as e:
            messages.append({
                "role": "user",
                "content": (
                    f"上条回复未遵守协作协议：{e}。"
                    "顶层必须含 kind、say、to、wake、done、escalate；"
                    "changes 或 requested_changes 非空时不能 accept。"
                    "请重新只输出符合约定的 JSON 对象。"
                ),
            })
            continue
        emit("say", agent=agent, text=content)
        return AgentTurn(content=content, decision=decision, tool_calls=used, history=messages)

    return AgentTurn(
        error=f"回合超过 {max_steps} 步仍未给出结论",
        tool_calls=used,
        history=messages,
    )


def _summarize_result(result: dict) -> str:
    """给事件流一句人话摘要，避免把整个 JSON 灌进日志。"""
    if not result.get("ok"):
        return f"失败：{result.get('error', '未知错误')}"
    for key in ("count", "passed", "total_cost", "over", "hotel", "date"):
        if key in result:
            value = result[key]
            if key == "passed":
                return "通过" if value else f"未通过（{len(result.get('issues') or [])} 个问题）"
            return f"{key}={value}"
    if "days" in result:
        return f"{len(result['days'])} 天"
    if "flights" in result:
        return f"{len(result['flights'])} 个航班"
    if "hotels" in result:
        return f"{len(result['hotels'])} 家酒店"
    if "pois" in result:
        return f"{len(result['pois'])} 个景点"
    return "完成"


def render_bus(bus: Bus, agent: str, limit: int = 12) -> str:
    """把该 Agent 相关的对话渲染成文本，作为它的上下文。"""
    lines: list[str] = []
    for m in bus.messages[-limit:]:
        if m.sender == agent:
            who = "你"
        else:
            who = m.sender
        to = "、".join(m.recipients) or "所有人"
        lines.append(f"[{who} → {to}] ({m.kind}) {m.content}")
        hard_check = (m.payload or {}).get("hard_check")
        if hard_check is not None:
            lines.append("  硬约束检查：" + json.dumps(hard_check, ensure_ascii=False))
    return "\n".join(lines) if lines else "（还没有对话）"
