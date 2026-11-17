"""图状态定义。

约定：并行节点各写各的字段，只有 errors 需要 reducer 合并。
违反这个约定会在运行时直接冲突，不要图省事共用字段。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from ..agents.lodging import LodgingPlan
from ..agents.negotiators.protocol import NegotiationLog
from ..agents.poi import PoiPlan
from ..agents.transport import TransportPlan
from ..agents.weather import WeatherPlan
from ..agents.protocol import AgentMessage, AgentProposal, CollaborationRequest
from ..models import Critique, Itinerary, LogEntry, TripBrief


class TripState(TypedDict, total=False):
    brief: TripBrief
    notes: list[str]
    transport: TransportPlan
    lodging: LodgingPlan
    poi: PoiPlan
    weather: WeatherPlan
    draft: Itinerary
    critique: Critique
    budget_review: dict
    experience_review: dict
    budget_review_action: str
    experience_review_action: str
    revision: int
    decision: str
    negotiation: NegotiationLog
    negotiated: bool
    # 协商轮次与用户倾向必须声明成通道：否则 LangGraph 会静默丢弃节点返回值，
    # 第二轮永远退化成第一轮，僵持后也会反复追问用户。
    neg_round: int
    user_hint: str
    forced_drops: list[str]
    force_cheaper_hotel: bool
    collab_done: bool
    collab_requests: list[dict]
    focus_poi_ids: list[str]
    # Agent 之间真实交换的消息链；不是从日志反推出来的展示字段。
    messages: Annotated[list[AgentMessage], operator.add]
    proposals: Annotated[list[AgentProposal], operator.add]
    collaboration_requests: Annotated[list[CollaborationRequest], operator.add]
    # 对等协作会话的持久化状态：中断恢复时要接着谈，不能重跑已花的模型调用。
    peer_state: dict
    peer_question: dict
    peer_answer: str
    peer_trace: list[dict]
    peer_document: dict
    document: dict
    approved_version: int
    team_tasks: dict
    blocked: bool
    peer_finalize_feedback: str
    peer_issue_codes: list[str]
    peer_stopped: bool
    peer_stop_reason: str
    llm_stats: dict
    log: Annotated[list[LogEntry], operator.add]
    errors: Annotated[list[str], operator.add]
