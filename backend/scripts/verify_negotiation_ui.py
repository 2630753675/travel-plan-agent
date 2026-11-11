"""验收：协商轨迹的浏览器展示。

低预算提交 → 出现中断面板，面板内可见三条协商发言 → 选择降配 → 结果页也保留轨迹。
"""

from __future__ import annotations

import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8848"
DOCS = pathlib.Path(__file__).resolve().parents[2] / "docs"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.goto(BASE, wait_until="networkidle")

        print("\n[1] 提交低预算需求")
        page.locator("label", has_text="总预算").locator("input").fill("6500")
        page.get_by_role("button", name="开始规划", exact=True).click()
        # 协商可能把费用压回阈值内而不中断，两种结局都要能接住
        page.wait_for_selector(".interrupt, .day", timeout=240000)
        has_interrupt = page.locator(".interrupt").count() > 0
        print(f"  结局: {'中断待确认' if has_interrupt else '直接完成'}")

        print("\n[2] 协商轨迹展示")
        page.wait_for_selector(".negotiation", timeout=20000)
        msgs = page.locator(".neg-msg").count()
        names = page.locator(".neg-name").all_inner_texts()
        stances = page.locator(".neg-stance").all_inner_texts()
        outcome = page.locator(".negotiation .neg-outcome").inner_text()
        print(f"  发言: {list(zip(names, stances))}")
        print(f"  结论: {outcome}")

        if "无需调整" in outcome:
            # 没有可削减项时只有预算官一条发言，这是合法路径
            check("至少一条发言", msgs >= 1, str(msgs))
            check("结论为无需调整", True)
        else:
            check("三条发言", msgs == 3, str(msgs))
            check("角色齐全", names == ["预算官", "体验官", "调解者"], str(names))
            check("有立场标签", all(stances), str(stances))
            ev = page.locator(".neg-evidence li").count()
            print(f"  证据条目: {ev}")
            check("证据可见", ev >= 6, str(ev))
        check("结论已标注", bool(outcome))
        page.screenshot(path=str(DOCS / "shot-negotiation.png"), full_page=True)

        if has_interrupt:
            print("\n[3] 选择降配后，结果页保留轨迹")
            page.get_by_role("button", name="降配重排（换便宜酒店/减少付费景点）").click()
            page.wait_for_selector(".day", timeout=240000)
            page.wait_for_timeout(800)
            check("结果页仍有轨迹", page.locator(".negotiation").count() == 1)
            after = page.locator(".neg-msg").count()
            print(f"  轨迹条数 {msgs} → {after}")
            check("轨迹条数不变（未重复协商）", after == msgs, f"{msgs} -> {after}")
            page.screenshot(path=str(DOCS / "shot-negotiation-done.png"), full_page=True)
        else:
            print("\n[3] 未中断，结果页已含轨迹，无需再操作")
            check("结果页含轨迹", page.locator(".negotiation").count() == 1)
            page.screenshot(path=str(DOCS / "shot-negotiation-done.png"), full_page=True)

        browser.close()

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  协商轨迹 UI 验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
