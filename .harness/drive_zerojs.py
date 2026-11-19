"""Verify the zero-JS prototype by clicking labels; capture each state."""
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\ui-system")
OUT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\.harness\ui-shots")
URL = (ROOT / "06-interactive.html").as_uri()

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 980})
    page.goto(URL)
    page.wait_for_timeout(200)

    def shot(name):
        page.screenshot(path=str(OUT / name), full_page=True)

    def vis(sel):
        return page.is_visible(sel)

    print("v1 visible:", vis("#step1:checked ~ .stage .v1") if False else vis(".v1"))
    shot("q1-intake.png")

    page.click('label[for="step2"]')
    page.wait_for_timeout(150)
    print("after step2 -> v2 visible:", vis(".v2"), "v1:", vis(".v1"))
    page.click('label[for="rv-noon"]')
    page.click('label[for="stay-same"]')
    page.wait_for_timeout(120)
    print("answer text:", page.inner_text(".answer").strip()[:60])
    shot("q2-clarify.png")

    page.click('label[for="step3"]')
    page.wait_for_timeout(120)
    page.click('label[for="play"]')
    page.wait_for_timeout(1600)
    shot("q3-running.png")
    print("running visible:", vis(".v3"))

    page.click('label[for="step4"]')
    page.wait_for_timeout(200)
    print("result visible:", vis(".v4"), "timetable visible:", vis(".p-tt"))
    shot("q4-plan.png")

    page.click('label[for="day-21"]')
    page.wait_for_timeout(120)
    shot("q5-dayfilter.png")

    page.click('label[for="b2"]')
    page.wait_for_timeout(120)
    print("popover visible:", vis(".col input:checked + .blk .pop"))
    shot("q6-block.png")

    page.click('label[for="app-budget"]')
    page.wait_for_timeout(120)
    print("budget panel:", vis(".p-budget"))
    page.click('label[for="scen-cheap"]')
    page.wait_for_timeout(120)
    print("cheap total:", page.inner_text(".sp-cheap .total"))
    shot("q7-budget.png")

    page.click('label[for="app-tt"]')
    page.wait_for_timeout(120)
    page.click('label[for="cmd-indoor"]')
    page.wait_for_timeout(150)
    print("v9 visible:", vis(".v9"), "new block:", vis(".new-1121"), "applied:", vis(".applied"))
    shot("q8-applied.png")

    page.click('label[for="app-change"]')
    page.wait_for_timeout(120)
    print("new change visible:", vis(".newchange"))
    shot("q9-change.png")

    browser.close()
