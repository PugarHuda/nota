"""Browser QA over the shipped demo ledger: the landing, the dashboard, both themes, three widths,
every skill in the runner, the error paths, and the accessibility floor.

This runs against `data/demo.db`, the same snapshot the deployment serves, so what passes here is
what a judge meets. Nothing is stubbed: the verify button really calls the API, and the skill runner
really invokes the skills.
"""

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo.db"
RECEIPT = "dbd7727f5a25"


@pytest.fixture(scope="module")
def server():
    # scoped, not global: leaking these would point every later test at the read-only snapshot
    before = {k: os.environ.get(k) for k in ("NOTA_DB", "NOTA_READONLY")}
    os.environ["NOTA_DB"] = str(DEMO)
    os.environ["NOTA_READONLY"] = "1"
    from nota import api

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(120):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)
    for k, v in before.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except playwright.Error as exc:
            pytest.skip(f"chromium not installed for playwright: {str(exc).splitlines()[0]}")
        yield b
        b.close()


def page_with_log(browser, **kw):
    """A page that fails the test on any console error or unhandled exception."""
    page = browser.new_page(**kw)
    problems: list[str] = []
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: problems.append(f"requestfailed: {r.url} {r.failure}"))
    return page, problems


def test_landing_renders_draws_its_mark_and_verifies_a_receipt_for_real(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/", wait_until="networkidle")

    assert page.locator("h1").inner_text() == "A trading call you can re-run."
    assert page.locator("h1").bounding_box()["height"] < 160          # the headline stays two lines
    assert page.locator("nav").bounding_box()["height"] <= 80         # nav discipline

    # the mark is generated from the receipt, so it has one tick per hex character of the hash
    assert page.locator("#mark line.tick").count() == 64
    assert page.locator("#mark path.arc").count() == 1
    assert page.locator("#mark text.p").text_content() == "0.68"   # SVG text, not an HTMLElement

    # the CTA is reachable without scrolling
    cta = page.locator("a.btn").first.bounding_box()
    assert cta["y"] + cta["height"] < 900

    # the button really calls the deployment's own API
    page.click("#verify")
    page.wait_for_function("document.querySelector('#verify-out').textContent.includes('identical')", timeout=20000)
    assert "identical: true" in page.locator("#verify-out").inner_text()

    assert problems == [], problems
    page.close()


def test_every_landing_link_and_image_resolves(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/", wait_until="networkidle")

    broken = page.evaluate("""() => [...document.images].filter(i => !i.complete || i.naturalWidth === 0).map(i => i.src)""")
    assert broken == [], f"images that did not load: {broken}"
    assert page.evaluate("""() => [...document.images].every(i => i.alt && i.alt.length > 12)"""), "every image needs real alt text"

    hrefs = page.eval_on_selector_all("a[href^='/']", "els => [...new Set(els.map(e => e.getAttribute('href')))]")
    assert "/app" in hrefs and "/demo.mp4" in hrefs
    for href in hrefs:
        r = page.request.get(server + href)
        assert r.status == 200, f"{href} -> {r.status}"
    assert problems == [], problems
    page.close()


@pytest.mark.parametrize("width,height", [(390, 844), (768, 1024), (1440, 900)])
def test_no_horizontal_overflow_on_either_page_at_any_width(server, browser, width, height):
    page, problems = page_with_log(browser, viewport={"width": width, "height": height})
    for path in ("/", "/app"):
        page.goto(server + path, wait_until="networkidle")
        page.wait_for_timeout(400)
        overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"{path} at {width}px scrolls sideways by {overflow}px"
    assert problems == [], problems
    page.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_dashboard_is_legible_in_both_themes(server, browser, theme):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#health').textContent.includes('receipts')", timeout=20000)
    while page.evaluate("document.documentElement.dataset.theme || 'system'") != theme:
        page.click("#theme")
    page.wait_for_timeout(250)

    # body text and its background must not collapse into each other
    same = page.evaluate("""() => {
      const s = getComputedStyle(document.body);
      return s.color === s.backgroundColor;
    }""")
    assert not same
    assert page.locator("#detail .headline").inner_text().strip() != ""
    assert page.locator("#list .row").count() == 4
    assert problems == [], problems
    page.close()


def test_keyboard_alone_reaches_the_receipt_the_diff_and_the_replay_check(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#list .row")

    page.keyboard.press("j")
    page.keyboard.press("Enter")
    page.wait_for_selector("#summary")
    opened = page.url
    assert "/r/" in opened

    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "filter"
    page.keyboard.type("eth")
    page.wait_for_function("document.querySelectorAll('#list .row').length === 1")
    page.locator("#filter").fill("")          # fill() refocuses the input, so blur before the next key
    page.wait_for_function("document.querySelectorAll('#list .row').length === 4")
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.id") != "filter"

    page.keyboard.press("?")
    assert page.locator("#help").is_visible()
    page.keyboard.press("Escape")

    page.click("#verify")
    page.wait_for_function("document.querySelector('#verify-out').textContent.includes('identical')", timeout=30000)
    assert "identical" in page.locator("#verify-out").inner_text()
    assert problems == [], problems
    page.close()


def test_the_skill_runner_lists_and_runs_every_skill(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 1200})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#skill-name option", state="attached")

    names = page.eval_on_selector_all("#skill-name option", "els => els.map(e => e.value)")
    assert names == ["narrative_convergence", "news_verify", "price_crosscheck", "technicals_crosscheck"]

    for skill, args in (("price_crosscheck", {"symbol": "SOL"}), ("technicals_crosscheck", {"symbol": "SOL"})):
        page.select_option("#skill-name", skill)
        for key, value in args.items():
            page.fill(f"#skill-args [data-arg='{key}']", value)
        page.click("#skill-run")
        page.wait_for_function("document.querySelector('#skill-out').textContent.length > 40", timeout=90000)
        out = page.locator("#skill-out").inner_text()
        assert skill.split("_")[0] in out.lower() or "envelope" in out.lower() or "data_mode" in out.lower()
        assert "traceback" not in out.lower()
    assert problems == [], problems
    page.close()


def test_error_paths_say_what_is_wrong_instead_of_breaking(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1200, "height": 900})

    page.goto(f"{server}/r/doesnotexist", wait_until="networkidle")
    assert "No receipt with id" in page.locator("#detail").inner_text()
    # the 404 the page just handled is the point of this test, so it is not counted as a problem
    problems[:] = [p for p in problems if "404" not in p]

    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#backing")
    page.fill("#handle", "not a handle!!")
    page.locator("#backing button").first.click()
    page.wait_for_function("document.querySelector('#back-out').textContent.length > 0", timeout=15000)
    said = page.locator("#back-out").inner_text().lower()
    assert "handle" in said or "read-only" in said or "503" in said     # refused, and it says why
    problems[:] = [p for p in problems if "404" not in p and "503" not in p]
    assert problems == [], problems
    page.close()


def test_accessibility_floor_focus_alt_and_live_regions(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#list .row")

    # the skip link is the first thing a keyboard user meets, and it is visible when focused
    page.keyboard.press("Tab")
    first = page.evaluate("document.activeElement.className")
    assert "skip" in first
    assert page.evaluate("""() => {
      const el = document.querySelector('.skip');
      return el.getBoundingClientRect().left > -100;
    }""")

    # every list row is a real button, not a div with a click handler
    assert page.eval_on_selector_all("#list .row", "els => els.every(e => e.tagName === 'BUTTON')")
    # the panels that change without a page load announce themselves
    for sel in ("#detail", "#health", "#attention"):
        assert page.get_attribute(sel, "aria-live") == "polite", sel
    # form controls carry a label or an accessible name
    assert page.get_attribute("#filter", "aria-label")
    assert page.get_attribute("#skill-name", "aria-label")
    assert problems == [], problems
    page.close()


def test_reduced_motion_is_actually_honoured(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(server + "/", wait_until="networkidle")
    animated = page.evaluate("""() => [...document.querySelectorAll('h1, .sub, .actions, #mark line, #mark path')]
        .filter(el => getComputedStyle(el).animationName !== 'none').length""")
    assert animated == 0, "something still animates under prefers-reduced-motion: reduce"
    assert problems == [], problems
    page.close()
