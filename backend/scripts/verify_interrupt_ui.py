"""验收：中断面板的浏览器交互。

低预算提交 → 出现中断面板 → 选择降配 → 产出更省钱的行程。
需要后端已在 127.0.0.1:8848 运行，且模型可用（否则走规则，同样会触发中断）。
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
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(BASE, wait_until="networkidle")

        print("\n[1] 填低预算并提交")
        page.locator("label", has_text="总预算").locator("input").fill("3000")
        page.get_by_role("button", name="开始规划", exact=True).click()

        print("\n[2] 等待中断面板")
        page.wait_for_selector(".interrupt", timeout=180000)
        text = page.locator(".interrupt").inner_text()
        print(f"  面板: {text[:120]}")
        check("出现中断面板", page.locator(".interrupt").is_visible())
        check("说明超出预算", "超出" in text, text[:60])
        check("状态显示等待决定", page.locator(".status-pill").inner_text() == "等待你的决定")

        # 协商走真实模型，停在哪一面板取决于这一轮谈到什么：
        # 谈崩 -> 先问倾向；谈出方案但仍超支 -> 直接问接受/降配/取消。
        # 两条都是合法路径，逐条走到底，而不是假设只有一条。
        deadlock = "谈不拢" in text
        if deadlock:
            print("  先停在协商僵持，按'预算优先'继续一轮")
            buttons = page.locator(".interrupt-actions button").all_inner_texts()
            check("僵持时给三个倾向", len(buttons) == 3, str(buttons))
            page.screenshot(path=str(DOCS / "shot-interrupt-deadlock.png"))
            page.get_by_role("button", name="预算优先，砍掉贵的").click()
            page.wait_for_selector(".interrupt", timeout=180000)
            text = page.locator(".interrupt").inner_text()
            print(f"  第二轮面板: {text[:100]}")
            check("表态后重新给出决定面板", "超出" in text, text[:60])
        else:
            page.screenshot(path=str(DOCS / "shot-interrupt.png"))

        buttons = page.locator(".interrupt-actions button").all_inner_texts()
        print(f"  选项: {buttons}")
        check("三个选项", len(buttons) == 3, str(buttons))

        print("\n[3] 选择降配")
        page.get_by_role("button", name="降配重排（换便宜酒店/减少付费景点）").click()
        page.wait_for_selector(".day", timeout=180000)
        check("产出行程", page.locator(".day").count() == 5, str(page.locator(".day").count()))
        check("面板已消失", page.locator(".interrupt").count() == 0)

        stats = page.locator(".stat strong").all_inner_texts()
        print(f"  统计: {stats}")
        cost = float(stats[0].replace(" 元", ""))
        budget = float(stats[1].replace(" 元", ""))
        print(f"  最终花费 {cost} / 预算 {budget}")
        check("降配后仍超预算但被记录", cost > budget)
        # 提示现在按类别分组展示（.notice-groups）；兼容没有 summary 时的平铺 .warnings
        if page.locator(".notice-groups").count() > 0:
            warns = page.locator(".notice-groups").inner_text()
        else:
            warns = page.locator(".warnings").inner_text()
        check("注明降配来源", "规则引擎执行" in warns or "降配" in warns, warns[:80])
        page.screenshot(path=str(DOCS / "shot-interrupt-done.png"), full_page=True)

        browser.close()

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  中断面板交互验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
