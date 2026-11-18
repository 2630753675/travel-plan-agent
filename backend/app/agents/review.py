"""Compact review packet and issue ledger helpers for peer reviews."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .tools_spec import ToolContext, execute

# Only deterministic checks with a known repair path may block delivery.
BLOCKING_ISSUES = {
    "AIRPORT_TRANSFER_LATE", "AIRPORT_TRANSFER_DUPLICATE", "TIME_OVERLAP",
    "MISSED_AIRPORT_DEADLINE", "OUTSIDE_OPEN_HOURS", "DAY_TOO_LONG", "CLOSED_DAY",
    "BUDGET_EXCEEDED", "MEAL_BEFORE_ARRIVAL", "MEAL_AFTER_FLIGHT",
    "MEAL_AFTER_AIRPORT_DEPARTURE", "ELDER_UNFIT",
}

OWNER_BY_TOOL = {
    "set_hotel": "logistics",
    "set_flights": "logistics",
    "assign_pois": "local_expert",
    "submit_day_plans": "local_expert",
    "replace_fixed_event": "local_expert",
    "remove_fixed_event": "local_expert",
}


def build_review_packet(ctx: ToolContext) -> dict:
    """Compile deterministic facts once; the model need not fetch overlapping reports."""
    check = execute(ctx, "check_plan", {})
    if not check.get("ok"):
        return {"ok": False, "error": check.get("error", "硬校验失败")}
    plan = execute(ctx, "get_plan", {})
    if not plan.get("ok"):
        return {"ok": False, "error": plan.get("error", "读取方案失败")}
    cost = execute(ctx, "day_costs", {})
    if not cost.get("ok"):
        return {"ok": False, "error": cost.get("error", "读取费用失败")}
    experience = execute(ctx, "experience_check", {})
    if not experience.get("ok"):
        return {"ok": False, "error": experience.get("error", "读取体验风险失败")}

    selected = {
        "plan_version": ctx.plan_version,
        "total_cost": plan["total_cost"],
        "budget_total": ctx.brief.budget_total,
        "over_budget": round(plan["total_cost"] - ctx.brief.budget_total, 1),
        "flights": plan["flights"],
        "hotel": plan["hotel"],
        "daily_costs": cost.get("days", []),
        "hard_check": check,
        "experience_check": experience,
    }
    encoded = json.dumps(selected, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        "ok": True,
        "packet_id": hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16],
        **selected,
    }


def make_issue_ledger(packet: dict, changes: list[dict], previous: list[dict]) -> list[dict]:
    """Validate reported issues and deterministically carry open/closed history."""
    facts = {
        (str(issue["code"]), str(issue.get("day") or "trip")): issue
        for issue in (packet.get("hard_check") or {}).get("issues", [])
        if issue.get("code")
    }
    previous_by_id = {
        (str(item.get("code")), str(item.get("day") or "trip")): item
        for item in previous if item.get("issue_id")
    }
    ledger: list[dict] = []
    seen: set[str] = set()

    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("每条评审问题必须是对象")
        code = str(change.get("code") or "")
        requested_day = str(change.get("day") or "trip")
        fact_key = (code, requested_day)
        if fact_key not in facts:
            matching = [key for key in facts if key[0] == code]
            if len(matching) == 1:
                fact_key = matching[0]
            else:
                raise ValueError(f"评审问题 {code or '(缺少 code)'} 不存在于当前 ReviewPacket 的确定性检查结果")
        fact = facts[fact_key]
        if fact.get("severity") != "blocker" or code not in BLOCKING_ISSUES:
            raise ValueError(f"问题 {code} 不是允许阻塞交付的确定性问题")
        acceptance = str(change.get("acceptance") or "").strip()
        if not acceptance:
            raise ValueError(f"问题 {code} 缺少可验证的 acceptance 条件")
        repair_tools = list(fact.get("repair_tools") or [])
        if not repair_tools or any(tool not in OWNER_BY_TOOL for tool in repair_tools):
            raise ValueError(f"问题 {code} 没有已知修复工具，不能要求自动返工")
        owner = OWNER_BY_TOOL.get(repair_tools[0])
        requested_owner = change.get("owner")
        if requested_owner not in {None, "", owner}:
            raise ValueError(f"问题 {code} 应派给 {owner}，不是 {requested_owner}")
        issue_day = str(fact.get("day") or "trip")
        issue_key = (code, issue_day)
        issue_id = f"{code}:{issue_day}"
        if issue_id in seen:
            continue
        seen.add(issue_id)
        previous_issue = previous_by_id.get(issue_key)
        ledger.append({
            "issue_id": issue_id,
            "code": code,
            "day": issue_day,
            "severity": "blocker",
            "owner": owner,
            "evidence": fact["message"],
            "acceptance": acceptance or (previous_issue or {}).get("acceptance", "对应确定性硬校验通过"),
            "repair_tools": repair_tools,
            "status": "open",
            "first_seen_version": (previous_issue or {}).get("first_seen_version", packet["plan_version"]),
            "fixed_in_version": None,
        })

    still_present = {(str(fact.get("code")), str(fact.get("day") or "trip")) for fact in facts.values()}
    for old in previous:
        issue_id = str(old.get("issue_id") or "")
        if not issue_id or issue_id in seen:
            continue
        item = deepcopy(old)
        key = (str(item.get("code") or ""), str(item.get("day") or "trip"))
        if key not in still_present and item.get("status") != "closed":
            item["status"] = "closed"
            item["fixed_in_version"] = packet["plan_version"]
            item["closure_evidence"] = "对应确定性硬校验问题已从当前版本消失"
        elif key in still_present:
            item["status"] = "open"
            item["evidence"] = facts[key]["message"]
            item["fixed_in_version"] = None
        ledger.append(item)
    return ledger
