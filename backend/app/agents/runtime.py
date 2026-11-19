"""Agent 运行时：一个真正的 ReAct 回合。

一个回合 = 模型看着自己的上下文，决定调哪个工具；工具结果回填后模型继续判断，
直到它给出结论（或触到步数上限）。

这里不做任何"替模型决定"的事：查什么、查几次、说什么、叫醒谁，全部写在模型输出里。
代码只负责三件必须确定的事——工具白名单、步数上限、输出格式校验。
"""

from __future__ import annotations

import asyncio
import hashlib
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

# 允许在同一个模型响应里真并发执行的只读工具：各自只写不同字段，且都不推进 plan_version。
PARALLEL_READ_TOOLS = {"search_flights", "search_hotels", "list_pois", "get_weather"}

# 回填给模型的历史字符预算：工具结果动辄几千字符，只按消息条数裁剪会让上下文被少数大结果吃掉。
# 结构化决策工具：参数就是决策对象。用它替代"在正文里输出 JSON"，
# 让格式错误变成一次短工具结果，而不是整轮重发完整上下文。
DECISION_TOOL = "submit_decision"

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": sorted(KINDS)},
        "say": {"type": "string", "description": "不超过 500 字"},
        "to": {"type": "array", "items": {"type": "string"}},
        "wake": {"type": "array", "items": {"type": "string"}},
        "done": {"type": "boolean"},
        "escalate": {"type": ["object", "null"]},
        "changes": {"type": "array", "items": {"type": "object"}},
        "evidence": {"type": "array", "items": {"type": "object"}},
        "advisory": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["kind", "say"],
    "additionalProperties": True,
}


def _decision_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": DECISION_TOOL,
            "description": "提交本回合决策，参数即决策对象（kind/say/to/wake/done 等）。用它代替在正文里写 JSON。",
            "parameters": DECISION_SCHEMA,
        },
    }
# 回填给模型的历史字符预算。实测（15 次请求）总输入 82,612 字符里，本回合提示占 81%、
# 工具结果 12%、上一回合历史≈0；预算压到 3000 只会把上下文裁空而不降总量，故取 6000 作为安全上限。
DEFAULT_HISTORY_CHARS = 6000


@dataclass
class AgentTurn:
    content: str = ""
    decision: dict = field(default_factory=dict)
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    history: list[dict] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)

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


def _message_chars(message: dict) -> int:
    """近似上下文成本：正文 + 工具调用参数的字符数。"""
    content = message.get("content") or ""
    calls = message.get("tool_calls") or []
    return len(content) + (len(json.dumps(calls, ensure_ascii=False)) if calls else 0)


def _complete_history(history: list[dict] | None, limit: int = 12,
                      max_chars: int = DEFAULT_HISTORY_CHARS) -> list[dict]:
    """按完整的 assistant tool_calls + tool 结果事务裁剪历史。

    不能直接对消息做 ``history[-limit:]``：那会把 role=tool 留在列表里，
    却丢掉它前面的 assistant.tool_calls，供应商会拒绝整个请求。每个工具事务
    必须作为一个不可分割的 group 保存；即使一个事务超过 limit，也完整保留。

    除消息条数外还受字符预算约束：工具结果往往几千字符，只按条数裁剪会让
    上下文被少数大结果吃掉；最新一组永远保留。
    """
    prior = [deepcopy(m) for m in (history or []) if m.get("role") != "system"]
    groups: list[list[dict]] = []
    i = 0
    while i < len(prior):
        msg = prior[i]
        group = [msg]
        i += 1
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            call_ids = {str(c.get("id")) for c in msg.get("tool_calls") or []}
            while i < len(prior) and prior[i].get("role") == "tool":
                tool_id = str(prior[i].get("tool_call_id"))
                if tool_id not in call_ids:
                    # 历史本身已损坏：不要把孤立 tool 消息发给供应商。
                    break
                group.append(prior[i])
                i += 1
        groups.append(group)
    selected: list[list[dict]] = []
    count = 0
    chars = 0
    for group in reversed(groups):
        group_chars = sum(_message_chars(message) for message in group)
        if selected and (count + len(group) > limit or chars + group_chars > max_chars):
            break
        selected.append(group)
        count += len(group)
        chars += group_chars
    selected.reverse()
    return [message for group in selected for message in group]


