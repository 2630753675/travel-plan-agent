"""多 Agent 消息协议。

这些模型不是 UI 日志，而是 Agent 之间真正交换的业务消息：
- `AgentMessage` 描述谁向谁说了什么、为什么说、基于哪些证据；
- `CollaborationRequest` 描述需要接收方行动的请求；
- `AgentProposal` 是 Planner 提案与 Critic 质疑共用的结构化载体。

LangGraph state 保存消息链，RunEvent 只负责把它流式投影到前端。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


AgentName = Literal[
    "planner", "budget_reviewer", "experience_reviewer", "user",
]

MessageIntent = Literal[
    "proposal", "request", "response", "critique", "revision", "decision", "question", "answer",
]


class AgentMessage(BaseModel):
    """一次可追溯的 Agent 间消息。"""

    message_id: str
    sender: AgentName | str
    receiver: AgentName | str
    intent: MessageIntent
    content: str
    reason: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    reply_to: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class CollaborationRequest(BaseModel):
    """一个 Agent 请求另一个 Agent 执行动作。"""

    request_id: str
    sender: AgentName | str
    receiver: AgentName | str
    question: str
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    reply_to: str | None = None


class AgentProposal(BaseModel):
    """Planner/Critic 之间交换的方案提案。"""

    proposal_id: str
    sender: AgentName | str
    receiver: AgentName | str
    revision: int
    summary: str
    constraints: list[str] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    reply_to: str | None = None
