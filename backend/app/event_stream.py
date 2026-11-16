"""统一编排事件工厂：把 agent、工具、校验和用户介入写入同一条可回放事件流。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from .activity import ACTOR_LABEL
from .models import LogEntry, RunEvent

_LABEL_TO_ACTOR = {label: key for key, label in ACTOR_LABEL.items()}

# 这些工具已经有真实的 tool_call/tool_result 事件，旧的摘要行会重复显示。
_SOURCE_TOOLS = {"search_flights", "search_hotels", "list_pois", "get_weather"}


class EventFactory:
    """为一次 run 产生稳定、递增、可关联的事件。"""

    def __init__(
        self,
        run_id: str,
        *,
        seq_start: int = 1,
        plan_version: int = 1,
        prior_events: list[dict] | None = None,
    ):
        self.run_id = run_id
        self.plan_version = plan_version
        self._seq = seq_start - 1
        self.events: list[RunEvent] = []
        self._active_agents: dict[str, str] = {}
        self._restored_agents: set[str] = set()
        self._active_stage: str | None = None
        self._active_stage_id: str | None = None
        self._tool_event_ids: dict[str, str] = {}
        self._restore_context(prior_events or [])

    def _restore_context(self, prior_events: list[dict]) -> None:
        """Rebuild open stage/agent/tool relationships across a resumed stream."""
        for raw in prior_events:
            event = RunEvent.model_validate(raw)
            if event.event_type == "stage_started":
                self._active_stage = event.stage
                self._active_stage_id = event.event_id
            elif event.event_type == "agent_started":
                self._active_agents[event.actor] = event.event_id
                self._restored_agents.add(event.actor)
            elif event.event_type == "agent_resumed":
                self._active_agents[event.actor] = event.event_id
                self._restored_agents.add(event.actor)
            elif event.event_type == "agent_completed":
                self._active_agents.pop(event.actor, None)
                self._restored_agents.discard(event.actor)
            elif event.event_type == "tool_call":
                call_id = event.payload.get("tool_call_id")
                if call_id:
                    self._tool_event_ids[str(call_id)] = event.event_id
            elif event.event_type == "tool_result":
                call_id = event.payload.get("tool_call_id")
                if call_id:
                    self._tool_event_ids.pop(str(call_id), None)

    def make(
        self,
        event_type: str,
        *,
        stage: str = "",
        actor: str = "system",
        payload: dict | None = None,
        parent_id: str | None = None,
    ) -> RunEvent:
        self._seq += 1
        event = RunEvent(
            run_id=self.run_id,
            plan_version=self.plan_version,
            event_id=uuid4().hex,
            seq=self._seq,
            occurred_at=datetime.now(timezone.utc),
            event_type=event_type,  # type: ignore[arg-type]
            stage=stage,
            actor=actor,
            parent_id=parent_id,
            payload=payload or {},
        )
        self.events.append(event)
        return event

    def from_log(self, row: LogEntry) -> list[RunEvent]:
        """Convert a legacy display row without inventing runtime tool calls."""
        actor = _LABEL_TO_ACTOR.get(row.actor, row.actor)
        view = {
            "text": row.text,
            "detail": row.detail,
            "kind": row.kind,
            "depth": row.depth,
            "tool": row.tool,
        }
        if row.kind == "stage":
            if row.group and row.group == self._active_stage:
                return []
            event = self.make("stage_started", stage=row.group, actor="system", payload=view)
            self._active_stage = row.group or None
            self._active_stage_id = event.event_id
            return [event]
        if row.kind == "tool":
            # 数据源调用已被 _ObservedSource 真实记录，跳过重复摘要
            if row.tool in _SOURCE_TOOLS:
                return []
            return [
                self.make(
                    "tool_exchange",
                    stage=row.group,
                    actor=actor,
                    payload={**view, "source": "legacy_summary"},
                )
            ]

        event_type = {
            "speech": "agent_message",
            "question": "user_question",
            "answer": "user_answer",
            "decision": "route_decision",
        }.get(row.kind, "agent_message")
        return [self.make(event_type, stage=row.group, actor=actor, payload=view)]

    def from_custom(self, row: dict) -> list[RunEvent]:
        """Normalize LangGraph custom events into the run event contract."""
        phase = row.get("phase")
        node = str(row.get("node") or row.get("from") or "system")
        from .activity import NODE_GROUP

        stage = str(row.get("stage") or NODE_GROUP.get(node, ""))
        if phase == "peer_message":
            to = row.get("to") or []
            return [
                self.make(
                    "agent_message",
                    stage="peer",
                    actor=str(row.get("agent") or "system"),
                    payload={
                        "text": row.get("text", ""),
                        "message_id": row.get("message_id"),
                        "to": list(to),
                        "recipients": list(to),
                        "sender": row.get("agent"),
                        "receiver": to[0] if to else None,
                        "intent": row.get("kind", "note"),
                        "reason": "",
                        "evidence": [],
                        "reply_to": row.get("reply_to"),
                        "payload": row.get("payload", {}),
                        "peer": True,
                    },
                )
            ]
        if phase == "peer_status":
            step = str(row.get("step") or "")
            if step == "wake":
                text = f"被叫醒（{row.get('reason') or '有新的消息'}）"
            elif step == "converged":
                text = f"协作收敛：{row.get('rounds')} 轮、{row.get('turns')} 个回合"
            elif step == "session_end":
                text = f"协作结束：{row.get('rounds')} 轮，收敛={row.get('converged')}"
            elif step == "round_cap":
                text = f"往返达到上限（{row.get('rounds')} 轮），带着未解决异议定稿"
            elif step == "escalated":
                text = f"无法与同伴协调，请示用户：{row.get('question')}"
            elif step == "turn_failed":
                text = f"回合失败：{row.get('error')}"
            else:
                text = step
            return [
                self.make(
                    "agent_message",
                    stage="peer",
                    actor=str(row.get("agent") or "system"),
                    payload={
                        "text": text,
                        "message_id": uuid4().hex,
                        "sender": row.get("agent"),
                        "receiver": None,
                        "intent": "note",
                        "peer": True,
                        "status": step,
                    },
                )
            ]
        if phase == "message" and isinstance(row.get("message"), dict):
            message = row["message"]
            return [
                self.make(
                    "agent_message",
                    stage=stage or "collab",
                    actor=str(message.get("sender") or node),
                    parent_id=message.get("reply_to"),
                    payload={
                        "text": message.get("content", ""),
                        "message_id": message.get("message_id"),
                        "sender": message.get("sender"),
                        "receiver": message.get("receiver"),
                        "intent": message.get("intent"),
                        "reason": message.get("reason", ""),
                        "evidence": message.get("evidence", []),
                        "reply_to": message.get("reply_to"),
                        "payload": message.get("payload", {}),
                    },
                )
            ]
        if phase == "log" and isinstance(row.get("entry"), dict):
            return self.from_log(LogEntry.model_validate(row["entry"]))
        if phase == "start":
            events: list[RunEvent] = []
            if stage and stage != self._active_stage:
                stage_event = self.make("stage_started", stage=stage, actor="system", payload={"stage": stage})
                self._active_stage = stage
                self._active_stage_id = stage_event.event_id
                events.append(stage_event)
            prior_agent_id = self._active_agents.get(node)
            if prior_agent_id and node not in self._restored_agents:
                # Some nodes emit lifecycle markers both internally and in wrap().
                # Keep the first active marker and ignore its duplicate.
                return events
            if prior_agent_id:
                self._restored_agents.discard(node)
            event_type = "agent_resumed" if prior_agent_id else "agent_started"
            agent = self.make(
                event_type,
                stage=stage,
                actor=node,
                parent_id=prior_agent_id or self._active_stage_id,
                payload={"node": node, "resumed": prior_agent_id is not None},
            )
            self._active_agents[node] = agent.event_id
            events.append(agent)
            return events
        if phase == "end":
            parent_id = self._active_agents.pop(node, None)
            if parent_id is None:
                return []
            self._restored_agents.discard(node)
            return [
                self.make(
                    "agent_completed",
                    stage=stage,
                    actor=node,
                    parent_id=parent_id,
                    payload={"node": node, "summary": row.get("summary", "")},
                )
            ]
        if phase == "edge":
            return [
                self.make(
                    "route_decision",
                    stage=stage,
                    actor="system",
                    payload={"from": row.get("from"), "to": row.get("to"), "reason": row.get("reason", "")},
                )
            ]
        if phase == "tool_call":
            call_id = str(row.get("tool_call_id") or "")
            event = self.make(
                "tool_call",
                stage=stage,
                actor=str(row.get("actor") or node),
                parent_id=self._active_agents.get(node),
                payload={
                    "tool_name": row.get("tool"),
                    "arguments": row.get("arguments", {}),
                    "tool_call_id": call_id,
                },
            )
            if call_id:
                self._tool_event_ids[call_id] = event.event_id
            return [event]
        if phase == "tool_result":
            call_id = str(row.get("tool_call_id") or "")
            return [
                self.make(
                    "tool_result",
                    stage=stage,
                    actor=str(row.get("actor") or node),
                    parent_id=self._tool_event_ids.get(call_id),
                    payload={
                        "tool_name": row.get("tool"),
                        "tool_call_id": call_id,
                        "status": row.get("status", "success"),
                        "summary": row.get("summary", ""),
                    },
                )
            ]
        if phase == "validation":
            return [
                self.make(
                    "validation_result",
                    stage=stage,
                    actor=node,
                    payload={key: value for key, value in row.items() if key != "phase"},
                )
            ]
        return []


def legacy_activity(events: list[RunEvent]) -> list[dict]:
    """迁移兼容投影。前端全部迁移后可移除 activity 字段。"""
    rows: list[dict] = []
    for event in events:
        payload = event.payload
        if event.event_type == "stage_started":
            kind = "stage"
        elif event.event_type in ("tool_call", "tool_exchange"):
            kind = "tool"
        elif event.event_type == "user_question":
            kind = "question"
        elif event.event_type == "user_answer":
            kind = "answer"
        elif event.event_type == "route_decision":
            kind = "decision"
        elif event.event_type == "agent_resumed":
            continue
        elif event.event_type == "agent_message":
            kind = payload.get("kind", "action")
        else:
            continue
        rows.append(
            {
                "ts": event.occurred_at.astimezone().strftime("%H:%M:%S"),
                "actor": ACTOR_LABEL.get(event.actor, event.actor),
                "kind": kind,
                "text": payload.get("text", ""),
                "detail": payload.get("detail", ""),
                "group": event.stage,
                "depth": payload.get("depth", 1),
                "tool": payload.get("tool_name", payload.get("tool", "")),
            }
        )
    return rows
