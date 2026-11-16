"""P4 验收（浏览器层）：真实点击 → SSE → 行程 → 单日重排。

依赖 Playwright + 系统 Edge，无需下载浏览器内核。
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
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        print("\n[1] 页面加载")
        page.goto(BASE, wait_until="networkidle")
        check("标题渲染", "旅行规划" in page.title())
        start_btn = page.get_by_role("button", name="开始规划", exact=True)
        check("表单可见", start_btn.is_visible())

        print("\n[2] 点击开始规划")
        start_btn.click()
        # 规划很快，先抢一张运行中截图，失败不影响判定
        try:
            page.screenshot(path=str(DOCS / "shot-ui-running.png"))
        except Exception:
            pass

        page.wait_for_selector(".day", timeout=240000)
        days = page.locator(".day").count()
        print(f"  行程天数 {days}")
        check("生成 5 天", days == 5, str(days))

        stats = page.locator(".stat strong").all_inner_texts()
        print(f"  统计: {stats}")
        check("统计区有 5 项", len(stats) >= 5, str(len(stats)))
        check("校验状态存在", any("通过" in s or "未解决" in s for s in stats), str(stats))

        trace_cards = page.locator(".agent").count()
        print(f"  编排过程卡片 {trace_cards} 个（已移入右侧抽屉）")
        # 过程展示已从主区移到右侧执行轨迹抽屉，这里改为检查抽屉内容
        page.get_by_role("button", name="执行轨迹").click()
        page.wait_for_selector(".drawer", timeout=10000)
        trace_rows = page.locator(".conversation-message").count()
        print(f"  抽屉内对话 {trace_rows} 条")
        check("轨迹有内容", trace_rows >= 7, str(trace_rows))
        page.get_by_role("button", name="收起").click()

        page.screenshot(path=str(DOCS / "shot-ui-done.png"), full_page=True)

        print("\n[3] 单日重排")
        day_cards = page.locator(".day")
        first_day_items = day_cards.nth(2).locator(".item .title").all_inner_texts()
        others_before = [
            day_cards.nth(i).locator(".item .title").all_inner_texts() for i in (0, 1, 3, 4)
        ]
        day_cards.nth(2).locator(".refine input").fill("这天太赶了，想轻松点，最好换个类型不一样的")
        day_cards.nth(2).locator(".refine button").click()
        page.wait_for_timeout(3000)
        after = day_cards.nth(2).locator(".item .title").all_inner_texts()
        others_after = [day_cards.nth(i).locator(".item .title").all_inner_texts() for i in (0, 1, 3, 4)]
        print(f"  重排前: {first_day_items[:4]}")
        print(f"  重排后: {after[:4]}")
        if first_day_items == after:
            # 模型可以判断"这天已经合适"，这不算失败，只记录
            print("  提示：本次模型判断该天无需改动")
        else:
            print("  内容已变化")
        check("重排后仍是完整 5 天", day_cards.count() == 5, str(day_cards.count()))
        check("其他各天未被改动", others_before == others_after)
        page.screenshot(path=str(DOCS / "shot-ui-refined.png"), full_page=True)

        print("\n[4] 分享链接可回放")
        url = page.url
        check("URL 带 plan 参数", "plan=" in url, url)
        if "plan=" in url:
            page2 = browser.new_page(viewport={"width": 1440, "height": 1000})
            page2.goto(url, wait_until="networkidle")
            page2.wait_for_selector(".day", timeout=120000)
            check("回放能看到行程", page2.locator(".day").count() == 5)
            page2.close()

        browser.close()

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  UI 交互全部验收项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
