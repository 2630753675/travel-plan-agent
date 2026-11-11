"""让 LLM 决定"每天去哪些景点"，硬约束仍由规则保证。

分工：LLM 只输出 POI id 的分配与理由；闭馆日、人群限制、时长上限、
时间轴编排全部由 planner 规则层执行。这样模型出错的代价被限制在
"少排/排错景点"，不会产出不可行的行程。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from ..llm import LLMClient, LLMError
from ..models import POI, TripBrief, WeatherDay

SYSTEM = "你是资深行程规划师。只输出 JSON，不要输出任何解释文字或 Markdown。"


def _format_candidates(candidates: list[POI]) -> str:
    rows = [
        {
            "id": p.id,
            "name": p.name,
            "category": p.category,
            "duration_min": p.duration_min,
            "ticket_price": p.ticket_price,
            "tags": p.tags,
            "indoor": p.indoor,
            "open": f"{p.open_time.strftime('%H:%M')}-{p.close_time.strftime('%H:%M')}",
        }
        for p in candidates
    ]
    return json.dumps(rows, ensure_ascii=False)


def _format_weather(days: list[WeatherDay]) -> str:
    rows = [
        {"date": str(w.date), "condition": w.condition, "rain_prob": w.rain_prob}
        for w in days
    ]
    return json.dumps(rows, ensure_ascii=False)


def _build_prompt(brief: TripBrief, candidates: list[POI], weather: list[WeatherDay]) -> str:
    dates = [str(brief.start_date + timedelta(days=i)) for i in range(brief.days)]
    return f"""行程需求：
出发地 {brief.origin}，目的地 {brief.destination}
日期 {brief.start_date} 至 {brief.end_date}（共 {brief.days} 天，需覆盖这些日期：{dates}）
人数 {brief.travelers}，总预算 {brief.budget_total:.0f} 元
同行人群 {brief.companions}，偏好 {brief.preferences}，节奏 {brief.pace}

候选景点（只能从这里选，用 id 引用）：
{_format_candidates(candidates)}

天气：
{_format_weather(weather)}

请为每一天选择 0-4 个景点。必须满足：
- 只用候选列表里的 id
- 同一天不要重复，整个行程尽量不重复
- 老人同行时避开 tags 含"爬山""步行多""老人不宜""老人慎选"的景点
- 雨天（rain_prob >= 0.5）优先 indoor=true 的景点
- 最后一天景点总时长不超过 120 分钟
- 第一天要考虑中午才落地，安排要少

输出严格如下 JSON：
{{"days":[{{"date":"YYYY-MM-DD","poi_ids":["p01","p02"],"reason":"不超过 20 字"}}]}}
只输出这个 JSON。"""


def _validate(
    raw: dict,
    brief: TripBrief,
    candidates: list[POI],
) -> dict[date, list[str]] | None:
    valid_ids = {p.id for p in candidates}
    days = raw.get("days")
    if not isinstance(days, list):
        return None

    out: dict[date, list[str]] = {}
    for entry in days:
        if not isinstance(entry, dict):
            continue
        try:
            d = date.fromisoformat(str(entry.get("date")))
        except ValueError:
            continue
        if d < brief.start_date or d > brief.end_date:
            continue
        ids = entry.get("poi_ids")
        if not isinstance(ids, list):
            ids = []
        # 未知 id 直接丢弃，不让模型编造的数据流进下游
        clean = [str(i) for i in ids if str(i) in valid_ids][:4]
        out[d] = clean

    if not out:
        return None
    # 缺日期不补，交给规则层回落
    return out


async def select_pois(
    brief: TripBrief,
    candidates: list[POI],
    weather: list[WeatherDay],
    client: LLMClient,
) -> dict[date, list[str]] | None:
    """成功返回日期到 POI id 的映射；任何失败返回 None 由调用方回落规则。"""
    if not candidates:
        return None
    try:
        raw = await client.chat_json(SYSTEM, _build_prompt(brief, candidates, weather))
    except LLMError:
        return None
    return _validate(raw, brief, candidates)
