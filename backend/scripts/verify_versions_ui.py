"""P3 验收（浏览器层）：版本面板可查看、可回滚。

覆盖：
A 首次生成后出现版本面板，当前版本标为"当前"
B 单日重排后多出一个版本，来源写着重排
C 点"回到这一版"能回滚，历史不被覆盖

需要后端已在 127.0.0.1:8848 运行。
"""

from __future__ import annotations

import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
BASE = "http://127.0.0.1:8848"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})

        print("\n[1] 生成行程并查看版本面板")
        page.goto(BASE, wait_until="networkidle")
        page.get_by_role("button", name="开始规划", exact=True).click()
        page.wait_for_selector(".day", timeout=240000)
        page.wait_for_selector(".versions", timeout=30000)

        rows = page.locator(".version-row")
        count1 = rows.count()
        sources1 = page.locator(".version-source").all_inner_texts()
        print(f"  版本 {count1} 个：{sources1}")
        check("版本面板出现", count1 >= 1, str(count1))
        check("首版来源是首次生成", sources1[0] == "首次生成", sources1[0])
        check("当前版本有标记", page.locator(".version-current").count() == 1)

        print("\n[2] 重排一天后多出一个版本")
        days = page.locator(".day")
        days.nth(2).locator(".refine input").fill("这天太赶了，想轻松点")
        days.nth(2).locator(".refine button").click()
        page.wait_for_timeout(4000)

        count2 = page.locator(".version-row").count()
        sources2 = page.locator(".version-source").all_inner_texts()
        print(f"  版本 {count2} 个：{sources2}")
        check("版本数增加", count2 > count1, f"{count1} -> {count2}")
        check("新版本来源是单日重排", sources2[-1] == "单日重排", sources2[-1])
        check("新版本有备注", bool(page.locator(".version-note").all_inner_texts()[-1].strip()) and page.locator(".version-note").all_inner_texts()[-1] != "—")

        page.locator(".versions").scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        page.screenshot(path=str(DOCS / "shot-versions.png"))

        print("\n[3] 回滚到首版")
        first_cost = page.locator(".day-cost").first.inner_text()
        # 点第一条历史版本上的"回到这一版"
        page.locator(".version-row:not(.current) button").first.click()
        page.wait_for_timeout(4000)

        sources3 = page.locator(".version-source").all_inner_texts()
        count3 = page.locator(".version-row").count()
        print(f"  版本 {count3} 个：{sources3}")
        check("回滚也留下一条历史", sources3[-1] == "回滚", sources3[-1])
        check("版本数继续增加", count3 > count2, f"{count2} -> {count3}")
        check("仍有且仅有一个当前版本", page.locator(".version-current").count() == 1)
        check("行程仍然完整", page.locator(".day").count() == 5, str(page.locator(".day").count()))
        check("回滚后备注写明目标版本", "v1" in page.locator(".version-note").all_inner_texts()[-1], page.locator(".version-note").all_inner_texts()[-1])

        print("\n[4] 回滚后页面可正常刷新")
        url = page.url
        page2 = browser.new_page(viewport={"width": 1440, "height": 1100})
        page2.goto(url, wait_until="networkidle")
        page2.wait_for_selector(".day", timeout=120000)
        page2.wait_for_selector(".versions", timeout=30000)
        check("刷新后历史仍在", page2.locator(".version-row").count() == count3, str(page2.locator(".version-row").count()))
        page2.close()

        browser.close()

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  版本面板验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
