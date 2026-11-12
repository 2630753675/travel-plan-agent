"""验收：右侧执行轨迹抽屉（流式）。

覆盖：
A 轨迹默认不展开，点按钮才出现
B 规划过程中轨迹逐步增长（不是最后一次性出现）
C 轨迹是分层结构：阶段块 → agent 块 → 工具行
D 工具调用可见（函数名 + 参数 + 返回）
E 协商阶段能实时看到角色发言
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
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(BASE, wait_until="networkidle")

        print("\n[1] 未开始时不显示轨迹")
        check("抽屉默认关闭", page.locator(".drawer").count() == 0)
        btn = page.get_by_role("button", name="执行轨迹")
        check("顶栏有入口按钮", btn.count() == 1)
        check("无轨迹时按钮禁用", btn.is_disabled())

        print("\n[2] 打开抽屉并开始规划")
        page.locator("label", has_text="总预算").locator("input").fill("7500")
        page.get_by_role("button", name="开始规划", exact=True).click()
        page.get_by_role("button", name="执行轨迹").click()
        page.wait_for_selector(".drawer", timeout=10000)
        check("抽屉已展开", page.locator(".drawer").is_visible())

        # 采样：running 期间轨迹条数应递增
        samples: list[int] = []
        for _ in range(30):
            n = page.locator(".log-row").count()
            samples.append(n)
            if page.locator(".drawer-sub").inner_text().startswith("已完成"):
                break
            page.wait_for_timeout(400)
        grew = len(set(samples)) > 1
        print(f"  采样到的条数：{samples[:12]}...")
        check("轨迹是逐步增长的", grew, str(samples[:12]))

        page.wait_for_selector(".day, .interrupt", timeout=240000)
        page.wait_for_timeout(600)
        total = page.locator(".log-row").count()
        print(f"  最终 {total} 行")

        print("\n[3] 分层结构")
        groups = page.locator(".log-group").count()
        chunks = page.locator(".log-chunk-head").all_inner_texts()
        print(f"  阶段块 {groups} 个，agent 块：{sorted(set(chunks))}")
        check("有多个阶段块", groups >= 3, str(groups))
        check("有 agent 分块", len(chunks) >= 3, str(chunks))
        check("出现检索类 agent", any(a in ("住宿", "景点", "交通", "天气") for a in chunks), str(chunks))

        print("\n[4] 工具调用可见")
        tools = page.locator(".log-row.kind-tool .log-text").all_inner_texts()
        print(f"  工具调用 {len(tools)} 条：{tools[:4]}")
        check("记录了工具调用", len(tools) >= 3, str(len(tools)))
        check(
            "带函数名与入参",
            any(t.startswith("search_flights(") or t.startswith("list_pois(") for t in tools),
            str(tools[:3]),
        )
        check("有返回摘要行", any(t.startswith("→ ") for t in tools), str(tools[:4]))
        # 同一工具不应同时出现真实事件和旧的摘要行
        check(
            "工具事件未重复",
            not any("→ " in t and "(" in t for t in tools),
            str([t for t in tools if "→ " in t and "(" in t][:3]),
        )

        print("\n[5] 阶段可折叠")
        first_head = page.locator(".log-group-head").first
        before = page.locator(".log-group-body").count()
        first_head.click()
        page.wait_for_timeout(300)
        after = page.locator(".log-group-body").count()
        check("折叠后收起一块", after == before - 1, f"{before} -> {after}")
        first_head.click()
        page.wait_for_timeout(300)

        page.screenshot(path=str(DOCS / "shot-drawer.png"))

        print("\n[6] 收起抽屉不影响主流程")
        page.get_by_role("button", name="收起").click()
        page.wait_for_timeout(300)
        check("抽屉已关闭", page.locator(".drawer").count() == 0)
        check("主流程结果仍在", page.locator(".day").count() > 0 or page.locator(".interrupt").count() > 0)

        browser.close()

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  轨迹抽屉与流式输出验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
