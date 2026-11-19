import json
import pathlib

from playwright.sync_api import sync_playwright

OUT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\.harness\ui-shots")
BASE = "http://127.0.0.1:4173"

RESULT = {
    "id": "demo",
    "brief": {"origin": "上海", "destination": "成都", "budget_total": 8000},
    "itinerary": {
        "days": [
            {
                "date": "2026-11-20",
                "day_cost": 2860,
                "notes": ["抵达日不排满"],
                "items": [
                    {"slot": "08:05", "start_time": "2026-11-20T08:05:00", "end_time": "2026-11-20T11:20:00", "type": "transport", "title": "MU5401 上海→成都", "cost": 1960, "duration_min": 195, "note": "含 23kg 行李"},
                    {"slot": "11:20", "start_time": "2026-11-20T11:20:00", "end_time": "2026-11-20T12:40:00", "type": "transfer", "title": "机场→酒店", "cost": 120, "duration_min": 80, "note": ""},
                    {"slot": "12:50", "start_time": "2026-11-20T12:50:00", "end_time": "2026-11-20T14:50:00", "type": "sight", "title": "春熙路太古里", "cost": 0, "duration_min": 120, "note": "免费"},
                    {"slot": "18:30", "start_time": "2026-11-20T18:30:00", "end_time": "2026-11-20T19:30:00", "type": "meal", "title": "晚餐窗口", "cost": 260, "duration_min": 60, "note": ""},
                    {"slot": "21:00", "start_time": "2026-11-20T21:00:00", "end_time": "2026-11-20T22:00:00", "type": "hotel", "title": "入住 · 第 1 晚", "cost": 520, "duration_min": 60, "note": ""},
                ],
            },
            {
                "date": "2026-11-21",
                "day_cost": 1000,
                "notes": [],
                "items": [
                    {"slot": "09:00", "start_time": "2026-11-21T09:00:00", "end_time": "2026-11-21T10:30:00", "type": "sight", "title": "杜甫草堂", "cost": 100, "duration_min": 90, "note": "地势平缓"},
                    {"slot": "12:00", "start_time": "2026-11-21T12:00:00", "end_time": "2026-11-21T13:00:00", "type": "meal", "title": "回酒店午休", "cost": 120, "duration_min": 60, "note": ""},
                    {"slot": "13:00", "start_time": "2026-11-21T13:00:00", "end_time": "2026-11-21T15:00:00", "type": "sight", "title": "宽窄巷子", "cost": 0, "duration_min": 120, "note": ""},
                    {"slot": "15:00", "start_time": "2026-11-21T15:00:00", "end_time": "2026-11-21T18:00:00", "type": "free", "title": "自由时间", "cost": 0, "duration_min": 180, "note": ""},
                    {"slot": "21:00", "start_time": "2026-11-21T21:00:00", "end_time": "2026-11-21T22:00:00", "type": "hotel", "title": "回酒店", "cost": 520, "duration_min": 60, "note": ""},
                ],
            },
            {
                "date": "2026-11-22",
                "day_cost": 900,
                "notes": [],
                "items": [
                    {"slot": "09:00", "start_time": "2026-11-22T09:00:00", "end_time": "2026-11-22T11:00:00", "type": "sight", "title": "成都博物馆", "cost": 0, "duration_min": 120, "note": "雨天室内"},
                    {"slot": "11:30", "start_time": "2026-11-22T11:30:00", "end_time": "2026-11-22T12:30:00", "type": "meal", "title": "午餐", "cost": 120, "duration_min": 60, "note": ""},
                    {"slot": "13:00", "start_time": "2026-11-22T13:00:00", "end_time": "2026-11-22T15:00:00", "type": "sight", "title": "四川博物院", "cost": 0, "duration_min": 120, "note": ""},
                    {"slot": "21:00", "start_time": "2026-11-22T21:00:00", "end_time": "2026-11-22T22:00:00", "type": "hotel", "title": "回酒店", "cost": 520, "duration_min": 60, "note": ""},
                ],
            },
            {
                "date": "2026-11-23",
                "day_cost": 1000,
                "notes": [],
                "items": [
                    {"slot": "08:30", "start_time": "2026-11-23T08:30:00", "end_time": "2026-11-23T10:00:00", "type": "sight", "title": "武侯祠", "cost": 100, "duration_min": 90, "note": ""},
                    {"slot": "10:30", "start_time": "2026-11-23T10:30:00", "end_time": "2026-11-23T12:00:00", "type": "sight", "title": "锦里古街", "cost": 0, "duration_min": 90, "note": ""},
                    {"slot": "12:00", "start_time": "2026-11-23T12:00:00", "end_time": "2026-11-23T13:00:00", "type": "meal", "title": "午餐（就地）", "cost": 140, "duration_min": 60, "note": ""},
                    {"slot": "21:00", "start_time": "2026-11-23T21:00:00", "end_time": "2026-11-23T22:00:00", "type": "hotel", "title": "回酒店", "cost": 520, "duration_min": 60, "note": ""},
                ],
            },
            {
                "date": "2026-11-24",
                "day_cost": 2200,
                "notes": [],
                "items": [
                    {"slot": "09:20", "start_time": "2026-11-24T09:20:00", "end_time": "2026-11-24T10:30:00", "type": "transfer", "title": "酒店→天府机场", "cost": 160, "duration_min": 70, "note": "中雨，多留 20 分钟"},
                    {"slot": "13:00", "start_time": "2026-11-24T13:00:00", "end_time": "2026-11-24T16:05:00", "type": "transport", "title": "MU5402 成都→上海", "cost": 1920, "duration_min": 185, "note": ""},
                ],
            },
        ],
        "total_cost": 7960,
        "budget_total": 8000,
        "warnings": ["预算余量仅 40 元"],
    },
    "critique": {
        "passed": True,
        "issues": [{"severity": "warning", "code": "BUDGET_NEAR", "message": "预算使用 99.5%，余量 40 元"}],
        "suggestions": [],
    },
    "revision": 3,
    "errors": [],
    "version": 8,
    "document": {"title": "上海—成都 5 日适老行程", "overview": "以春熙路为大本营，每天 1–2 处。", "days": []},
    "summary": {
        "cost": {
            "total": 7960,
            "budget": 8000,
            "items": [
                {"key": "transport", "label": "交通", "amount": 3880, "share": 0.487},
                {"key": "hotel", "label": "住宿", "amount": 2080, "share": 0.261},
                {"key": "meal", "label": "餐饮", "amount": 1120, "share": 0.141},
                {"key": "sight", "label": "门票", "amount": 600, "share": 0.075},
                {"key": "other", "label": "市内交通", "amount": 280, "share": 0.035},
            ],
        },
        "source": {"kind": "estimate", "label": "估算", "fetched_at": "2026-11-19", "note": "价格未预订"},
        "warning_groups": [],
    },
    "issue_ledger": [
        {"code": "RAIN_OUTDOOR", "status": "closed", "fixed_in_version": 8, "acceptance": "雨天不排户外"},
        {"code": "BUDGET_NEAR", "status": "open", "first_seen_version": 7, "acceptance": "余量 < 5% 提醒"},
    ],
    "llm_stats": {"calls": 15, "total_tokens": 136307, "latency_ms": 122000},
    "team_state": "approved",
    "share": "demo-share",
}

