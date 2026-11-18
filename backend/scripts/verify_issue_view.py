"""界面数据回归：Issue Ledger 视图与差异交付摘要。

不调用真实模型/网络。
运行：
    .venv/Scripts/python.exe scripts/verify_issue_view.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from app.agents import hub  # noqa: E402
from app.api.main import _issue_ledger_view  # noqa: E402
from verify_peer_success import ASSIGNMENT, DAYS  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'} {name}{(': ' + str(detail)) if detail and not cond else ''}")
    if not cond:
        failures.append(name)


def main() -> int:
    ledger = [
        {"issue_id": "AIRPORT_TRANSFER_DUPLICATE:2026-11-24", "code": "AIRPORT_TRANSFER_DUPLICATE",
         "severity": "blocker", "owner": "local_expert", "status": "open",
         "acceptance": "返程日只保留一条送机", "first_seen_version": 9,
         "fixed_in_version": None, "evidence": "两条送机", "private": "不应暴露"},
        {"issue_id": "BUDGET_EXCEEDED:trip", "code": "BUDGET_EXCEEDED", "severity": "blocker",
         "owner": "logistics", "status": "closed", "acceptance": "总费用不超预算",
         "first_seen_version": 8, "fixed_in_version": 10, "evidence": "超支"},
        {"code": "", "status": "open"},
        "not-a-dict",
    ]
    view = _issue_ledger_view({"pending_issue_codes": ledger})
    check("账本裁剪为界面字段", len(view) == 2, len(view))
    check("不暴露无关字段", all("private" not in row and "issue_id" not in row for row in view))
    check("保留 open/closed 状态与关闭版本",
          {row["status"] for row in view} == {"open", "closed"}
          and next(r for r in view if r["status"] == "closed")["fixed_in_version"] == 10)
    check("空账本返回空数组", _issue_ledger_view({}) == [])
    check("旧会话无该字段不报错", _issue_ledger_view({"architecture": "professional_team"}) == [])

    base = {"version": 7, "days": [{"date": day, "poi_ids": ASSIGNMENT[day]} for day in DAYS],
            "rationale": "首轮"}
    patch = {
        "base_version": 7,
        "rationale": "替换闭馆景点",
        "days": [{"date": DAYS[2], "poi_ids": ["p02", "p03"]},
                 {"date": DAYS[1], "poi_ids": ASSIGNMENT[DAYS[1]]}],
    }
    summary = hub._delivery_change_summary(base, patch, plan_version=9)
    check("只记录真正变化的日期", len(summary["fields"]) == 1
          and summary["fields"][0]["date"] == DAYS[2], summary["fields"])
    check("摘要含前后对比", summary["fields"][0]["before"] == ASSIGNMENT[DAYS[2]]
          and summary["fields"][0]["after"] == ["p02", "p03"])
    check("摘要绑定基线版本", summary["base_version"] == 7 and summary["plan_version"] == 9)
    hotel_patch = {"base_version": 7, "rationale": "换酒店", "hotel_id": "春熙里和颐"}
    hotel_summary = hub._delivery_change_summary(
        {"version": 7, "hotel_id": "旧酒店", "outbound_flight_no": "3U8890",
         "inbound_flight_no": "3U8891"}, hotel_patch, plan_version=8)
    check("交通住宿只记录变更字段", [f["kind"] for f in hotel_summary["fields"]] == ["hotel_id"],
          hotel_summary["fields"])
    empty = hub._delivery_change_summary(base, {"base_version": 7, "rationale": "无变化"}, plan_version=7)
    check("无实际变化时字段为空", empty["fields"] == [])

    print(f"\n{'PASS' if not failures else 'FAIL'} 界面数据回归{'通过' if not failures else '失败'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
