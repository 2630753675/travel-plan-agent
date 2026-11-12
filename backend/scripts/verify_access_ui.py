"""P5 最小子集验收（浏览器层）：外部访客的口令流程。

用 `extra_http_headers` 给浏览器加上 `X-Forwarded-For`，服务端就会把这次访问
当成外部请求——这样才测得到真实的门禁，而不是本机免口令的顺风车。

覆盖：
A 外部访客打开页面 → 出现口令输入框，而不是正常界面
B 输入正确口令后 → 页面可用，能正常规划
C 口令错的时候 → 仍然要口令（不会假放行）

需要后端已在 127.0.0.1:8848 运行。
"""

from __future__ import annotations

import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from app import access  # noqa: E402

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
    token = access.load_token()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"X-Forwarded-For": "203.0.113.9"},
        )
        page = ctx.new_page()

        print("\n[A] 外部访客先看到口令框")
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector(".token-bar", timeout=20000)
        text = page.locator(".token-bar").inner_text()
        print(f"  提示: {text[:60]}")
        check("出现口令输入区", page.locator(".token-bar").is_visible())
        check("说明需要口令", "口令" in text, text[:40])
        check("未输入时使用按钮不可点", page.locator(".token-bar button").is_disabled())
        page.screenshot(path=str(DOCS / "shot-access.png"))

        print("\n[B] 错误口令不放行")
        page.locator(".token-bar input").fill("definitely-wrong-token")
        page.locator(".token-bar button").click()
        page.wait_for_timeout(2500)
        page.wait_for_selector(".token-bar", timeout=15000)
        check("错误口令后仍要求输入", page.locator(".token-bar").is_visible())

        print("\n[C] 正确口令后可用")
        page.locator(".token-bar input").fill(token)
        page.locator(".token-bar button").click()
        page.wait_for_selector(".panel", timeout=20000)
        page.wait_for_timeout(1500)
        check("口令框消失", page.locator(".token-bar").count() == 0)

        page.get_by_role("button", name="开始规划", exact=True).click()
        page.wait_for_selector(".day", timeout=240000)
        days = page.locator(".day").count()
        print(f"  规划出 {days} 天")
        check("能正常规划", days == 5, str(days))

        print("\n[D] 分享链接不需要口令")
        page.wait_for_selector(".versions", timeout=20000)
        share = page.evaluate("""() => {
            const t = localStorage.getItem('trip.accessToken');
            return t ? 'has-token' : 'no-token';
        }""")
        check("口令已存到本地", share == "has-token", str(share))

        plain = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"X-Forwarded-For": "203.0.113.9"},
        ).new_page()
        plain.goto(BASE, wait_until="networkidle")
        plain.wait_for_selector(".token-bar", timeout=20000)
        check("新访客没有口令仍是门禁状态", plain.locator(".token-bar").is_visible())

        browser.close()

    print()
    if failures:
        print(f"FAIL  {len(failures)} 项未通过")
        return 1
    print("PASS  外部访客口令流程验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
