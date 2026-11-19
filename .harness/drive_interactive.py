"""Drive the interactive prototype through its main flows and capture screenshots."""
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\ui-proposals")
OUT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\.harness\ui-shots")
URL = (ROOT / "16-interactive-flow.html").as_uri()

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.wait_for_timeout(300)

    page.screenshot(path=str(OUT / "p1-intake.png"), full_page=True)

    page.click("#btn-start")
    page.wait_for_timeout(700)
    page.screenshot(path=str(OUT / "p2-running.png"), full_page=True)

    page.click("#btn-skip")
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "p3-result.png"), full_page=True)

    # 点击 11-21 的第二个景点块（宽窄巷子 13:00）
    blocks = page.query_selector_all('#canvas .blk[data-day="11-21"]')
    if blocks:
        blocks[2].click()
        page.wait_for_timeout(250)
    page.screenshot(path=str(OUT / "p4-selected.png"), full_page=True)

    page.click('.tab[data-tab="budget"]')
    page.wait_for_timeout(250)
    page.fill("#b-hotel-range", "680")
    page.dispatch_event("#b-hotel-range", "input")
    page.wait_for_timeout(200)
    page.screenshot(path=str(OUT / "p5-budget.png"), full_page=True)

    page.click('.tab[data-tab="timetable"]')
    page.fill("#instruct", "把 11-21 下午换成室内")
    page.click("#btn-send")
    page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / "p6-rework.png"), full_page=True)
    page.wait_for_timeout(2200)
    page.screenshot(path=str(OUT / "p7-after.png"), full_page=True)

    page.keyboard.press("Control+k")
    page.wait_for_timeout(250)
    page.screenshot(path=str(OUT / "p8-palette.png"), full_page=True)

    title = page.inner_text("#top-version")
    st = page.inner_text("#st-sel")
    print("version:", title, "| selection:", st)
    print("errors:", errors)
    browser.close()
