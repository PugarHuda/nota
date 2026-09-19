"""An automated accessibility gate: axe-core over every page, in both themes, at a desk and a phone width.

axe-core is the engine browser accessibility tools use; axe-playwright-python ships it and runs it in the
page. It cannot prove a page accessible, but a serious or critical rule it does catch - text a screen
reader cannot name, a scroll box a keyboard cannot reach, content outside any landmark - must never
ship again, so it fails the suite here instead of being found by a reader.
"""

import pytest

from tests.test_qa_browser import browser, server  # noqa: F401  (the same shipped-ledger server and browser)

playwright = pytest.importorskip("playwright.sync_api")
Axe = pytest.importorskip("axe_playwright_python.sync_playwright").Axe

# moderate in axe's own ranking, but each one is a structural promise these pages make
ALWAYS = {"landmark-one-main", "region", "scrollable-region-focusable"}


@pytest.mark.parametrize("width", [1366, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("path", ["/", "/ja", "/app", "/scorecard", "/demo"])
def test_axe_finds_nothing_serious(server, browser, path, theme, width):  # noqa: F811
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    ctx.add_init_script(f"try{{localStorage.setItem('nota.theme','{theme}')}}catch(e){{}}")
    page = ctx.new_page()
    page.goto(server + path, wait_until="networkidle")
    if path == "/app":   # axe must read the rendered receipt, not the page between two renders
        page.wait_for_selector("#detail .headline")
        page.wait_for_function("() => document.querySelector('#health').textContent.includes('receipts')")
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.documentElement.dataset.theme") == theme
    found = Axe().run(page).response["violations"]
    bad = [f"{v['id']} ({v['impact']}): {[n['target'] for n in v['nodes'][:3]]}"
           for v in found if v["impact"] in ("serious", "critical") or v["id"] in ALWAYS]
    ctx.close()
    assert bad == [], f"{path} {theme} {width}px: {bad}"
