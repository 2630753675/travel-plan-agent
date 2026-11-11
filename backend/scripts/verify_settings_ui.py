"""S4 验收：设置页交互。

覆盖：打开设置 → 三个分组齐全 → 填模型连接 → 测试连接 → 保存 → 顶栏模式切换。
需要后端已在 127.0.0.1:8848 运行。
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright

from verify_settings import FakeLLM

BASE = "http://127.0.0.1:8848"
DOCS = pathlib.Path(__file__).resolve().parents[2] / "docs"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def reset_config() -> None:
    body = json.dumps(
        {
            "llm": {"enabled": False, "api_key": ""},
            "orchestration": {"max_revisions": 3, "node_timeout_s": 60, "token_budget": 60000},
            "datasource": {"kind": "mock", "http_base_url": ""},
        }
    ).encode()
    req = urllib.request.Request(
        BASE + "/api/config", data=body, headers={"Content-Type": "application/json"}, method="PUT"
    )
    urllib.request.urlopen(req, timeout=15).read()


def main() -> int:
    fake = FakeLLM("ok")
    try:
        reset_config()
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
            check("默认显示规则引擎", page.locator(".mode-pill").inner_text() == "规则引擎")

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

            print("\n[5] 关闭后顶栏显示模型模式")
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
            rev_input = page.locator(".cfg").nth(1).locator("input").first
            print(f"  定位到: placeholder={rev_input.get_attribute('placeholder')!r} value={rev_input.input_value()!r}")
            rev_input.fill("2")
            page.wait_for_timeout(300)
            print(f"  填写后 value={rev_input.input_value()!r}")
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
        reset_config()
        fake.stop()

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  设置页交互全部验收项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