def _trim_for_request(messages: list[dict], max_chars: int = DEFAULT_HISTORY_CHARS) -> list[dict]:
    """请求前按字符预算裁剪**上一回合**的历史，本回合的步骤一律保留。

    试验过把本回合较早的工具事务也裁掉：模型会忘记"已经做过什么"并重复执行
    （脚本模型上表现为把请求数从 15 次涨到 50 次），因此这里只裁剪更早的回合。
    """
    if len(messages) < 3:
        return messages
    system = messages[0]
    prompts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not prompts:
        return messages
    last_user = prompts[-1]
    budget = max(0, max_chars - _message_chars(messages[last_user]))
    head = _complete_history(messages[1:last_user], limit=12, max_chars=budget)
    return [system] + head + messages[last_user:]


def _validate_history_messages(messages: list[dict]) -> None:
    """在请求前拒绝孤立 tool 结果，避免把本地状态错误传给供应商。"""
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            pending = {str(c.get("id")) for c in message.get("tool_calls") or []}
        elif role == "tool":
            tool_id = str(message.get("tool_call_id"))
            if tool_id not in pending:
                raise ValueError("恢复历史损坏：tool 结果没有对应的 assistant tool_calls")
            pending.discard(tool_id)
        elif pending:
            # assistant 工具调用后必须先接收全部工具结果，才能进入下一条消息。
            raise ValueError("恢复历史损坏：assistant tool_calls 未闭合")
    if pending:
        raise ValueError("恢复历史损坏：assistant tool_calls 未闭合")


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
    if len(decision["say"]) > 1200:
        raise ValueError("say 超过 1200 字；只保留结论、问题和必要依据，不要复述完整方案")
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
    # Critic 没有工具，它的证据是服务端生成的 ReviewPacket：objection 必须映射到该包中的
    # 确定性 issue 才能进入 Issue Ledger，accept 还要满足硬校验通过，均已在 hub 复核。
    critic_packet_evidence = decision.get("agent") == "critic_agent" and kind in {"accept", "objection"}
    if kind in {"proposal", "objection", "accept", "final"} and not critic_packet_evidence:
        evidence = decision.get("evidence")
        if evidence is not None and (not isinstance(evidence, list) or any(not isinstance(item, dict) for item in evidence)):
            raise ValueError("关键结论的 evidence 必须是对象数组")
        if not evidence and not any(marker in decision.get("say", "") for marker in ("未核实", "待确认", "数据源没有", "无法验证")):
            raise ValueError("关键结论必须提供 evidence 引用，或明确标记事实未核实/待确认")


