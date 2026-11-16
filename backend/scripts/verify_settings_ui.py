"""S4 验收：设置页交互。

覆盖：打开设置 → 三个分组齐全 → 填模型连接 → 测试连接 → 保存 → 顶栏模式切换。
需要后端已在 127.0.0.1:8848 运行。

若检测到已配置真实密钥，自动降级为只读检查，避免覆盖用户配置。
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright

from verify_settings import FakeLLM, restore, snapshot

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
    initial = snapshot()
    has_real_key = bool(initial["llm"]["api_key_set"])
    if has_real_key:
        print("\n注意：检测到已配置真实密钥。本次只做只读检查，不写入任何密钥。")

    fake = FakeLLM("ok")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(BASE, wait_until="networkidle")

            print("\n[1] 打开设置")
            page.get_by_role("button", name="设置").click()
            page.wait_for_selector(".cfg", timeout=10000)
            sections = page.locator(".cfg h3").all_inner_texts()
            print(f"  分组: {sections}")
            check("三个分组齐全", sections == ["模型连接", "编排参数", "数据源"], str(sections))
            expect_mode = "多 Agent 协作" if initial["llm"]["enabled"] else "规则模式 · 非 Agent"
            check(f"顶栏初始为{expect_mode}", page.locator(".mode-pill").inner_text() == expect_mode)

            if has_real_key:
                print("\n[2] 已配置密钥：检查掩码回显（不写入）")
                ph = page.locator("input[type='password']").get_attribute("placeholder")
                print(f"  placeholder: {ph}")
                check("回显为掩码", "****" in (ph or ""), str(ph))
                # 掩码形如 sk-6****f4c（已保存，留空则不修改）；完整密钥远长于此
                check("未回传完整密钥", len(ph or "") <= 40, str(len(ph or "")))

                print("\n[3] 修改编排参数")
                rev = page.locator(".cfg").nth(1).locator("input").first
                before = rev.input_value()
                new_val = "2" if before != "2" else "4"
                rev.fill(new_val)
                page.wait_for_timeout(200)
                check("输入已生效", rev.input_value() == new_val, rev.input_value())
                page.get_by_role("button", name="保存").click()
                page.wait_for_timeout(1200)
                with urllib.request.urlopen(BASE + "/api/config", timeout=10) as r:
                    cfg = json.loads(r.read().decode())
                check(
                    "max_revisions 已保存",
                    str(cfg["orchestration"]["max_revisions"]) == new_val,
                    str(cfg["orchestration"]),
                )
                page.screenshot(path=str(DOCS / "shot-settings.png"))
                page.get_by_role("button", name="关闭").click()
            else:
                print("\n[2] 填写模型连接")
                page.fill("input[placeholder='https://api.deepseek.com/v1']", fake.base_url)
                page.fill("input[placeholder='deepseek-chat']", "fake-model")
                page.fill("input[type='password']", "sk-ui-test-1234567890")
                page.check(".cfg input[type='checkbox']")

                print("\n[3] 测试连接")
                page.get_by_role("button", name="测试连接").click()
                page.wait_for_selector(".cfg-msg", timeout=40000)
                text = page.locator(".cfg-msg").inner_text()
                print(f"  提示: {text}")
                check("连接成功", "连接成功" in text, text)

                print("\n[4] 保存")
                page.get_by_role("button", name="保存").click()
                page.wait_for_timeout(1200)
                msg = page.locator(".cfg-msg").inner_text()
                print(f"  提示: {msg}")
                check("保存成功", "已保存" in msg, msg)
                page.screenshot(path=str(DOCS / "shot-settings.png"))

                print("\n[5] 关闭后顶栏显示规则模式")
                page.get_by_role("button", name="关闭").click()
                page.wait_for_timeout(400)
                mode = page.locator(".mode-pill").inner_text()
                check("显示模型生成", mode == "模型生成", mode)

                print("\n[6] 重新打开：密钥以掩码展示")
                page.get_by_role("button", name="设置").click()
                page.wait_for_selector(".cfg")
                ph = page.locator("input[type='password']").get_attribute("placeholder")
                print(f"  placeholder: {ph}")
                check("回显为掩码", "****" in (ph or ""), str(ph))
                check("明文不回传", "sk-ui-test-1234567890" not in (ph or ""), str(ph))
                page.get_by_role("button", name="关闭").click()

                print("\n[7] 修改编排参数")
                page.get_by_role("button", name="设置").click()
                page.wait_for_selector(".cfg")
                rev = page.locator(".cfg").nth(1).locator("input").first
                print(f"  定位到: value={rev.input_value()!r}")
                rev.fill("2")
                page.wait_for_timeout(300)
                print(f"  填写后 value={rev.input_value()!r}")
                page.get_by_role("button", name="保存").click()
                page.wait_for_timeout(1200)
                print(f"  保存提示: {page.locator('.cfg-msg').inner_text()}")
                with urllib.request.urlopen(BASE + "/api/config", timeout=10) as r:
                    cfg = json.loads(r.read().decode())
                check(
                    "max_revisions 已保存",
                    cfg["orchestration"]["max_revisions"] == 2,
                    str(cfg["orchestration"]),
                )
                page.get_by_role("button", name="关闭").click()

            browser.close()
    finally:
        restore(initial)
        fake.stop()

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print(f"PASS  设置页交互验收通过（{'只读模式，未触碰你的密钥' if has_real_key else '全量'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
