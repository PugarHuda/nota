"""Browser end-to-end checks of the dashboard (Chromium via Playwright) against a real uvicorn server.

Runs whenever `playwright install chromium` has been done; otherwise the module is skipped with a
clear reason. Nothing is mocked: the server serves a seeded ledger through the real API.
"""

import socket
import threading
import time

import pytest
import uvicorn

from arena import api
from tests.test_api import _seed

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def server(tmp_path, monkeypatch):
    first, second = _seed(tmp_path, monkeypatch)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started
    yield f"http://127.0.0.1:{port}", first, second
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except playwright.Error as exc:  # browser binary missing: say so, do not pretend
            pytest.skip(f"chromium not installed for playwright: {str(exc).splitlines()[0]}")
        yield b
        b.close()


def test_keyboard_navigation_verify_replay_backing_and_filter(server, browser):
    base, first, second = server
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(base + "/")
    page.wait_for_selector("#list .row")
    assert page.locator("#list .row").count() == 2
    page.wait_for_function("document.querySelector('#health').textContent.includes('receipts')", timeout=20000)
    assert "RYO" in page.locator("#health").inner_text()
    assert page.locator("#detail .headline").inner_text().startswith("SOL: LONG")  # newest receipt opens first

    page.keyboard.press("j")
    page.keyboard.press("Enter")
    page.wait_for_url(f"{base}/r/{first.id}")
    assert "no trade" in page.locator("#detail .headline").inner_text()
    page.keyboard.press("p")  # previous receipt of this symbol: none for the oldest, so stay
    assert page.url.endswith(first.id)

    page.goto(f"{base}/r/{second.id}")
    page.wait_for_selector("#verify")
    page.click("#verify")
    page.wait_for_function("document.querySelector('#verify-out').textContent.includes('identical')")
    assert "identical: true" in page.locator("#verify-out").inner_text()

    page.fill("#handle", "e2e_judge")
    page.click("#backing button[data-stance='agree']")
    page.wait_for_function("document.querySelector('#back-counts').textContent.startsWith('1 agree')")
    page.wait_for_function("document.querySelector('#backers').textContent.includes('e2e_judge')")
    page.fill("#handle", "x")
    page.click("#backing button[data-stance='disagree']")
    assert "3-32" in page.locator("#back-out").inner_text()

    page.fill("#filter", "no_trade")
    page.wait_for_function("document.querySelectorAll('#list .row').length === 1")
    assert "no_trade" in page.locator("#list .row").first.get_attribute("aria-label")
    page.keyboard.press("Escape")
    page.keyboard.press("?")
    assert page.locator("#help").is_visible()
    page.keyboard.press("Escape")
    assert not page.locator("#help").is_visible()
    assert errors == [], errors
    page.close()


def test_permalink_404_exports_and_mobile_layout(server, browser):
    base, first, second = server
    page = browser.new_page(viewport={"width": 390, "height": 800})
    page.goto(f"{base}/r/doesnotexist")
    page.wait_for_selector("#detail .banner")
    assert "No receipt with id doesnotexist" in page.locator("#detail").inner_text()
    page.goto(f"{base}/r/{second.id}")
    page.wait_for_selector("#detail .headline")
    scroll = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert scroll <= 0, f"page body scrolls horizontally by {scroll}px on a phone"
    assert page.locator("a[href$='.json']").count() == 1 and page.locator("a[href$='.md']").count() == 1
    assert page.locator("a[href^='https://x.com/intent/post']").count() == 1
    head = page.evaluate("document.querySelector('meta[property=\"og:image\"]').content")
    assert head.endswith(f"/r/{second.id}.png")
    page.close()
