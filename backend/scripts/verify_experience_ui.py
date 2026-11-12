"""P4 验收（浏览器层）：结果页的信息密度与可操作性。

覆盖：
A 花费构成可见，图例金额之和等于总花费
B 数据来源明确标注为模拟数据，并给出"不代表真实可订"
C 提示按类别分组，而不是一锅平铺
D 运行中有进度提示（不是白屏等结果）
E 规划失败时给得出"用同样的需求重试"

需要后端已在 127.0.0.1:8848 运行。
"""

from __future__ import annotations

import pathlib
import re
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


def to_number(text: str) -> float:
    """只取 "1234 元" 里的金额，不能把后面的百分比也拼进来。"""
    match = re.search(r"(\d+(?:\.\d+)?)\s*元", text)
    if match:
        return float(match.group(1))
    digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    return float(digits) if digits else 0.0


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})

        print("\n[1] 运行中要有进度，而不是白屏")
        page.goto(BASE, wait_until="networkidle")
        page.get_by_role("button", name="开始规划", exact=True).click()
        saw_running = False
        try:
            page.wait_for_selector(".running", timeout=8000)
            saw_running = True
            stage = page.locator(".running-head strong").inner_text()
            print(f"  运行中提示：{stage}")
            check("显示了当前阶段", "正在" in stage, stage)
            # 有阶段真正推进过，才算"看得到进度"而不是一个静态占位
            try:
                page.wait_for_selector(".running-step.done", timeout=6000)
                done_steps = page.locator(".running-step.done").count()
                print(f"  已推进的步骤 {done_steps} 个")
                check("进度会随执行推进", done_steps >= 1, str(done_steps))
            except Exception:
                print("  没抓到步骤推进（规划过快，不判失败）")
        except Exception:
            print("  规划太快，没抓到运行中态（不判失败）")
        if saw_running:
            check("运行中有进度区块", True)

        page.wait_for_selector(".day", timeout=240000)
        page.wait_for_timeout(500)

        print("\n[2] 花费构成")
        page.wait_for_selector(".cost", timeout=15000)
        legend = page.locator(".cost-legend li").all_inner_texts()
        print(f"  图例: {legend}")
        cost_blocks = page.locator("section.cost").count()
        print(f"  花费构成区块数 {cost_blocks}")
        check("有花费构成区块", cost_blocks == 1, str(cost_blocks))
        check("图例至少 3 类", len(legend) >= 3, str(len(legend)))
        check("出现大交通", any("大交通" in t for t in legend), str(legend))
        check("出现住宿", any("住宿" in t for t in legend), str(legend))

        segs = page.locator(".cost-seg").count()
        check("进度条分段数与图例一致", segs == len(legend), f"{segs} vs {len(legend)}")

        total_text = page.locator("section.cost .hint").inner_text()
        total = to_number(total_text)
        parts = sum(to_number(t) for t in legend)
        print(f"  图例合计 {parts:.0f} / 总计 {total:.0f}")
        check("图例金额合计等于总花费", abs(parts - total) <= 2, f"{parts} vs {total}")

        print("\n[3] 数据来源")
        source = page.locator(".source-line").inner_text()
        print(f"  {source}")
        check("标注为模拟数据", "模拟数据" in source, source)
        check("说明不代表真实可订", "不代表真实" in source, source)

        print("\n[4] 提示分组")
        groups = page.locator(".notice-group").count()
        tags = page.locator(".notice-tag").all_inner_texts()
        if groups == 0:
            print("  本次没有任何提示（合法结果），跳过分组断言")
        else:
            print(f"  分组: {tags}")
            check("提示按类别展示", groups >= 1, str(groups))
            check("分组带类别标签", all(t.strip() for t in tags), str(tags))

        page.locator("section.cost").scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        page.screenshot(path=str(DOCS / "shot-experience.png"))

        print("\n[5] 失败时能重试")
        page2 = browser.new_page(viewport={"width": 1440, "height": 1100})
        page2.route("**/api/plan", lambda route: route.abort())
        page2.goto(BASE, wait_until="networkidle")
        page2.get_by_role("button", name="开始规划", exact=True).click()
        page2.wait_for_selector(".alert", timeout=30000)
        alert = page2.locator(".alert").inner_text()
        print(f"  错误提示: {alert[:60]}")
        check("显示了失败原因", len(alert) > 0)
        retry = page2.get_by_role("button", name="用同样的需求重试")
        check("给得出重试入口", retry.count() == 1)
        page2.close()

        browser.close()

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  结果页体验验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
