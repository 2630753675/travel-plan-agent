"""运行前的需求追问。

只读结构化需求，判断“哪些信息会显著改变行程、但用户没有说”。
能推断的写成确认项（infer），真正无法推断的才提问（ask）。
"""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .llm import LLMClient, LLMError
from .models import TripBrief

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 2


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
你的任务：判断有没有“会显著改变行程、但用户没有说明”的信息，并给出最多 2 条追问。

规则：
1. 能合理推断的，用 kind="infer" 写成确认项，让用户确认，不要直接问；推断必须有依据。
2. 只有真正无法推断、并且不同答案会明显改变行程时，才用 kind="ask"。
3. 已经有明确字段的信息，不要再问；不要问“有什么偏好”“想去哪”这类泛问题。
4. 最多 2 条；如果信息足够，返回 {"questions": []}，不要凑数。
5. 每条给 2–3 个具体、互斥的选项；note 写清这个选择的代价或后果。
6. 每条问题必须能说清“答案如何改变行程”；说不清就不问。
7. 中文；title 一句话；why 说明它影响行程的哪一部分。
8. 不要重复：两条问题不能是同一个信息点的不同问法。

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


def _norm_title(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;（）()「」\"'“”]+", "", text).lower()


def _duplicate(a: str, b: str) -> bool:
    left, right = _norm_title(a), _norm_title(b)
    if not left or not right:
        return False
    if left == right:
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.82


def _clean(items: object) -> list[FollowUpQuestion]:
    if not isinstance(items, list):
        return []
    out: list[FollowUpQuestion] = []
    for raw in items[:MAX_QUESTIONS + 2]:
        if not isinstance(raw, dict):
            continue
        try:
            q = FollowUpQuestion(**raw)
        except (ValidationError, TypeError):
            continue
        if '？' in q.title or '?' in q.title:
            q.kind = 'ask'
        # “默认……”这类是确认项，不应因为带问号而被标成提问。
        if '默认' in q.title or '确认一下' in q.title:
            q.kind = 'infer'
        if not q.options:
            q.options = _fallback_options(q.kind)
        if q.kind == 'ask' and len(q.options) < 2:
            continue
        if not q.options:
            continue
        for opt in q.options:
            if not opt.value:
                opt.value = opt.label
        if any(_duplicate(q.title, existing.title) for existing in out):
            continue
        out.append(q)
        if len(out) >= MAX_QUESTIONS:
            break
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


def classify_failure(exc: Exception) -> dict:
    """把底层错误翻译成用户可读、可判断是否重试的失败信息。"""
    text = str(exc).lower()
    if "429" in text:
        return {"code": "rate_limited", "retryable": True, "message": "模型服务繁忙，请稍后重试"}
    if "timeout" in text or "超时" in text:
        return {"code": "timeout", "retryable": True, "message": "生成追问超时，请重试"}
    if any(f"http {code}" in text for code in (500, 502, 503, 504)):
        return {"code": "upstream", "retryable": True, "message": "模型服务暂时不可用，请重试"}
    if "域名" in text or "无法连接" in text:
        return {"code": "network", "retryable": True, "message": "无法连接模型服务，请检查网络后重试"}
    if "缺少" in text or "401" in text or "403" in text or "未配置" in text:
        return {"code": "config", "retryable": False, "message": "模型配置不可用，请先检查设置"}
    if "json" in text or "结构" in text or "空内容" in text or "截断" in text:
        return {"code": "invalid_output", "retryable": True, "message": "模型返回的追问格式不可用，请重试"}
    return {"code": "unknown", "retryable": True, "message": "追问生成失败，请重试"}


async def safe_generate(
    client: LLMClient, brief: TripBrief, raw_text: str = ""
) -> tuple[list[FollowUpQuestion], dict | None]:
    """失败时返回空列表和结构化失败信息；调用方决定重试或直接开跑。"""
    try:
        return await generate_questions(client, brief, raw_text), None
    except LLMError as e:
        logger.warning("追问生成失败：%s", e)
        return [], classify_failure(e)