def _evidence_id(agent: str, tool: str, arguments: object, result: dict) -> str:
    """内容寻址的证据 ID：同一事实得同一 ID，不同事实不会共用 ID。"""
    payload = json.dumps(
        {"agent": agent, "tool": tool, "arguments": arguments, "result": result},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _repair_history(messages: list[dict]) -> list[dict]:
    """丢弃尾部不完整的事务，返回可安全发送的历史。

    历史里可能残留"有 assistant.tool_calls、没有对应 tool 结果"的尾巴（例如旧版本
    决策工具漏回填）。整轮因此失败代价太大：能把尾巴修好就继续。

    修法优先保留内容：去掉这条 assistant 的 tool_calls（它说过的话仍是上下文），
    只有连正文都没有时才整条丢弃；孤立的 tool 结果直接丢弃。
    """
    working = [dict(message) for message in messages]
    while working:
        try:
            _validate_history_messages(working)
            return working
        except ValueError:
            for index in range(len(working) - 1, -1, -1):
                message = working[index]
                if message.get("role") == "assistant" and message.get("tool_calls"):
                    stripped = dict(message)
                    stripped.pop("tool_calls", None)
                    if stripped.get("content") in (None, ""):
                        working = working[:index]
                    else:
                        working = working[:index] + [stripped]
                    break
            else:
                # 没有可修的 assistant 事务：只剩孤立 tool 结果，丢掉最后一条重试。
                working = working[:-1]
    return working


def _finalize_decision(agent: str, decision: dict, ctx: ToolContext) -> dict:
    """把"模型给出的决策"变成可执行决策：统一做协议、角色与证据校验。"""
    if not isinstance(decision, dict):
        raise ValueError("决策必须是 JSON 对象")
    decision = dict(decision)
    # 工具 schema 只强制 kind/say；其余字段是"有默认值"的可选字段。
    # 之前 schema 与校验不一致，真实模型照 schema 填会被判缺字段，白烧 4 次请求。
    for field, default in (("to", []), ("wake", []), ("done", True), ("escalate", None)):
        if field not in decision or (decision[field] is None and field != "escalate"):
            decision[field] = default
    if agent == "critic_agent":
        decision["agent"] = "critic_agent"
        # 评审依据是服务端 ReviewPacket，Critic 没有工具，不接受它自带（必然是编造或空的）证据。
        decision["evidence"] = []
    _validate_decision(decision)
    if agent == "critic_agent" and decision.get("kind") == "objection":
        changes = decision.get("changes")
        if not isinstance(changes, list) or not changes or any(
            not isinstance(item, dict)
            or not isinstance(item.get("code"), str)
            or not isinstance(item.get("acceptance"), str)
            for item in changes
        ):
            raise ValueError("Critic objection 的 changes 必须包含 code 和 acceptance")
    evidence = decision.get("evidence") or []
    if evidence and (
        not isinstance(evidence, list)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or item["id"] not in ctx.evidence
            for item in evidence
        )
    ):
        raise ValueError("evidence 必须引用本会话工具成功结果中的 evidence_id")
    return decision


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
    allowed_tools: set[str] | None = None,
) -> AgentTurn:
    """跑完一个 Agent 回合。

    history 只接受本角色上一回合的 AgentTurn.history，由调用方按角色保存。
    不读取其他角色的私有历史，不修改传入列表；工具消息也完整保留。
    """
    messages: list[dict] = [{"role": "system", "content": system}]
    # 只在完整工具事务之间裁剪；绝不能截断 assistant.tool_calls 与 tool 结果的配对。
    messages.extend(_complete_history(history, limit=12))
    try:
        _validate_history_messages(messages[1:])
    except ValueError as exc:
        # 历史已被写坏（例如旧版本漏回填决策工具的 tool 结果）：修好尾部继续，
        # 哪怕整段历史都不可用，也比让用户卡在一个永远恢复不了的中断上好。
        repaired = _repair_history(messages[1:])
        emit("history_repaired", agent=agent, dropped=len(messages) - 1 - len(repaired),
             reason=str(exc))
        messages = [messages[0]] + repaired
    messages.append({"role": "user", "content": prompt})
    schemas = tools_spec.schemas_for(tools)
    schemas = schemas + [_decision_tool()]
    role_allowed_tools = set(tools) & {schema["function"]["name"] for schema in schemas}
    role_allowed_tools.add(DECISION_TOOL)
    if allowed_tools is not None:
        role_allowed_tools &= allowed_tools | {DECISION_TOOL}
        schemas = [schema for schema in schemas if schema["function"]["name"] in role_allowed_tools]
    allowed_tools = role_allowed_tools
    used: list[str] = []
    evidence_ids: list[str] = []

    transient_markers = ("未返回内容或工具调用", "请求超时", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "无法连接")
    for _ in range(max_steps):
        resp = None
        for attempt in range(2):
            try:
                messages = _trim_for_request(messages)
                resp = await client.chat_tools(messages, schemas)
                break
            except (LLMError, TimeoutError, asyncio.TimeoutError) as e:
                transient = isinstance(e, (TimeoutError, asyncio.TimeoutError)) or any(
                    marker in str(e) for marker in transient_markers
                )
                if transient and attempt == 0:
                    emit("retry", agent=agent, reason=str(e) or type(e).__name__, attempt=1)
                    continue
                return AgentTurn(error=f"{type(e).__name__}: {str(e).strip() or '未提供异常消息'}", error_type=type(e).__name__, tool_calls=used, history=messages)
        if resp is None:
            return AgentTurn(error="模型重试后仍无响应", error_type="LLMError", tool_calls=used, history=messages)

        calls = resp.get("tool_calls") or []
        if calls:
            messages.append(_assistant_msg(resp.get("content") or "", calls))
            for call in calls:
                emit("tool_call", agent=agent, tool=call["name"], arguments=call["arguments"])
            precomputed: dict[str, dict] = {}
            if len(calls) > 1 and all(
                call["name"] in PARALLEL_READ_TOOLS
                and isinstance(call["arguments"], dict)
                and call["name"] in allowed_tools
                and str(call["id"]) not in ctx.operations
                for call in calls
            ):
                # 四个只读查询写的是互不相干的字段（transport / lodging / poi / weather），
                # 且都不推进 plan_version，可以真并发；其余工具保持串行。
                results = await asyncio.gather(*(
                    asyncio.to_thread(tools_spec.execute, ctx, call["name"], call["arguments"])
                    for call in calls
                ))
                precomputed = {str(call["id"]): result for call, result in zip(calls, results)}
            for call in calls:
                used.append(call["name"])
                if call["name"] == DECISION_TOOL:
                    try:
                        if not isinstance(call["arguments"], dict):
                            raise ValueError("参数必须是 JSON 对象")
                        decision = _finalize_decision(agent, dict(call["arguments"]), ctx)
                    except ValueError as exc:
                        result = {"ok": False, "error": f"决策不合协议：{exc}"}
                        emit("tool_result", agent=agent, tool=DECISION_TOOL, ok=False,
                             summary=_summarize_result(result), evidence_id=None)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False),
                        })
                        continue
                    text = str(decision.get("say") or "")
                    emit("say", agent=agent, text=text)
                    # 决策工具也是 tool_call：必须回填对应的 tool 消息，否则历史里会留下
                    # 未闭合的 assistant.tool_calls，下一回合恢复时被判为损坏。
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps({"ok": True, "accepted": True}, ensure_ascii=False),
                    })
                    emit("tool_result", agent=agent, tool=DECISION_TOOL, ok=True,
                         summary=f"决策已提交（{decision.get('kind')}）", evidence_id=None)
                    return AgentTurn(content=text, decision=decision, tool_calls=used,
                                     history=messages, evidence_ids=evidence_ids)
                prior = ctx.operations.get(str(call["id"]))
                fresh = False
                if (
                    prior
                    and prior.get("status") == "succeeded"
                    and prior.get("agent") == agent
                    and prior.get("tool") == call["name"]
                ):
                    # 恢复后同一调用已成功过：复用结果，避免重复写副作用。
                    result = {**deepcopy(prior.get("result") or {}), "replayed": True}
                elif call["name"] not in allowed_tools:
                    reason = "本次返工未授权此工具" if allowed_tools is not None else f"角色 {agent} 无权调用工具"
                    result = {"ok": False, "error": f"{reason}：{call['name']}"}
                elif not isinstance(call["arguments"], dict):
                    result = {"ok": False, "error": "工具参数必须是 JSON 对象"}
                else:
                    fresh = True
                    before_version = ctx.plan_version
                    if str(call["id"]) in precomputed:
                        result = precomputed[str(call["id"])]
                    else:
                        result = tools_spec.execute(ctx, call["name"], call["arguments"])
                if result.get("ok") and not result.get("evidence_id"):
                    # 证据 ID 由"谁、用什么参数、得到什么结果"算出来，而不是随机数：
                    # 同一事实重复出现会得到同一个 ID，不同事实不会共用 ID，引用也因此可复核。
                    evidence_id = _evidence_id(agent, call["name"], call["arguments"], result)
                    evidence = {
                        "evidence_id": evidence_id,
                        "agent": agent,
                        "tool": call["name"],
                        "arguments": call["arguments"],
                        "summary": _summarize_result(result),
                        "fields": sorted(key for key in result if key != "ok")[:20],
                    }
                    ctx.evidence[evidence_id] = evidence
                    if evidence_id not in evidence_ids:
                        evidence_ids.append(evidence_id)
                    result = {**result, "evidence_id": evidence_id}
                if fresh:
                    ctx.operations[str(call["id"])] = {
                        "agent": agent,
                        "tool": call["name"],
                        "arguments": call["arguments"],
                        "status": "succeeded" if result.get("ok") else "failed",
                        "result": deepcopy(result),
                        "before_version": before_version,
                        "after_version": ctx.plan_version,
                    }
                emit(
                    "tool_result",
                    agent=agent,
                    tool=call["name"],
                    ok=bool(result.get("ok")),
                    summary=_summarize_result(result),
                    evidence_id=result.get("evidence_id"),
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
            decision = _finalize_decision(agent, _parse_decision(content), ctx)
        except ValueError as e:
            messages.append({
                "role": "user",
                "content": (
                    f"上条回复未遵守协作协议：{e}。"
                    "顶层必须含 kind、say、to、wake、done、escalate；"
                    "changes 或 requested_changes 非空时不能 accept；"
                    "关键结论的 evidence 必须是 [{\"id\":\"工具返回的 evidence_id\"}]，事实不足则明确标记待确认。"
                    "请重新只输出符合约定的 JSON 对象。"
                ),
            })
            continue
        emit("say", agent=agent, text=content)
        return AgentTurn(content=content, decision=decision, tool_calls=used, history=messages, evidence_ids=evidence_ids)

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


def render_bus(bus: Bus, agent: str, limit: int = 6, content_limit: int = 180) -> str:
    """把该 Agent 相关的对话渲染成文本，作为它的上下文。

    只给最近几条、且单条截断：完整消息都在各自历史里，总线的作用是“谁说了什么”。
    """
    lines: list[str] = []
    for m in bus.messages[-limit:]:
        who = "你" if m.sender == agent else m.sender
        to = "、".join(m.recipients) or "所有人"
        text = m.content if len(m.content) <= content_limit else m.content[:content_limit] + "…"
        lines.append(f"[{who} → {to}] ({m.kind}) {text}")
        hard_check = (m.payload or {}).get("hard_check")
        if hard_check is not None:
            lines.append("  硬约束检查：" + json.dumps(hard_check, ensure_ascii=False))
    return "\n".join(lines) if lines else "（还没有对话）"
