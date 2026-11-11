"""让模型理解用户对某一天的意见并重排该日。

与 llm_selector 的分工一致：模型只决定"这天去哪些景点"，时间轴与硬约束仍由 planner 执行。
模型失败时返回 None，调用方回落关键词规则。
"""

from __future__ import annotations

import json
from datetime import date

from ..llm import LLMClient, LLMError
from ..models import DayPlan, POI, TripBrief

SYSTEM = "你是行程调整助手。只输出 JSON，不要输出解释文字或 Markdown。"


def _day_snapshot(day: DayPlan) -> str:
    rows = [
        {
            "time": f"{i.start_time.strftime('%H:%M')}-{i.end_time.strftime('%H:%M')}",
            "type": i.type,
            "title": i.title,
        }
        for i in day.items
        if i.type != "rest"
    ]
    return json.dumps(rows, ensure_ascii=False)


def _candidates(candidates: list[POI], used_elsewhere: set[str]) -> str:
    rows = [
        {
            "id": p.id,
            "name": p.name,
            "duration_min": p.duration_min,
            "ticket_price": p.ticket_price,
            "tags": p.tags,
            "indoor": p.indoor,
        }
        for p in candidates
        if p.id not in used_elsewhere
    ]
    return json.dumps(rows, ensure_ascii=False)


async def refine_day(
    brief: TripBrief,
    day: DayPlan,
    feedback: str,
    candidates: list[POI],
    used_elsewhere: set[str],
    is_rainy: bool,
    client: LLMClient,
) -> list[str] | None:
    """成功返回该日新的 POI id 列表（可能为空表示这天改成休息），失败返回 None。"""
    valid_ids = {p.id for p in candidates if p.id not in used_elsewhere}
    if not valid_ids:
        return None

    prompt = f"""我们要调整一次 {brief.destination} 行程中的某一天。

当天已有安排：
{_day_snapshot(day)}

用户意见：{feedback or "（没写具体意见）"}

其他天已使用、本次不能重复选择的景点：{sorted(used_elsewhere) or "无"}
可选景点（只能从这里选，用 id）：
{_candidates(candidates, used_elsewhere)}

当天天气：{"有雨，优先室内景点" if is_rainy else "无明显降雨"}
同行人群：{brief.companions}，节奏：{brief.pace}

请结合用户意见重新选择这一天的景点。必须满足：
- 只用可选列表里的 id，0-4 个
- 用户说"赶/紧/累/轻松"就减少数量，说要"多/丰富"就增加
- 老人同行时避开 tags 含"爬山""步行多""老人不宜"的景点
- 选 0 个表示这天改成自由活动

输出严格如下 JSON，只输出这个 JSON：
{{"poi_ids":["p01"],"reason":"不超过 30 字，说明改了什么"}}"""

    try:
        raw = await client.chat_json(SYSTEM, prompt)
    except LLMError:
        return None

    ids = raw.get("poi_ids")
    if not isinstance(ids, list):
        return None
    clean = [str(i) for i in ids if str(i) in valid_ids][:4]
    return clean


def day_of(day: DayPlan) -> date:
    return day.date
