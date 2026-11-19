"""Screenshot the UI proposals for self-review (writes to a temp dir, not the repo)."""

from __future__ import annotations

import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\ui-proposals")
OUT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\.harness\ui-shots")
OUT.mkdir(parents=True, exist_ok=True)

TARGETS = [
    ("index.html", "index", 1440, 1000),
    ("01-control-room.html", "a-control-room", 1440, 1400),
    ("02-itinerary-first.html", "b-itinerary-first", 1440, 1200),
    ("03-review-board.html", "c-review-board", 1440, 1000),
    ("02-itinerary-first.html", "b-narrow", 430, 1000),
    ("04-map-first.html", "d-map-first", 1440, 1000),
    ("05-timetable.html", "e-timetable", 1440, 1200),
    ("06-travel-kit.html", "f-travel-kit", 1440, 1200),
    ("07-radio-briefing.html", "g-radio", 1440, 1000),
    ("08-tradeoff-sandbox.html", "h-sandbox", 1440, 1100),
    ("09-change-log.html", "i-changelog", 1440, 1200),
]

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    for name, tag, width, height in TARGETS:
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        page.goto((ROOT / name).as_uri())
        page.wait_for_timeout(250)
        path = OUT / f"{tag}.png"
        page.screenshot(path=str(path), full_page=True)
        print(f"{tag}: {path} ({path.stat().st_size // 1024} KB)")
        page.close()
    browser.close()
