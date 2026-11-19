"""Drive the premium prototype and capture its states."""
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\ui-system")
OUT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\.harness\ui-shots")
URL = (ROOT / "07-premium.html").as_uri()

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 980})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.wait_for_timeout(250)

    def shot(name):
        page.screenshot(path=str(OUT / name), full_page=True)

    def vis(sel):
        return page.is_visible(sel)

    shot("r1-intake.png")
    page.click('label[for="step2"]')
    page.wait_for_timeout(200)
    print("v2:", vis(".v2"), "v1:", vis(".v1"))
    page.click('text=中午前后，别太赶')
    page.click('text=四晚同一家')
    page.wait_for_timeout(150)
    shot("r2-decide.png")

    page.click('label[for="step3"]')
    page.wait_for_timeout(200)
    page.click('label[for="play"]')
    page.wait_for_timeout(1800)
    shot("r3-orbit.png")
    print("v3:", vis(".v3"))

    page.click('label[for="step4"]')
    page.wait_for_timeout(250)
    print("v4:", vis(".v4"), "tt:", vis(".p-tt"))
    shot("r4-plan.png")

    page.click('label[for="day-21"]')
    page.wait_for_timeout(200)
    print("ctx21:", vis(".ctx-21"))
    shot("r5-day21.png")

    page.click('label[for="k2"]')
    page.wait_for_timeout(150)
    print("popover:", vis(".col input:checked + .blk .pop"))
    shot("r6-block.png")

    page.click('label[for="app-budget"]')
    page.wait_for_timeout(150)
    print("budget:", vis(".p-budget"))
    page.click('label[for="scen-cheap"]')
    page.wait_for_timeout(150)
    shot("r7-budget.png")

    page.click('label[for="app-tt"]')
    page.wait_for_timeout(120)
    page.click('label[for="cmd-indoor"]')
    page.wait_for_timeout(200)
    print("v9:", vis(".v9"), "new:", vis(".new-1121"), "applied:", vis(".applied"))
    shot("r8-applied.png")

    page.click('label[for="app-change"]')
    page.wait_for_timeout(150)
    print("newchange:", vis(".newchange"))
    shot("r9-change.png")

    print("errors:", errors)
    browser.close()
