"""领域模型。所有 agent 的输入输出都必须经过这里的校验。"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Slot = Literal["上午", "中午", "下午", "晚上"]
ItemType = Literal["transport", "sight", "meal", "hotel", "rest", "fixed"]
Companion = Literal["老人", "儿童", "无"]
Severity = Literal["blocker", "warning"]


class TripBrief(BaseModel):
    """用户表单提交后经 Clarifier 归一化的需求。"""

    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    start_date: date
    end_date: date
    travelers: int = Field(ge=1, le=20)
    budget_total: float = Field(gt=0, description="总预算，元")
    companions: list[Companion] = Field(default_factory=lambda: ["无"])
    preferences: list[str] = Field(default_factory=list)
    pace: Literal["轻松", "适中", "紧凑"] = "适中"
    planning_mode: Literal["fast", "expert", "peer"] = "fast"

    @model_validator(mode="after")
    def _check(self) -> TripBrief:
        if self.end_date < self.start_date:
            raise ValueError("end_date 不能早于 start_date")
        if self.origin == self.destination:
            raise ValueError("出发地与目的地不能相同")
        if self.days > 14:
            raise ValueError(f"行程 {self.days} 天过长，第一版支持 1-14 天")
        if "无" in self.companions and len(self.companions) > 1:
            raise ValueError("'无' 不能和其他同行人群同时选择")
        return self

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def has_elder(self) -> bool:
        return "老人" in self.companions

    @property
    def has_child(self) -> bool:
        return "儿童" in self.companions


class Flight(BaseModel):
    flight_no: str
    dep_city: str
    arr_city: str
    dep_date: date
    dep_time: time
    arr_time: time
    duration_min: int = Field(gt=0)
    price: float = Field(ge=0)
    seats_left: int = Field(ge=0)


class Hotel(BaseModel):
    name: str
    district: str
    price_per_night: float = Field(ge=0)
    rating: float = Field(ge=0, le=5)
    tags: list[str] = Field(default_factory=list)
    distance_to_center_km: float = Field(ge=0)
    # 有了坐标，住宿 agent 才能算"离用户想去的景点有多远"
    lat: float = 0.0
    lng: float = 0.0


class POI(BaseModel):
    id: str
    name: str
    category: str
    open_time: time
    close_time: time
    closed_days: list[int] = Field(default_factory=list, description="0=周一 ... 6=周日")
    duration_min: int = Field(gt=0)
    ticket_price: float = Field(ge=0)
    recommended_slot: list[Slot] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    lat: float
    lng: float
    indoor: bool = False

    def is_open_on(self, d: date) -> bool:
        return d.weekday() not in self.closed_days


class WeatherDay(BaseModel):
    date: date
    condition: Literal["晴", "多云", "阴", "小雨", "中雨", "大雨"]
    temp_low: int
    temp_high: int
    rain_prob: float = Field(ge=0, le=1)

    @property
    def is_rainy(self) -> bool:
        return self.rain_prob >= 0.5 or self.condition in ("小雨", "中雨", "大雨")


class Item(BaseModel):
    slot: Slot
    start_time: time
    end_time: time
    type: ItemType
    title: str
    ref_id: str | None = None
    cost: float = Field(default=0, ge=0)
    duration_min: int = Field(default=0, ge=0)
    note: str = ""


class DayPlan(BaseModel):
    date: date
    items: list[Item] = Field(default_factory=list)
    day_cost: float = Field(default=0, ge=0)
    meal_estimate: float = Field(default=0, ge=0)
    notes: list[str] = Field(default_factory=list)


class FixedEvent(BaseModel):
    """Agent 明确指定的固定时段安排（如去机场、预约参观）。"""

    date: date
    title: str = Field(min_length=1)
    start_time: time
    end_time: time
    kind: Literal["activity", "airport_transfer"] = "activity"
    note: str = ""


class Itinerary(BaseModel):
    days: list[DayPlan] = Field(default_factory=list)
    total_cost: float = Field(default=0, ge=0)
    budget_total: float = Field(gt=0)
    warnings: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    severity: Severity
    code: str
    day: date | None = None
    message: str


class RunEvent(BaseModel):
    """统一编排事件契约，用于实时流、持久化、回放与 UI 展示。"""

    schema_version: int = 1
    run_id: str
    plan_version: int = Field(default=1, ge=1)
    event_id: str
    seq: int = Field(ge=1)
    occurred_at: datetime
    event_type: Literal[
        "run_started", "run_resumed", "stage_started", "agent_started", "agent_message",
        "agent_completed", "agent_resumed", "tool_call", "tool_result", "tool_exchange", "validation_result", "route_decision",
        "user_question", "user_answer", "artifact_created", "run_interrupted",
        "run_completed", "run_failed",
    ]
    stage: str = ""
    actor: str = "system"
    parent_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# 过渡兼容：现有前端展示字段继续从事件派生，完成前端迁移后移除。
class LogEntry(BaseModel):
    """旧版轨迹视图模型；新数据以 RunEvent 为准。"""

    ts: str
    actor: str
    kind: Literal["stage", "action", "decision", "question", "answer", "tool", "speech"] = "action"
    text: str
    detail: str = ""
    group: str = ""
    depth: int = 1
    tool: str = ""



class Critique(BaseModel):
    passed: bool
    issues: list[Issue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @property
    def blockers(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "blocker"]
