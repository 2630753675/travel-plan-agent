"""运行前的需求追问。

只读结构化需求，判断“哪些信息会显著改变行程、但用户没有说”。
能推断的写成确认项（infer），真正无法推断的才提问（ask）。
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .llm import LLMClient, LLMError
from .models import TripBrief

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 3


class FollowUpOption(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    value: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=80)


class FollowUpQuestion(BaseModel):
    kind: Literal["infer", "ask"] = "ask"
    title: str = Field(min_length=2, max_length=60)
    why: str = Field(default="", max_length=120)
    confidence: Literal["高", "中", "低"] = "中"
    options: list[FollowUpOption] = Field(default_factory=list)
    allow_custom: bool = True


class FollowUpRequest(BaseModel):
    brief: TripBrief
    raw_text: str = Field(default="", max_length=1000)


SYSTEM = """你是旅行规划团队的“需求澄清官”。你会收到一份结构化需求和用户原话。
你的任务：判断有没有“会显著改变行程、但用户没有说明”的信息，并给出最多 3 条追问。

规则：
1. 能合理推断的，用 kind="infer" 写成确认项，让用户确认，不要直接问；推断必须有依据。
2. 只有真正无法推断、并且不同答案会明显改变行程时，才用 kind="ask"。
3. 已经有明确字段的信息，不要再问；不要问“有什么偏好”这类泛问题。
4. 最多 3 条；如果信息足够，返回 {"questions": []}，不要凑数。
5. 每条给 2–3 个具体、互斥的选项；note 写清这个选择的代价或后果。
6. 中文；title 一句话；why 说明它影响行程的哪一部分。

严格输出 JSON，不要 markdown：
{"questions":[{"kind":"infer|ask","title":"...","why":"...","confidence":"高|中|低","options":[{"label":"...","value":"...","note":"..."}],"allow_custom":true}]}
"""


def _fallback_options(kind: str) -> list[FollowUpOption]:
    if kind == "infer":
        return [
            FollowUpOption(label="同意，这样安排", value="agree"),
            FollowUpOption(label="不同意，按普通节奏", value="disagree", note="可能更累"),
        ]
    return []


def _clean(items: object) -> list[FollowUpQuestion]:
    if not isinstance(items, list):
        return []
    out: list[FollowUpQuestion] = []
    for raw in items[:MAX_QUESTIONS]:
        if not isinstance(raw, dict):
            continue
        try:
            q = FollowUpQuestion(**raw)
        except (ValidationError, TypeError):
            continue
        if '？' in q.title or '?' in q.title:
            q.kind = 'ask'
        if not q.options:
            q.options = _fallback_options(q.kind)
        if not q.options:
            continue
        for opt in q.options:
            if not opt.value:
                opt.value = opt.label
        out.append(q)
    return out


async def generate_questions(
    client: LLMClient, brief: TripBrief, raw_text: str = ""
) -> list[FollowUpQuestion]:
    payload = {
        "brief": brief.model_dump(mode="json"),
        "raw_text": raw_text.strip()[:800],
        "days": brief.days,
    }
    data = await client.chat_json(SYSTEM, json.dumps(payload, ensure_ascii=False))
    return _clean(data.get("questions") if isinstance(data, dict) else None)


async def safe_generate(
    client: LLMClient, brief: TripBrief, raw_text: str = ""
) -> tuple[list[FollowUpQuestion], str]:
    """失败时返回空列表和原因；调用方决定是否直接开跑。"""
    try:
        return await generate_questions(client, brief, raw_text), ""
    except LLMError as e:
        logger.warning("追问生成失败：%s", e)
        return [], str(e)
