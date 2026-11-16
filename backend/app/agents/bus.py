"""对等 Agent 的消息总线。

没有中心调度者：谁能被唤醒、谁该处理消息，由 Agent 自己发的消息决定。
调度器只做两件事——按到达顺序让醒着的 Agent 轮流处理信箱、检查是否收敛。
真正的"派谁去干"是 Agent 在消息里写出来的（`wake` 字段）。

会话必须可序列化：用户确认后要能从中断处继续，不能重跑已经花掉的模型调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

# 消息语义：
# request   有具体请求要对方处理
# wake      叫醒某人（通常附带 request）
# proposal  给出方案
# objection 反对并说明理由
# accept    认可
# escalate  无法协调，要请示用户
# final     定稿
KINDS = {"request", "wake", "proposal", "objection", "accept", "escalate", "final", "note"}


@dataclass
class Envelope:
    sender: str
    recipients: list[str]
    kind: str
    content: str
    payload: dict = field(default_factory=dict)
    reply_to: str | None = None
    round: int = 0
    mid: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict:
        return {
            "mid": self.mid,
            "sender": self.sender,
            "recipients": list(self.recipients),
            "kind": self.kind,
            "content": self.content,
            "payload": self.payload,
            "reply_to": self.reply_to,
            "round": self.round,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Envelope":
        return cls(
            sender=raw["sender"],
            recipients=list(raw.get("recipients") or []),
            kind=raw.get("kind", "note"),
            content=raw.get("content", ""),
            payload=raw.get("payload") or {},
            reply_to=raw.get("reply_to"),
            round=int(raw.get("round", 0)),
            mid=raw.get("mid") or uuid4().hex,
        )


class Bus:
    """消息 + 信箱 + 唤醒状态。"""

    def __init__(self) -> None:
        self.messages: list[Envelope] = []
        self.mailboxes: dict[str, list[str]] = {}
        self.awake: list[str] = []
        self.sleeping: list[str] = []
        self.wake_log: list[dict] = []
        self.round = 0

    # --- 投递 ---
    def post(self, env: Envelope) -> Envelope:
        if env.kind not in KINDS:
            env.kind = "note"
        self.messages.append(env)
        for who in env.recipients:
            self.mailboxes.setdefault(who, []).append(env.mid)
        return env

    def wake(self, agent: str, *, by: str, reason: str = "") -> None:
        if agent in self.awake:
            return
        if agent in self.sleeping:
            self.sleeping.remove(agent)
        self.awake.append(agent)
        self.wake_log.append({"agent": agent, "by": by, "reason": reason, "round": self.round})

    def sleep(self, agent: str) -> None:
        if agent in self.awake:
            self.awake.remove(agent)
        if agent not in self.sleeping:
            self.sleeping.append(agent)

    # --- 读取 ---
    def take(self, agent: str) -> list[Envelope]:
        """取走某人的全部待处理消息。"""
        ids = self.mailboxes.pop(agent, [])
        by_id = {m.mid: m for m in self.messages}
        return [by_id[i] for i in ids if i in by_id]

    def unread(self, agent: str) -> int:
        return len(self.mailboxes.get(agent, []))

    def any_awake(self) -> bool:
        """还有醒着的 Agent 就继续；醒着但没事可做时，Agent 自己会去睡。"""
        return bool(self.awake)

    def transcript(self, limit: int = 40) -> list[dict]:
        return [
            {
                "sender": m.sender,
                "recipients": m.recipients,
                "kind": m.kind,
                "content": m.content,
                "round": m.round,
            }
            for m in self.messages[-limit:]
        ]

    def to_dict(self) -> dict:
        return {
            "messages": [m.to_dict() for m in self.messages],
            "mailboxes": {k: list(v) for k, v in self.mailboxes.items()},
            "awake": list(self.awake),
            "sleeping": list(self.sleeping),
            "wake_log": list(self.wake_log),
            "round": self.round,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Bus":
        bus = cls()
        if not raw:
            return bus
        bus.messages = [Envelope.from_dict(m) for m in raw.get("messages") or []]
        bus.mailboxes = {k: list(v) for k, v in (raw.get("mailboxes") or {}).items()}
        bus.awake = list(raw.get("awake") or [])
        bus.sleeping = list(raw.get("sleeping") or [])
        bus.wake_log = list(raw.get("wake_log") or [])
        bus.round = int(raw.get("round") or 0)
        return bus


def latest(bus: Bus, kind: str, sender: str | None = None) -> Envelope | None:
    for m in reversed(bus.messages):
        if m.kind == kind and (sender is None or m.sender == sender):
            return m
    return None


def ideas_from(bus: Bus, sender: str, kind: str) -> list[Envelope]:
    """取某人某类消息的全部（按时间正序），用于判断"这个角色表过态没有"。"""
    return [m for m in bus.messages if m.sender == sender and m.kind == kind]