VERSIONS = {
    "versions": [
        {"version": 8, "created_at": "2026-11-19", "source": "team", "note": "雨天改室内", "total_cost": 7960, "day_count": 5, "passed": True, "blockers": 0},
        {"version": 7, "created_at": "2026-11-19", "source": "initial", "note": "首版", "total_cost": 8100, "day_count": 5, "passed": False, "blockers": 1},
    ]
}

REFINED = json.loads(json.dumps(RESULT))
REFINED["version"] = 9
REFINED["itinerary"]["days"][1]["items"][2]["title"] = "成都博物馆（室内）"
REFINED["itinerary"]["days"][1]["items"][2]["cost"] = 0

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 960})

    def route(r):
        url = r.request.url
        if "/versions" in url:
            r.fulfill(status=200, content_type="application/json", body=json.dumps(VERSIONS))
        elif "/refine" in url:
            r.fulfill(status=200, content_type="application/json", body=json.dumps(REFINED))
        elif "/api/plan/demo" in url:
            r.fulfill(status=200, content_type="application/json", body=json.dumps(RESULT))
        else:
            r.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/plan/demo**", route)

    page.goto(BASE + "/?plan=demo")
    page.wait_for_timeout(1200)
    page.screenshot(path=str(OUT / "a1-v1-plan.png"), full_page=False)
    print("plan visible:", page.locator("text=已评审").count() > 0)

    page.click("text=预算")
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / "a2-v1-budget.png"), full_page=False)

    page.click("text=变更")
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / "a3-v1-change.png"), full_page=False)

    page.click("text=时刻表")
    page.wait_for_timeout(200)
    page.click("text=11-21")
    page.wait_for_timeout(200)
    page.screenshot(path=str(OUT / "a4-v1-day.png"), full_page=False)

    page.click(".v1-blk.sight")
    page.wait_for_timeout(200)
    page.screenshot(path=str(OUT / "a5-v1-block.png"), full_page=False)

    page.click("text=交付 →")
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "a6-v1-share.png"), full_page=False)

    page.goto(BASE + "/")
    page.wait_for_timeout(600)
    page.screenshot(path=str(OUT / "a7-v1-home.png"), full_page=False)
    page.click("text=开始规划")
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "a8-v1-demand.png"), full_page=False)
    page.click("text=继续，确认取舍")
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "a9-v1-brief.png"), full_page=False)

    browser.close()
