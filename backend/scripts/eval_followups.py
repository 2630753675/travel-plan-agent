"""追问质量评测（真实模型，手动运行）。

用法：
    backend/.venv/Scripts/python.exe backend/scripts/eval_followups.py --limit 5

输出 docs/eval-followups.json，不进入 CI，避免偶然消耗模型额度。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.followups import MAX_QUESTIONS, _duplicate, generate_questions  # noqa: E402
from app.llm import LLMError, LLMClient  # noqa: E402
from app.models import TripBrief  # noqa: E402
from app.settings import load  # noqa: E402

CASES = [
    {
        "name": "成都-老人-人文",
        "brief": dict(origin="上海", destination="成都", start_date="2026-12-10", end_date="2026-12-14",
                      travelers=2, budget_total=8000, companions=["老人"], preferences=["人文", "美食"], pace="适中"),
        "raw_text": "成都 5 天，2 人，带 1 位老人，预算 8000，想看人文、吃点好的，节奏别太赶",
    },
    {
        "name": "东京-购物-美食",
        "brief": dict(origin="上海", destination="东京", start_date="2026-12-02", end_date="2026-12-05",
                      travelers=2, budget_total=12000, companions=["无"], preferences=["购物", "美食"], pace="紧凑"),
        "raw_text": "东京 4 天，2 人，预算 1.2 万，主要购物和吃，节奏可以紧一点",
    },
    {
        "name": "三亚-儿童-海边",
        "brief": dict(origin="北京", destination="三亚", start_date="2026-12-18", end_date="2026-12-20",
                      travelers=3, budget_total=6000, companions=["儿童"], preferences=["自然"], pace="轻松"),
        "raw_text": "三亚 3 天，一家三口带一个 6 岁孩子，预算 6000，想住海边，别太累",
    },
    {
        "name": "西安-独行-历史",
        "brief": dict(origin="杭州", destination="西安", start_date="2026-12-08", end_date="2026-12-09",
                      travelers=1, budget_total=3000, companions=["无"], preferences=["人文"], pace="适中"),
        "raw_text": "西安 2 天，一个人，预算 3000，看历史遗迹，住得方便就行",
    },
    {
        "name": "北京-老人儿童-人文",
        "brief": dict(origin="上海", destination="北京", start_date="2026-12-11", end_date="2026-12-15",
                      travelers=4, budget_total=15000, companions=["老人", "儿童"], preferences=["人文"], pace="轻松"),
        "raw_text": "北京 5 天，4 个人，带老人和小孩，预算 1.5 万，想看故宫长城，节奏慢一点",
    },
    {
        "name": "上海-一日-美食",
        "brief": dict(origin="苏州", destination="上海", start_date="2026-12-06", end_date="2026-12-06",
                      travelers=2, budget_total=1000, companions=["无"], preferences=["美食"], pace="紧凑"),
        "raw_text": "上海一天，两个人，预算 1000，主要吃，顺便逛逛",
    },
    {
        "name": "成都-低预算-自然",
        "brief": dict(origin="重庆", destination="成都", start_date="2026-12-20", end_date="2026-12-26",
                      travelers=2, budget_total=5000, companions=["无"], preferences=["自然"], pace="适中"),
        "raw_text": "成都 7 天，2 人，预算只有 5000，想多看自然，住青旅也行",
    },
    {
        "name": "云南-摄影-长线",
        "brief": dict(origin="广州", destination="昆明", start_date="2026-12-01", end_date="2026-12-06",
                      travelers=3, budget_total=9000, companions=["无"], preferences=["自然", "人文"], pace="适中"),
        "raw_text": "云南 6 天，3 人，预算 9000，喜欢摄影，想去大理丽江，节奏适中",
    },
    {
        "name": "广州-短途-美食",
        "brief": dict(origin="深圳", destination="广州", start_date="2026-12-13", end_date="2026-12-15",
                      travelers=2, budget_total=4000, companions=["无"], preferences=["美食"], pace="轻松"),
        "raw_text": "广州 3 天，2 人，预算 4000，主要吃早茶和粤菜，不想排队太久",
    },
    {
        "name": "哈尔滨-冬季-老人",
        "brief": dict(origin="上海", destination="哈尔滨", start_date="2026-12-22", end_date="2026-12-25",
                      travelers=2, budget_total=9000, companions=["老人"], preferences=["自然", "人文"], pace="轻松"),
        "raw_text": "哈尔滨 4 天，2 人，带老人，预算 9000，看冰雪，怕冷，节奏慢",
    },
]

GENERIC = ("有什么偏好", "想去哪", "有什么要求", "预算多少", "想怎么玩")


def flags_for(questions) -> list[str]:
    flags: list[str] = []
    if len(questions) > MAX_QUESTIONS:
        flags.append("too_many")
    for i, q in enumerate(questions):
        if any(g in q.title for g in GENERIC):
            flags.append("generic")
        if q.kind == "ask" and len(q.options) < 2:
            flags.append("ask_without_options")
        if not q.why:
            flags.append("missing_why")
        for other in questions[:i]:
            if _duplicate(q.title, other.title):
                flags.append("duplicate")
    return sorted(set(flags))


async def main(limit: int) -> int:
    cfg = load()
    if not cfg.llm.configured:
        print("未配置模型，无法评测", file=sys.stderr)
        return 2
    client = LLMClient(cfg.llm, cfg.orchestration.node_timeout_s, budget=None)
    results = []
    for case in CASES[:limit]:
        brief = TripBrief(**case["brief"])
        started = time.time()
        try:
            questions = await generate_questions(client, brief, case["raw_text"])
            elapsed = round(time.time() - started, 1)
            results.append({
                "name": case["name"],
                "count": len(questions),
                "titles": [q.title for q in questions],
                "kinds": [q.kind for q in questions],
                "flags": flags_for(questions),
                "elapsed_s": elapsed,
                "error": "",
            })
            print(f"{case['name']}: {len(questions)} 条，{elapsed}s，flags={results[-1]['flags']}")
        except LLMError as e:
            results.append({"name": case["name"], "count": 0, "titles": [], "kinds": [],
                            "flags": ["error"], "elapsed_s": 0, "error": str(e)[:200]})
            print(f"{case['name']}: 失败 {e}")

    report = {
        "model": cfg.llm.model,
        "samples": len(results),
        "max_questions": MAX_QUESTIONS,
        "results": results,
        "summary": {
            "errors": sum(1 for r in results if r["error"]),
            "duplicates": sum(1 for r in results if "duplicate" in r["flags"]),
            "generic": sum(1 for r in results if "generic" in r["flags"]),
            "too_many": sum(1 for r in results if "too_many" in r["flags"]),
        },
    }
    out = ROOT / "docs" / "eval-followups.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告写入 {out}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5, help="评测前 N 个样本")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(max(1, min(args.limit, len(CASES))))))
