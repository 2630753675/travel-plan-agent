"""验收：右侧 Agent 对话抽屉（流式）。

覆盖：
A 轨迹默认不展开，点按钮才出现
B 规划过程中对话消息逐步增长
C 轨迹是用户可读结构：阶段块 → 角色气泡
D 工具调用折叠为"查看依据"，展开后可见工具与结果
E 协商阶段能实时看到角色发言
F 阶段块可折叠，旧日志选择器不再作为 UI 契约
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
            n = page.locator(".conversation-message").count()
            samples.append(n)
            if page.locator(".drawer-sub").inner_text().startswith("已完成"):
                break
            page.wait_for_timeout(400)
        grew = len(set(samples)) > 1
        print(f"  采样到的条数：{samples[:12]}...")
        check("轨迹是逐步增长的", grew, str(samples[:12]))

        page.wait_for_selector(".day, .interrupt", timeout=240000)
        page.wait_for_timeout(600)
        total = page.locator(".conversation-message").count()
        print(f"  最终 {total} 条消息")

        print("\n[3] 分层结构")
        groups = page.locator(".conversation-group").count()
        bubbles = page.locator(".message-bubble").all_inner_texts()
        actors = page.locator(".message-meta strong").all_inner_texts()
        agent_actors = page.locator(".conversation-message:not(.kind-tool) .message-meta strong").all_inner_texts()
        print(f"  阶段块 {groups} 个，角色：{sorted(set(actors))}")
        check("有多个阶段块", groups >= 3, str(groups))
        check("有对话气泡", len(bubbles) >= 3, str(len(bubbles)))
        check("出现真正 Agent 角色", any(a in ("规划 Agent", "预算评审 Agent", "体验评审 Agent") for a in actors), str(actors))
        check("查询工具不作为 Agent", not any(a in ("航班查询工具", "酒店查询工具", "景点查询工具", "天气查询工具") for a in agent_actors), str(agent_actors))

        print("\n[4] 工具调用可见")
        evidence = page.locator(".evidence-toggle")
        print(f"  依据入口 {evidence.count()} 个")
        check("有可展开的依据", evidence.count() >= 1, str(evidence.count()))
        if evidence.count() > 0:
            evidence.first.click()
            page.wait_for_timeout(200)
            check("展开后显示工具名", page.locator(".evidence-body").count() >= 1)
            check("工具依据有内容", len(page.locator(".evidence-row").all_inner_texts()) >= 1)
        check("不把工具调用平铺成消息", page.locator(".log-row").count() == 0)

        print("\n[5] 阶段可折叠")
        first_head = page.locator(".conversation-stage").first
        before = page.locator(".conversation-messages").count()
        first_head.click()
        page.wait_for_timeout(300)
        after = page.locator(".conversation-messages").count()
        check("折叠后收起一块", after == before - 1, f"{before} -> {after}")
        first_head.click()
        page.wait_for_timeout(300)

        page.screenshot(path=str(DOCS / "shot-drawer.png"))

        print("\n[6] 收起抽屉不影响主流程")
        page.get_by_role("button", name="收起", exact=True).click()
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
