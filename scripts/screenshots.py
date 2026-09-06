"""Capture README screenshots of the dashboard from the shipped demo ledger (no keys, no mocks).

    uv run python scripts/screenshots.py      # writes docs/img/*.png
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ARENA_DB", str(ROOT / "data" / "demo.db"))
os.environ.setdefault("ARENA_READONLY", "1")

from arena.api import app  # noqa: E402

OUT = ROOT / "docs" / "img"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1400, "height": 900})
        page.goto(base + "/")
        page.wait_for_function("document.querySelector('#health').textContent.includes('receipts')", timeout=20000)
        page.wait_for_function("document.querySelector('#summary-position').textContent.includes('Position')")
        page.screenshot(path=str(OUT / "dashboard.png"))
        page.click("#verify")
        page.wait_for_function("document.querySelector('#verify-out').textContent.includes('identical')")
        page.locator("#verify").scroll_into_view_if_needed()
        page.screenshot(path=str(OUT / "verify-replay.png"), clip={"x": 300, "y": 0, "width": 800, "height": 900})
        mobile = b.new_page(viewport={"width": 390, "height": 844})
        mobile.goto(base + "/")
        mobile.wait_for_selector("#detail .headline")
        mobile.screenshot(path=str(OUT / "mobile.png"))
        first = page.evaluate("document.querySelector('#list .row').getAttribute('aria-label')")
        rid = page.evaluate("location.pathname.split('/').pop() || document.querySelector('[data-open]')?.dataset.open")
        card = b.new_page()
        card.goto(f"{base}/r/{rid}.png")
        card.screenshot(path=str(OUT / "card.png"))
        b.close()
    srv.should_exit = True
    print("wrote", sorted(f.name for f in OUT.glob("*.png")), "| first row:", first)


if __name__ == "__main__":
    main()
